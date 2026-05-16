"""tmux セッション・ウィンドウ管理モジュール。"""
import os
import shlex
import subprocess
import sys
from pathlib import Path

from config import TMUX_SESSION
from process_scanner import ClaudeSession


def _tmux(*args: str) -> str:
    r = subprocess.run(["tmux", *args], capture_output=True, text=True)
    return r.stdout.strip()


def _tmux_ok(*args: str) -> bool:
    return subprocess.run(["tmux", *args], capture_output=True).returncode == 0


def _check_tmux() -> None:
    if subprocess.run(["which", "tmux"], capture_output=True).returncode != 0:
        print("エラー: tmux が見つかりません。`brew install tmux` でインストールしてください。")
        sys.exit(1)


class TmuxManager:
    def __init__(self, session: str = TMUX_SESSION):
        _check_tmux()
        self.session = session
        self._key_to_window: dict[str, str] = {}  # session key -> window name

    def ensure_session(self) -> None:
        if not _tmux_ok("has-session", "-t", self.session):
            _tmux("new-session", "-d", "-s", self.session, "-n", "dashboard")

    def setup_dashboard(self, monitor_script: str) -> None:
        """dashboard ウィンドウで dashboard.py --loop を起動する。"""
        target = f"{self.session}:dashboard"
        if not _tmux_ok("select-window", "-t", target):
            _tmux("new-window", "-t", self.session, "-n", "dashboard")
        cmd = f"{shlex.quote(sys.executable)} {shlex.quote(monitor_script)} --loop"
        _tmux("send-keys", "-t", target, cmd, "Enter")

    def list_windows(self) -> list[str]:
        out = _tmux("list-windows", "-t", self.session, "-F", "#{window_name}")
        return out.splitlines() if out else []

    def add_window(self, session: ClaudeSession, socket_path: str | None = None) -> str:
        # monitor 再起動後は _key_to_window が復元済みの場合がある。既存ウィンドウを再利用する。
        existing = self._key_to_window.get(session.key)
        if existing and existing in self.list_windows():
            return existing
        name = self._unique_name(session.short_dir)
        if socket_path:
            cmd = self._socket_cmd(socket_path)
        else:
            shell = os.environ.get("SHELL", "/bin/zsh")
            cmd = (
                f"cd {shlex.quote(session.cwd)} && exec {shell}"
                if session.cwd else f"exec {shell}"
            )
        _tmux("new-window", "-t", self.session, "-n", name, cmd)
        self._key_to_window[session.key] = name
        return name

    def is_socket_client_running(self, window_name: str) -> bool:
        out = _tmux("list-panes", "-t", f"{self.session}:{window_name}",
                    "-F", "#{pane_current_command}")
        cmd = out.lower()
        # bash = リトライラッパー実行中, python = socket_client 直接接続中
        # zsh/sh = フォールバックシェル（未接続）
        return "python" in cmd or "bash" in cmd

    def reattach_window(self, window_name: str, socket_path: str) -> None:
        target = f"{self.session}:{window_name}"
        cmd = self._socket_cmd(socket_path)
        _tmux("send-keys", "-t", target, cmd, "Enter")

    def _socket_cmd(self, socket_path: str) -> str:
        client = Path(__file__).parent / "socket_client.py"
        # --retry で起動タイミングのレースに対応（最大30秒リトライ）
        return (
            f"{shlex.quote(sys.executable)} {shlex.quote(str(client))} "
            f"--retry {shlex.quote(socket_path)}"
        )

    def rename_window(self, key: str, new_name: str) -> None:
        old_name = self._key_to_window.get(key)
        if old_name and old_name in self.list_windows():
            new_name = new_name[:30]
            _tmux("rename-window", "-t", f"{self.session}:{old_name}", new_name)
            self._key_to_window[key] = new_name

    def remove_window(self, key: str) -> None:
        name = self._key_to_window.pop(key, None)
        if name and name in self.list_windows():
            _tmux("kill-window", "-t", f"{self.session}:{name}")

    def _unique_name(self, base: str) -> str:
        base = base[:20]
        existing = self.list_windows()
        if base not in existing:
            return base
        for i in range(2, 50):
            candidate = f"{base[:17]}-{i}"
            if candidate not in existing:
                return candidate
        return base

    def window_for(self, key: str) -> str | None:
        return self._key_to_window.get(key)
