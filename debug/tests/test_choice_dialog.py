import pytest as _pytest; pytestmark = _pytest.mark.legacy  # raw passthrough 化で本番未使用（debug/replay 用）
"""選択肢ダイアログ（"Do you want to proceed?" + 1. Yes / ❯ 2. ... / 3. No）が
LOG に流れず footer ブロックとして扱われることを検証。

報告: 「Claude Code で選択肢が出たときの処理が非常に不完全」
原因: "1. Yes" / "3. No" は footer キーワードを含まないため LOG 扱いになり、
      ダイアログがフッターから分断されて流れていた。
修正: _find_footer_start が question マーカー / option 行群を検出して
      ダイアログブロックごと footer に含める。
"""
import re
from pathlib import Path

from pty_emulator import TuiEmulator

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "choice-dialog"
SNAPSHOT = Path(__file__).resolve().parent.parent / "snapshots" / "choice-dialog.expected.txt"

_OPT_RE = re.compile(r"^\s*(?:❯\s*)?\d+\.\s+(Yes|No|Yes,|Default|Sonnet|Haiku)")


def _sections(snapshot: str):
    for ch in snapshot.split("=== cycle ")[1:]:
        body = ch[ch.find("\n") + 1:]
        m_log = re.search(r"^LOG:\n(.*?)(?=^FOOTER:|\Z)", body, re.DOTALL | re.M)
        m_foot = re.search(r"^FOOTER:.*?\n(.*?)(?=^CURSOR:|\Z)", body, re.DOTALL | re.M)
        log = m_log.group(1).splitlines() if m_log else []
        foot = m_foot.group(1).splitlines() if m_foot else []
        yield log, foot


def test_dialog_options_not_dominant_in_log() -> None:
    """選択肢/質問行は LOG より FOOTER に多く出る（ダイアログが footer 扱い）。"""
    snap = SNAPSHOT.read_text()
    log_hits = foot_hits = 0
    for log, foot in _sections(snap):
        for l in log:
            if _OPT_RE.match(l.strip()) or "Do you want to proceed" in l:
                log_hits += 1
        for l in foot:
            if _OPT_RE.match(l.strip()) or "Do you want to proceed" in l:
                foot_hits += 1
    assert foot_hits > log_hits, (
        f"選択肢/質問が FOOTER({foot_hits}) より LOG({log_hits}) に多い "
        f"= ダイアログがログに流れている"
    )


def test_find_footer_start_includes_dialog_block() -> None:
    """question マーカーと option 群があるとき footer_start が question 行まで上がる。"""
    em = TuiEmulator(rows=20, cols=80)
    visible = [
        "some log line above",
        "",
        "  Do you want to proceed?",
        "",
        "  1. Yes",
        "",
        "  2. Yes, and don't ask again for: git commit",
        "",
        "❯ 3. No",
        "─────────────────────",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
        "──────────────────────",
    ] + [""] * 8
    fs = em._find_footer_start(visible, cursor_y=10)
    assert visible[fs].strip().startswith("Do you want to proceed"), (
        f"footer_start={fs} → {visible[fs]!r}（期待: 'Do you want to proceed?' 行）"
    )


def test_dialog_block_survives_stray_separator() -> None:
    """ダイアログと footer の間に '─────  *' のような半端な区切りがあっても
    look-ahead でダイアログを footer に取り込む。"""
    em = TuiEmulator(rows=20, cols=80)
    visible = [
        "log above",
        "",
        "  Do you want to proceed?",
        "  1. Yes",
        "  2. Yes, and don't ask again for: x",
        "  3. No",
        "──────── ────────────",
        "─────       *",            # 半端な区切り
        "  ⏵⏵ bypass permissions on",
        "──────────────────────",
    ] + [""] * 10
    fs = em._find_footer_start(visible, cursor_y=8)
    assert visible[fs].strip().startswith("Do you want to proceed"), (
        f"footer_start={fs} → {visible[fs]!r}"
    )


def test_compact_resume_dialog_info_text_in_footer() -> None:
    """/compact resume ダイアログの説明文（区切り線〜options 間）も footer。

    "This session is ... old" 等が options の上にあるが LOG に流れていた回帰防止。
    区切り線がダイアログ上端になり、その下の説明文 + options が footer。
    """
    em = TuiEmulator(rows=30, cols=100)
    visible = [
        "log content above the dialog",
        "  ⎿  OK",
        "",
        "──────────────────────────────",   # 3: 区切り線 = ダイアログ上端
        "  This session is 2h 52m old and 138.3k tokens.",
        "",
        "  Resuming the full session will consume a substantial portion",
        "",
        "    1. Resume from summary (recommended)",
        "    2. Resume full session as-is",
        "  ❯ 3. Don't ask me again",
        "",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
        "──────────────────────────────",
    ] + [""] * 16
    fs = em._find_footer_start(visible, cursor_y=12)
    assert visible[fs].strip().startswith("──"), (
        f"footer_start={fs} → {visible[fs]!r}（期待: 区切り線 index 3）"
    )
    # 説明文が footer 領域に入っている
    assert fs <= 4, f"説明文 'This session is' が LOG 域に残った: footer_start={fs}"


def test_plain_numbered_list_not_treated_as_dialog() -> None:
    """質問マーカーが無い単発の番号リストはダイアログ扱いしない（誤検出防止）。"""
    em = TuiEmulator(rows=20, cols=80)
    visible = [
        "Here are the steps:",
        "1. First do this",
        "regular paragraph text continues here and is long enough",
        "more narrative text without any dialog markers at all",
        "",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
        "──────────────────────",
    ] + [""] * 13
    fs = em._find_footer_start(visible, cursor_y=5)
    # footer は "⏵⏵ bypass" 行から。番号リストは log のまま。
    assert fs >= 5, f"単発番号リストを誤ってダイアログ扱い: footer_start={fs}"
