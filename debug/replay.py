"""TuiEmulator オフラインリプレイ。

PTY_PROXY_LOG=1 で録画した pty_raw.bin（PTY → proxy の生バイト）を、claude を起動
せずに TuiEmulator に流し込み、各サイクルの emit を決定的に再現する。

使い方:
  python debug/replay.py <bytes_file> [options]

  --width N             初期端末幅（cols, default 120）
  --height N            初期端末高さ（rows, default 24）
  --chunk-size K        K バイトごとに区切って feed する（フレーム断片化バグ再現用）
  --events EVENTS_LOG   events.log を読み、pty_raw 行のサイズ列をそのまま chunk 境界に使う
  --resize-at SPEC      "offset:rowsxcols[,offset:rowsxcols...]" 指定位置で resize 発火
                        events.log を渡した場合は client_*_resize 行が自動で適用される
  --diff FILE           出力スナップショットを FILE と比較し、不一致なら exit 1
  --out FILE            出力スナップショットを FILE に書く（指定なしなら stdout）
  --quiet               出力を抑制（--diff のみ動かしたいとき）

スナップショット形式（人が読める形）:

  === cycle 1 (offset=13 fed=13) ===
  LOG:
  FOOTER:
    > _
  CURSOR: (0, 2)
"""
import argparse
import json
import sys
from pathlib import Path

# claude-master のルートを import path に追加
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pty_constants import _ANSI_ALL_RE  # noqa: E402
from pty_emulator import TuiEmulator  # noqa: E402


def _strip_ansi(data: bytes) -> str:
    return _ANSI_ALL_RE.sub(b"", data).decode("utf-8", errors="replace")


def _format_cycle(idx: int, offset: int, fed: int, logs: list[bytes],
                  footer: list[bytes], cursor,
                  prev_footer_text: list[str] | None = None,
                  prev_cursor=None) -> tuple[str, list[str]]:
    """サイクル 1 つを整形。前回 FOOTER と同じなら "(unchanged)" で省略。

    戻り値: (整形済みテキスト, 今回の footer_text — 次回比較用)
    """
    log_text = [_strip_ansi(b).rstrip() for b in logs]
    footer_text = [_strip_ansi(b).rstrip() for b in footer]

    has_log = any(l.strip() for l in log_text)
    footer_unchanged = (prev_footer_text is not None and footer_text == prev_footer_text)
    cursor_unchanged = (cursor == prev_cursor)

    # サイクル全体が "何も変わっていない" 場合は超圧縮表記
    if not has_log and footer_unchanged and cursor_unchanged:
        return f"=== cycle {idx} (offset={offset} fed={fed}) === (no change)", footer_text

    lines = [f"=== cycle {idx} (offset={offset} fed={fed}) ==="]
    if has_log:
        lines.append("LOG:")
        for l in log_text:
            lines.append(f"  {l}")
    if footer_unchanged:
        lines.append("FOOTER: (unchanged)")
    elif footer_text:
        lines.append("FOOTER:")
        for l in footer_text:
            lines.append(f"  {l}")
    else:
        lines.append("FOOTER: (empty)")
    if cursor is not None:
        lines.append(f"CURSOR: ({cursor[0]}, {cursor[1]})")
    return "\n".join(lines), footer_text


def _parse_resize_spec(spec: str) -> list[tuple[int, int, int]]:
    """'1000:24x80,2000:30x100' → [(1000, 24, 80), (2000, 30, 100)]"""
    out: list[tuple[int, int, int]] = []
    if not spec:
        return out
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        offset_s, size_s = part.split(":")
        rows_s, cols_s = size_s.lower().split("x")
        out.append((int(offset_s), int(rows_s), int(cols_s)))
    return out


def _load_events(events_path: Path) -> tuple[list[int], list[tuple[int, int, int]]]:
    """events.log を読み、pty_raw のチャンクサイズ列と resize イベントを返す。

    resize イベントは client_*_resize 行と同じタイムスタンプ近傍に発生する。
    pty_raw の累積バイト数をオフセットとして対応付ける。
    """
    chunks: list[int] = []
    resize_specs: list[tuple[int, int, int]] = []  # (pty_raw_offset, rows, cols)
    pending_resize: tuple[int, int] | None = None  # 次の pty_raw 行で確定する resize
    cumulative = 0
    for line in events_path.read_text().splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            size = int(parts[-1])
        except ValueError:
            continue
        name = parts[-2]
        if name == "pty_raw":
            chunks.append(size)
            cumulative += size
            if pending_resize is not None:
                resize_specs.append((cumulative, pending_resize[0], pending_resize[1]))
                pending_resize = None
        elif name.endswith("_resize") and name.startswith("client_"):
            # この行の内容は別ファイル（client_*_resize.bin）に書かれている。
            # ファイル末尾エントリだけ拾うので簡略化のため events.log だけでは内容を確定できない。
            # 代わりに resize は --resize-at で明示的に渡してもらう前提とする。
            # ここでは何もしない（resize_specs は --resize-at で上書き）。
            pass
    return chunks, resize_specs


def _slice_chunks(data: bytes, chunk_size: int | None,
                  chunk_sizes: list[int] | None) -> list[tuple[int, bytes]]:
    """data を (offset_before, chunk_bytes) のリストへ。

    chunk_sizes が指定されればそれを使い、なければ chunk_size 固定で割る。
    chunk_size も None なら 1 チャンクで全部渡す。
    """
    out: list[tuple[int, bytes]] = []
    if chunk_sizes:
        offset = 0
        for size in chunk_sizes:
            if offset >= len(data):
                break
            end = min(offset + size, len(data))
            out.append((offset, data[offset:end]))
            offset = end
        if offset < len(data):  # events.log より bytes が長い場合は残りを 1 チャンク
            out.append((offset, data[offset:]))
        return out
    if chunk_size is None or chunk_size <= 0:
        return [(0, data)]
    offset = 0
    while offset < len(data):
        end = min(offset + chunk_size, len(data))
        out.append((offset, data[offset:end]))
        offset = end
    return out


def replay(
    data: bytes,
    rows: int = 24,
    cols: int = 120,
    chunk_size: int | None = None,
    chunk_sizes: list[int] | None = None,
    resize_specs: list[tuple[int, int, int]] | None = None,
) -> tuple[str, dict]:
    """data を TuiEmulator に流し込み、(スナップショットテキスト, 統計) を返す。"""
    emulator = TuiEmulator(rows=rows, cols=cols)
    resize_specs = sorted(resize_specs or [], key=lambda x: x[0])
    chunks = _slice_chunks(data, chunk_size, chunk_sizes)

    blocks: list[str] = []
    cycle = 0
    total_logs = 0
    next_resize_idx = 0
    prev_footer_text: list[str] | None = None
    prev_cursor = None
    for chunk_offset, chunk in chunks:
        # チャンク前のオフセットに resize が予約されていれば先に適用する
        while (next_resize_idx < len(resize_specs)
               and resize_specs[next_resize_idx][0] <= chunk_offset):
            _, rr, cc = resize_specs[next_resize_idx]
            emulator.resize(rr, cc)
            blocks.append(f"=== resize → {rr}x{cc} at offset={chunk_offset} ===")
            prev_footer_text = None  # resize 後は比較リセット
            prev_cursor = None
            next_resize_idx += 1

        cycle += 1
        logs, footer, cursor = emulator.feed(chunk)
        total_logs += len(logs)
        formatted, prev_footer_text = _format_cycle(
            cycle, chunk_offset, len(chunk), logs, footer, cursor,
            prev_footer_text, prev_cursor,
        )
        prev_cursor = cursor
        blocks.append(formatted)

    stats = {
        "cycles": cycle,
        "total_logs": total_logs,
        "final_cols": emulator._cols,
        "final_rows": emulator._rows,
        "usage": emulator.extract_usage(),
    }
    return "\n\n".join(blocks) + "\n", stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Offline replay of PTY bytes through TuiEmulator")
    ap.add_argument("bytes_file", type=Path, help="path to pty_raw.bin (or any byte stream)")
    ap.add_argument("--width", type=int, default=120)
    ap.add_argument("--height", type=int, default=24)
    ap.add_argument("--chunk-size", type=int, default=None,
                    help="feed in fixed-size chunks; default = single chunk")
    ap.add_argument("--events", type=Path, default=None,
                    help="events.log path; use pty_raw chunk sizes as feed boundaries")
    ap.add_argument("--resize-at", type=str, default="",
                    help="comma-separated 'offset:rowsxcols' resize events")
    ap.add_argument("--diff", type=Path, default=None,
                    help="compare output to this file; exit 1 on mismatch")
    ap.add_argument("--out", type=Path, default=None,
                    help="write output snapshot here (default: stdout)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--stats", action="store_true",
                    help="print stats JSON to stderr after replay")
    args = ap.parse_args()

    data = args.bytes_file.read_bytes()
    chunk_sizes: list[int] | None = None
    resize_specs: list[tuple[int, int, int]] = _parse_resize_spec(args.resize_at)
    if args.events is not None:
        ev_chunks, ev_resizes = _load_events(args.events)
        chunk_sizes = ev_chunks
        if not resize_specs:
            resize_specs = ev_resizes

    snapshot, stats = replay(
        data,
        rows=args.height,
        cols=args.width,
        chunk_size=args.chunk_size,
        chunk_sizes=chunk_sizes,
        resize_specs=resize_specs,
    )

    if args.out:
        args.out.write_text(snapshot)
    elif not args.quiet and args.diff is None:
        sys.stdout.write(snapshot)

    if args.stats:
        sys.stderr.write(json.dumps(stats, ensure_ascii=False) + "\n")

    if args.diff is not None:
        expected = args.diff.read_text()
        if snapshot != expected:
            sys.stderr.write(f"MISMATCH: snapshot != {args.diff}\n")
            sys.stderr.write(f"  actual cycles={stats['cycles']} logs={stats['total_logs']}\n")
            # 簡易 diff（最初の不一致行）
            for i, (a, b) in enumerate(zip(snapshot.splitlines(), expected.splitlines())):
                if a != b:
                    sys.stderr.write(f"  first diff at line {i + 1}:\n")
                    sys.stderr.write(f"    actual:   {a!r}\n")
                    sys.stderr.write(f"    expected: {b!r}\n")
                    break
            return 1
        if not args.quiet:
            print(f"OK: {stats['cycles']} cycles, {stats['total_logs']} log lines emitted, matches snapshot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
