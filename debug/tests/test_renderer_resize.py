import pytest as _pytest; pytestmark = _pytest.mark.legacy  # raw passthrough 化で本番未使用（debug/replay 用）
"""reset() の挙動を pin する回帰テスト。

tmux 側を激しく resize したときフッター位置が画面先頭に飛び、以降の出力が
下方向に流れてスクロールバックを汚染する現象の回帰防止。
reset() はカーソル追跡状態だけリセットし、viewport 全クリアはしない。
"""
from pty_renderer import TerminalRenderer


def test_reset_does_not_force_viewport_clear() -> None:
    """reset() 後の render は \\x1b[H\\x1b[2J を emit しない
    （フッターが画面先頭に飛んで以降が下に流れるとログ汚染になるため）。"""
    r = TerminalRenderer()
    r.render(log_lines=[b"hello"], footer_lines=[b"footer"], cursor_pos=(0, 6), cols=80)
    r.reset()
    out = r.render([b"new"], [b"new footer"], cursor_pos=(0, 10), cols=100)
    assert b"\x1b[H\x1b[2J" not in out
    assert b"\x1b[?2026h" not in out


def test_reset_clears_state_but_keeps_footer_height() -> None:
    """reset() で _last_footer / _last_cursor / _max_footer_height はクリア、
    _footer_height は保持（go_up 計算で旧フッター位置を正しく消すため）。"""
    r = TerminalRenderer()
    r.render([b"a"], [b"f"], cursor_pos=(0, 1), cols=80)
    assert r._last_footer != []
    fh = r._footer_height
    r.reset()
    assert r._last_footer == []
    assert r._last_cursor is None
    assert r._max_footer_height == 0
    assert r._footer_height == fh  # 保持


def test_render_after_reset_uses_goup_to_clear_old_footer() -> None:
    """reset 後の最初の render は go_up + \\x1b[J で旧フッター位置を消す。
    その上の折返し残骸は scroll に任せる（強制クリアしない）。"""
    r = TerminalRenderer()
    r.render([b"a"], [b"f1", b"f2"], cursor_pos=(1, 1), cols=80)  # footer 2 行
    r.reset()
    out = r.render([b"b"], [b"new1", b"new2"], cursor_pos=(1, 1), cols=80)
    # \r + cursor up + \x1b[J のシーケンスが含まれる
    assert b"\r" in out
    assert b"\x1b[J" in out
    # 旧フッターの行数だけ上に戻る
    assert b"\x1b[1A" in out or b"\x1b[2A" in out
