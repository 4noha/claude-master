import pytest as _pytest; pytestmark = _pytest.mark.legacy  # raw passthrough 化で本番未使用（debug/replay 用）
"""pipeline_replay の回帰テスト。

  - 同幅で実行すれば host と client の最終画面は完全一致（divergence 0）
  - host_cols < client_cols の場合は host の方が truncate されるが、host の
    内容は client の内容のサブセットになる（情報が逆方向には増えない）

これらが将来崩れたら pty_proxy の host vs client レンダリング pipeline に
バグが入った可能性が高い。
"""
import json
from pathlib import Path

import pytest

from pipeline_replay import pipeline_replay

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _all_fixtures() -> list[Path]:
    return sorted(p for p in FIXTURES_DIR.iterdir()
                  if p.is_dir() and (p / "bytes.bin").exists())


@pytest.mark.parametrize("fixture_dir", _all_fixtures(), ids=lambda p: p.name)
def test_same_width_zero_divergence(fixture_dir: Path) -> None:
    """host と client が同幅なら最終画面の論理内容は完全一致する。"""
    meta = json.loads((fixture_dir / "meta.json").read_text())
    data = (fixture_dir / "bytes.bin").read_bytes()
    width = meta["width"]
    rows = meta["height"]

    result = pipeline_replay(data, rows=rows, host_cols=width, client_cols=width,
                              chunk_size=4096)
    assert result["divergence_cycles"] == [], (
        f"{fixture_dir.name}: {len(result['divergence_cycles'])} divergent cycles "
        f"with matching widths (expected 0). This means host and client "
        f"TerminalRenderer produced different content despite same input."
    )


@pytest.mark.parametrize("fixture_dir", _all_fixtures(), ids=lambda p: p.name)
def test_host_words_appear_in_client(fixture_dir: Path) -> None:
    """host_cols < client_cols 時、host で見える「単語」は client でも見える。

    行レベル比較は折返し片で false positive を出すので、単語集合で確認する。
    host だけにある単語があれば「片方の renderer に偏ったコンテンツ」を示し、
    レンダリング pipeline のバグ候補となる。
    """
    import re
    meta = json.loads((fixture_dir / "meta.json").read_text())
    data = (fixture_dir / "bytes.bin").read_bytes()
    rows = meta["height"]

    result = pipeline_replay(data, rows=rows, host_cols=80, client_cols=140,
                              chunk_size=4096)
    word_re = re.compile(r"[A-Za-z0-9_]{4,}")
    host_words = set()
    client_words = set()
    for l in result["host_final"]:
        host_words.update(word_re.findall(l))
    for l in result["client_final"]:
        client_words.update(word_re.findall(l))

    only_host = host_words - client_words
    # ノイズ単語（行末でちぎれた wrap 片）を除外: 4 字以下 / 数字のみ
    only_host = {w for w in only_host if len(w) >= 5 and not w.isdigit()}
    # host_cols=80 < client_cols=140 のとき、host が wrap で部分単語を見せる可能性
    # があるので 5 単語以下なら許容（false positive 抑制）
    if len(only_host) > 5:
        pytest.fail(
            f"{fixture_dir.name}: host が client にない単語を {len(only_host)} 件保持:\n"
            + "\n".join(f"  - {w}" for w in list(only_host)[:8])
        )
