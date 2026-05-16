"""設定ファイル(~/.claude-master.toml)ローダの本番経路テスト。

優先度 env > file > default、不正ファイルの無視、[claude-master]
テーブル、CLAUDE_MASTER_CONFIG でのパス指定を検証する。
"""
import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """tmp の TOML を指す config モジュールを返すファクトリ。"""
    path = tmp_path / "cm.toml"

    def _load(text: str | None, env: dict | None = None):
        for k in ("NAV_SCROLL_STEP", "NAV_PAGE_STEP", "SIZE_POLICY",
                  "HOST_FLOW_SCROLLBACK", "NAV_KEY", "TMUX_SESSION"):
            monkeypatch.delenv(k, raising=False)
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        if text is None:
            monkeypatch.setenv("CLAUDE_MASTER_CONFIG", str(tmp_path / "nope.toml"))
        else:
            path.write_text(text)
            monkeypatch.setenv("CLAUDE_MASTER_CONFIG", str(path))
        import config
        return importlib.reload(config)

    return _load


def test_file_overrides_default(cfg) -> None:
    c = cfg("nav_scroll_step = 3\n")
    assert c.NAV_SCROLL_STEP == 3
    assert c.NAV_PAGE_STEP == 10            # 未指定は既定
    assert c.SIZE_POLICY == "client"        # 未指定は既定


def test_env_overrides_file(cfg) -> None:
    c = cfg("nav_scroll_step = 3\n", env={"NAV_SCROLL_STEP": "9"})
    assert c.NAV_SCROLL_STEP == 9           # env が file に勝つ


def test_missing_file_uses_defaults(cfg) -> None:
    c = cfg(None)
    assert c.NAV_SCROLL_STEP == 1
    assert c.SIZE_POLICY == "client"
    assert c.NAV_KEY == b"\x1c"


def test_malformed_file_ignored(cfg) -> None:
    c = cfg("this is = not = valid = toml [[[\n")
    assert c.NAV_SCROLL_STEP == 1           # 壊れたファイルは既定へ
    assert c.SIZE_POLICY == "client"


def test_table_section_supported(cfg) -> None:
    c = cfg('[claude-master]\nsize_policy = "host"\nnav_page_step = 4\n')
    assert c.SIZE_POLICY == "host"
    assert c.NAV_PAGE_STEP == 4


def test_typed_values_bool_and_str(cfg) -> None:
    c = cfg('host_flow_scrollback = true\nnav_key = "ctrl-]"\n')
    assert c.HOST_FLOW_SCROLLBACK is True
    assert c.NAV_KEY == b"\x1d"             # ctrl-] = \x1d


def test_invalid_int_falls_back(cfg) -> None:
    c = cfg('nav_scroll_step = "abc"\n')
    assert c.NAV_SCROLL_STEP == 1           # 不正値は既定


def test_int_clamped_to_range(cfg) -> None:
    c = cfg("nav_scroll_step = 99999\n")    # 上限 1000
    assert c.NAV_SCROLL_STEP == 1000
