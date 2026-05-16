"""PtyProxy のサイズ状態追跡と _apply_pty_size の統合テスト。

resolve_pty_size は pure 関数として test_size_policy.py で検証済み。
本テストは PtyProxy インスタンス上で:

  - _record_host_size / _record_client_size / _forget_client_size の状態遷移
  - _apply_pty_size が termios で実 PTY サイズを正しくセットする

を実際の master_fd（os.openpty）に対して検証する。
"""
import fcntl
import os
import struct
import termios
import unittest.mock as mock
from pathlib import Path

import pytest

from pty_proxy import PtyProxy


def _winsz(fd: int) -> tuple[int, int]:
    buf = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
    rows, cols, _, _ = struct.unpack("HHHH", buf)
    return rows, cols


@pytest.fixture
def proxy(tmp_path):
    """master/slave PTY を用意し PtyProxy を構築。teardown で close。"""
    master_fd, slave_fd = os.openpty()
    sock_path = tmp_path / "test.sock"
    try:
        # IOLogger を無効化（PTY_PROXY_LOG=1 でなければ初期化されない）
        p = PtyProxy(master_fd, child_pid=12345, child_pgid=12345, sock_path=sock_path)
        yield p
    finally:
        for fd in (master_fd, slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def test_record_host_size_updates_state(proxy: PtyProxy) -> None:
    proxy._record_host_size(40, 100)
    assert proxy._host_size == (40, 100)
    assert proxy._latest_size == (40, 100)


def test_record_client_size_updates_state(proxy: PtyProxy) -> None:
    proxy._record_client_size(fd=7, rows=30, cols=120)
    assert proxy._client_sizes == {7: (30, 120)}
    assert proxy._last_client_size == (30, 120)
    assert proxy._latest_size == (30, 120)


def test_forget_client_size_falls_back(proxy: PtyProxy) -> None:
    proxy._record_client_size(fd=7, rows=30, cols=120)
    proxy._record_client_size(fd=8, rows=40, cols=80)
    proxy._forget_client_size(7)
    assert 7 not in proxy._client_sizes
    # 残った client が last_client_size になる
    assert proxy._last_client_size == (40, 80)
    proxy._forget_client_size(8)
    assert proxy._client_sizes == {}
    assert proxy._last_client_size is None


def test_apply_pty_size_host_policy(proxy: PtyProxy) -> None:
    proxy._size_policy = "host"
    proxy._record_host_size(45, 110)
    proxy._record_client_size(7, 30, 80)  # 無視されるはず
    rows, cols = proxy._apply_pty_size()
    assert (rows, cols) == (45, 110)
    # 実 PTY サイズも同じ
    assert _winsz(proxy.master_fd) == (45, 110)


def test_apply_pty_size_client_policy(proxy: PtyProxy) -> None:
    proxy._size_policy = "client"
    proxy._record_host_size(45, 110)
    proxy._record_client_size(7, 30, 80)
    rows, cols = proxy._apply_pty_size()
    assert (rows, cols) == (30, 80)
    assert _winsz(proxy.master_fd) == (30, 80)


def test_apply_pty_size_client_falls_back_to_host(proxy: PtyProxy) -> None:
    """client policy だが client がいないとき host にフォールバック。"""
    proxy._size_policy = "client"
    proxy._record_host_size(45, 110)
    rows, cols = proxy._apply_pty_size()
    assert (rows, cols) == (45, 110)


def test_apply_pty_size_smallest_policy(proxy: PtyProxy) -> None:
    proxy._size_policy = "smallest"
    proxy._record_host_size(45, 110)
    proxy._record_client_size(7, 30, 80)
    proxy._record_client_size(8, 50, 60)
    rows, cols = proxy._apply_pty_size()
    # min(45, 30, 50) rows = 30 ; min(110, 80, 60) cols = 60
    assert (rows, cols) == (30, 60)
    assert _winsz(proxy.master_fd) == (30, 60)


def test_apply_pty_size_latest_policy(proxy: PtyProxy) -> None:
    proxy._size_policy = "latest"
    proxy._record_host_size(45, 110)
    rows, cols = proxy._apply_pty_size()
    assert (rows, cols) == (45, 110)
    proxy._record_client_size(7, 30, 80)
    rows, cols = proxy._apply_pty_size()
    assert (rows, cols) == (30, 80)
    # 再度 host を update すると host が latest になる
    proxy._record_host_size(55, 130)
    rows, cols = proxy._apply_pty_size()
    assert (rows, cols) == (55, 130)


def test_emulator_resize_tracks_pty_size(proxy: PtyProxy) -> None:
    """_apply_pty_size 後に TuiEmulator のサイズも変わる。"""
    assert proxy._emulator is not None
    proxy._size_policy = "client"
    proxy._record_client_size(7, 30, 80)
    proxy._apply_pty_size()
    assert proxy._emulator._rows == 30
    assert proxy._emulator._cols == 80


def test_hybrid_scroll_attrs(proxy: PtyProxy) -> None:
    """ハイブリッド構成の scroll 状態:

      - client は per-fd ScrollRenderer（`_client_scrolls`）を持つ
      - host も managed scroll 用 `_host_scroll`/`_host_nav_mode` を持つ
        （`SIZE_POLICY != host` のとき Ctrl-\\ で history を遡る）
      - `_host_raw_mode` で生中継 or viewport 再描画を切替

    旧ヒューリスティック再構成・raw passthrough 時代の属性は持たない。"""
    from pty_scroll import ScrollRenderer
    assert isinstance(proxy._client_scrolls, dict)
    assert isinstance(proxy._host_scroll, ScrollRenderer)
    assert proxy._host_nav_mode is False           # 既定は OFF
    assert isinstance(proxy._host_raw_mode, bool)
    for absent in ("_host_renderer", "_client_renderers", "_relayout_mode",
                   "_client_cols", "_pending_clear", "_recent_raw"):
        assert not hasattr(proxy, absent), f"持たないはずの属性が存在: {absent}"


def test_apply_pty_size_resizes_emulator_screen_only(proxy: PtyProxy) -> None:
    """_apply_pty_size は pyte screen を resize するが renderer reset はしない
    （raw passthrough では renderer が無い）。"""
    proxy._size_policy = "client"
    proxy._record_host_size(40, 100)
    proxy._record_client_size(7, 30, 80)
    rows, cols = proxy._apply_pty_size()
    assert (rows, cols) == (30, 80)
    assert proxy._emulator is not None
    assert (proxy._emulator._rows, proxy._emulator._cols) == (30, 80)


def test_full_resize_sequence_simulates_tmux_attach(proxy: PtyProxy) -> None:
    """tmux クライアントが接続 → resize → detach のシナリオ。"""
    proxy._size_policy = "client"
    # 起動時: host TTY 検出
    proxy._record_host_size(40, 100)
    proxy._apply_pty_size()
    assert _winsz(proxy.master_fd) == (40, 100)
    # tmux クライアント接続後の最初の resize event
    proxy._record_client_size(fd=7, rows=30, cols=140)
    proxy._apply_pty_size()
    assert _winsz(proxy.master_fd) == (30, 140)
    # tmux 内でリサイズ
    proxy._record_client_size(fd=7, rows=50, cols=180)
    proxy._apply_pty_size()
    assert _winsz(proxy.master_fd) == (50, 180)
    # tmux detach
    proxy._forget_client_size(7)
    proxy._apply_pty_size()
    assert _winsz(proxy.master_fd) == (40, 100)


def test_default_policy_is_client(tmp_path, monkeypatch) -> None:
    """default SIZE_POLICY は client（tmux ウィンドウサイズが正）。

    claude-master は本来 tmux ウィンドウサイズを正とする設計。回帰防止。
    実機の ~/.claude-master.toml に依存しないよう、設定ファイル不在・
    環境変数なしの「素の既定」を検証する（環境非依存）。
    """
    import importlib
    import config
    monkeypatch.delenv("SIZE_POLICY", raising=False)
    monkeypatch.setenv("CLAUDE_MASTER_CONFIG", str(tmp_path / "absent.toml"))
    importlib.reload(config)
    try:
        assert config.SIZE_POLICY == "client"
    finally:
        monkeypatch.delenv("CLAUDE_MASTER_CONFIG", raising=False)
        importlib.reload(config)


def test_smallest_keeps_host_unbroken_with_smaller_client(proxy: PtyProxy) -> None:
    """host + より小さい tmux client 同時接続時、PTY=最小なので host の
    描画が崩れない（絶対座標が host 範囲内に収まる）回帰テスト。"""
    proxy._size_policy = "smallest"
    proxy._record_host_size(50, 200)        # host は大きい
    proxy._apply_pty_size()
    assert _winsz(proxy.master_fd) == (50, 200)
    # 小さい tmux client が接続
    proxy._record_client_size(fd=9, rows=44, cols=160)
    proxy._apply_pty_size()
    # PTY は最小に → claude は 44x160 で描画 → host(50x200) に収まり崩れない
    assert _winsz(proxy.master_fd) == (44, 160)
    # client detach で host サイズへ戻る
    proxy._forget_client_size(9)
    proxy._apply_pty_size()
    assert _winsz(proxy.master_fd) == (50, 200)


def _set_winsz(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def test_largest_refreshes_live_host_size_not_stale(tmp_path) -> None:
    """回帰: _apply_pty_size は実 host 端末サイズを読み直す。

    起動時に取り損ねた/古い _host_size のままだと largest が client(tmux)
    サイズに固定され、host nav が「カーソル最下部まで」しか遡れなくなる
    （viewport>モデルで max_oy 減少）。client RESIZE 経由の _apply_pty_size
    でも live host を反映することを検証。"""
    master_fd, slave_fd = os.openpty()
    try:
        _set_winsz(slave_fd, 50, 180)            # 実 host は 50 行
        p = PtyProxy(master_fd, child_pid=222, child_pgid=222,
                     sock_path=tmp_path / "h.sock")
        p._stdin_fd = slave_fd
        p._size_policy = "largest"
        p._host_size = (10, 40)                  # 古い/誤った値（起動取り損ね相当）
        p._record_client_size(fd=7, rows=47, cols=176)  # tmux は 47 行
        rows, cols = p._apply_pty_size()
        # 実 host(50) を読み直し largest = max(host50, client47)=50 行
        assert p._host_size == (50, 180), "live host を読み直していない"
        assert (rows, cols) == (50, 180)
        assert p._emulator is not None
        assert (p._emulator._rows, p._emulator._cols) == (50, 180)
        # → host viewport(50) == モデル(50) なので nav は history 全深度
    finally:
        for fd in (master_fd, slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def test_apply_pty_size_keeps_host_size_when_ioctl_fails(tmp_path) -> None:
    """無効 fd で _ioctl_winsz が失敗しても既存 _host_size を壊さない。"""
    master_fd, slave_fd = os.openpty()
    try:
        p = PtyProxy(master_fd, child_pid=223, child_pgid=223,
                     sock_path=tmp_path / "h2.sock")
        p._stdin_fd = 9999                       # 無効 fd → OSError
        p._size_policy = "largest"
        p._record_host_size(48, 170)
        p._apply_pty_size()
        assert p._host_size == (48, 170)         # 壊れない
    finally:
        for fd in (master_fd, slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass
