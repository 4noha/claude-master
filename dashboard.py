"""ステータスファイルを読み込んで整形表示するダッシュボード。

使い方:
  python dashboard.py          # 一度だけ表示して終了
  python dashboard.py --loop   # 3秒ごとにリフレッシュ（tmux ウィンドウ用）
"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from config import STATUS_FILE

_MIN_W = 78
_REPO_DIR = str(Path(__file__).parent)

# 起動時に一度だけ設定される更新メッセージ（空文字 = 非表示）
_git_update_msg: str | None = None  # None = 未チェック


def _init_git_check() -> None:
    """起動時に一度だけ origin と差分を確認する。ネットワーク呼び出しはここだけ。"""
    global _git_update_msg
    try:
        subprocess.run(
            ["git", "-C", _REPO_DIR, "fetch", "--quiet", "origin"],
            capture_output=True, timeout=15,
        )
        behind = int(subprocess.check_output(
            ["git", "-C", _REPO_DIR, "rev-list", "--count", "HEAD..origin/master"],
            text=True, timeout=5,
        ).strip() or "0")
        ahead = int(subprocess.check_output(
            ["git", "-C", _REPO_DIR, "rev-list", "--count", "origin/master..HEAD"],
            text=True, timeout=5,
        ).strip() or "0")
        if behind > 0:
            _git_update_msg = f"↓ {behind} 件の更新あり (git pull)"
        elif ahead > 0:
            _git_update_msg = f"↑ {ahead} 件ローカル先行"
        else:
            _git_update_msg = "最新"
    except Exception:
        _git_update_msg = ""


def _term_width() -> int:
    return max(_MIN_W, shutil.get_terminal_size(fallback=(78, 24)).columns)


def render(data: dict, w: int | None = None) -> str:
    W = w if w is not None else _term_width()
    sessions = data.get("sessions", [])
    updated = data.get("updated_at", "—")
    # 行レイアウト: ║ {pid:6} {dir:dir_w} {started:13} {cpu:5} {mem:7} {use:5} {reset:10} ║
    # 固定オーバーヘッド: ║(1) sp(1) pid(6) sp(1) sp(1) started(13) sp(1) cpu(5) sp(1) mem(7) sp(1) use(5) sp(1) reset(10) sp(1) ║(1) = 56
    _RESET_W = 10
    _OVERHEAD = 56  # ║ + 固定カラム幅合計 + スペース + ║
    dir_w = max(16, W - _OVERHEAD)
    lines = [
        "╔" + "═" * (W - 2) + "╗",
        "║" + " Claude Code Sessions ".center(W - 2) + "║",
        "╠" + "═" * (W - 2) + "╣",
        ("║ {:<6} {:<" + str(dir_w) + "} {:<13} {:>5} {:>7} {:>5} {:>" + str(_RESET_W) + "} ║").format(
            "PID", "Dir", "Started", "CPU%", "Mem MB", "Use%", "Resets"
        ),
        "╠" + "─" * (W - 2) + "╣",
    ]
    if not sessions:
        lines.append("║" + "  (CLI セッションなし)".ljust(W - 2) + "║")
    else:
        for s in sessions:
            usage = s.get("usage_percent")
            reset = s.get("reset_time", "")
            reset_tz = s.get("reset_tz", "")
            usage_str = f"{usage}%" if usage is not None else "—"
            reset_str = reset if reset else "—"
            fmt = "║ {:<6} {:<" + str(dir_w) + "} {:<13} {:>4.1f}% {:>7.1f} {:>5} {:>" + str(_RESET_W) + "} ║"
            lines.append(fmt.format(
                s["pid"],
                s["short_dir"][:dir_w],
                s["start_time"],
                s["cpu_percent"],
                s["mem_mb"],
                usage_str,
                reset_str,
            ))
            # limit サブ行: usage / reset 情報を空欄でも常時表示
            sid = f"[{s['session_id'][:8]}]" if s.get("session_id") else ""
            limit_parts = [f"Limit: {usage_str}", f"Resets: {reset_str}"]
            if reset_tz:
                limit_parts.append(f"({reset_tz})")
            sub = ("  ".join(filter(None, [sid] + limit_parts))).strip()
            lines.append("║  " + sub.ljust(W - 4) + "║")

    footer_info = f"更新: {updated}  セッション数: {len(sessions)}"
    if _git_update_msg:
        footer_info += f"  ツール: {_git_update_msg}"
    lines += [
        "╠" + "─" * (W - 2) + "╣",
        "║ " + footer_info.ljust(W - 3) + "║",
        "╚" + "═" * (W - 2) + "╝",
    ]
    return "\n".join(lines)


def load() -> dict:
    try:
        return json.loads(Path(STATUS_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main() -> None:
    loop = "--loop" in sys.argv
    if loop:
        _init_git_check()  # tmux 起動時に一度だけリモート確認
    while True:
        if loop:
            print("\033[2J\033[H", end="", flush=True)
        print(render(load()))
        if not loop:
            break
        time.sleep(3)


if __name__ == "__main__":
    main()
