"""Claude Code CLI プロセスの検出モジュール。"""
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

from config import INCLUDE_VSCODE


@dataclass
class ClaudeSession:
    pid: int
    cwd: str
    session_id: Optional[str]
    start_time: str
    cpu_percent: float
    mem_mb: float

    @property
    def key(self) -> str:
        return self.session_id or f"pid-{self.pid}"

    @property
    def short_dir(self) -> str:
        return self.cwd.rstrip("/").split("/")[-1] or "unknown"

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "cwd": self.cwd,
            "short_dir": self.short_dir,
            "session_id": self.session_id,
            "start_time": self.start_time,
            "cpu_percent": self.cpu_percent,
            "mem_mb": self.mem_mb,
            "key": self.key,
        }


def _get_cwd_lsof(pid: int) -> str:
    try:
        r = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.splitlines():
            if line.startswith("n"):
                return line[1:]
    except Exception:
        pass
    return ""


def _extract_session_id(cmdline: list[str]) -> Optional[str]:
    for i, arg in enumerate(cmdline):
        if arg == "--resume" and i + 1 < len(cmdline):
            val = cmdline[i + 1]
            if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", val):
                return val
    return None


def _is_vscode_session(cmdline: list[str]) -> bool:
    joined = " ".join(cmdline)
    return "--output-format" in joined and "stream-json" in joined


def scan() -> list[ClaudeSession]:
    return _scan_psutil() if _HAS_PSUTIL else _scan_ps()


def _is_claude_proc(name: str, cmdline: list[str]) -> bool:
    # ターミナル起動: name がバージョン文字列、cmdline[0] が "claude"
    # VS Code 起動: name が "claude"、cmdline[0] がフルパス ending in /claude
    if name == "claude":
        return True
    if cmdline and (cmdline[0] == "claude" or cmdline[0].endswith("/claude")):
        return True
    return False


def _scan_psutil() -> list[ClaudeSession]:
    sessions = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time", "memory_info"]):
        try:
            name = proc.info["name"] or ""
            cmdline: list[str] = proc.info["cmdline"] or []
            if not _is_claude_proc(name, cmdline):
                continue

            if _is_vscode_session(cmdline) and not INCLUDE_VSCODE:
                continue

            try:
                cwd = proc.cwd()
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                cwd = _get_cwd_lsof(proc.info["pid"])

            sessions.append(ClaudeSession(
                pid=proc.info["pid"],
                cwd=cwd,
                session_id=_extract_session_id(cmdline),
                start_time=datetime.fromtimestamp(
                    proc.info["create_time"]
                ).strftime("%m-%d %H:%M"),
                cpu_percent=proc.cpu_percent(),
                mem_mb=round(proc.info["memory_info"].rss / 1024 / 1024, 1),
            ))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except (SystemError, OSError, Exception):
            # macOS の psutil で proc_cmdline 等が SystemError を吐くことがある
            # （保護プロセス・ゾンビ等）。1 プロセス分スキップしてスキャン継続。
            continue
    return sessions


def _scan_ps() -> list[ClaudeSession]:
    try:
        r = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=10)
    except Exception:
        return []

    sessions = []
    for line in r.stdout.splitlines()[1:]:
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        pid_str, cpu_str, command = parts[1], parts[2], parts[10]

        cmd_base = command.split()[0]
        if not (cmd_base == "claude" or cmd_base.endswith("/claude")):
            continue

        cmdline = command.split()
        if _is_vscode_session(cmdline) and not INCLUDE_VSCODE:
            continue

        try:
            pid = int(pid_str)
        except ValueError:
            continue

        cwd = _get_cwd_lsof(pid)
        sessions.append(ClaudeSession(
            pid=pid,
            cwd=cwd,
            session_id=_extract_session_id(cmdline),
            start_time=parts[8],
            cpu_percent=float(cpu_str),
            mem_mb=0.0,
        ))
    return sessions


if __name__ == "__main__":
    results = scan()
    if not results:
        print("Claude CLI セッションが見つかりません")
    for s in results:
        print(
            f"PID={s.pid:<6} dir={s.short_dir:<20} "
            f"session={s.session_id or '(新規)':<36} "
            f"cpu={s.cpu_percent:>5.1f}% mem={s.mem_mb:>7.1f}MB"
        )
