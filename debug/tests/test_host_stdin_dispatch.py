"""host stdin ディスパッチ（_handle_host_stdin）の統合テスト。

実 `_loop` と同じ判定順（raw/flow → wheel → pagekey → NAV_KEY →
nav-mode scroll → 通常転送）を _handle_host_stdin で再現し、**実
HistoryScreen に行を流して描画結果（display-oracle）で**検証する。
内部カウンタではなく「画面に何が見えるか」を見ることで:
  - PAGEKEY/nav の遡りが累積する（1 ステップで頭打ちにならない）
  - 端末 passive レポート(focus 等)で遡りが消えない
  - 実ユーザー操作では live 復帰
  - 遡り中に claude が出力し続けても表示位置がドリフトしない
を機械検知する。
"""
import os
import sys
from pathlib import Path

import pyte
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "debug"))

import pty_proxy  # noqa: E402
from pty_proxy import PtyProxy, _HS, _HP, _NAV_KEY, _PGUP  # noqa: E402
from display_oracle import DisplayTerminal  # noqa: E402

_UP = b"\x1b[A"
_FOCUS_IN = b"\x1b[I"
_CURSOR_REPORT = b"\x1b[24;80R"
_MOUSE_CLICK = b"\x1b[<0;3;4M"
_R, _C = 24, 80


@pytest.fixture
def proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(pty_proxy, "SESSION_LOG", "")
    master_fd, slave_fd = os.openpty()
    p = PtyProxy(master_fd, child_pid=321, child_pgid=321,
                 sock_path=tmp_path / "s.sock")
    p._stdin_fd = -1                       # _render_host_now は no-op
    # 実 HistoryScreen に番号付き行を流す（history.top を作る）
    p._emulator.resize(_R, _C)
    st = pyte.ByteStream(p._emulator._screen)
    st.feed(("\r\n".join(f"L{i:04d}" for i in range(300))).encode())
    p._emu_stream = st                     # 後でさらに feed する用
    yield p
    for fd in (master_fd, slave_fd):
        try:
            os.close(fd)
        except OSError:
            pass


def _top(proxy) -> str:
    """現在の _host_scroll viewport の先頭の非空行（display-oracle）。"""
    d = DisplayTerminal(rows=_R, cols=_C, history=False)
    d.feed(proxy._host_scroll.render_viewport(proxy._emulator._screen, _R, _C))
    for ln in d.lines():
        if ln.strip():
            return ln.strip()
    return ""


def _step(proxy, key: bytes) -> str:
    """1 キー dispatch → 再描画（_loop 同様 scroll 後に render）→ 先頭行。"""
    proxy._handle_host_stdin(key)
    return _top(proxy)


def test_navmode_arrow_accumulates_with_pagekey_on(proxy, monkeypatch) -> None:
    """回帰本体: PAGEKEY_SCROLL=on で nav-mode に入り ↑ を連打すると、
    表示が **1 ステップで頭打ちにならず累積して上へ遡る**。"""
    monkeypatch.setattr(pty_proxy, "PAGEKEY_SCROLL", True)
    monkeypatch.setattr(pty_proxy, "WHEEL_SCROLL", False)
    _step(proxy, _NAV_KEY)                              # nav ON
    assert proxy._host_nav_mode is True
    _top(proxy)                                        # 初回 render で max_oy 確定
    seen = []
    for _ in range(8):
        seen.append(_step(proxy, _UP))
    # 先頭行が単調に過去（小さい番号）へ進み、最後は最初より十分上
    nums = [int(s[1:5]) for s in seen if s.startswith("L")]
    assert nums == sorted(nums, reverse=True), f"単調に遡っていない: {seen}"
    assert nums[0] - nums[-1] >= _HS * 5, (
        f"1 ステップで頭打ち（累積していない）: {nums}")


def test_navmode_arrow_accumulates_with_pagekey_off(proxy, monkeypatch) -> None:
    """対照群: PAGEKEY_SCROLL=off でも当然累積。"""
    monkeypatch.setattr(pty_proxy, "PAGEKEY_SCROLL", False)
    monkeypatch.setattr(pty_proxy, "WHEEL_SCROLL", False)
    _step(proxy, _NAV_KEY)
    _top(proxy)
    seen = [_step(proxy, _UP) for _ in range(6)]
    nums = [int(s[1:5]) for s in seen if s.startswith("L")]
    assert nums == sorted(nums, reverse=True) and nums[0] > nums[-1]


def test_navmode_exit_returns_to_live(proxy, monkeypatch) -> None:
    monkeypatch.setattr(pty_proxy, "PAGEKEY_SCROLL", True)
    _step(proxy, _NAV_KEY)
    _top(proxy)
    for _ in range(5):
        _step(proxy, _UP)
    assert proxy._host_scroll.follow_bottom_active is False
    _step(proxy, _NAV_KEY)                              # nav OFF
    assert proxy._host_nav_mode is False
    assert proxy._host_scroll.follow_bottom_active is True
    assert "L0299" in _top(proxy) or _top(proxy).startswith("L02")


def test_pagekey_not_reset_by_passive_terminal_reports(proxy, monkeypatch) -> None:
    """非 nav で PageUp 遡り中に focus/cursor-report/mouse が来ても
    表示位置が動かない（戻ると「非 nav scroll が完璧に壊れる」）。"""
    monkeypatch.setattr(pty_proxy, "PAGEKEY_SCROLL", True)
    monkeypatch.setattr(pty_proxy, "WHEEL_SCROLL", False)
    _top(proxy)
    _step(proxy, _PGUP)
    anchored = _step(proxy, _PGUP)
    assert anchored.startswith("L")
    assert proxy._host_scroll.follow_bottom_active is False
    for passive in (_FOCUS_IN, b"\x1b[O", _CURSOR_REPORT, _MOUSE_CLICK):
        assert _step(proxy, passive) == anchored, (
            f"{passive!r} で表示位置が動いた=破綻")
        assert proxy._host_scroll.follow_bottom_active is False


def test_pagekey_resets_on_real_user_input(proxy, monkeypatch) -> None:
    """実ユーザー操作（文字・Enter・矢印・Tab）では live 復帰。"""
    monkeypatch.setattr(pty_proxy, "PAGEKEY_SCROLL", True)
    for trigger in (b"a", b"\r", _UP, b"\t"):
        proxy._host_scroll.follow_bottom()
        _top(proxy)
        _step(proxy, _PGUP)
        assert proxy._host_scroll.follow_bottom_active is False
        _step(proxy, trigger)
        assert proxy._host_scroll.follow_bottom_active is True, (
            f"{trigger!r} で live 復帰しなかった")


def test_scrolled_view_stable_while_claude_outputs(proxy, monkeypatch) -> None:
    """**本命の修正**: 非 nav で遡って読んでいる間に claude が出力し
    続けても、表示先頭行が動かない（先頭アンカー）。旧来は最下部基準で
    canvas が伸びるたび下へドリフトし「複数箇所のバッファが混ざる」。"""
    monkeypatch.setattr(pty_proxy, "PAGEKEY_SCROLL", True)
    _top(proxy)
    for _ in range(4):
        _step(proxy, _PGUP)
    anchored = _top(proxy)
    assert anchored.startswith("L")
    # claude が出力し続ける（history.top が伸びる）。ユーザーは不動。
    for k in range(300, 360):
        proxy._emu_stream.feed(f"NEW{k}\r\n".encode())
        assert _top(proxy) == anchored, (
            f"claude 出力で表示がドリフトした: {anchored!r} -> {_top(proxy)!r}")
    # 下端まで戻れば live 追従に復帰し最新が viewport に見える
    proxy._host_scroll.follow_bottom()
    d = DisplayTerminal(rows=_R, cols=_C, history=False)
    d.feed(proxy._host_scroll.render_viewport(proxy._emulator._screen, _R, _C))
    assert d.contains("NEW359"), f"live 復帰で最新が見えない: {d.lines()[-3:]}"


def test_raw_mode_forwards_all_no_scroll(proxy, monkeypatch) -> None:
    monkeypatch.setattr(pty_proxy, "PAGEKEY_SCROLL", True)
    proxy._host_raw_mode = True
    _step(proxy, _NAV_KEY)
    _step(proxy, _UP)
    assert proxy._host_nav_mode is False
    assert proxy._host_scroll.follow_bottom_active is True
