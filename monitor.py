"""Claude Code セッション監視デーモン。

使い方:
  python monitor.py          # フォアグラウンド実行
  python monitor.py start    # バックグラウンドデーモン起動
  python monitor.py stop     # デーモン停止
  python monitor.py status   # 現在のセッション一覧を表示して終了
"""
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from config import LOG_FILE, PID_FILE, POLL_INTERVAL, STATUS_FILE
from limit_watcher import LimitEvent, LimitWatcher
from process_scanner import ClaudeSession, scan
from resume_scheduler import ResumeScheduler, send_to_socket
from tmux_manager import TmuxManager

SESSIONS_DIR = Path.home() / ".claude-master" / "sessions"

_ESC = b"\x1b"
_INTERRUPT_MSG = (
    "\n⚠ Usage limit at {pct}% (resets: {reset}). "
    "現在の作業状態を要約して一時停止してください。\n"
)
_RESUME_MSG = "プラン制限がリセットされました。中断前の作業を再開してください。\n"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

SCRIPT_PATH = str(Path(__file__).resolve())


def _read_session_status(pid: int) -> dict:
    """pty_proxy が書き出した <pid>.status.json を読む（usage / reset 時刻）。"""
    p = SESSIONS_DIR / f"{pid}.status.json"
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_status(sessions: list[ClaudeSession], manager: TmuxManager) -> None:
    data = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sessions": [
            {
                **s.to_dict(),
                "window_name": manager.window_for(s.key),
                **_read_session_status(s.pid),
            }
            for s in sessions
        ],
    }
    Path(STATUS_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _handle_limit_event(
    event: LimitEvent,
    session: ClaudeSession,
    manager: TmuxManager,
    scheduler: ResumeScheduler,
    status: dict,
) -> None:
    pct = event.usage_percent
    reset = f"{event.reset_time} {event.reset_tz}".strip()
    # is_active が明示的に False の場合のみアイドルとみなす（旧バージョン互換でデフォルト True）
    is_active = status.get("is_active", True)

    if event.level == "approaching":
        log.warning("使用量 %d%% に到達: %s", pct, session.key)
        manager.rename_window(session.key, f"{session.short_dir}[⚠{pct}%]")
        return

    if not is_active:
        log.info("使用量 %d%% — アイドル中のため中断スキップ: %s", pct, session.key)
        manager.rename_window(session.key, f"{session.short_dir}[⚠{pct}%]")
        return

    log.warning("使用量 %d%% — セッション中断: %s", pct, session.key)
    sock_path = str(SESSIONS_DIR / f"{session.pid}.sock")
    if Path(sock_path).exists():
        msg = _INTERRUPT_MSG.format(pct=pct, reset=reset)
        send_to_socket(sock_path, _ESC + msg.encode())
        reset_at = scheduler.schedule(event, sock_path)
        log.info("再開スケジュール登録: %s → %s", session.key, reset_at)
    manager.rename_window(session.key, f"{session.short_dir}[PAUSED]")


def _resume_sessions(
    scheduler: ResumeScheduler,
    current: dict[str, ClaudeSession],
    manager: TmuxManager,
    watcher: LimitWatcher,
) -> None:
    for key, socket_path in scheduler.due(datetime.now()):
        log.info("セッション再開: %s", key)
        if Path(socket_path).exists():
            send_to_socket(socket_path, _RESUME_MSG.encode())
        watcher.clear(key)
        session = current.get(key)
        if session:
            manager.rename_window(key, session.short_dir)


async def run_loop(manager: TmuxManager) -> None:
    known: dict[str, ClaudeSession] = {}
    watcher = LimitWatcher()
    scheduler = ResumeScheduler()

    while True:
        current = {s.key: s for s in scan()}

        for key, session in current.items():
            if key not in known:
                sock = SESSIONS_DIR / f"{session.pid}.sock"
                socket_path = str(sock) if sock.exists() else None
                window = manager.add_window(session, socket_path=socket_path)
                mode = "PTY proxy" if socket_path else "shell"
                log.info("新規セッション検出: pid=%d dir=%s window=%s mode=%s",
                         session.pid, session.cwd, window, mode)
            else:
                status = _read_session_status(session.pid)
                event = watcher.check(key, status)
                if event:
                    _handle_limit_event(event, session, manager, scheduler, status)
                elif scheduler.is_pending(key) and status.get("is_active"):
                    # スケジュール済みだがセッションがアクティブ = 手動再開
                    log.info("手動再開を検出、スケジュールキャンセル: %s", key)
                    scheduler.remove(key)
                    watcher.clear(key)
                    manager.rename_window(key, session.short_dir)

        for key in list(known):
            if key not in current:
                manager.remove_window(key)
                watcher.clear(key)
                scheduler.remove(key)
                log.info("セッション終了: key=%s", key)

        _resume_sessions(scheduler, current, manager, watcher)

        known = current
        _write_status(list(current.values()), manager)

        await asyncio.sleep(POLL_INTERVAL)


def cmd_run() -> None:
    manager = TmuxManager()
    manager.ensure_session()
    # 再起動時に重複ウィンドウを作らないよう、前回の STATUS_FILE から window 名を復元
    try:
        prev = json.loads(Path(STATUS_FILE).read_text())
        existing = set(manager.list_windows())
        for s in prev.get("sessions", []):
            key, win = s.get("key"), s.get("window_name")
            if key and win and win in existing:
                manager._key_to_window[key] = win
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    manager.setup_dashboard(str(Path(__file__).parent / "dashboard.py"))
    log.info("claude-master 起動")
    print(f"監視開始 (tmux session: {manager.session})")
    print(f"ダッシュボード: tmux attach -t {manager.session}")

    def _shutdown(sig, frame):
        log.info("シグナル受信、終了")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        asyncio.run(run_loop(manager))
    except (SystemExit, KeyboardInterrupt):
        print("\n停止しました")


def cmd_start() -> None:
    if Path(PID_FILE).exists():
        pid = int(Path(PID_FILE).read_text().strip())
        try:
            os.kill(pid, 0)
            print(f"すでに起動中です (PID={pid})")
            return
        except ProcessLookupError:
            pass

    proc = subprocess.Popen(
        [sys.executable, SCRIPT_PATH, "--daemon"],
        stdout=open(LOG_FILE, "a"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    Path(PID_FILE).write_text(str(proc.pid))
    print(f"起動しました (PID={proc.pid})")
    print(f"ログ: {LOG_FILE}")


def cmd_stop() -> None:
    if not Path(PID_FILE).exists():
        print("起動していません")
        return
    pid = int(Path(PID_FILE).read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        Path(PID_FILE).unlink(missing_ok=True)
        print(f"停止しました (PID={pid})")
    except ProcessLookupError:
        Path(PID_FILE).unlink(missing_ok=True)
        print("プロセスが見つかりません（PIDファイルを削除しました）")


def cmd_status() -> None:
    sessions = scan()
    if not sessions:
        print("Claude CLI セッションが見つかりません")
        return
    print(f"{'PID':<8} {'Dir':<20} {'Started':<14} {'CPU%':>6} {'Mem MB':>8}  {'接続'}  ")
    print("-" * 75)
    for s in sessions:
        sock = SESSIONS_DIR / f"{s.pid}.sock"
        mode = "PTY proxy" if sock.exists() else "shell のみ"
        print(
            f"{s.pid:<8} {s.short_dir:<20} {s.start_time:<14} "
            f"{s.cpu_percent:>6.1f} {s.mem_mb:>8.1f}  {mode}"
        )


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] == "--daemon":
        cmd_run()
    elif args[0] == "start":
        cmd_start()
    elif args[0] == "stop":
        cmd_stop()
    elif args[0] == "status":
        cmd_status()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
