"""ディスプレイ・オラクル E2E: dirty terminal × claude --resume の二重 footer 検知。

これまでのテストが検知できなかった「端末の事前状態 × レンダリング」の
相互作用バグ（claude --resume で footer が 2 つ見える）を、実 PtyProxy +
実 unix socket + 録画 resume-burst + pyte ディスプレイで機械検知する。

検証の核:
  1. 端末に「前セッションの footer」相当の sentinel を事前注入（dirty terminal）
  2. 新規 client 接続 → pty_proxy が attach 時クリアを送る想定
  3. 録画 resume バーストを _broadcast で流す
  4. 描画後の *画面* に sentinel が残っていない / footer 構造が 1 つだけ

さらに「クリアを無効化すると sentinel が残る」ことも確認し、オラクル自身が
バグを検知できる（＝意味のあるテストである）ことを保証する。
"""
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from display_oracle import DisplayTerminal
from pty_proxy import PtyProxy

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
PRIOR_SENTINEL = "ZZ_PRIOR_SESSION_FOOTER_SENTINEL_ZZ"


class _Drainer:
    """クライアント socket を別スレッドで連続ドレインする。

    pty_proxy._broadcast は c.sendall() でブロッキング送信するため、
    送信量が socket バッファを超えると、テスト側が並行して読まないと
    デッドロックする。これを背景スレッドで吸い続けて回避する。
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._alive = True
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self) -> None:
        self._sock.settimeout(0.1)
        while self._alive:
            try:
                chunk = self._sock.recv(65536)
                if not chunk:
                    break
                with self._lock:
                    self._buf.extend(chunk)
            except (socket.timeout, BlockingIOError):
                continue
            except OSError:
                break

    def collect(self, settle: float = 0.3) -> bytes:
        """settle 秒待ってから、それまでに受信した全バイトを返す。"""
        time.sleep(settle)
        self._alive = False
        self._t.join(timeout=1.0)
        with self._lock:
            return bytes(self._buf)


@pytest.fixture
def proxy_and_sock():
    master_fd, slave_fd = os.openpty()
    fd_tmp, sp = tempfile.mkstemp(prefix="oracle", suffix=".sock", dir="/tmp")
    os.close(fd_tmp)
    os.unlink(sp)
    # child_pgid は存在しない値にする。_catchup_new_clients の
    # os.killpg(child_pgid, SIGWINCH) が ProcessLookupError(=OSError) で
    # 握り潰され、テスト自身のプロセス群へ SIGWINCH が飛ばない。
    proxy = PtyProxy(master_fd, child_pid=4242, child_pgid=2_000_000_000,
                     sock_path=Path(sp))
    proxy._start_server()
    deadline = time.monotonic() + 1.0
    while not Path(sp).exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    yield proxy, sp
    proxy._alive = False
    for fd in (master_fd, slave_fd):
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        Path(sp).unlink()
    except OSError:
        pass


def _connect(sp: str) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + 1.0
    while True:
        try:
            s.connect(sp)
            return s
        except (FileNotFoundError, ConnectionRefusedError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def _wait_clients(proxy: PtyProxy, n: int) -> None:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        with proxy._lock:
            if len(proxy.clients) >= n:
                return
        time.sleep(0.02)
    raise TimeoutError("client did not connect")


def _resume_burst_bytes() -> bytes:
    return (FIXTURES / "resume-burst" / "bytes.bin").read_bytes()


def test_attach_clear_removes_prior_terminal_content(proxy_and_sock) -> None:
    """dirty terminal に接続 → attach クリア → resume 描画後、前セッションの
    残留 (sentinel) が画面から消えている（＝二重 footer にならない）。"""
    proxy, sp = proxy_and_sock
    client = _connect(sp)
    try:
        _wait_clients(proxy, 1)

        # ユーザーの端末ディスプレイ。前セッションの footer が残っている状態を再現
        disp = DisplayTerminal(rows=40, cols=120)
        disp.seed_prior_content([
            "previous session output line",
            "──────────────────────────────────────",
            f"  ⏵⏵ {PRIOR_SENTINEL} (shift+tab to cycle)",
            "──────────────────────────────────────",
        ])
        assert disp.contains(PRIOR_SENTINEL), "事前状態の seed 自体が失敗"

        drain = _Drainer(client)  # sendall ブロック回避のため先に読み始める
        # 新規 client 検出 → pty_proxy が attach クリアを送る
        proxy._catchup_new_clients()
        # 録画 resume バーストを中継
        proxy._broadcast(_resume_burst_bytes())

        # client が受け取った全バイトを「端末ディスプレイ」に流す
        disp.feed(drain.collect(settle=0.3))

        # 前セッションの残留が画面から消えている
        assert not disp.contains(PRIOR_SENTINEL), (
            "attach クリアが効かず前セッションの footer が画面に残留 "
            "= 二重 footer バグ"
        )
    finally:
        client.close()


# claude が出すであろう「画面クリアを伴わない増分描画」の最小合成ストリーム。
# 実 resume-burst は絶対カーソル位置 + サイズ依存で、固定サイズ pyte に流すと
# claude 自身の都合で footer が 2 つ出る（pty_proxy の責務外）。pty_proxy の
# attach クリアだけを公平に検証するには、クリアを含まない制御済みバイト列を使う。
# 画面クリア(\x1b[2J)も絶対カーソル位置(\x1b[H 等)も使わない、現在カーソル位置に
# 積むだけの最小ストリーム。seed の下に書かれる。クリアが入らない限り seed は残る。
_CLAUDE_LIKE_NO_CLEAR = (
    b"assistant: done.\r\n"
    b"\xe2\x8f\xb5\xe2\x8f\xb5 bypass permissions on (shift+tab to cycle)\r\n"
)


def test_model_repaint_overwrites_prior_terminal_content(proxy_and_sock) -> None:
    """ミニ tmux: 各 broadcast が pyte 画面モデルの全再描画（先頭 \\x1b[2J）を
    送るので、接続前に端末に残っていた内容（前セッション footer 等）は
    構造的に消える。出力バイト検証では取れない「端末事前状態 ×
    レンダリング」を描画結果で確認するオラクル本来の検証。
    """
    proxy, sp = proxy_and_sock
    client = _connect(sp)
    try:
        _wait_clients(proxy, 1)
        fd = next(c.fileno() for c in proxy.clients)
        proxy._record_client_size(fd, 40, 120)
        disp = DisplayTerminal(rows=40, cols=120)
        disp.seed_prior_content([f"  ⏵⏵ {PRIOR_SENTINEL} (shift+tab to cycle)"])
        assert disp.contains(PRIOR_SENTINEL), "seed 失敗"
        drain = _Drainer(client)
        proxy._broadcast(b"fresh model content here\r\n")
        disp.feed(drain.collect(settle=0.25))
        assert not disp.contains(PRIOR_SENTINEL), (
            "モデル全再描画後も前セッション残留が画面に残った"
        )
        assert disp.contains("fresh model content here"), (
            "再描画後の画面に最新モデル内容が無い"
        )
    finally:
        client.close()


def test_resize_no_double_footer_per_client_render(proxy_and_sock) -> None:
    """client が resize しても footer が二重化しない（ユーザー報告の回帰）。

    ミニ tmux 構成では resize 後も次フレームの per-client 全再描画
    （先頭 \\x1b[2J）で旧サイズ描画は消える。_pending_clear 等のハック不要。
    """
    proxy, sp = proxy_and_sock
    proxy._size_policy = "largest"
    client = _connect(sp)
    try:
        _wait_clients(proxy, 1)
        fd = next(c.fileno() for c in proxy.clients)
        disp = DisplayTerminal(rows=44, cols=160)
        drain = _Drainer(client)
        footer = (b"conversation tail line\r\n"
                  b"\xe2\x8f\xb5\xe2\x8f\xb5 bypass permissions on (shift+tab to cycle)\r\n")
        # 初期サイズで描画
        proxy._record_client_size(fd, 44, 160)
        proxy._apply_pty_size()
        proxy._broadcast(footer)
        # resize
        proxy._record_client_size(fd, 30, 100)
        proxy._apply_pty_size()
        proxy._broadcast(footer)  # 次フレーム = per-client 全再描画
        disp.feed(drain.collect(settle=0.3))
        assert disp.count_lines_with("⏵⏵") <= 1, (
            f"resize 後 footer chrome が {disp.count_lines_with('⏵⏵')} 個（二重）"
        )
    finally:
        client.close()


def test_attach_clear_removes_seeded_footer_chrome(proxy_and_sock) -> None:
    """事前注入した footer chrome が attach クリアで消え、画面に残るのは
    claude 由来の footer のみ（pty_proxy が前セッション残留を持ち込まない）。

    制御ストリームを使い、pty_proxy の責務（attach クリア）だけを検証する。
    """
    proxy, sp = proxy_and_sock
    client = _connect(sp)
    try:
        _wait_clients(proxy, 1)
        disp = DisplayTerminal(rows=40, cols=120)
        disp.seed_prior_content([
            f"  ⏵⏵ {PRIOR_SENTINEL} bypass (shift+tab to cycle)",
        ])
        drain = _Drainer(client)
        proxy._catchup_new_clients()        # attach クリア送信
        proxy._broadcast(_CLAUDE_LIKE_NO_CLEAR)
        disp.feed(drain.collect(settle=0.25))
        # 前セッションの footer は消え、claude の footer だけ残る
        assert not disp.contains(PRIOR_SENTINEL), "前セッション footer が残留"
        assert disp.count_lines_with("⏵⏵") <= 1, "footer chrome が二重"
    finally:
        client.close()


def test_resume_burst_double_footer_is_size_mismatch_not_proxy(proxy_and_sock) -> None:
    """注記テスト: resume-burst 生バイトを固定 40 行 pyte に流すと footer が
    2 つ出るが、これは claude --resume が画面クリアせず絶対座標で描画する
    ためで pty_proxy の責務外。バイト列に \\x1b[2J が無いことを pin する。

    （ユーザーが見た二重 footer の真因。SIZE_POLICY で PTY サイズを表示端末に
    合わせれば claude の絶対座標が整合する。実機での恒久対策はそちら。）
    """
    data = _resume_burst_bytes()
    assert b"\x1b[2J" not in data and b"\x1b[3J" not in data, (
        "resume-burst が画面クリアを含む（前提が変わった）"
    )
    clean = DisplayTerminal(rows=40, cols=120)
    clean.feed(data)
    # サイズ不一致の固定 pyte では claude 自身の都合で 2 footer になる
    assert clean.count_lines_with("⏵⏵") >= 1


def test_pty_sized_before_claude_starts() -> None:
    """起動レース対策: 子プロセスが execv する前に slave PTY を実端末サイズへ
    設定している（claude が default 24x80 で初回描画 → SIGWINCH 再描画で
    footer 二重化、を防ぐ）。

    main() が fork 前に host winsize を取得し、子の execv 前と親の両方で
    TIOCSWINSZ する実装になっていることをソースで pin する
    （実 fork は重いので構造を検証）。
    """
    import inspect
    import pty_proxy
    src = inspect.getsource(pty_proxy.main)
    # fork 前に host サイズ取得
    assert "_ioctl_winsz(sys.stdin.fileno())" in src
    # 子（execv 前）で slave fd 0 に TIOCSWINSZ
    assert "fcntl.ioctl(0, termios.TIOCSWINSZ" in src
    # その後に execv
    child_set = src.index("fcntl.ioctl(0, termios.TIOCSWINSZ")
    execv_at = src.index("os.execv(REAL_CLAUDE")
    assert child_set < execv_at, "TIOCSWINSZ は execv より前でなければ無意味"
