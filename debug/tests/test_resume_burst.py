import pytest as _pytest; pytestmark = _pytest.mark.legacy  # raw passthrough 化で本番未使用（debug/replay 用）
"""claude --resume の会話履歴一括再描画で本文が重複 emit されない回帰テスト。

報告: `claude --resume <id>` 起動で同じ行が 2 行出てきた。
原因: 同じ可視テキストでも選択ハイライト (\x1b[48;2;55;55;55m) / 折返し /
      色状態の差で ANSI バイト列が変わり、_recent_emitted のバイト完全一致
      dedup をすり抜けて同一文が 2〜3 回 emit されていた。
修正: _recent_emitted を ANSI 剥がしテキストキーで dedup（_norm_emit）。
      maxlen も resume の大量再描画用に 2000 へ拡大。
"""
import json
import re
from collections import Counter
from pathlib import Path

from pty_emulator import TuiEmulator
from pty_constants import _ANSI_ALL_RE
from replay import replay

FX = Path(__file__).resolve().parent.parent / "fixtures" / "resume-burst"


def _emit_log_lines(snapshot: str) -> list[str]:
    out, in_log = [], False
    for l in snapshot.splitlines():
        if l.startswith("LOG:"):
            in_log = True
            continue
        if l.startswith("FOOTER:") or l.startswith("CURSOR:"):
            in_log = False
            continue
        if in_log and l.strip():
            out.append(l.strip())
    return out


def test_resume_burst_no_prose_duplication() -> None:
    """本文（罫線/ツール行以外）の同一行が 3 回以上 emit されない。"""
    meta = json.loads((FX / "meta.json").read_text())
    data = (FX / "bytes.bin").read_bytes()
    snap, _ = replay(data, rows=meta["height"], cols=meta["width"], chunk_size=4096)
    lines = [
        l for l in _emit_log_lines(snap)
        if len(l) > 12
        and not l.startswith(("⏺", "⎿"))            # ツール行は別トレードオフ
        and not all(c in "─━═│├┼┤╭╮╰╯└┘ " for c in l)  # 罫線除外
    ]
    dups = [(t, n) for t, n in Counter(lines).items() if n >= 3]
    assert not dups, f"resume バーストで本文が重複 emit: {dups[:5]}"


def test_norm_emit_ignores_ansi_styling() -> None:
    """_norm_emit は ANSI/末尾空白を無視し同一テキストを同一キーにする。"""
    plain = "  ただし副作用として、ログに残るのは初回描画版のみ".encode()
    highlighted = (b"\x1b[48;2;55;55;55m  \x1b[0m\x1b[38;2;255;255;255m"
                   + "ただし副作用として、ログに残るのは初回描画版のみ".encode()
                   + b"\x1b[0m   ")
    a = TuiEmulator._norm_emit(plain)
    b = TuiEmulator._norm_emit(highlighted)
    assert a == b, f"ANSI 差で別キーになった:\n  {a!r}\n  {b!r}"


def test_recent_emitted_large_enough_for_resume() -> None:
    """resume の大量再描画でも _recent_emitted が溢れない maxlen。"""
    em = TuiEmulator(rows=50, cols=164)
    assert em._recent_emitted.maxlen is not None
    assert em._recent_emitted.maxlen >= 2000
