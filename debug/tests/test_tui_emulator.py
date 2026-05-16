import pytest as _pytest; pytestmark = _pytest.mark.legacy  # raw passthrough 化で本番未使用（debug/replay 用）
"""TuiEmulator の主要な不変条件を pinning する単体テスト。"""
from pty_emulator import TuiEmulator


def _decode(lines: list[bytes]) -> list[str]:
    """ANSI を剥がして可読化。"""
    import re
    ansi = re.compile(rb"\x1b\[[0-9;]*m")
    return [ansi.sub(b"", l).decode("utf-8", errors="replace") for l in lines]


def test_feed_empty_returns_empty() -> None:
    em = TuiEmulator(rows=24, cols=80)
    logs, footer, cursor = em.feed(b"")
    assert logs == []
    assert footer == []
    assert cursor is None


def test_lazy_emission_holds_one_cycle() -> None:
    """新規可視行は 1 サイクル _pending に保留され、安定したら emit される。
    カーソルは行末で次行へ移っていないと、その行は y==cursor_y で skip される。
    """
    em = TuiEmulator(rows=10, cols=40)
    # \r\n でカーソルを次行に進める（cursor_y=1）→ row 0 は cursor 行ではなく pending に入る
    logs1, _, _ = em.feed(b"hello world\r\n")
    # 1 サイクル目: 新規行は pending、まだ emit されない
    assert not any("hello world" in s for s in _decode(logs1))
    # 2 サイクル目: 同じ画面状態なら "安定" と判断され emit
    logs2, _, _ = em.feed(b"")
    assert any("hello world" in s for s in _decode(logs2))


def test_wide_char_no_space_between() -> None:
    """全角文字の継続セルは空白に変換せず、隣接して emit される。"""
    em = TuiEmulator(rows=10, cols=40)
    em.feed("ログを追記\r\n".encode("utf-8"))  # cursor を次行へ
    logs, _, _ = em.feed(b"")  # cycle 2 で安定 emit
    text = "".join(_decode(logs))
    assert "ログを追記" in text
    # 全角間に空白が割り込まないこと
    assert "ロ グ" not in text


def test_footer_text_not_emitted_as_log() -> None:
    """footer chrome（❯ 入力プロンプト等）はログとして emit されない。"""
    em = TuiEmulator(rows=10, cols=40)
    em.feed(b"\x1b[10;1H")  # 最下行へ
    em.feed(b"\xe2\x9d\xaf placeholder")  # ❯ placeholder
    em.feed(b"")
    logs, footer, _ = em.feed(b"")
    log_text = "".join(_decode(logs))
    footer_text = "".join(_decode(footer))
    assert "❯" not in log_text
    assert "❯" in footer_text


def test_resize_preserves_history_emit() -> None:
    """resize 後も既 emit 内容は重複しない（_recent_emitted で重複除去）。"""
    em = TuiEmulator(rows=10, cols=80)
    # スクロールアウトを誘発する量のテキスト
    for i in range(20):
        em.feed(f"line {i}\r\n".encode())
        em.feed(b"")  # lazy emit を進める
    em.resize(10, 100)
    logs, _, _ = em.feed(b"")
    # resize 直後に emit される行は限定的（重複 emit していない）
    decoded = _decode(logs)
    # 全 20 行を二重に出力していないことだけ確認
    line0_count = sum(1 for s in decoded if "line 0" in s)
    assert line0_count <= 1


def test_extract_usage_picks_up_percent() -> None:
    em = TuiEmulator(rows=10, cols=120)
    em.feed(b"You've used 93% of your session limit \xc2\xb7 resets 4pm (Asia/Tokyo)\r\n")
    em.feed(b"")
    usage = em.extract_usage()
    assert usage is not None
    assert usage["usage_percent"] == 93
    assert usage["reset_time"] == "4pm"
    assert usage["reset_tz"] == "Asia/Tokyo"


def test_resize_does_not_re_emit_visible_tool_rows() -> None:
    """tmux resize 後、画面に映っていた ⏺/⎿ ツール行が再 emit されない。

    過去にあった `_is_tool_row = ... and self._emitted_visible` の緩和ルールが
    リサイズで _emitted_visible が空になった瞬間に y>0 行で誤発火し、
    visible 領域のツール行群が全部ログに再追加される回帰の防止テスト。
    """
    em = TuiEmulator(rows=24, cols=120)
    # Tool 行 3 つを feed して emit を進める
    feed_data = (
        b"\xe2\x8f\xba Bash(git status)\r\n"
        b"\xe2\x8f\xba Bash(git log)\r\n"
        b"\xe2\x8f\xba Read(README.md)\r\n"
    )
    em.feed(feed_data)
    em.feed(b"")  # lazy emit を進めて _emitted_visible / _recent_emitted に入れる

    # resize: 同じ可視内容のはず（pyte は内容を保持して resize）
    em.resize(24, 80)

    # resize 直後の空 feed で何も再 emit されないこと（特に ⏺ 行）
    logs, _, _ = em.feed(b"")
    decoded = _decode(logs)
    rerun_tool_rows = [s for s in decoded if "Bash(git" in s or "Read(README" in s]
    assert rerun_tool_rows == [], (
        f"resize 後にツール行が再 emit された: {rerun_tool_rows}"
    )


def test_extract_usage_monotonic() -> None:
    """新しい値が古い値より小さければ上書きしない（後の画面で消えるバグ対策）。"""
    em = TuiEmulator(rows=10, cols=120)
    em.feed(b"You've used 93% of your session limit\r\n")
    em.feed(b"You've used 12% of your session limit\r\n")
    em.feed(b"")
    usage = em.extract_usage()
    assert usage["usage_percent"] == 93
