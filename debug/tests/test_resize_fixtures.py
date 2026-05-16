import pytest as _pytest; pytestmark = _pytest.mark.legacy  # raw passthrough 化で本番未使用（debug/replay 用）
"""全 fixture を mid-stream で resize 注入し、レンダリングが破綻しないことを検証。

過去に発見されたバグ群（cogitated-duplicate-on-resize / フッタードリフト /
カーソル誤位置）はすべて resize イベントが起点だった。本テストは:

  1. 例外なく完走する
  2. resize 注入版でも内容が消失しない（少なくとも一定数の単語は残る）
  3. resize 注入版 == resize なし版の最終内容のサブセット（追加で重複生成されない）

ことを検証する。Layer 1 (TuiEmulator) と Layer 1.5 (pipeline) の両方を回す。
"""
import json
import re
from pathlib import Path

import pytest

from pipeline_replay import pipeline_replay
from replay import replay

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"

WORD_RE = re.compile(r"[A-Za-z0-9_]{4,}")


def _all_fixtures() -> list[Path]:
    return sorted(p for p in FIXTURES_DIR.iterdir()
                  if p.is_dir() and (p / "bytes.bin").exists())


def _resize_offset(data_len: int) -> int:
    """全体の 30% 〜 70% のどこかで resize を発火させる安全な offset。"""
    return max(4096, data_len // 3)


@pytest.mark.parametrize("fixture_dir", _all_fixtures(), ids=lambda p: p.name)
def test_replay_with_resize_no_exception(fixture_dir: Path) -> None:
    """TuiEmulator レベルで mid-stream resize しても例外が出ない。"""
    meta = json.loads((fixture_dir / "meta.json").read_text())
    data = (fixture_dir / "bytes.bin").read_bytes()
    width = meta["width"]
    rows = meta["height"]

    # 半分の位置で幅を半分にする
    snapshot, stats = replay(
        data, rows=rows, cols=width,
        chunk_size=4096,
        resize_specs=[(_resize_offset(len(data)), rows, width // 2)],
    )
    # 何かしらの内容が emit されていること
    assert "===" in snapshot
    assert stats["cycles"] > 0


@pytest.mark.parametrize("fixture_dir", _all_fixtures(), ids=lambda p: p.name)
def test_pipeline_replay_with_resize_no_exception(fixture_dir: Path) -> None:
    """host + client パイプラインで mid-stream resize しても例外が出ない。"""
    meta = json.loads((fixture_dir / "meta.json").read_text())
    data = (fixture_dir / "bytes.bin").read_bytes()
    width = meta["width"]
    rows = meta["height"]

    off = _resize_offset(len(data))
    result = pipeline_replay(
        data, rows=rows, host_cols=width, client_cols=width,
        chunk_size=4096,
        resize_events=[(off, max(20, rows // 2), max(40, width // 2), width)],
    )
    assert result["resize_applied"] == 1
    assert result["cycles"] > 0


@pytest.mark.parametrize("fixture_dir", _all_fixtures(), ids=lambda p: p.name)
def test_resize_does_not_lose_all_content(fixture_dir: Path) -> None:
    """resize 後も画面に意味あるコンテンツが残っている（全消失しない）。"""
    meta = json.loads((fixture_dir / "meta.json").read_text())
    data = (fixture_dir / "bytes.bin").read_bytes()
    width = meta["width"]
    rows = meta["height"]

    off = _resize_offset(len(data))
    result = pipeline_replay(
        data, rows=rows, host_cols=width, client_cols=width,
        chunk_size=4096,
        resize_events=[(off, rows, width // 2, width // 2)],
    )
    visible = " ".join(s for s in result["host_final"] if s.strip())
    words = WORD_RE.findall(visible)
    # フッターのみ残る最小ケースでも数単語は期待できる
    assert len(words) >= 3, (
        f"{fixture_dir.name}: resize 後に意味あるコンテンツが消失 (words={len(words)})"
    )


@pytest.mark.parametrize("fixture_dir", _all_fixtures(), ids=lambda p: p.name)
def test_resize_no_duplicate_log_emission(fixture_dir: Path) -> None:
    """resize 前後で同じログ行が二重 emit されないことを検証。

    過去の cogitated-duplicate-on-resize バグの直接的な回帰テスト。
    """
    meta = json.loads((fixture_dir / "meta.json").read_text())
    data = (fixture_dir / "bytes.bin").read_bytes()
    width = meta["width"]
    rows = meta["height"]

    off = _resize_offset(len(data))
    snapshot, _ = replay(
        data, rows=rows, cols=width, chunk_size=4096,
        resize_specs=[(off, rows, width // 2)],
    )
    # snapshot 内で「 ✻ Cogitated for ... s」のような完了行が複数の cycle に
    # わたって LOG に重複 emit されていないことを確認。
    cogitated_log = []
    in_log = False
    for line in snapshot.splitlines():
        if line.startswith("LOG:"):
            in_log = True
            continue
        if line.startswith("FOOTER:") or line.startswith("CURSOR:"):
            in_log = False
            continue
        if in_log and "Cogitated for" in line:
            cogitated_log.append(line.strip())
    # 同じテキストの完了行が 3 回以上 LOG に出れば異常
    from collections import Counter
    dups = [(t, c) for t, c in Counter(cogitated_log).items() if c >= 3]
    assert not dups, (
        f"{fixture_dir.name}: resize 後に '✻ Cogitated for ...' が重複 emit: {dups[:3]}"
    )


def _emit_log_lines(snapshot: str) -> list[str]:
    """snapshot から LOG セクションの中身（rstrip 済み）を全部抽出。"""
    out = []
    in_log = False
    for line in snapshot.splitlines():
        if line.startswith("LOG:"):
            in_log = True
            continue
        if line.startswith("FOOTER:") or line.startswith("CURSOR:"):
            in_log = False
            continue
        if in_log and line.strip():
            out.append(line.strip())
    return out


@pytest.mark.parametrize("fixture_dir", _all_fixtures(), ids=lambda p: p.name)
def test_resize_does_not_cause_massive_drops(fixture_dir: Path) -> None:
    """resize 後に LOG 行が壊滅的に減らないこと（緩いガード）。

    厳密な検出は実機（新規 claude セッションで既知の不具合を聞いて確認）に任せ、
    ここは "半分以上消えたら異常" の最低保証だけ持つ。
    """
    meta = json.loads((fixture_dir / "meta.json").read_text())
    data = (fixture_dir / "bytes.bin").read_bytes()
    width = meta["width"]
    rows = meta["height"]

    snap_no, _ = replay(data, rows=rows, cols=width, chunk_size=4096)
    snap_with, _ = replay(
        data, rows=rows, cols=width, chunk_size=4096,
        resize_specs=[(_resize_offset(len(data)), rows, max(40, width // 2))],
    )
    set_no = set(_emit_log_lines(snap_no))
    set_with = set(_emit_log_lines(snap_with))
    if not set_no:
        return
    loss = len(set_no - set_with) / len(set_no)
    assert loss < 0.5, (
        f"{fixture_dir.name}: resize 後の LOG 行集合が "
        f"{loss:.0%} 失われた。代表的に消えた行:\n  "
        + "\n  ".join(list(set_no - set_with)[:3])
    )
