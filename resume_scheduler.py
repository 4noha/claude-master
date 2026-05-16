"""リセット時刻にセッションを再開するスケジューラー。

pending は JSON ファイルに永続化するため monitor 再起動後も再開が機能する。
"""
import json
import re
import socket as _socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from limit_watcher import LimitEvent

_PENDING_FILE = Path.home() / ".claude-master" / "pending_resumes.json"


def send_to_socket(socket_path: str, data: bytes) -> bool:
    """UNIX ソケットに bytes を送信する。接続できなければ False を返す。"""
    try:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(socket_path)
        sock.sendall(data)
        sock.close()
        return True
    except OSError:
        return False


def _parse_tz_offset(tz_str: str) -> float:
    """'UTC+9', 'UTC-5:30', 'Asia/Tokyo' などからオフセット時間 (float) を返す。不明は 0.0。"""
    if not tz_str:
        return 0.0
    # "+9" / "UTC+9" / "UTC-5:30" 形式
    m = re.search(r"([+-])(\d{1,2})(?::(\d{2}))?", tz_str)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours = int(m.group(2))
        minutes = int(m.group(3) or 0)
        return sign * (hours + minutes / 60)
    # "Asia/Tokyo" 等の IANA タイムゾーン名
    try:
        from zoneinfo import ZoneInfo
        utcoffset = datetime.now(tz=ZoneInfo(tz_str)).utcoffset()
        if utcoffset is not None:
            return utcoffset.total_seconds() / 3600
    except Exception:
        pass
    return 0.0


def _parse_reset_datetime(time_str: str, tz_str: str) -> datetime | None:
    """"8:30 pm" / "5am" + "UTC+9" / "Asia/Tokyo" → aware datetime（今日か翌日の次の発生時刻）。"""
    if not time_str:
        return None
    s = time_str.strip().lower()
    t = None
    for fmt in ("%I:%M %p", "%I:%M%p", "%I %p", "%I%p", "%H:%M"):
        try:
            t = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if t is None:
        return None

    offset_h = _parse_tz_offset(tz_str)
    tz = timezone(timedelta(hours=offset_h))
    now = datetime.now(tz=tz)
    candidate = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


class ResumeScheduler:
    def __init__(self) -> None:
        self._pending: dict[str, tuple[datetime, str]] = {}  # key → (reset_at, socket_path)
        self._load()

    def schedule(self, event: LimitEvent, socket_path: str) -> datetime | None:
        """reset_at を計算して pending に登録。計算できなければ None を返す。"""
        reset_at = _parse_reset_datetime(event.reset_time, event.reset_tz)
        if reset_at is None:
            return None
        self._pending[event.session_key] = (reset_at, socket_path)
        self._save()
        return reset_at

    def due(self, now: datetime) -> list[tuple[str, str]]:
        """now が reset_at を過ぎたエントリを返し pending から削除する。"""
        now_utc = now.astimezone(timezone.utc)
        result = []
        for key, (reset_at, sock) in list(self._pending.items()):
            if now_utc >= reset_at.astimezone(timezone.utc):
                result.append((key, sock))
                del self._pending[key]
        if result:
            self._save()
        return result

    def is_pending(self, key: str) -> bool:
        return key in self._pending

    def remove(self, key: str) -> None:
        self._pending.pop(key, None)
        self._save()

    def _save(self) -> None:
        _PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            k: {"reset_at": v[0].isoformat(), "socket_path": v[1]}
            for k, v in self._pending.items()
        }
        _PENDING_FILE.write_text(json.dumps(data, indent=2))

    def _load(self) -> None:
        if not _PENDING_FILE.exists():
            return
        try:
            data = json.loads(_PENDING_FILE.read_text())
            for k, v in data.items():
                reset_at = datetime.fromisoformat(v["reset_at"])
                self._pending[k] = (reset_at, v["socket_path"])
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
