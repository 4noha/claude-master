import pytest as _pytest; pytestmark = _pytest.mark.legacy  # raw passthrough 化で本番未使用（debug/replay 用）
"""fixtures/ 全件を replay して snapshots/<name>.expected.txt と diff する回帰テスト。"""
import json
from pathlib import Path

import pytest

from replay import replay

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
SNAPSHOTS_DIR = Path(__file__).resolve().parent.parent / "snapshots"


def _all_fixtures() -> list[Path]:
    return sorted(p for p in FIXTURES_DIR.iterdir() if p.is_dir() and (p / "bytes.bin").exists())


def _parse_resize_at(spec: str) -> list[tuple[int, int, int]]:
    """meta.json の 'resize_at': 'OFFSET:RxC[,...]' をパース。"""
    out: list[tuple[int, int, int]] = []
    for part in (s.strip() for s in spec.split(",") if s.strip()):
        off_s, dims_s = part.split(":")
        rr, cc = (int(x) for x in dims_s.lower().split("x"))
        out.append((int(off_s), rr, cc))
    return out


@pytest.mark.parametrize("fixture_dir", _all_fixtures(), ids=lambda p: p.name)
def test_fixture_matches_snapshot(fixture_dir: Path) -> None:
    """各 fixture を chunk-size 4096 で replay し expected snapshot と完全一致する。

    meta.json に 'resize_at' があればその resize を再現する（回帰 fixture 用）。
    """
    name = fixture_dir.name
    meta = json.loads((fixture_dir / "meta.json").read_text())
    bytes_data = (fixture_dir / "bytes.bin").read_bytes()
    resize_specs = _parse_resize_at(meta.get("resize_at", ""))

    snapshot, stats = replay(
        bytes_data,
        rows=meta["height"],
        cols=meta["width"],
        chunk_size=4096,
        resize_specs=resize_specs or None,
    )

    expected_path = SNAPSHOTS_DIR / f"{name}.expected.txt"
    assert expected_path.exists(), f"snapshot missing: {expected_path}"
    expected = expected_path.read_text()

    if snapshot != expected:
        # 最初の不一致行で詳しく失敗を報告
        for i, (a, b) in enumerate(zip(snapshot.splitlines(), expected.splitlines())):
            if a != b:
                pytest.fail(
                    f"snapshot mismatch at line {i + 1}:\n"
                    f"  actual:   {a!r}\n"
                    f"  expected: {b!r}\n"
                    f"  cycles={stats['cycles']} logs={stats['total_logs']}"
                )
        pytest.fail(f"snapshot length differs: actual={len(snapshot)} expected={len(expected)}")


def test_replay_is_deterministic() -> None:
    """同じ入力に対して replay() は 2 回呼び出しても同じ出力を返す。"""
    fixtures = _all_fixtures()
    assert fixtures, "no fixtures found"
    fixture_dir = fixtures[0]
    meta = json.loads((fixture_dir / "meta.json").read_text())
    bytes_data = (fixture_dir / "bytes.bin").read_bytes()

    snap1, _ = replay(bytes_data, rows=meta["height"], cols=meta["width"], chunk_size=4096)
    snap2, _ = replay(bytes_data, rows=meta["height"], cols=meta["width"], chunk_size=4096)
    assert snap1 == snap2
