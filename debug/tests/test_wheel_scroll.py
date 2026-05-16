"""WHEEL_SCROLL（nav-mode に入らずマウスホイールで履歴を遡る）の
本番経路テスト。classify_wheel と host 側 `_host_wheel` を検証する。
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import pyte  # noqa: E402

import pty_proxy  # noqa: E402
from pty_proxy import PtyProxy  # noqa: E402
from pty_scroll import classify_wheel  # noqa: E402

sys.path.insert(0, str(ROOT / "debug"))
from display_oracle import DisplayTerminal  # noqa: E402

_R, _C = 24, 80


def _top(proxy) -> str:
    d = DisplayTerminal(rows=_R, cols=_C, history=False)
    d.feed(proxy._host_scroll.render_viewport(proxy._emulator._screen, _R, _C))
    for ln in d.lines():
        if ln.strip():
            return ln.strip()
    return ""

_WU = b"\x1b[<64;10;5M"   # SGR ホイール上
_WD = b"\x1b[<65;10;5M"   # SGR ホイール下
_WU_LEGACY = b"\x1b[M`! !"   # レガシー ホイール上


def test_classify_wheel_sgr_legacy_and_passthrough() -> None:
    assert classify_wheel(_WU) == -1            # 上 = 過去へ
    assert classify_wheel(_WD) == 1             # 下 = 新しい方へ
    assert classify_wheel(b"\x1b[<80;1;1M") == -1   # ctrl 修飾付きでも上
    assert classify_wheel(_WU_LEGACY) == -1
    assert classify_wheel(b"\x1b[Ma! !") == 1
    assert classify_wheel(b"\x1b[<0;3;4M") is None   # 左クリック=透過
    assert classify_wheel(b"\x1b[<35;3;4M") is None  # motion=透過
    assert classify_wheel(b"abc") is None
    assert classify_wheel(b"\x1b[A") is None
    assert classify_wheel(b"") is None


@pytest.fixture
def proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(pty_proxy, "SESSION_LOG", "")
    master_fd, slave_fd = os.openpty()
    p = PtyProxy(master_fd, child_pid=66, child_pgid=66,
                 sock_path=tmp_path / "s.sock")
    p._stdin_fd = -1
    p._emulator.resize(_R, _C)
    pyte.ByteStream(p._emulator._screen).feed(
        ("\r\n".join(f"L{i:04d}" for i in range(300))).encode())
    yield p
    for fd in (master_fd, slave_fd):
        try:
            os.close(fd)
        except OSError:
            pass


def test_disabled_returns_false(proxy, monkeypatch) -> None:
    monkeypatch.setattr(pty_proxy, "WHEEL_SCROLL", False)
    assert proxy._host_wheel(_WU) is False
    assert proxy._host_scroll.follow_bottom_active is True


def test_wheel_scrolls_without_navmode(proxy, monkeypatch) -> None:
    monkeypatch.setattr(pty_proxy, "WHEEL_SCROLL", True)
    monkeypatch.setattr(pty_proxy, "NAV_WHEEL_STEP", 3)
    assert proxy._host_nav_mode is False
    _top(proxy)                                    # 初回 render（max_oy 確定）
    assert proxy._host_wheel(_WU) is True          # 消費
    _top(proxy)
    n1 = int(_top(proxy)[1:5])
    assert proxy._host_scroll.follow_bottom_active is False   # 過去へ
    assert proxy._host_wheel(_WU_LEGACY) is True    # レガシーも
    _top(proxy)
    n2 = int(_top(proxy)[1:5])
    assert n2 < n1                                  # さらに上へ累積
    assert proxy._host_wheel(_WD) is True           # 下で戻る
    _top(proxy)
    assert int(_top(proxy)[1:5]) > n2
    assert proxy._host_nav_mode is False


def test_non_wheel_mouse_passthrough(proxy, monkeypatch) -> None:
    monkeypatch.setattr(pty_proxy, "WHEEL_SCROLL", True)
    assert proxy._host_wheel(b"\x1b[<0;3;4M") is False  # クリック=透過
    assert proxy._host_wheel(b"a") is False             # 文字=透過
    assert proxy._host_scroll.follow_bottom_active is True


def test_raw_and_flow_disable_wheel(proxy, monkeypatch) -> None:
    monkeypatch.setattr(pty_proxy, "WHEEL_SCROLL", True)
    proxy._host_raw_mode = True
    assert proxy._host_wheel(_WU) is False
    proxy._host_raw_mode = False
    proxy._host_flow_mode = True
    assert proxy._host_wheel(_WU) is False
    assert proxy._host_scroll.follow_bottom_active is True
