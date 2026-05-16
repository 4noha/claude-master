import pytest as _pytest; pytestmark = _pytest.mark.legacy  # raw passthrough 化で本番未使用（debug/replay 用）
"""snapshot の LOG セクションに footer chrome が漏れていないか自動検査する。

debug 環境を作った際に手動で行った grep 検査を pytest 化したもの。
将来 footer 候補テキストが新たに出現したとき、無関係なログとして emit され
ていれば即座に test failure になる。

new false positive が見つかったら _ALLOWED_LOG_PATTERNS に追加する
（例: `(ctrl+o to expand)` はツール結果サマリの正規表示）。
"""
import json
import re
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
SNAPSHOTS_DIR = Path(__file__).resolve().parent.parent / "snapshots"

# LOG セクション内で許容するパターン（実 fixture で確認済の正常表示）。
# これらに「ヒット」する行はチェックから除外する。
_ALLOWED_LOG_PATTERNS = (
    re.compile(r"\(ctrl\+o to expand\)"),       # ツール結果省略行（⎿ 配下）
    re.compile(r"^[^a-zA-Z]*❯"),                # ❯ プロンプト（意図的に LOG 出力）
    re.compile(r"⎿"),                            # ⎿ ツール結果配下行は全て LOG として正常
    re.compile(r"⏺"),                            # ⏺ 完了行は LOG として正常
    # diff / コード行: 行番号 + 任意の +/- + 本文。footer キーワード文字列を
    # 含んでいてもソースコードなので LOG で正しい（pty_constants.py 自身の
    # diff 等。これを footer 扱いするとログ消失する）。
    re.compile(r"^\s*\d+\s+(?:[-+]\s|\s{2,}|[-+]?\t)"),
    re.compile(r'^\s*"[^"]*"\s*$'),              # 文字列リテラル単独
)

# LOG に出てきたら "footer 漏れの可能性が高い" と疑う候補パターン
_SUSPECT_PATTERNS = (
    re.compile(r"\bctrl\+[a-z]\s+to\s+\w"),
    re.compile(r"\bshift\+[a-z]+\s+to\s+\w"),
    re.compile(r"\bTip:\s*(ctrl|shift|alt)\+"),
    re.compile(r"\bbypass permissions"),
    re.compile(r"\besc to interrupt"),
    re.compile(r"\baccept edits"),
    re.compile(r"\bPress Ctrl-C again"),
    re.compile(r"\bPress up to edit queued"),
    re.compile(r"You've (used|hit) \d+% of your session"),
)


def _parse_cycles(snap: str) -> list[tuple[str, str]]:
    """=== cycle で split。(header, body) のリストを返す。"""
    chunks = snap.split("=== cycle ")
    out: list[tuple[str, str]] = []
    for ch in chunks[1:]:
        nl = ch.find("\n")
        out.append((ch[:nl].rstrip(" ="), ch[nl + 1:]))
    return out


def _extract_log(body: str) -> list[str]:
    m = re.search(r"^LOG:\n(.*?)(?=^[A-Z]+:|\Z)", body, re.DOTALL | re.MULTILINE)
    if not m:
        return []
    return [l for l in m.group(1).splitlines() if l.strip()]


def _is_allowed(line: str) -> bool:
    return any(p.search(line) for p in _ALLOWED_LOG_PATTERNS)


def _all_snapshots() -> list[Path]:
    return sorted(p for p in SNAPSHOTS_DIR.glob("*.expected.txt"))


@pytest.mark.parametrize("snap_path", _all_snapshots(), ids=lambda p: p.stem)
def test_no_footer_leak_in_log(snap_path: Path) -> None:
    """LOG セクションに _SUSPECT_PATTERNS が含まれていれば fail。
    _ALLOWED_LOG_PATTERNS のいずれかにマッチする行は許容する。
    """
    snap = snap_path.read_text()
    cycles = _parse_cycles(snap)
    leaks: list[tuple[str, str, str]] = []  # (cycle header, pattern, line)
    for header, body in cycles:
        for line in _extract_log(body):
            if _is_allowed(line):
                continue
            for pat in _SUSPECT_PATTERNS:
                if pat.search(line):
                    leaks.append((header, pat.pattern, line.strip()[:160]))
                    break
    if leaks:
        msg_lines = [f"{snap_path.name}: {len(leaks)} 件の footer 漏れ候補:"]
        for h, p, l in leaks[:10]:
            msg_lines.append(f"  cycle {h}: pattern={p!r}")
            msg_lines.append(f"    line: {l}")
        pytest.fail("\n".join(msg_lines))


def test_allowed_patterns_not_overbroad() -> None:
    """_ALLOWED_LOG_PATTERNS は明らかな footer を allow しないこと。回帰防止。"""
    must_be_caught = [
        "                ctrl+r to search history",
        "Tip: ctrl+s to show snippets",
        "Press Ctrl-C again to exit",
        "You've used 93% of your session limit",
    ]
    for line in must_be_caught:
        # 許容パターンに一致しない
        assert not _is_allowed(line), f"over-broad allow: {line!r}"
        # 検出パターンには一致する
        assert any(p.search(line) for p in _SUSPECT_PATTERNS), f"not caught: {line!r}"
