"""tmux 環境で pty_proxy + mock_claude を動かし、capture-pane で出力を取得するハーネス。

オフラインリプレイ（replay.py）で再現できない実環境バグ専用:
  - 起動直後のフッター消失
  - マウスホイール（コピーモード）での画面崩れ
  - host 端末と tmux クライアントの並列レンダリング差異
  - 実 SIGWINCH での resize 挙動

使い方:
  python debug/tmux_harness.py --fixture sample-recent --width 120 --height 40
  python debug/tmux_harness.py --fixture footer-recovery --diff debug/snapshots/footer-recovery.tmux.txt
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSION = "claude-master-debug"

# 決定性のため diff 比較時に正規化する一過性パターン
_SPINNER_CHARS = "✻✽✶✷✸✹✺✢✣✤✥✦✧✩✪✫✬✭✮✯✰✱✲✳✴✵·"
_NORMALIZE_PATTERNS = (
    (re.compile(rf"[{re.escape(_SPINNER_CHARS)}]"), "*"),     # spinner 文字を * に
    (re.compile(r"\(\d+s(?:\s*·\s*\w+)?\)"), "(NNs)"),         # "(15s · thinking)" → "(NNs)"
    (re.compile(r"\(\d+m \d+s(?:\s*·\s*\w+)?\)"), "(NNm NNs)"),  # 分秒
    (re.compile(r"for\s+\d+s\b"), "for NNs"),                  # "for 39s"
    (re.compile(r"for\s+\d+m \d+s\b"), "for NNm NNs"),         # "for 10m 22s"
    (re.compile(r"[ \t]+$", re.MULTILINE), ""),                # 行末空白
)


def normalize_for_diff(text: str) -> str:
    """tmux capture-pane の一過性要素（spinner / 経過時間）を平準化。"""
    for pat, repl in _NORMALIZE_PATTERNS:
        text = pat.sub(repl, text)
    return text


def tmux(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], check=check, capture_output=capture, text=True)


class TmuxHarness:
    def __init__(self, session: str = SESSION) -> None:
        self.session = session
        self._dual = False  # クライアントペインを作ったか

    def kill(self) -> None:
        subprocess.run(["tmux", "kill-session", "-t", self.session],
                       capture_output=True, text=True)

    def start(self, fixture_dir: Path, width: int, height: int,
              dual: bool = False, mock_delay: float = 0.02) -> None:
        """tmux セッションを作って pane 0 で pty_proxy + mock_claude を起動。

        dual=True なら pane 1 を split し socket_client で接続（dual-terminal バグ用）。
        mock_delay: chunk 毎の sleep。決定性向上のため default 20ms。0 で無効化。
        """
        self.kill()
        tmux("new-session", "-d", "-s", self.session,
             "-x", str(width), "-y", str(height))
        # tmux のレンダリングが整うまで一瞬待つ
        time.sleep(0.2)

        bytes_path = fixture_dir / "bytes.bin"
        env_setup = (
            f"export REAL_CLAUDE={ROOT}/debug/mock_claude.py;"
            f"export MOCK_FIXTURE={bytes_path};"
            f"export MOCK_DELAY={mock_delay};"
            f"export PYTHONPATH={ROOT};"
            f"export PYTHONUNBUFFERED=1;"
        )
        cmd = f"{env_setup} python3 {ROOT}/pty_proxy.py"
        tmux("send-keys", "-t", f"{self.session}:0.0", cmd, "Enter")

        # pty_proxy が socket を作るまで待つ
        deadline = time.monotonic() + 5.0
        sock_path: Path | None = None
        while time.monotonic() < deadline:
            socks = sorted(
                (Path.home() / ".claude-master" / "sessions").glob("*.sock"),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )
            if socks and (time.time() - socks[0].stat().st_mtime) < 5:
                sock_path = socks[0]
                break
            time.sleep(0.1)
        self.sock_path = sock_path

        if dual and sock_path is not None:
            tmux("split-window", "-h", "-t", f"{self.session}:0",
                 "-l", "50%")
            client_cmd = f"python3 {ROOT}/socket_client.py {sock_path}"
            tmux("send-keys", "-t", f"{self.session}:0.1", client_cmd, "Enter")
            self._dual = True
            time.sleep(0.5)

    def add_clients(self, n: int) -> None:
        """proxy ペインの隣に N 個の socket_client ペインを並べて追加する。
        マルチ tmux 観察用。複数 client が独立して描画されることを目視確認できる。
        """
        if self.sock_path is None:
            return
        for _ in range(n):
            tmux("split-window", "-v", "-t", f"{self.session}:0")
            tmux("select-layout", "-t", f"{self.session}:0", "tiled")
            # 末尾ペインに socket_client を起動
            pane_idx = self._last_pane_index()
            client_cmd = f"python3 {ROOT}/socket_client.py {self.sock_path}"
            tmux("send-keys", "-t", f"{self.session}:0.{pane_idx}", client_cmd, "Enter")
            time.sleep(0.3)

    def _last_pane_index(self) -> int:
        out = subprocess.run(
            ["tmux", "list-panes", "-t", f"{self.session}:0", "-F", "#{pane_index}"],
            check=True, capture_output=True, text=True,
        ).stdout.split()
        return int(out[-1]) if out else 0

    def resize(self, width: int, height: int) -> None:
        """ウィンドウサイズを変えて SIGWINCH を発火させる。"""
        tmux("resize-window", "-t", self.session, "-x", str(width), "-y", str(height))
        time.sleep(0.3)

    def scroll_up(self, pages: int = 1) -> None:
        """copy-mode に入って previous-page を発行（マウスホイール上スクロール相当）。"""
        tmux("copy-mode", "-t", f"{self.session}:0.0")
        for _ in range(pages):
            tmux("send-keys", "-t", f"{self.session}:0.0", "-X", "previous-page")
        time.sleep(0.2)
        tmux("send-keys", "-t", f"{self.session}:0.0", "-X", "cancel")

    def detach_clients(self) -> None:
        subprocess.run(["tmux", "detach-client", "-s", self.session],
                       capture_output=True)

    def toggle_nav_mode(self, pane: int = 1) -> None:
        """socket_client が動いているクライアントペインで Ctrl-\\ を送る。
        nav-mode の ON/OFF をトグル。dual=True で起動したときに pane 1 が対象。
        """
        # tmux の send-keys に C-\\ を渡す
        tmux("send-keys", "-t", f"{self.session}:0.{pane}", "C-\\")
        time.sleep(0.3)

    def send_keys(self, keys: str, pane: int = 0) -> None:
        """任意キーを送る（プロンプト送信などに使用）。"""
        tmux("send-keys", "-t", f"{self.session}:0.{pane}", keys)
        time.sleep(0.2)

    def attach_capture(self) -> str:
        """detach 中でも capture-pane は動くので、socket 経由のレンダリングが
        attach 状態に依存しないことを検証するために使う。"""
        # tmux capture-pane は attach 不要で動作する。detach 後の最新状態を返す。
        return self.capture()

    def capture(self, pane: int = 0, escape: bool = False) -> str:
        """pane の現在の表示を文字列で返す。escape=True なら ANSI 付き (-e)。"""
        target = f"{self.session}:0.{pane}"
        args = ["capture-pane", "-p", "-t", target]
        if escape:
            args.insert(2, "-e")
        result = subprocess.run(["tmux", *args],
                                check=True, capture_output=True, text=True)
        return result.stdout

    def wait_for_idle(self, max_wait: float = 15.0,
                      stable_captures: int = 5, interval: float = 0.25) -> None:
        """capture-pane が stable_captures 回連続で同じになるまで待つ。

        mock_claude が高速にバイトを流すと、pyte の lazy emission や
        フッターアニメーションでキャプチャ毎に内容が揺れる。連続一致で
        スクリーンが完全に落ち着いたと判断する。
        """
        deadline = time.monotonic() + max_wait
        stable_count = 0
        last = self.capture()
        while time.monotonic() < deadline:
            time.sleep(interval)
            cur = self.capture()
            if cur == last:
                stable_count += 1
                if stable_count >= stable_captures:
                    return
            else:
                stable_count = 0
                last = cur


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", required=True,
                    help="debug/fixtures/<name> ディレクトリ名")
    ap.add_argument("--width", type=int, default=120)
    ap.add_argument("--height", type=int, default=40)
    ap.add_argument("--dual", action="store_true",
                    help="pane 1 を split して socket_client を起動")
    ap.add_argument("--multi-client", type=int, default=0,
                    help="proxy 起動後に N 個の socket_client ペインを並べる（複数 tmux 観察用）")
    ap.add_argument("--resize-to", type=str, default="",
                    help="起動後 <wait>:WxH を発火 (例: 2.0:80x24)")
    ap.add_argument("--scroll", type=int, default=0,
                    help="起動安定後に N ページ上スクロール")
    ap.add_argument("--nav-toggle", action="store_true",
                    help="--dual 時に client pane へ Ctrl-\\ を送り nav-mode を ON/OFF")
    ap.add_argument("--detach-test", action="store_true",
                    help="detach 後も capture-pane が同じ内容を返すか検証")
    ap.add_argument("--out", type=Path, default=None,
                    help="capture 結果をこのパスに書く")
    ap.add_argument("--diff", type=Path, default=None,
                    help="capture を指定スナップショットと比較し exit 1 で不一致")
    ap.add_argument("--keep", action="store_true",
                    help="終了時に tmux セッションを残す（デバッグ用）")
    ap.add_argument("--mock-delay", type=float, default=0.02,
                    help="mock_claude の chunk 間 sleep（決定性向上、default 20ms）")
    ap.add_argument("--normalize", action="store_true",
                    help="capture を spinner / 経過時間平準化して --diff 比較")
    args = ap.parse_args()

    fixture_dir = ROOT / "debug" / "fixtures" / args.fixture
    if not (fixture_dir / "bytes.bin").exists():
        sys.stderr.write(f"fixture が見つかりません: {fixture_dir}\n"); return 1
    meta_path = fixture_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    harness = TmuxHarness()
    print(f"[harness] starting tmux session {harness.session} ({args.width}x{args.height})")
    harness.start(fixture_dir, args.width, args.height, dual=args.dual,
                  mock_delay=args.mock_delay)
    print(f"[harness] socket: {harness.sock_path}")

    if args.multi_client > 0:
        print(f"[harness] adding {args.multi_client} client pane(s)")
        harness.add_clients(args.multi_client)

    if args.resize_to:
        wait_s, size_s = args.resize_to.split(":")
        rows_s, cols_s = size_s.lower().split("x")
        time.sleep(float(wait_s))
        print(f"[harness] resize → {rows_s}x{cols_s}")
        harness.resize(int(cols_s), int(rows_s))

    harness.wait_for_idle()

    if args.scroll > 0:
        print(f"[harness] scroll up {args.scroll} page(s)")
        harness.scroll_up(args.scroll)

    if args.nav_toggle and args.dual:
        print("[harness] nav-mode ON / OFF (client pane Ctrl-\\)")
        before = harness.capture(pane=1)
        harness.toggle_nav_mode(pane=1)
        time.sleep(0.5)
        after_on = harness.capture(pane=1)
        harness.toggle_nav_mode(pane=1)
        time.sleep(0.5)
        after_off = harness.capture(pane=1)
        nav_on_visible = "NAV MODE ON" in after_on
        nav_off_visible = "NAV MODE OFF" in after_off
        print(f"  NAV ON visible:  {nav_on_visible}")
        print(f"  NAV OFF visible: {nav_off_visible}")

    if args.detach_test:
        print("[harness] detach test")
        before = harness.capture()
        harness.detach_clients()
        time.sleep(0.3)
        after = harness.capture()
        same = before.rstrip() == after.rstrip()
        print(f"  capture before/after detach: {'identical' if same else 'CHANGED ⚠'}")

    captured = harness.capture()
    print(f"[harness] captured {len(captured)} bytes from pane 0")

    if args.dual:
        captured_client = harness.capture(pane=1)
        print(f"[harness] captured {len(captured_client)} bytes from pane 1 (client)")

    exit_code = 0
    if args.out:
        args.out.write_text(captured)
        print(f"[harness] wrote → {args.out}")

    if args.diff is not None:
        expected = args.diff.read_text() if args.diff.exists() else ""
        actual = captured
        if args.normalize:
            actual = normalize_for_diff(actual)
            expected = normalize_for_diff(expected)
        if actual.rstrip() != expected.rstrip():
            sys.stderr.write(f"MISMATCH against {args.diff}"
                             f"{' (normalized)' if args.normalize else ''}\n")
            for i, (a, b) in enumerate(zip(actual.splitlines(),
                                          expected.splitlines())):
                if a != b:
                    sys.stderr.write(f"  line {i + 1}:\n  actual:   {a!r}\n  expected: {b!r}\n")
                    break
            exit_code = 1
        else:
            print(f"[harness] golden match ✓"
                  f"{' (normalized)' if args.normalize else ''}")

    if not args.keep:
        harness.kill()
    else:
        print(f"[harness] tmux session kept: tmux attach -t {harness.session}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
