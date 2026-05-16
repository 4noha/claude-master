"""
PTY プロキシへの接続クライアント。tmux ウィンドウ内で動作し、
双方向で Claude セッションに接続する。

使い方: python socket_client.py [--retry] <socket_path>
  --retry  接続失敗時に最大30秒リトライする（起動タイミングのレース対策）

キーバインド:
  nav キー（既定 Ctrl-\\ = \\x1c。環境変数 NAV_KEY で変更可。例
  NAV_KEY=ctrl-]）でナビゲーションモードをトグル。ON 時はキー入力を
  Claude に転送せず ↑↓/PgUp/PgDn/Home/End/jk で過去ログをスクロール。
  同じキーをもう一度押すと OFF。
"""
import fcntl
import os
import select
import signal
import socket
import struct
import sys
import termios
import time
import tty

from config import (NAV_KEY, NAV_PAGE_STEP, NAV_SCROLL_STEP, NAV_WHEEL_STEP,
                    PAGEKEY_SCROLL, WHEEL_SCROLL)
from pty_scroll import classify_wheel, is_live_reset_key

RESIZE_MAGIC = b"\xff\xff"
SCROLL_MAGIC = b"\xff\xfe"  # + !h(dy): proxy 側 ScrollRenderer を pan
_NAV_KEY = NAV_KEY  # 既定 Ctrl-\ (\x1c)。config の NAV_KEY 環境変数で変更可
_NAV_ON_MSG  = ("\r\n\x1b[33m[NAV MODE ON — ↑↓/PgUp/PgDn/Home/End/jk で"
                "ログをスクロール。同じキーで解除]\x1b[0m\r\n").encode()
_NAV_OFF_MSG = "\r\n\x1b[33m[NAV MODE OFF]\x1b[0m\r\n".encode()

# nav-mode 中のキー → スクロール量 dy（負=上/古い方へ遡る, 正=下/新しい方へ）。
# 大きな値は proxy 側で canvas 端に clamp される。
_S = NAV_SCROLL_STEP                   # ↑↓jk の移動行数
_P = NAV_PAGE_STEP                     # PageUp/Dn の移動行数（step と独立）
_SCROLL_KEYS: dict[bytes, int] = {
    b"\x1b[A": -_S,        b"k": -_S,        # ↑ / k : _S 行上（古い）
    b"\x1b[B": _S,         b"j": _S,         # ↓ / j : _S 行下（新しい）
    b"\x1b[5~": -_P,                         # PageUp  : _P 行（既定 10）
    b"\x1b[6~": _P,                          # PageDown: _P 行
    b"\x1b[H": -1000000,   b"g": -1000000,   # Home / g : 最古へ（速度非依存）
    b"\x1b[F": 1000000,    b"G": 1000000,    # End / G : 最下部(live)へ
}
_PGUP = b"\x1b[5~"
_PGDN = b"\x1b[6~"
_W = NAV_WHEEL_STEP  # ホイール1ノッチの移動行数
_FOLLOW_DY = 32767   # SCROLL_MAGIC で送ると proxy 側 scrollback→0（live 復帰）


def _send_resize(sock: socket.socket, stdin_fd: int) -> None:
    try:
        buf = fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, b"\x00" * 8)
        rows, cols, _, _ = struct.unpack("HHHH", buf)
        sock.sendall(RESIZE_MAGIC + struct.pack("!HH", rows, cols))
    except OSError:
        pass


def _connect(sock_path: str, retry: bool) -> socket.socket:
    max_attempts = 30 if retry else 1
    for attempt in range(max_attempts):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(sock_path)
            return sock
        except (FileNotFoundError, ConnectionRefusedError):
            sock.close()
            if attempt + 1 >= max_attempts:
                sys.exit(f"接続失敗: {sock_path}\nプロキシが起動していない可能性があります")
            time.sleep(1)
    raise RuntimeError("unreachable")


def main() -> None:
    args = sys.argv[1:]
    retry = "--retry" in args
    positional = [a for a in args if a != "--retry"]

    if not positional:
        sys.exit("Usage: socket_client.py [--retry] <socket_path>")

    sock = _connect(positional[0], retry)
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    old_tty = termios.tcgetattr(stdin_fd)
    nav_mode = False  # ナビゲーションモード（True の間はキー入力を転送しない）
    pk_scrolled = False  # PAGEKEY_SCROLL: PageUp/Dn でスクロール中か

    try:
        tty.setraw(stdin_fd)
        _send_resize(sock, stdin_fd)
        signal.signal(signal.SIGWINCH, lambda s, f: _send_resize(sock, stdin_fd))

        while True:
            try:
                r, _, _ = select.select([stdin_fd, sock.fileno()], [], [])
            except (ValueError, OSError):
                break

            for fd in r:
                if fd == stdin_fd:
                    try:
                        data = os.read(stdin_fd, 1024)
                    except OSError:
                        return
                    if not data:
                        continue

                    # WHEEL_SCROLL: nav-mode に入らずマウスホイールで
                    # managed scroll（PageUp/Dn 判定より先に消費）。
                    if WHEEL_SCROLL:
                        wd = classify_wheel(data)
                        if wd is not None:
                            dy = wd * min(32767, _W)  # -1=上(過去) 1=下
                            try:
                                sock.sendall(SCROLL_MAGIC
                                             + struct.pack("!h", dy))
                            except OSError:
                                return
                            pk_scrolled = True
                            continue

                    # PAGEKEY_SCROLL: nav-mode に入らず PageUp/PageDown で
                    # managed scroll。他キーで live(最下部) へ自動復帰。
                    if PAGEKEY_SCROLL and data in (_PGUP, _PGDN):
                        dy = -min(32767, _P) if data == _PGUP \
                            else min(32767, _P)
                        try:
                            sock.sendall(SCROLL_MAGIC
                                         + struct.pack("!h", dy))
                        except OSError:
                            return
                        pk_scrolled = True
                        continue
                    if ((PAGEKEY_SCROLL or WHEEL_SCROLL) and pk_scrolled
                            and is_live_reset_key(data)):
                        # 実ユーザー操作（端末 passive レポートでない）→
                        # live 復帰してから、このキーは下で通常どおり処理。
                        # focus(?1004h) 等で誤発火させない。
                        try:
                            sock.sendall(SCROLL_MAGIC
                                         + struct.pack("!h", _FOLLOW_DY))
                        except OSError:
                            return
                        pk_scrolled = False

                    # Ctrl-\ でナビゲーションモードをトグル
                    if _NAV_KEY in data:
                        nav_mode = not nav_mode
                        msg = _NAV_ON_MSG if nav_mode else _NAV_OFF_MSG
                        os.write(stdout_fd, msg)
                        if not nav_mode:
                            # nav-mode 終了: proxy 側 ScrollRenderer を live
                            # (最下部) へ戻す。これが無いとスクロールバック
                            # 位置に貼り付いたまま＝「抜けられない」バグ。
                            # host 側 _host_scroll.follow_bottom() と等価。
                            pk_scrolled = False
                            try:
                                sock.sendall(SCROLL_MAGIC
                                             + struct.pack("!h", _FOLLOW_DY))
                            except OSError:
                                return
                        # Ctrl-\ 以外の部分だけ転送（nav_mode=False の場合）
                        rest = data.replace(_NAV_KEY, b"")
                        if rest and not nav_mode:
                            try:
                                sock.sendall(rest)
                            except OSError:
                                return
                        continue

                    # ナビゲーションモード中: スクロールキーは proxy へ
                    # SCROLL_MAGIC として送る（proxy が画面モデルを pan して
                    # 過去ログを再描画）。その他キーは claude へ転送しない。
                    if nav_mode:
                        dy = _SCROLL_KEYS.get(data)
                        if dy is not None:
                            try:
                                sock.sendall(SCROLL_MAGIC + struct.pack("!h",
                                             max(-32768, min(32767, dy))))
                            except OSError:
                                return
                        continue

                    try:
                        sock.sendall(data)
                    except OSError:
                        return

                else:
                    try:
                        data = sock.recv(4096)
                    except OSError:
                        data = b""
                    if not data:
                        msg = "\r\n\033[33m--- Claude session ended ---\033[0m\r\n"
                        os.write(stdout_fd, msg.encode())
                        return
                    os.write(stdout_fd, data)

    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, old_tty)
        sock.close()


if __name__ == "__main__":
    main()
