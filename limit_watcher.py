"""上限監視: usage_percent のしきい値を超えたセッションを検出する。"""
from dataclasses import dataclass
from typing import Literal

from config import LIMIT_INTERRUPT_PERCENT, LIMIT_WARN_PERCENT

LimitLevel = Literal["approaching", "interrupt", "reached"]
_LEVEL_ORDER: dict[LimitLevel, int] = {"approaching": 0, "interrupt": 1, "reached": 2}


@dataclass
class LimitEvent:
    session_key: str
    level: LimitLevel
    usage_percent: int
    reset_time: str
    reset_tz: str


class LimitWatcher:
    """セッションごとに usage_percent を監視し、上位レベルに上がった時だけ LimitEvent を返す。"""

    def __init__(self) -> None:
        self._notified: dict[str, LimitLevel] = {}

    def check(self, key: str, status: dict) -> LimitEvent | None:
        pct = status.get("usage_percent")
        if pct is None:
            return None

        if pct >= 100:
            level: LimitLevel = "reached"
        elif pct >= LIMIT_INTERRUPT_PERCENT:
            level = "interrupt"
        elif pct >= LIMIT_WARN_PERCENT:
            level = "approaching"
        else:
            self._notified.pop(key, None)
            return None

        prev = self._notified.get(key)
        if prev is not None and _LEVEL_ORDER[level] <= _LEVEL_ORDER[prev]:
            return None

        self._notified[key] = level
        return LimitEvent(
            session_key=key,
            level=level,
            usage_percent=pct,
            reset_time=status.get("reset_time", ""),
            reset_tz=status.get("reset_tz", ""),
        )

    def clear(self, key: str) -> None:
        self._notified.pop(key, None)
