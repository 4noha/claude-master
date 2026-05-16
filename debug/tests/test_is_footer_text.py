import pytest as _pytest; pytestmark = _pytest.mark.legacy  # raw passthrough 化で本番未使用（debug/replay 用）
"""_is_footer_text の全キーワード分岐を網羅。"""
import pytest

from pty_constants import _FOOTER_KEYWORDS_TEXT, _is_footer_text


@pytest.mark.parametrize("keyword", _FOOTER_KEYWORDS_TEXT)
def test_each_footer_keyword_matches(keyword: str) -> None:
    """`_FOOTER_KEYWORDS_TEXT` の全エントリは _is_footer_text で True を返す。"""
    assert _is_footer_text(f"prefix {keyword} suffix") is True, keyword


def test_input_prompt_is_footer() -> None:
    assert _is_footer_text("❯ hello world") is True
    assert _is_footer_text("❯") is True


def test_horizontal_box_is_footer() -> None:
    assert _is_footer_text("─" * 40) is True
    assert _is_footer_text("═" * 20) is True


def test_spinner_dingbat_is_footer() -> None:
    # ✻ / ✳ / ✶ などは spinner 先頭文字
    assert _is_footer_text("✻ Worked for 10m 22s") is True
    assert _is_footer_text("✳ Cogitating…") is True


def test_transient_tail_is_footer() -> None:
    # "…" で終わる進行中 status
    assert _is_footer_text("Reading 1 file…") is True
    assert _is_footer_text("Reading 1 file… (ctrl+o to expand)") is True


def test_agent_active_prefix() -> None:
    # ⏺ + Running/Working などはアクティブ状態
    assert _is_footer_text("⏺ agent-name(task) Running…") is True
    assert _is_footer_text("⏺ Bash(ls)") is False  # 完了状態（… なし、active 語なし）はログへ


def test_plain_log_is_not_footer() -> None:
    assert _is_footer_text("Hello, world.") is False
    assert _is_footer_text("Read 239 lines from README.md") is False
    assert _is_footer_text("") is False
    assert _is_footer_text("   ") is False


def test_session_limit_dialog_keywords() -> None:
    # ユーザーが過去に "93% of your session limit" がログに流れたバグを報告したケース
    assert _is_footer_text("You've used 93% of your session limit") is True
    assert _is_footer_text("Press Ctrl-C again to exit") is True


def test_diff_code_lines_with_keywords_are_not_footer() -> None:
    """diff/コード行は footer キーワード文字列を含んでも footer ではない。

    pty_constants.py 自身の diff（footer キーワードがソースに並ぶ）が
    footer 誤判定でログ消失していた回帰の防止。
    """
    diff_lines = [
        '276 -    "You\'ve hit your limit", "What do you want to do",',
        '277 +    "Enter to confirm", "Esc to cancel",',
        '278 -    "Stop and wait for limit", "Upgrade your plan",',
        "  48          parts: list[bytes] = []",
        " 263 -_ACTIVE_FOOTER_KEYWORDS = (",
        "  29 +            # accept edits の行",
    ]
    for line in diff_lines:
        assert _is_footer_text(line) is False, f"diff 行が footer 判定: {line!r}"


def test_real_footer_still_detected_after_diff_guard() -> None:
    """diff ガード追加後も通常の footer 行は引き続き footer と判定される。"""
    assert _is_footer_text("⏵⏵ bypass permissions on (shift+tab to cycle)") is True
    assert _is_footer_text("You've used 93% of your session limit") is True
    assert _is_footer_text("Press Ctrl-C again to exit") is True
    # 行番号風だが diff ではない（footer キーワードが主体）はそのまま footer
    assert _is_footer_text("Press up to edit queued messages") is True


def test_history_search_hint_is_footer() -> None:
    """↑↓ 履歴検索時の "ctrl+r to search history" がフッター扱いになる(debug 環境で発見)。"""
    assert _is_footer_text("ctrl+r to search history") is True
    assert _is_footer_text("                  ctrl+r to search history") is True


def test_tip_hint_is_footer() -> None:
    """"Tip: ctrl+s to show snippets" のような操作ヒント Tip がフッター扱いになる。"""
    assert _is_footer_text("Tip: ctrl+s to show snippets") is True
    assert _is_footer_text("Tip: ctrl+r to search") is True
    # "Tip: " で始まるが ctrl+ がない場合は従来通り通常テキスト
    assert _is_footer_text("Tip: 1. fix the bug first") is False
