"""resolve_pty_size の 4 ポリシーの pure 関数テスト。"""
import pytest

from pty_proxy import resolve_pty_size


def test_host_policy_uses_host_size() -> None:
    """host policy: 常に host_size を返す。client の変化は無視。"""
    s = resolve_pty_size("host",
                          host_size=(40, 100),
                          client_sizes={1: (30, 80), 2: (50, 120)},
                          last_client_size=(50, 120),
                          latest_size=(50, 120))
    assert s == (40, 100)


def test_host_policy_fallback_default() -> None:
    """host が未設定なら default にフォールバック。"""
    s = resolve_pty_size("host", host_size=None, client_sizes={},
                          last_client_size=None, latest_size=None,
                          default=(24, 80))
    assert s == (24, 80)


def test_client_policy_uses_last_client() -> None:
    """client policy: 最後の client resize を採用。"""
    s = resolve_pty_size("client",
                          host_size=(40, 100),
                          client_sizes={1: (30, 80)},
                          last_client_size=(30, 80),
                          latest_size=(30, 80))
    assert s == (30, 80)


def test_client_policy_falls_back_to_host_when_no_client() -> None:
    """client 不在なら host にフォールバック。"""
    s = resolve_pty_size("client",
                          host_size=(40, 100), client_sizes={},
                          last_client_size=None, latest_size=(40, 100))
    assert s == (40, 100)


def test_latest_policy_picks_latest_regardless() -> None:
    """latest policy: 最新 resize を採用（host か client か問わない）。"""
    # host が最後に変わった想定
    s = resolve_pty_size("latest",
                          host_size=(40, 100),
                          client_sizes={1: (30, 80)},
                          last_client_size=(30, 80),
                          latest_size=(40, 100))
    assert s == (40, 100)
    # client が最後に変わった想定
    s = resolve_pty_size("latest",
                          host_size=(40, 100),
                          client_sizes={1: (30, 80)},
                          last_client_size=(30, 80),
                          latest_size=(30, 80))
    assert s == (30, 80)


def test_largest_policy_picks_max_of_all() -> None:
    """largest policy: host を含む接続中端末の (rows, cols) max。"""
    s = resolve_pty_size("largest",
                          host_size=(40, 100),
                          client_sizes={1: (30, 80), 2: (50, 120)},
                          last_client_size=(50, 120),
                          latest_size=(50, 120))
    assert s == (50, 120)  # max rows=50, max cols=120


def test_smallest_policy_picks_min_of_all() -> None:
    """smallest policy: host を含む接続中端末の (rows, cols) min。"""
    s = resolve_pty_size("smallest",
                          host_size=(40, 100),
                          client_sizes={1: (30, 80), 2: (50, 120)},
                          last_client_size=(50, 120),
                          latest_size=(50, 120))
    assert s == (30, 80)  # min rows=30, min cols=80


def test_smallest_with_only_host() -> None:
    s = resolve_pty_size("smallest",
                          host_size=(40, 100), client_sizes={},
                          last_client_size=None, latest_size=(40, 100))
    assert s == (40, 100)


def test_unknown_policy_falls_back_to_host() -> None:
    s = resolve_pty_size("unknown",
                          host_size=(40, 100), client_sizes={},
                          last_client_size=None, latest_size=None)
    assert s == (40, 100)


def test_empty_state_returns_default() -> None:
    """全部 None なら default を返す（assertion 用）。"""
    s = resolve_pty_size("smallest",
                          host_size=None, client_sizes={},
                          last_client_size=None, latest_size=None,
                          default=(24, 80))
    assert s == (24, 80)


def test_policy_is_case_insensitive() -> None:
    s = resolve_pty_size("HOST",
                          host_size=(40, 100), client_sizes={},
                          last_client_size=None, latest_size=None)
    assert s == (40, 100)
