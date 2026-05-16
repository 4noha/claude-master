"""
PTY プロキシ: claude を PTY 上で動かし、複数クライアントに I/O を多重化する。

使い方: python pty_proxy.py [claude の引数...]
  REAL_CLAUDE 環境変数で本物の claude バイナリパスを指定可能
  PTY_PROXY_DEBUG=1 を設定するとデバッグログ (~/.claude-master-proxy.log) を出力
  PTY_PROXY_LOG=1   各 I/O チャネルの生バイトを ~/.claude-master/logs/<pid>/ に記録
                    （pty_raw / host_in / host_out / client_<n>_in / client_<n>_out）
"""
import fcntl
import json
import logging
import os
import pty
import select
import signal
import socket
import struct
import sys
import termios
import threading
import time
import tty
from pathlib import Path
from typing import IO

from config import (HOST_FLOW_SCROLLBACK, NAV_KEY, NAV_PAGE_STEP,
                    NAV_SCROLL_STEP, NAV_WHEEL_STEP, PAGEKEY_SCROLL,
                    SESSION_LOG, SIZE_POLICY, WHEEL_SCROLL)
from pty_emulator import TuiEmulator
from pty_scroll import (HistoryFlusher, ScrollRenderer, classify_wheel,
                        is_live_reset_key, line_to_text)


def resolve_pty_size(
    policy: str,
    host_size: tuple[int, int] | None,
    client_sizes: dict[int, tuple[int, int]],
    last_client_size: tuple[int, int] | None,
    latest_size: tuple[int, int] | None,
    default: tuple[int, int] = (24, 80),
) -> tuple[int, int]:
    """SIZE_POLICY に従い PTY の (rows, cols) を決定する。pure 関数（テスト容易）。

    policy:
      host:     host stdin TTY のサイズを常に正とする
      client:   最後に resize を送ってきたクライアントを正とする（不在時は host）
      latest:   host か client か問わず最新の resize イベントを正とする（旧挙動）
      smallest: host を含む接続中端末の中の最小サイズ（全員同じ内容を見られる安全モード）
    """
    p = (policy or "client").lower()
    if p == "host":
        return host_size or default
    if p == "client":
        return last_client_size or host_size or default
    if p == "latest":
        return latest_size or host_size or default
    if p == "smallest":
        sizes = list(client_sizes.values())
        if host_size is not None:
            sizes.append(host_size)
        if not sizes:
            return default
        return (min(s[0] for s in sizes), min(s[1] for s in sizes))
    if p == "largest":
        sizes = list(client_sizes.values())
        if host_size is not None:
            sizes.append(host_size)
        if not sizes:
            return default
        return (max(s[0] for s in sizes), max(s[1] for s in sizes))
    # 未知のポリシーは host fallback
    return host_size or default

_DEBUG = os.environ.get("PTY_PROXY_DEBUG") == "1"
if _DEBUG:
    _log_path = Path.home() / ".claude-master-proxy.log"
    logging.basicConfig(
        filename=str(_log_path),
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    _log = logging.getLogger("pty_proxy")
else:
    _log = logging.getLogger("pty_proxy")
    _log.addHandler(logging.NullHandler())

SESSIONS_DIR = Path.home() / ".claude-master" / "sessions"
LOGS_DIR = Path.home() / ".claude-master" / "logs"
REAL_CLAUDE = os.environ.get("REAL_CLAUDE", str(Path.home() / ".local" / "bin" / "claude"))
RESIZE_MAGIC = b"\xff\xff"  # クライアントからのリサイズ通知の先頭マジックバイト
SCROLL_MAGIC = b"\xff\xfe"  # nav-mode スクロール通知（+ !h(dy)）
_LOG_IO = bool(os.environ.get("PTY_PROXY_LOG"))

# host が ScrollRenderer 描画のとき（SIZE_POLICY!=host）の nav-mode 用。
_NAV_KEY = NAV_KEY  # 既定 Ctrl-\ (\x1c)。config の NAV_KEY 環境変数で変更可
_HOST_NAV_ON = ("\r\n\x1b[33m[NAV MODE ON — ↑↓/PgUp/PgDn/Home/End/jk で"
                "ログをスクロール。同じキーで解除]\x1b[0m\r\n").encode()
_HOST_NAV_OFF = "\r\n\x1b[33m[NAV MODE OFF]\x1b[0m\r\n".encode()
_HS = NAV_SCROLL_STEP  # ↑↓jk の移動行数（host nav）
_HP = NAV_PAGE_STEP    # PageUp/Dn の移動行数（step と独立）
_HOST_SCROLL_KEYS: dict[bytes, int] = {
    b"\x1b[A": -_HS, b"k": -_HS,
    b"\x1b[B": _HS,  b"j": _HS,
    b"\x1b[5~": -_HP,
    b"\x1b[6~": _HP,
    b"\x1b[H": -1000000, b"g": -1000000,
    b"\x1b[F": 1000000,  b"G": 1000000,
}
_PGUP = b"\x1b[5~"  # PageUp
_PGDN = b"\x1b[6~"  # PageDown

# attach / 起動 / PTY サイズ変化時に一度だけ送る画面クリア。
# \x1b[2J で全消去後、カーソルを「画面最下部の左端」へ置く。
# claude (--resume 含む) はカーソルが最下部にある前提で「下から描画 +
# スクロールアップ」するため、クリア後にカーソルが最上部 (\x1b[H) だと
# 絶対座標/相対描画がずれて画面が崩れる（ユーザー実測で確認）。
# 行番号 9999 は各端末が自分の実サイズへ clamp するので、host と tmux で
# サイズが違っても各々の最下部に正しく落ちる。
# 起動直後、最初の per-client 再描画が出るまでの一瞬、host 端末に前の
# シェル内容が残らないよう一度だけ送る（render_viewport も先頭で \x1b[2J
# するので冗長だが、起動の見栄えのため）。
_CLEAR_SEQ = b"\x1b[2J\x1b[9999;1H"


class IOLogger:
    """各 I/O チャネルの生バイトをファイルへ追記する。デバッグ・回帰検証用。"""

    def __init__(self, pid: int, enabled: bool) -> None:
        self.enabled = enabled
        if not enabled:
            return
        self.dir = LOGS_DIR / str(pid)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._files: dict[str, IO[bytes]] = {}
        self._events = (self.dir / "events.log").open("w", buffering=1)
        self._lock = threading.Lock()
        self._t0 = time.monotonic()

    def log(self, name: str, data: bytes) -> None:
        if not self.enabled or not data:
            return
        with self._lock:
            f = self._files.get(name)
            if f is None:
                f = (self.dir / f"{name}.bin").open("wb")
                self._files[name] = f
            f.write(data)
            f.flush()
            ts = time.monotonic() - self._t0
            self._events.write(f"{ts:9.3f} {name:20s} {len(data):6d}\n")

    def close(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            for f in self._files.values():
                try:
                    f.close()
                except OSError:
                    pass
            try:
                self._events.close()
            except OSError:
                pass


def _ioctl_winsz(fd: int) -> tuple[int, int]:
    buf = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
    rows, cols, _, _ = struct.unpack("HHHH", buf)
    return rows, cols


def _set_pty_size(master_fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def _kill_pgid(pgid: int) -> None:
    """プロセスグループを SIGTERM → SIGKILL で確実に終了させる。"""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except OSError:
            break


class PtyProxy:
    def __init__(self, master_fd: int, child_pid: int, child_pgid: int, sock_path: Path):
        self.master_fd = master_fd
        self.child_pid = child_pid
        self.child_pgid = child_pgid
        self.sock_path = sock_path
        self.clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._alive = True
        self._stdin_fd: int = -1
        self._iolog = IOLogger(child_pid, _LOG_IO)
        self._client_id: dict[int, int] = {}
        self._known_client_fds: set[int] = set()
        # client は pyte 画面モデルを自分サイズへ viewport 再描画する
        # ScrollRenderer を fd ごとに保持。
        self._client_scrolls: dict[int, ScrollRenderer] = {}
        # host: SIZE_POLICY=host のとき生パススルー（VSCode のネイティブ
        # スクロールバックを活用）。それ以外（largest 等）のときは
        # _host_scroll で viewport 再描画し、Ctrl-\ nav-mode + スクロール
        # キーで過去ログを遡れる（host_raw_mode が False の経路）。
        self._host_scroll: ScrollRenderer = ScrollRenderer()
        self._host_nav_mode: bool = False
        # SIZE_POLICY のための状態
        self._size_policy: str = SIZE_POLICY
        self._host_raw_mode: bool = (SIZE_POLICY == "host")
        # HOST_FLOW_SCROLLBACK（実験的オプトイン）: SIZE_POLICY!=host でも
        # host へ確定行を流し native scrollback を得る。host_raw_mode のときは
        # 元々生中継で native scrollback が効くので無関係（False のまま）。
        self._host_flow_mode: bool = (
            (not self._host_raw_mode) and HOST_FLOW_SCROLLBACK)
        self._host_flow: HistoryFlusher = HistoryFlusher()
        self._host_flow_first: bool = True
        self._client_sizes: dict[int, tuple[int, int]] = {}  # fd -> (rows, cols)
        self._last_client_size: tuple[int, int] | None = None
        self._host_size: tuple[int, int] | None = None
        self._latest_size: tuple[int, int] | None = None
        self._next_cid: int = 0
        try:
            # pyte スクリーンは usage/is_active 走査のためだけに保持（再構成しない）
            self._emulator: TuiEmulator | None = TuiEmulator(rows=24, cols=80)
        except RuntimeError:
            _log.warning("pyte not available, usage extraction disabled")
            self._emulator = None
        self._status_path = SESSIONS_DIR / f"{child_pid}.status.json"
        # 自動再開: セッション復帰後に ❯ アイドル状態で一度だけ注入するプロンプト
        self._inject_prompt: str | None = None
        self._inject_ready_at: float | None = None
        self._last_usage: dict | None = None
        self._last_is_active: bool | None = None
        self._last_status_write: float = 0.0
        # セッション全文ログ（ファイル）。SIZE_POLICY/flow とは独立。
        self._session_fp: IO[str] | None = None
        self._session_flusher: HistoryFlusher | None = None
        self._open_session_log()

    # ── セッション全文ログ ─────────────────────────────────────────────
    def _open_session_log(self) -> None:
        """SESSION_LOG が有効ならファイルを追記オープン。"""
        spec = (SESSION_LOG or "").strip()
        if not spec or spec.lower() in ("false", "0", "off", "no"):
            return
        if spec.lower() in ("true", "1", "on", "yes"):
            path = LOGS_DIR / f"session-{self.child_pid}.log"
        else:
            path = Path(os.path.expanduser(spec))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 追記・行バッファ（途中でハードキルされても直前まで残る）
            self._session_fp = open(path, "a", buffering=1, encoding="utf-8",
                                    errors="replace")
            self._session_flusher = HistoryFlusher()
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            self._session_fp.write(
                f"\n===== claude-master session pid={self.child_pid} "
                f"start {ts} =====\n")
        except OSError as e:
            _log.warning("session log open failed: %s", e)
            self._session_fp = None
            self._session_flusher = None

    def _session_log_capture(self) -> None:
        """_broadcast から毎回。確定スクロールアウト行を逐次ファイルへ。"""
        if (self._session_fp is None or self._session_flusher is None
                or self._emulator is None):
            return
        try:
            self._session_flusher.capture(self._emulator._screen)
            new = self._session_flusher.drain()
            if new:
                self._session_fp.write(
                    "".join(line_to_text(ln) + "\n" for ln in new))
        except (OSError, ValueError) as e:
            _log.debug("session log write error: %s", e)

    def _finalize_session_log(self) -> None:
        """終了時: 残りの確定行＋最終可視画面を書き出して閉じる。"""
        if self._session_fp is None:
            return
        try:
            if self._session_flusher is not None and self._emulator is not None:
                self._session_flusher.capture(self._emulator._screen)
                tail = self._session_flusher.drain()
                if tail:
                    self._session_fp.write(
                        "".join(line_to_text(ln) + "\n" for ln in tail))
                # 一度もスクロールアウトしていない最終可視画面の中身
                screen = self._emulator._screen
                vis = [line_to_text(screen.buffer[y])
                       for y in range(screen.lines)]
                while vis and not vis[-1]:
                    vis.pop()
                if vis:
                    self._session_fp.write("\n".join(vis) + "\n")
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            self._session_fp.write(
                f"===== session end {ts} =====\n")
        except (OSError, ValueError) as e:
            _log.debug("session log finalize error: %s", e)
        finally:
            try:
                self._session_fp.close()
            except OSError:
                pass
            self._session_fp = None

    # ── サイズポリシー ─────────────────────────────────────────────────
    def _record_host_size(self, rows: int, cols: int) -> None:
        self._host_size = (rows, cols)
        self._latest_size = (rows, cols)

    def _record_client_size(self, fd: int, rows: int, cols: int) -> None:
        self._client_sizes[fd] = (rows, cols)
        self._last_client_size = (rows, cols)
        self._latest_size = (rows, cols)

    def _forget_client_size(self, fd: int) -> None:
        self._client_sizes.pop(fd, None)
        if self._client_sizes:
            # 残りクライアントの中で最近 dict 末尾のものを last とみなす
            self._last_client_size = list(self._client_sizes.values())[-1]
        else:
            self._last_client_size = None

    def _apply_pty_size(self) -> tuple[int, int]:
        """SIZE_POLICY に従い PTY サイズを再計算して適用、(rows, cols) を返す。

        raw passthrough 構成では renderer が無いのでリセットは不要。
        PTY サイズを TIOCSWINSZ で設定すれば claude が SIGWINCH を受けて
        新サイズで再描画し、その生バイトが全端末に中継される。
        pyte スクリーンも追従 resize する（usage 走査用）。
        """
        # host サイズを実端末から読み直す。_host_size は起動時/SIGWINCH 時
        # しか更新されず、client RESIZE_MAGIC 経由の _apply_pty_size では古い
        # ままになる。largest/smallest は host の実サイズに追従しないと、
        # モデルが client(tmux) サイズに固定され host nav が
        # 「カーソル最下部まで」しか遡れない（viewport>モデルで max_oy 減少）。
        if self._stdin_fd >= 0:
            try:
                hr, hc = _ioctl_winsz(self._stdin_fd)
                if hr > 0 and hc > 0:
                    self._host_size = (hr, hc)
            except OSError:
                pass
        rows, cols = resolve_pty_size(
            self._size_policy,
            self._host_size,
            self._client_sizes,
            self._last_client_size,
            self._latest_size,
        )
        _set_pty_size(self.master_fd, rows, cols)
        if self._emulator is not None:
            prev = (self._emulator._rows, self._emulator._cols)
            try:
                self._emulator.resize(rows, cols)
            except Exception as e:
                _log.exception("emulator resize error: %s", e)
            if (rows, cols) != prev and self._host_flow_mode:
                # リサイズで pyte が reflow するので flow の identity 追跡を
                # 再 arm し、次回 flush は clean baseline（全消去）から。
                # capture 済み _pending（確定済みの正しい行）は保持。
                self._host_flow.reset()
                self._host_flow_first = True
        # ミニ tmux 構成では各端末は pyte 画面モデルから自分サイズで再描画
        # するので、旧来の「サイズ変化時に全端末クリア」ハックは不要
        # （次フレームの per-client repaint で正しく出る）。
        return rows, cols

    # ── ソケットサーバー（別スレッド） ───────────────────────────────────
    def _start_server(self) -> None:
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self.sock_path))
        srv.listen(8)
        threading.Thread(target=self._accept_loop, args=(srv,), daemon=True).start()

    def _accept_loop(self, srv: socket.socket) -> None:
        while self._alive:
            try:
                srv.settimeout(1.0)
                conn, _ = srv.accept()
                with self._lock:
                    self.clients.append(conn)
                    self._client_id[conn.fileno()] = self._next_cid
                    self._next_cid += 1
            except socket.timeout:
                continue
            except OSError:
                break

    # ── I/O ブロードキャスト ─────────────────────────────────────────────
    def _broadcast(self, data: bytes) -> None:
        """claude 出力で pyte 画面モデルを更新し、各端末へ自分サイズで再描画。

        ハイブリッド構成:
          - PTY サイズ = host 端末サイズ（SIZE_POLICY=host 既定）。claude は
            host サイズで絶対座標描画する。
          - **host** は claude の生バイトをそのまま中継（verbatim）。よって
            VSCode 端末の **ネイティブスクロールバック**が普通に効き、
            ユーザーは過去ログをスクロールで読める（claude を直接動かした
            ときと全く同じ挙動）。PTY==host サイズなので崩れない。
          - **tmux client** は host サイズの忠実 pyte 画面モデルから自分の
            端末サイズで viewport を再描画して受け取る（ミニ tmux）。
            小さい端末は最下部追従 + nav-mode pan、大きい端末は余白。
            tmux 側はネイティブスクロール不可（tmux copy-mode を使う想定。
            ユーザーが「仕方ない」と許容済み）。
        log/footer のヒューリスティック分類は一切しない（脆さの根を断つ）。
        """
        self._iolog.log("pty_raw", data)

        if self._emulator is None:
            # pyte 無し: 画面モデルを作れないので生中継にフォールバック
            self._fallback_raw_broadcast(data)
            self._maybe_write_status()
            return

        self._emulator.feed_screen_only(data)

        # セッション全文ログ（ファイル）。描画とは独立・常時。
        self._session_log_capture()

        # host 出力:
        #   host_raw_mode (SIZE_POLICY=host): claude 生バイトを verbatim →
        #     VSCode のネイティブスクロールバックで過去ログを読める
        #   それ以外 (largest 等): pyte 画面モデルから host サイズで viewport
        #     再描画（ミニ tmux）。Ctrl-\ nav-mode + スクロールキーで遡る
        if self._stdin_fd >= 0:
            try:
                if self._host_raw_mode:
                    os.write(sys.stdout.fileno(), data)
                    self._iolog.log("host_out", data)
                else:
                    try:
                        hr, hc = _ioctl_winsz(self._stdin_fd)
                    except OSError:
                        hr, hc = (self._host_size or (24, 80))
                    if self._host_flow_mode and not self._host_nav_mode:
                        # quiescence ゲート: ストリーミング中は確定行を
                        # capture（取りこぼし防止）するだけで scrollback へは
                        # 書かず、live は安全な全消去再描画で見せる。確定行の
                        # scrollback 書き出しは claude 静止時に
                        # _flush_host_flow() でまとめて行う（遷移中の中間状態
                        # を native scrollback に混ぜない＝壊れない）。
                        self._host_flow.capture(self._emulator._screen)
                    out = self._host_scroll.render_viewport(
                        self._emulator._screen, hr, hc)
                    os.write(sys.stdout.fileno(), out)
                    self._iolog.log("host_out", out)
            except OSError:
                pass

        # 自動再開プロンプトの注入: アイドル ❯ 状態が 1 秒続いたら注入
        if self._inject_prompt:
            if self._emulator.idle_prompt_visible():
                if self._inject_ready_at is None:
                    self._inject_ready_at = time.monotonic()
                elif time.monotonic() - self._inject_ready_at >= 1.0:
                    prompt = self._inject_prompt
                    self._inject_prompt = None
                    self._inject_ready_at = None
                    _log.debug("auto-resume: injecting prompt (%d chars)", len(prompt))
                    try:
                        os.write(self.master_fd, prompt.encode() + b"\r")
                    except OSError as e:
                        _log.debug("inject write error: %s", e)
            else:
                self._inject_ready_at = None

        self._render_clients()
        self._maybe_write_status()

    def _fallback_raw_broadcast(self, data: bytes) -> None:
        """pyte が無いときだけ使う生バイト中継。"""
        if self._stdin_fd >= 0:
            try:
                os.write(sys.stdout.fileno(), data)
            except OSError:
                pass
        with self._lock:
            snapshot = list(self.clients)
        for c in snapshot:
            try:
                c.sendall(data)
            except OSError:
                self._drop(c)

    def _render_host_now(self) -> None:
        """host を ScrollRenderer で即再描画（nav-mode スクロール反映用）。
        host_raw_mode のときは呼ばれない。"""
        if self._emulator is None or self._stdin_fd < 0:
            return
        try:
            hr, hc = _ioctl_winsz(self._stdin_fd)
        except OSError:
            hr, hc = (self._host_size or (24, 80))
        try:
            os.write(sys.stdout.fileno(),
                     self._host_scroll.render_viewport(
                         self._emulator._screen, hr, hc))
        except OSError:
            pass

    def _host_wheel(self, data: bytes) -> bool:
        """WHEEL_SCROLL: nav-mode に入らずマウスホイールで managed scroll。

        ホイール上=過去へ / 下=新しい方へ nav_wheel_step 行。consume して
        claude へ転送しない（claude 自身のホイール用途は無効になる）。
        ホイール以外のマウス(クリック/ドラッグ)・無効時・raw/flow は False
        （= claude へ透過）。`_host_pagekey` より先に呼ぶこと（pagekey の
        「他キーで live 復帰」がホイールで誤発火しないように）。"""
        if (not WHEEL_SCROLL or self._host_raw_mode
                or self._host_flow_mode):
            return False
        d = classify_wheel(data)
        if d is None:
            return False
        self._host_scroll.scroll(d * NAV_WHEEL_STEP)
        self._render_host_now()
        return True

    def _host_pagekey(self, data: bytes) -> bool:
        """PAGEKEY_SCROLL: nav-mode に入らず PageUp/PageDown で managed
        scroll する。

        - PageUp/PageDown 単独入力 → `_host_scroll` を pan して即再描画。
          claude へは転送しない。True を返す（呼び出し側は消費）。
        - それ以外のキー → **nav-mode 中でなく**スクロール中（scrollback>0）
          なら live へ復帰＋再描画してから False を返す（カーソル移動/文字
          入力で位置リセット）。nav-mode 中は nav-mode がスクロール寿命を
          持つので auto-reset しない（さもないと `_loop` 順序上 nav-mode の
          ↑↓/jk が毎回 follow_bottom され 1 ステップしか遡れなくなる＝
          socket_client が pk_scrolled でゲートしているのと同じ理由）。
        host_raw_mode/flow_mode のとき・無効時は False（何もしない）。
        """
        if (not PAGEKEY_SCROLL or self._host_raw_mode
                or self._host_flow_mode):
            return False
        if data == _PGUP:
            self._host_scroll.scroll(-_HP)
            self._render_host_now()
            return True
        if data == _PGDN:
            self._host_scroll.scroll(_HP)
            self._render_host_now()
            return True
        # ページキー以外: nav-mode 中でなく、スクロール中で、かつ
        # 「実ユーザー操作」（端末の passive レポートでない）のときだけ
        # live 復帰。フォーカスレポート(?1004h)等で誤発火させない。
        if (not self._host_nav_mode and self._host_scroll.scrollback > 0
                and is_live_reset_key(data)):
            self._host_scroll.follow_bottom()
            self._render_host_now()
        return False

    def _handle_host_stdin(self, data: bytes) -> None:
        """host stdin 1 読み取りのディスパッチ（`_loop` から分離＝テスト可能）。

        判定順は固定: raw/flow 全転送 → wheel → pagekey → NAV_KEY トグル
        → nav-mode スクロール → 通常転送。順序自体が仕様（nav-mode と
        pagekey/wheel の相互作用はこの順序に依存）なので統合テストで担保する。
        """
        if self._host_raw_mode or self._host_flow_mode:
            # 生パススルー / flow: native scrollback 利用。全キー転送
            self._iolog.log("host_in", data)
            os.write(self.master_fd, data)
            return
        if self._host_wheel(data):
            return                       # WHEEL_SCROLL 消費（pagekey より先）
        if self._host_pagekey(data):
            return                       # PAGEKEY_SCROLL 消費
        if _NAV_KEY in data:
            # Ctrl-\ で nav-mode トグル
            self._host_nav_mode = not self._host_nav_mode
            if not self._host_nav_mode:
                self._host_scroll.follow_bottom()
            os.write(sys.stdout.fileno(),
                     _HOST_NAV_ON if self._host_nav_mode else _HOST_NAV_OFF)
            rest = data.replace(_NAV_KEY, b"")
            if rest and not self._host_nav_mode:
                self._iolog.log("host_in", rest)
                os.write(self.master_fd, rest)
            elif self._host_nav_mode and self._emulator is not None:
                self._render_host_now()
            return
        if self._host_nav_mode:
            # nav-mode 中: スクロールキーは host_scroll を pan、他は不転送
            dy = _HOST_SCROLL_KEYS.get(data)
            if dy is not None:
                self._host_scroll.scroll(dy)
                self._render_host_now()
            return
        self._iolog.log("host_in", data)
        os.write(self.master_fd, data)

    def _flush_host_flow(self) -> None:
        """claude 静止（idle tick）時に呼ぶ。capture 済みの確定行を host の
        端末ネイティブ scrollback へまとめて書き出し、live 領域を no-clear
        in-place 再描画する。pending が無ければ何もしない。

        画面が安定している瞬間にだけ scrollback を書くので、出力バースト中
        の中間フレーム・リサイズ途中の崩れた状態が native scrollback に
        混ざらない（= ユーザー案。flow の壊れを構造的に防ぐ）。"""
        if (not self._host_flow_mode or self._host_nav_mode
                or self._emulator is None or self._stdin_fd < 0):
            return
        if not self._host_flow.has_pending:
            return
        try:
            hr, hc = _ioctl_winsz(self._stdin_fd)
        except OSError:
            hr, hc = (self._host_size or (24, 80))
        committed = self._host_flow.drain()
        try:
            out = self._host_scroll.render_flow(
                self._emulator._screen, hr, hc, committed,
                self._host_flow_first)
            self._host_flow_first = False
            os.write(sys.stdout.fileno(), out)
            self._iolog.log("host_out", out)
        except OSError:
            pass

    def _render_clients(self) -> None:
        """pyte 画面モデルを各 client へそのサイズで viewport 再描画。

        host は生パススルー（_broadcast 側で処理）なのでここでは扱わない。
        """
        screen = self._emulator._screen
        with self._lock:
            snapshot = list(self.clients)
        dead: list[socket.socket] = []
        for c in snapshot:
            fd = c.fileno()
            sr = self._client_scrolls.get(fd)
            if sr is None:
                sr = ScrollRenderer()
                self._client_scrolls[fd] = sr
            rows, cols = self._client_sizes.get(fd, (24, 80))
            try:
                out = sr.render_viewport(screen, rows, cols)
                c.sendall(out)
                cid = self._client_id.get(fd, -1)
                self._iolog.log(f"client_{cid}_out", out)
            except OSError:
                dead.append(c)
        for c in dead:
            self._drop(c)

    def _maybe_write_status(self) -> None:
        if self._emulator is None:
            return
        now = time.monotonic()
        if now - self._last_status_write < 5.0:
            return
        self._last_status_write = now
        usage = self._emulator.extract_usage()
        is_active = self._emulator.is_active()
        if usage == self._last_usage and is_active == self._last_is_active:
            return
        self._last_usage = usage
        self._last_is_active = is_active
        try:
            payload = {
                "pid": self.child_pid,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "is_active": is_active,
            }
            if usage:
                payload.update(usage)
            self._status_path.parent.mkdir(parents=True, exist_ok=True)
            self._status_path.write_text(json.dumps(payload, ensure_ascii=False))
        except OSError as e:
            _log.debug("status write error: %s", e)

    def _drop(self, conn: socket.socket) -> None:
        with self._lock:
            if conn in self.clients:
                self.clients.remove(conn)
            fd = conn.fileno()
            self._client_id.pop(fd, None)
            self._known_client_fds.discard(fd)
            self._client_scrolls.pop(fd, None)
            self._forget_client_size(fd)
            remaining = len(self.clients)
        try:
            conn.close()
        except OSError:
            pass
        # policy に従ってサイズ再計算（host fallback / smallest 再計算など）
        if self._stdin_fd >= 0:
            try:
                rows, cols = _ioctl_winsz(self._stdin_fd)
                self._record_host_size(rows, cols)
            except OSError:
                pass
        try:
            self._apply_pty_size()
            if remaining == 0:
                os.killpg(self.child_pgid, signal.SIGWINCH)
        except OSError:
            pass

    def _cleanup_child(self) -> int:
        """残存ワーカーを含むプロセスグループを終了させ、ステータスを返す。"""
        _kill_pgid(self.child_pgid)
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        try:
            _, status = os.waitpid(self.child_pid, 0)
            return os.waitstatus_to_exitcode(status)
        except ChildProcessError:
            return getattr(self, "_child_exit_status", 0)

    # ── メインループ ──────────────────────────────────────────────────────
    def run(self) -> int:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self._start_server()

        stdin_fd = sys.stdin.fileno()
        _log.debug("run: stdin_fd=%d isatty=%s", stdin_fd, os.isatty(stdin_fd))

        if not os.isatty(stdin_fd):
            _log.error("stdin is not a tty, cannot set raw mode")
            sys.stderr.write("pty_proxy: stdin は tty ではありません\n")

        try:
            old_tty = termios.tcgetattr(stdin_fd)
        except termios.error as e:
            _log.error("tcgetattr failed: %s", e)
            sys.stderr.write(f"pty_proxy: tcgetattr エラー: {e}\n")
            old_tty = None

        def _sigint_handler(sig: int, frame) -> None:  # noqa: ARG001
            _log.debug("SIGINT received, forwarding to child pgid=%d", self.child_pgid)
            _kill_pgid(self.child_pgid)
            raise KeyboardInterrupt

        self._stdin_fd = stdin_fd

        def _sigwinch_handler(s: int, f) -> None:  # noqa: ARG001
            rows, cols = _ioctl_winsz(stdin_fd)
            self._record_host_size(rows, cols)
            # PTY サイズを policy に従って再計算。claude が SIGWINCH を受けて
            # 再描画し、その生バイトが全端末に中継される（raw passthrough）。
            self._apply_pty_size()

        try:
            if old_tty is not None:
                tty.setraw(stdin_fd)
                _log.debug("setraw OK")
            rows, cols = _ioctl_winsz(stdin_fd)
            self._record_host_size(rows, cols)
            self._apply_pty_size()
            # raw passthrough では pty_proxy が画面掃除をしないため、claude が
            # （resume 等で）全画面クリアせず増分描画すると、この端末に前の
            # セッションが残した footer 等が残留して二重に見える。attach 時に
            # 一度だけ全画面クリアして claude にクリーンな画面を渡す
            # （毎フレーム掃除ではない＝旧来の脆い再構成とは別物。tmux 等と同じ）。
            if old_tty is not None:
                try:
                    os.write(sys.stdout.fileno(), _CLEAR_SEQ)
                except OSError:
                    pass
            signal.signal(signal.SIGWINCH, _sigwinch_handler)
            signal.signal(signal.SIGINT, _sigint_handler)
            return self._loop(stdin_fd if old_tty is not None else -1)
        except KeyboardInterrupt:
            return 130
        except Exception as e:
            _log.exception("run loop error: %s", e)
            raise
        finally:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            if old_tty is not None:
                try:
                    termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, old_tty)
                except termios.error:
                    pass
            self._alive = False
            self._finalize_session_log()
            self.sock_path.unlink(missing_ok=True)
            self._status_path.unlink(missing_ok=True)
            self._iolog.close()

    def _catchup_new_clients(self) -> None:
        """新規クライアント接続時、pyte 画面モデルから現画面を即再描画して送る。

        ミニ tmux 構成ではサーバ側に忠実な画面モデル（pyte）があるので、
        遅れて接続したクライアント（monitor が後から socket_client を起動
        する等）にも、その時点の画面をそのクライアントのサイズで描いて
        送れる（tmux の attach 時 server-screen replay と同じ）。
        生バイト replay も SIGWINCH 待ちも不要。真っ白にならない。"""
        with self._lock:
            clients = list(self.clients)
            cur_fds = {c.fileno() for c in clients}
        new_fds = cur_fds - self._known_client_fds
        self._known_client_fds = cur_fds
        if not new_fds or self._emulator is None:
            return
        screen = self._emulator._screen
        for c in clients:
            fd = c.fileno()
            if fd not in new_fds:
                continue
            sr = self._client_scrolls.get(fd)
            if sr is None:
                sr = ScrollRenderer()
                self._client_scrolls[fd] = sr
            rows, cols = self._client_sizes.get(fd, (24, 80))
            try:
                c.sendall(sr.render_viewport(screen, rows, cols))
            except OSError:
                pass

    def _child_exited(self) -> bool:
        try:
            pid, status = os.waitpid(self.child_pid, os.WNOHANG)
            if pid:
                self._child_exit_status = os.waitstatus_to_exitcode(status)
                return True
        except ChildProcessError:
            self._child_exit_status = 0
            return True
        return False

    def _handle_client_data(self, conn: socket.socket, fd: int,
                            data: bytes) -> None:
        """client から recv したバイト列を処理する本番ディスパッチ。

        `_loop` の select/recv/drop プラミングから分離（recv 済みデータを
        渡す前提。空データの drop は呼び出し側責務）。RESIZE/SCROLL マジック
        を剥がして残りを claude へ転送する。
        """
        cid = self._client_id.get(fd, -1)
        if data.startswith(RESIZE_MAGIC) and len(data) >= 6:
            rows, cols = struct.unpack("!HH", data[2:6])
            self._record_client_size(fd, rows, cols)
            # SIZE_POLICY に従い PTY サイズを再計算。claude が SIGWINCH で
            # 再描画し生バイトが全端末へ中継される。
            self._apply_pty_size()
            self._iolog.log(f"client_{cid}_resize",
                            f"{rows}x{cols}\n".encode())
            data = data[6:]
        if data.startswith(SCROLL_MAGIC) and len(data) >= 4:
            # nav-mode スクロール: その client の ScrollRenderer を pan
            (dy,) = struct.unpack("!h", data[2:4])
            sr = self._client_scrolls.get(fd)
            if sr is None:
                sr = ScrollRenderer()
                self._client_scrolls[fd] = sr
            sr.scroll(dy)
            self._iolog.log(f"client_{cid}_scroll", f"{dy}\n".encode())
            # 即座に再描画して pan を反映（claude 出力を待たない）
            if self._emulator is not None:
                crows, ccols = self._client_sizes.get(fd, (24, 80))
                try:
                    conn.sendall(sr.render_viewport(
                        self._emulator._screen, crows, ccols))
                except OSError:
                    self._drop(conn)
            data = data[4:]
        if data:
            self._iolog.log(f"client_{cid}_in", data)
            os.write(self.master_fd, data)

    def _loop(self, stdin_fd: int) -> int:
        input_base = [stdin_fd] if stdin_fd >= 0 else []
        _log.debug("_loop start: master_fd=%d stdin_fd=%d", self.master_fd, stdin_fd)
        self._child_exit_status: int = 0

        while True:
            with self._lock:
                client_fds = [c.fileno() for c in self.clients]

            all_fds = input_base + client_fds + [self.master_fd]
            try:
                r, _, _ = select.select(all_fds, [], [], 0.5)
            except (ValueError, OSError) as e:
                _log.debug("select error: %s", e)
                break

            if not r:
                # select タイムアウト＝claude 出力なし＝静止。ここで初めて
                # 確定行を native scrollback へ書き出す（quiescence ゲート）。
                self._flush_host_flow()
                if self._child_exited():
                    _log.debug("child exited (timeout path)")
                    return self._cleanup_child()
                # アイドル中に接続してきた新規クライアントに現在のフッター状態を送信
                self._catchup_new_clients()
                continue

            r_set = set(r)

            if stdin_fd >= 0 and stdin_fd in r_set:
                try:
                    data = os.read(stdin_fd, 1024)
                    if not data:
                        _log.debug("stdin EOF")
                    else:
                        self._handle_host_stdin(data)
                except OSError as e:
                    _log.debug("stdin read error: %s", e)

            for fd in client_fds:
                if fd not in r_set:
                    continue
                with self._lock:
                    conn = next((c for c in self.clients if c.fileno() == fd), None)
                if conn is None:
                    continue
                try:
                    data = conn.recv(4096)
                except OSError:
                    data = b""
                if not data:
                    self._drop(conn)
                    continue
                self._handle_client_data(conn, fd, data)

            if self.master_fd in r_set:
                try:
                    data = os.read(self.master_fd, 4096)
                    self._broadcast(data)
                except OSError:
                    _log.debug("master_fd EIO, cleaning up child pgid=%d", self.child_pgid)
                    return self._cleanup_child()

            if self._child_exited():
                _log.debug("child exited (post-select path)")
                return self._cleanup_child()

        return 0


AUTO_RESUME_FILE = Path.home() / ".claude-master" / "auto_resume.json"


def _read_auto_resume() -> dict | None:
    """auto_resume.json を読み込み、5 分以内なら内容を返す。"""
    if not AUTO_RESUME_FILE.exists():
        return None
    try:
        data = json.loads(AUTO_RESUME_FILE.read_text())
        if time.time() - data.get("created_at", 0) > 300:
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _find_session_id(project_dir: Path | None = None) -> str | None:
    """カレントプロジェクトの最新セッション ID を ~/.claude/projects/ から検索する。"""
    cwd = project_dir or Path.cwd()
    # Claude Code はパスの `/` を `-` に、非 ASCII 文字（Unicode ハイフン等）も `-` に変換する
    project_name = "".join(c if c.isascii() else "-" for c in str(cwd)).replace("/", "-")
    project_path = Path.home() / ".claude" / "projects" / project_name
    if not project_path.exists():
        return None
    jsonls = list(project_path.glob("*.jsonl"))
    if not jsonls:
        return None
    return max(jsonls, key=lambda p: p.stat().st_mtime).stem


def main() -> None:
    if not Path(REAL_CLAUDE).exists():
        sys.exit(f"エラー: claude が見つかりません: {REAL_CLAUDE}\n"
                 f"REAL_CLAUDE 環境変数で正しいパスを指定してください")

    args = sys.argv[1:]
    inject_prompt: str | None = None

    # fork 前にホスト端末サイズを取得しておく。pty.fork() の子は即 execv で
    # claude を起動するため、親が後から TIOCSWINSZ するのでは間に合わず、
    # claude が default(24x80) で --resume を描画 → 後で正サイズに SIGWINCH
    # → claude は \x1b[2J しないので最初の誤サイズ footer が残り二重化する。
    try:
        _init_rows, _init_cols = _ioctl_winsz(sys.stdin.fileno())
        if _init_rows <= 0 or _init_cols <= 0:
            _init_rows, _init_cols = 24, 80
    except OSError:
        _init_rows, _init_cols = 24, 80

    while True:
        pid, master_fd = pty.fork()
        if pid == 0:
            # 子: execv 前に slave PTY（fd 0=制御端末）のサイズを実端末へ。
            # ここで設定すれば claude は起動時から正しいサイズで描画し、
            # default サイズでの初回描画 → SIGWINCH 再描画の二重化が起きない
            # （完全レースフリー）。
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ,
                            struct.pack("HHHH", _init_rows, _init_cols, 0, 0))
            except OSError:
                pass
            os.execv(REAL_CLAUDE, [REAL_CLAUDE] + args)
            sys.exit(1)

        # 親側でも保険として master_fd 経由で設定（同一 pty に反映）。
        _set_pty_size(master_fd, _init_rows, _init_cols)

        try:
            child_pgid = os.getpgid(pid)
        except OSError:
            child_pgid = pid

        sock_path = SESSIONS_DIR / f"{pid}.sock"
        proxy = PtyProxy(master_fd, pid, child_pgid, sock_path)
        if inject_prompt:
            proxy._inject_prompt = inject_prompt
            inject_prompt = None

        exit_code = proxy.run()

        # 自動再開チェック: セッション終了後に auto_resume.json があれば再起動
        resume = _read_auto_resume()
        if resume:
            AUTO_RESUME_FILE.unlink(missing_ok=True)
            session_id = resume.get("session_id") or _find_session_id(
                Path(resume["project_dir"]) if "project_dir" in resume else None
            )
            if session_id:
                inject_prompt = resume.get("task", "")
                args = ["--resume", session_id]
                _log.debug("auto-resume: session=%s task=%r", session_id, inject_prompt[:60])
                continue

        sys.exit(exit_code)


if __name__ == "__main__":
    main()
