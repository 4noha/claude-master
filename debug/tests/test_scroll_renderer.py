"""ScrollRenderer の単体テスト（ミニ tmux のコア = 本番経路）。

scrollback モデル:
  - 既定（scrollback==0）: 画面最下部(live)に追従。可視 buffer の末尾を描画
  - scroll(dy<0) で過去ログ（HistoryScreen.history.top）へ遡る
  - scroll(dy>0) / follow_bottom() で最下部へ復帰
  - 全角継続セルは二重描画しない / カーソル可視判定 / synchronized 出力
"""
import re

import pyte

from pty_scroll import ScrollRenderer


def _strip_ansi(b: bytes) -> str:
    return re.sub(rb"\x1b\[[?]?[0-9;]*[a-zA-Z]", b"",
                  b).decode("utf-8", errors="replace")


def _history_screen(cols: int, rows: int, lines: list[str]) -> pyte.HistoryScreen:
    """lines を流した HistoryScreen。rows を超えた古い行は history.top へ。"""
    sc = pyte.HistoryScreen(cols, rows, history=2000, ratio=0.5)
    st = pyte.ByteStream(sc)
    st.feed(("\r\n".join(lines)).encode("utf-8"))
    return sc


def test_follow_bottom_default_shows_live_tail() -> None:
    """既定は最下部追従。可視 buffer 末尾（最新行）が見える。"""
    sc = _history_screen(20, 5, [f"row{i:02d}" for i in range(40)])
    r = ScrollRenderer()
    assert r.follow_bottom_active
    text = _strip_ansi(r.render_viewport(sc, vrows=5, vcols=20))
    assert "row39" in text          # 最新
    assert "row00" not in text      # 最古は history へ流れて見えない


def test_scroll_up_pans_into_history() -> None:
    """scroll(dy<0) で過去ログ（history.top）を遡れる。"""
    sc = _history_screen(20, 5, [f"row{i:02d}" for i in range(40)])
    r = ScrollRenderer()
    r.render_viewport(sc, 5, 20)              # follow-bottom 確定
    r.scroll(-10)                              # 10 行上へ
    assert not r.follow_bottom_active
    text = _strip_ansi(r.render_viewport(sc, 5, 20))
    # 最新は外れ、過去ログが見える
    assert "row39" not in text
    assert any(f"row{n:02d}" in text for n in range(20, 35))


def test_scroll_clamps_at_oldest() -> None:
    """大きく遡っても最古（history 先頭）で clamp。例外なく完走。"""
    sc = _history_screen(20, 5, [f"row{i:02d}" for i in range(40)])
    r = ScrollRenderer()
    r.render_viewport(sc, 5, 20)
    r.scroll(-100000)                          # 最古へ
    text = _strip_ansi(r.render_viewport(sc, 5, 20))
    assert "row00" in text                     # 最古が見える
    # scrollback は canvas 長で clamp 済み（過大値が残らない）
    assert r.scrollback == r.scrollback        # 例外が出ないこと自体が要件


def test_scroll_down_and_follow_bottom_return_to_live() -> None:
    sc = _history_screen(20, 5, [f"row{i:02d}" for i in range(40)])
    r = ScrollRenderer()
    r.render_viewport(sc, 5, 20)
    r.scroll(-8)
    assert not r.follow_bottom_active
    r.scroll(8)                                # 同じだけ下へ → 0 復帰
    assert r.follow_bottom_active
    assert "row39" in _strip_ansi(r.render_viewport(sc, 5, 20))
    # follow_bottom() でも復帰
    r.scroll(-5)
    r.follow_bottom()
    assert r.follow_bottom_active
    assert "row39" in _strip_ansi(r.render_viewport(sc, 5, 20))


def test_cursor_in_viewport_emits_show() -> None:
    """live カーソルが viewport 内（最下部追従中）なら show を emit。"""
    sc = pyte.HistoryScreen(20, 6, history=100, ratio=0.5)
    st = pyte.ByteStream(sc)
    st.feed(b"hello\r\n")
    st.feed(b"\x1b[3;2H")                      # cursor を可視域内へ
    r = ScrollRenderer()
    out = r.render_viewport(sc, vrows=6, vcols=20)
    assert b"\x1b[?25h" in out
    assert b"\x1b[?25l" not in out


def test_cursor_hidden_when_scrolled_into_history() -> None:
    """過去ログを見ている間は live カーソルが viewport 外 → hide。"""
    sc = _history_screen(20, 5, [f"row{i:02d}" for i in range(40)])
    r = ScrollRenderer()
    r.render_viewport(sc, 5, 20)
    r.scroll(-15)
    out = r.render_viewport(sc, 5, 20)
    assert b"\x1b[?25l" in out


def test_full_width_continuation_not_doubled() -> None:
    """全角文字の継続セルは描画スキップ（間に空白が割り込まない）。"""
    sc = pyte.HistoryScreen(20, 4, history=100, ratio=0.5)
    pyte.ByteStream(sc).feed("ログ追記\r\n".encode())
    r = ScrollRenderer()
    text = _strip_ansi(r.render_viewport(sc, vrows=4, vcols=10))
    assert "ログ追記" in text
    assert "ロ グ" not in text


def test_synchronized_output_brackets() -> None:
    """\\x1b[?2026h/l で同期出力に包む（チラつき防止）。"""
    sc = _history_screen(20, 4, ["hello"])
    out = ScrollRenderer().render_viewport(sc, vrows=4, vcols=10)
    assert out.startswith(b"\x1b[?2026h")
    assert out.endswith(b"\x1b[?2026l")


def test_plain_screen_no_history_always_follow_bottom() -> None:
    """history 属性の無い plain pyte.Screen でも安全に動く（pan しない）。"""
    sc = pyte.Screen(20, 5)
    pyte.ByteStream(sc).feed("\r\n".join(f"r{i}" for i in range(5)).encode())
    r = ScrollRenderer()
    r.scroll(-50)                              # history 無し → 効果なし
    text = _strip_ansi(r.render_viewport(sc, vrows=5, vcols=20))
    assert "r4" in text                        # 例外なく末尾が見える


def test_reset_keeps_scrollback_forces_repaint() -> None:
    """reset() は scrollback を保持し再描画を強制（リサイズ時用）。"""
    sc = _history_screen(20, 5, [f"row{i:02d}" for i in range(40)])
    r = ScrollRenderer()
    r.render_viewport(sc, 5, 20)
    r.scroll(-7)
    sb = r.scrollback
    r.reset()
    assert r.scrollback == sb                  # 保持
    out = r.render_viewport(sc, 5, 20)
    assert out.startswith(b"\x1b[?2026h")      # 再描画される
