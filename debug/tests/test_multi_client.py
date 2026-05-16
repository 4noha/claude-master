"""複数 socket_client が同時接続したときの PtyProxy 挙動を unix-socket で実テスト。

検証項目:
  - 2 つのクライアントが同時に accept される
  - PTY 出力が両方にブロードキャストされる
  - 各クライアント独立に renderer / scroll renderer / size 状態を持つ
  - 一方が disconnect しても他方は影響を受けない
  - SIZE_POLICY による複数クライアント時の動作（largest / smallest / client）
"""
import fcntl
import os
import select
import socket
import struct
import termios
import threading
import time
from pathlib import Path

import pytest

from pty_proxy import PtyProxy, RESIZE_MAGIC


def _read_all(sock: socket.socket, timeout: float = 0.5) -> bytes:
    """timeout 秒以内に読めるだけ読む。non-blocking で集める。"""
    out = bytearray()
    sock.settimeout(0.05)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(65536)
            if not chunk:
                break
            out.extend(chunk)
        except socket.timeout:
            if out:
                return bytes(out)
        except BlockingIOError:
            time.sleep(0.05)
    return bytes(out)


def _winsz(fd: int) -> tuple[int, int]:
    buf = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
    rows, cols, _, _ = struct.unpack("HHHH", buf)
    return rows, cols


@pytest.fixture
def proxy_and_sock():
    """PtyProxy を立ち上げて accept loop を起動。

    macOS の AF_UNIX 104 字制限を避けるため /tmp 直下に短いパスで作る。
    """
    import tempfile
    master_fd, slave_fd = os.openpty()
    fd_tmp, sock_path = tempfile.mkstemp(prefix="pxy", suffix=".sock", dir="/tmp")
    os.close(fd_tmp)
    os.unlink(sock_path)  # bind 用に削除（mkstemp が作ったファイルは消す）
    sock_path_obj = Path(sock_path)
    proxy = PtyProxy(master_fd, child_pid=99999, child_pgid=99999, sock_path=sock_path_obj)
    proxy._start_server()
    deadline = time.monotonic() + 1.0
    while not sock_path_obj.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    yield proxy, sock_path
    proxy._alive = False
    for fd in (master_fd, slave_fd):
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        sock_path_obj.unlink()
    except OSError:
        pass


def _connect(sock_path: str) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + 1.0
    while True:
        try:
            s.connect(sock_path)
            return s
        except (FileNotFoundError, ConnectionRefusedError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def _wait_clients(proxy: PtyProxy, n: int, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with proxy._lock:
            if len(proxy.clients) >= n:
                return
        time.sleep(0.02)
    raise TimeoutError(f"only {len(proxy.clients)}/{n} clients connected")


class _Drainer(threading.Thread):
    """socket を背景スレッドで読み続ける。

    `_broadcast` はテストスレッドで同期的に `conn.sendall` するため、
    受信側を誰も読まないと OS ソケットバッファが埋まって sendall が
    ブロックしデッドロックする。連続 broadcast するテストでは
    broadcast ループの **前** に起動しておく。
    """

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(daemon=True)
        self._sock = sock
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._alive = True
        sock.settimeout(0.1)
        self.start()

    def run(self) -> None:
        while self._alive:
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            with self._lock:
                self._buf.extend(chunk)

    def reset(self, settle: float = 0.3) -> None:
        """settle 秒待って今までの受信を破棄（live フレームを捨てる）。"""
        time.sleep(settle)
        with self._lock:
            self._buf.clear()

    def collect(self, settle: float = 0.3) -> bytes:
        time.sleep(settle)
        with self._lock:
            return bytes(self._buf)

    def stop(self) -> None:
        self._alive = False


def test_new_client_gets_model_repaint_on_attach(proxy_and_sock) -> None:
    """新規接続時、pyte 画面モデルからそのクライアントサイズで再描画を受信。

    ミニ tmux 構成: サーバ側の忠実な画面モデルを attach した client の
    サイズで描いて送る（tmux の server-screen replay 相当）。生バイトの
    replay や SIGWINCH 待ちは不要で、真っ白にならない。
    ScrollRenderer の repaint は \\x1b[?2026h（synchronized）で始まり
    \\x1b[2J（全消去）を含む。
    """
    proxy, sock_path = proxy_and_sock
    proxy._broadcast(b"hello model world\r\n")  # 画面モデルに内容を作る
    a = _connect(sock_path)
    try:
        _wait_clients(proxy, 1)
        proxy._record_client_size(a.fileno() if False else
                                  next(c.fileno() for c in proxy.clients), 24, 80)
        proxy._catchup_new_clients()  # モデルを client サイズで再描画
        time.sleep(0.1)
        data = _read_all(a, timeout=0.4)
        assert b"\x1b[2J" in data, f"再描画に全消去が無い: {data[:80]!r}"
        assert b"hello model world" in data, (
            f"画面モデルの内容が新規 client に届いていない: {data[:120]!r}"
        )
    finally:
        a.close()


def test_late_client_gets_recent_output_replayed(proxy_and_sock) -> None:
    """遅れて接続したクライアントが空白にならず、直近の生出力を replay 受信。

    raw passthrough にはサーバ側画面モデルが無く、claude の出力（resume
    バースト）後に接続した tmux クライアントが真っ白になる回帰の防止。
    """
    proxy, sock_path = proxy_and_sock
    # クライアント接続前に claude 出力が流れた状況を作る
    burst = b"conversation line A\r\nconversation line B\r\nSENTINEL_RECENT_OUT\r\n"
    proxy._broadcast(burst)
    # その後でクライアントが接続（late join）
    a = _connect(sock_path)
    try:
        _wait_clients(proxy, 1)
        proxy._catchup_new_clients()  # 新規検出 → クリア + 直近 replay
        time.sleep(0.1)
        data = _read_all(a, timeout=0.4)
        assert b"\x1b[2J" in data, "クリアが来ていない"
        assert b"SENTINEL_RECENT_OUT" in data, (
            f"遅れて接続したクライアントに直近出力が replay されず空白: {data[:120]!r}"
        )
    finally:
        a.close()


def test_screen_clear_only_for_new_clients(proxy_and_sock) -> None:
    """既存クライアントには再 catchup で重ねてクリアを送らない（チラ防止）。"""
    proxy, sock_path = proxy_and_sock
    a = _connect(sock_path)
    try:
        _wait_clients(proxy, 1)
        proxy._catchup_new_clients()
        time.sleep(0.1)
        _read_all(a, timeout=0.3)  # 初回クリアを消費
        proxy._catchup_new_clients()  # 2 回目：新規なし → 何も送らない
        time.sleep(0.1)
        assert _read_all(a, timeout=0.3) == b"", "既存クライアントに再クリアが飛んだ"
    finally:
        a.close()


def test_two_clients_can_both_connect(proxy_and_sock) -> None:
    proxy, sock_path = proxy_and_sock
    a = _connect(sock_path)
    b = _connect(sock_path)
    try:
        _wait_clients(proxy, 2)
        assert len(proxy.clients) == 2
        # 各クライアントに固有の cid が振られている
        cids = list(proxy._client_id.values())
        assert len(set(cids)) == 2
    finally:
        a.close()
        b.close()


def _server_fds(proxy: PtyProxy) -> list[int]:
    """proxy.clients の server-side socket fd を返す（順序保証）。"""
    return [c.fileno() for c in proxy.clients]


def test_broadcast_renders_model_per_client_at_their_size(proxy_and_sock) -> None:
    """ミニ tmux: _broadcast は生中継ではなく、pyte 画面モデルを各 client の
    サイズで再描画して送る。サイズの違う 2 client が **各々のサイズ** の
    フレーム（synchronized + 全消去 + 内容）を受け取り、内容は共通。
    """
    proxy, sock_path = proxy_and_sock
    a = _connect(sock_path)
    b = _connect(sock_path)
    try:
        _wait_clients(proxy, 2)
        fds = [c.fileno() for c in proxy.clients]
        proxy._record_client_size(fds[0], 24, 80)
        proxy._record_client_size(fds[1], 40, 120)  # 別サイズ
        proxy._broadcast("ねこ hello world\r\n".encode())
        time.sleep(0.1)
        da = _read_all(a, timeout=0.4)
        db = _read_all(b, timeout=0.4)
        for who, d in (("A", da), ("B", db)):
            assert d, f"client {who} 何も受信していない"
            assert b"\x1b[2J" in d, f"client {who} に再描画(全消去)が無い: {d[:60]!r}"
            assert "hello world".encode() in d, (
                f"client {who} に画面モデル内容が来ていない: {d[:120]!r}"
            )
        # 生 verbatim ではない（再描画フレームなので synchronized 制御を含む）
        assert b"\x1b[?2026h" in da
    finally:
        a.close()
        b.close()


def test_repeated_broadcast_keeps_rendering(proxy_and_sock) -> None:
    """連続 _broadcast でも毎回モデル再描画が届き、最新内容が反映される。"""
    proxy, sock_path = proxy_and_sock
    a = _connect(sock_path)
    try:
        _wait_clients(proxy, 1)
        proxy._record_client_size(next(c.fileno() for c in proxy.clients), 24, 80)
        proxy._broadcast(b"first line\r\n")
        proxy._broadcast(b"second line\r\n")
        proxy._broadcast(b"FINAL_MARKER\r\n")
        time.sleep(0.1)
        data = _read_all(a, timeout=0.4)
        assert b"FINAL_MARKER" in data, (
            f"最新 broadcast 内容が描画に反映されていない: {data[:120]!r}"
        )
    finally:
        a.close()


def test_scroll_magic_pans_client_into_history(proxy_and_sock) -> None:
    """SCROLL_MAGIC を送ると、その client の ScrollRenderer が過去ログを
    遡り、live の最新行ではなく古い内容を含むフレームが返る。
    （ミニ tmux モードでもログをスクロールで読める = ユーザー要望）。
    """
    import struct
    from pty_proxy import SCROLL_MAGIC
    proxy, sock_path = proxy_and_sock
    a = _connect(sock_path)
    try:
        _wait_clients(proxy, 1)
        conn = next(iter(proxy.clients))          # proxy 側の socket
        fd = conn.fileno()
        proxy._record_client_size(fd, 6, 40)
        # broadcast ループ中に sendall がブロックしないよう先に drain 起動
        drain = _Drainer(a)
        # 画面行数を超える出力 → 古い行が history.top へ
        for i in range(60):
            proxy._broadcast(f"logline{i:03d}\r\n".encode())
        # ここまでの live フレーム（最新行を含む）を捨てる
        drain.reset(settle=0.3)
        # 上へ大きくスクロール（実ソケット送信 → proxy が recv して dispatch）
        a.sendall(SCROLL_MAGIC + struct.pack("!h", -1000))
        conn.settimeout(1.0)
        payload = conn.recv(4096)
        proxy._handle_client_data(conn, fd, payload)
        data = drain.collect(settle=0.3)
        drain.stop()
        text = data.decode("utf-8", "replace")
        assert "logline000" in text or any(
            f"logline{n:03d}" in text for n in range(0, 20)
        ), f"スクロールで過去ログが見えない: {text[-200:]!r}"
        assert "logline059" not in text, "最新行がまだ見えている（pan していない）"
    finally:
        a.close()


def test_scroll_magic_follow_returns_client_to_live(proxy_and_sock) -> None:
    """SCROLL_MAGIC + 大きな正 dy（socket_client が nav-mode 終了時に送る
    _FOLLOW_DY=32767）でその client が live(最下部) へ復帰する。

    回帰: 以前は socket_client が nav-mode を抜けても follow を送らず、
    proxy 側がスクロールバック位置に貼り付いて「抜けられない」バグ。
    """
    import struct
    from pty_proxy import SCROLL_MAGIC
    proxy, sock_path = proxy_and_sock
    a = _connect(sock_path)
    try:
        _wait_clients(proxy, 1)
        conn = next(iter(proxy.clients))
        fd = conn.fileno()
        proxy._record_client_size(fd, 6, 40)
        drain = _Drainer(a)
        for i in range(60):
            proxy._broadcast(f"logline{i:03d}\r\n".encode())
        # 過去へ pan
        a.sendall(SCROLL_MAGIC + struct.pack("!h", -1000))
        conn.settimeout(1.0)
        proxy._handle_client_data(conn, fd, conn.recv(4096))
        drain.reset(settle=0.3)
        # nav-mode 終了相当: follow(32767) を送る
        a.sendall(SCROLL_MAGIC + struct.pack("!h", 32767))
        proxy._handle_client_data(conn, fd, conn.recv(4096))
        text = drain.collect(settle=0.3).decode("utf-8", "replace")
        drain.stop()
        assert "logline059" in text, (
            f"follow で live(最新)へ戻っていない: {text[-200:]!r}")
        assert "logline000" not in text, "まだ過去ログに貼り付いている"
        sr = proxy._client_scrolls.get(fd)
        assert sr is not None and sr.scrollback == 0  # live 追従
    finally:
        a.close()


def test_independent_size_per_client(proxy_and_sock) -> None:
    proxy, sock_path = proxy_and_sock
    a = _connect(sock_path)
    b = _connect(sock_path)
    try:
        _wait_clients(proxy, 2)
        fds = _server_fds(proxy)
        proxy._record_client_size(fds[0], 24, 80)
        proxy._record_client_size(fds[1], 30, 120)
        sizes = set(proxy._client_sizes.values())
        assert (24, 80) in sizes
        assert (30, 120) in sizes
        # 内部独立性: 別の fd 経由で更新しても他方は変わらない
        proxy._record_client_size(fds[0], 40, 100)
        assert proxy._client_sizes[fds[0]] == (40, 100)
        assert proxy._client_sizes[fds[1]] == (30, 120)
    finally:
        a.close()
        b.close()


def test_size_policy_largest_with_multiple_clients(proxy_and_sock) -> None:
    """largest policy で複数クライアントの最大サイズが PTY に適用される。"""
    proxy, sock_path = proxy_and_sock
    proxy._size_policy = "largest"
    a = _connect(sock_path)
    b = _connect(sock_path)
    try:
        _wait_clients(proxy, 2)
        fds = _server_fds(proxy)
        proxy._record_client_size(fds[0], 24, 80)
        proxy._record_client_size(fds[1], 50, 200)
        proxy._apply_pty_size()
        rows, cols = _winsz(proxy.master_fd)
        assert (rows, cols) == (50, 200)
    finally:
        a.close()
        b.close()


def test_size_policy_smallest_with_multiple_clients(proxy_and_sock) -> None:
    proxy, sock_path = proxy_and_sock
    proxy._size_policy = "smallest"
    a = _connect(sock_path)
    b = _connect(sock_path)
    try:
        _wait_clients(proxy, 2)
        fds = _server_fds(proxy)
        proxy._record_client_size(fds[0], 24, 80)
        proxy._record_client_size(fds[1], 50, 200)
        proxy._apply_pty_size()
        rows, cols = _winsz(proxy.master_fd)
        assert (rows, cols) == (24, 80)
    finally:
        a.close()
        b.close()


def test_drop_one_does_not_affect_other(proxy_and_sock) -> None:
    """片方を切断 → broadcast 経由で _drop が呼ばれ、他方は引き続き受信可能。"""
    proxy, sock_path = proxy_and_sock
    a = _connect(sock_path)
    b = _connect(sock_path)
    try:
        _wait_clients(proxy, 2)
        a.close()
        # broadcast が a への sendall で OSError → _drop で回収される
        for _ in range(3):
            proxy._broadcast(b"after-disconnect\r\n")
            time.sleep(0.05)
        data_b = _read_all(b, timeout=0.5)
        assert len(data_b) > 0, "client B received nothing after A disconnect"
        assert len(proxy.clients) == 1
    finally:
        b.close()
