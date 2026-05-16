"""PAGEKEY_SCROLL（nav-mode に入らず PageUp/PageDown でスクロール）の
本番経路テスト。`_host_pagekey` の戻り値（consume/透過）と、実
HistoryScreen を render した表示結果（display-oracle）で検証する。
内部カウンタではなく「画面に何が見えるか」と follow 状態を見る。
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
from pty_proxy import PtyProxy, _PGUP, _PGDN  # noqa: E402
from display_oracle import DisplayTerminal  # noqa: E402

_R, _C = 24, 80


@pytest.fixture
def proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(pty_proxy, "SESSION_LOG", "")
    master_fd, slave_fd = os.openpty()
    p = PtyProxy(master_fd, child_pid=55, child_pgid=55,
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


def _top(proxy) -> str:
    d = DisplayTerminal(rows=_R, cols=_C, history=False)
    d.feed(proxy._host_scroll.render_viewport(proxy._emulator._screen, _R, _C))
    for ln in d.lines():
        if ln.strip():
            return ln.strip()
    return ""


def _num(proxy) -> int:
    t = _top(proxy)
    return int(t[1:5]) if t.startswith("L") else -1


def test_disabled_returns_false_no_scroll(proxy, monkeypatch) -> None:
    monkeypatch.setattr(pty_proxy, "PAGEKEY_SCROLL", False)
    assert proxy._host_pagekey(_PGUP) is False
    assert proxy._host_scroll.follow_bottom_active is True


def test_pageup_pagedown_scroll_without_navmode(proxy, monkeypatch) -> None:
    monkeypatch.setattr(pty_proxy, "PAGEKEY_SCROLL", True)
    assert proxy._host_nav_mode is False
    _top(proxy)                                    # 初回 render（max_oy 確定）
    assert proxy._host_pagekey(_PGUP) is True       # 消費
    _top(proxy)
    n1 = _num(proxy)
    assert proxy._host_scroll.follow_bottom_active is False
    assert proxy._host_pagekey(_PGUP) is True
    _top(proxy)
    n2 = _num(proxy)
    assert n2 < n1                                  # さらに過去へ累積
    assert proxy._host_pagekey(_PGDN) is True
    _top(proxy)
    assert _num(proxy) > n2                          # PageDown で新しい方へ
    assert proxy._host_nav_mode is False


def test_other_key_resets_to_live_passive_does_not(proxy, monkeypatch) -> None:
    monkeypatch.setattr(pty_proxy, "PAGEKEY_SCROLL", True)
    _top(proxy)
    proxy._host_pagekey(_PGUP)
    _top(proxy)
    proxy._host_pagekey(_PGUP)
    _top(proxy)
    assert proxy._host_scroll.follow_bottom_active is False
    # 端末 passive レポート（focus）は live に戻さない（False で透過）
    assert proxy._host_pagekey(b"\x1b[I") is False
    assert proxy._host_scroll.follow_bottom_active is False
    # 実ユーザー操作（↑ / 文字）は live 復帰
    assert proxy._host_pagekey(b"\x1b[A") is False  # 透過しつつ
    assert proxy._host_scroll.follow_bottom_active is True
    proxy._host_pagekey(_PGUP)
    assert proxy._host_scroll.follow_bottom_active is False
    assert proxy._host_pagekey(b"a") is False
    assert proxy._host_scroll.follow_bottom_active is True


def test_raw_mode_disables_pagekey(proxy, monkeypatch) -> None:
    monkeypatch.setattr(pty_proxy, "PAGEKEY_SCROLL", True)
    proxy._host_raw_mode = True
    assert proxy._host_pagekey(_PGUP) is False
    assert proxy._host_scroll.follow_bottom_active is True


def test_flow_mode_disables_pagekey(proxy, monkeypatch) -> None:
    monkeypatch.setattr(pty_proxy, "PAGEKEY_SCROLL", True)
    proxy._host_flow_mode = True
    assert proxy._host_pagekey(_PGUP) is False
    assert proxy._host_scroll.follow_bottom_active is True
