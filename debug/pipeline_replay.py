"""Host + tmux client の 2 レンダラを並列に走らせ、最終可視状態を比較する。

pty_proxy はホスト stdout 用と各 socket client 用に **独立した TerminalRenderer
インスタンス** を保持しており、それぞれ異なる端末幅で render() を呼ぶ。
ホスト幅と tmux client 幅が違うと、

  - `_truncate_visible` の結果が違う
  - `_max_footer_height` のドリフトが個別に発生する
  - `_last_cursor` の追跡が個別

ため見た目が分岐する。本ツールは:

  1. PTY bytes を TuiEmulator に流す（1 回）
  2. 同じ (logs, footer, cursor) を host_renderer / client_renderer に渡し
     それぞれ ANSI バイトを生成
  3. 各バイト列を独立した pyte.Screen にフィードして「実端末が描画した結果」を再現
  4. 各サイクル後に host_screen と client_screen の可視テキストを比較

  出力:
    - --out FILE : 各サイクルの可視状態
    - --diff-screens : host vs client divergence の行数を表示
    - --on-divergence : 不一致時に詳細を吐く
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pyte  # noqa: E402

from pty_constants import _ANSI_ALL_RE  # noqa: E402
from pty_emulator import TuiEmulator  # noqa: E402
from pty_renderer import TerminalRenderer  # noqa: E402


def _screen_text(screen: pyte.Screen) -> list[str]:
    """pyte.Screen の可視領域を行ごとに rstrip 済みテキストで返す。"""
    out: list[str] = []
    for y in range(screen.lines):
        line = screen.buffer[y]
        if not line:
            out.append("")
            continue
        max_col = max(line.keys())
        chars: list[str] = []
        for x in range(max_col + 1):
            ch = line.get(x)
            if ch is None:
                chars.append(" ")
            elif ch.data == "":
                continue
            else:
                chars.append(ch.data)
        out.append("".join(chars).rstrip())
    return out


def _format_screen(label: str, lines: list[str]) -> str:
    out = [f"--- {label} ---"]
    out.extend(lines)
    return "\n".join(out)


import re as _re

_WS_RE = _re.compile(r"\s+")


def _flat_content(lines: list[str]) -> str:
    """画面の論理コンテンツ。折り返し位置 / 空白量に依存しない比較用。"""
    joined = " ".join(s for s in lines if s.strip())
    return _WS_RE.sub(" ", joined).strip()


def pipeline_replay(
    data: bytes,
    rows: int = 24,
    host_cols: int = 80,
    client_cols: int = 100,
    chunk_size: int = 4096,
    resize_events: list[tuple[int, int, int, int]] | None = None,
) -> dict:
    """完全パイプラインを並列再生。

    resize_events: [(offset, new_rows, new_host_cols, new_client_cols), ...]
      指定 offset の chunk 処理前に TuiEmulator / 2 pyte screen / 2 renderer
      を同期的にリサイズする。SIGWINCH を offline 再現する用途。

    Returns:
      {
        "cycles": int,
        "divergence_cycles": [int],
        "host_final": list[str],
        "client_final": list[str],
        "host_bytes_total": int,
        "client_bytes_total": int,
        "resize_applied": int,
      }
    """
    emu_cols = max(host_cols, client_cols)
    emulator = TuiEmulator(rows=rows, cols=emu_cols)
    host_renderer = TerminalRenderer()
    client_renderer = TerminalRenderer()

    host_screen = pyte.Screen(host_cols, rows)
    host_stream = pyte.ByteStream(host_screen)
    client_screen = pyte.Screen(client_cols, rows)
    client_stream = pyte.ByteStream(client_screen)

    resize_specs = sorted(resize_events or [], key=lambda x: x[0])
    next_resize = 0
    cur_host_cols = host_cols
    cur_client_cols = client_cols
    cur_rows = rows

    cycle_n = 0
    host_total = 0
    client_total = 0
    divergence_cycles: list[int] = []
    resize_applied = 0

    offset = 0
    while offset < len(data):
        # この offset で resize が予約されていれば先に適用
        while (next_resize < len(resize_specs)
               and resize_specs[next_resize][0] <= offset):
            _, rr, hc, cc = resize_specs[next_resize]
            cur_rows, cur_host_cols, cur_client_cols = rr, hc, cc
            new_emu = max(hc, cc)
            try:
                emulator.resize(rr, new_emu)
            except Exception:
                pass
            try:
                host_screen.resize(rr, hc)
                client_screen.resize(rr, cc)
            except Exception:
                pass
            host_renderer.reset()
            client_renderer.reset()
            resize_applied += 1
            next_resize += 1

        end = min(offset + chunk_size, len(data))
        chunk = data[offset:end]
        offset = end
        cycle_n += 1

        logs, footer, cursor = emulator.feed(chunk)
        host_bytes = host_renderer.render(logs, footer, cursor, cur_host_cols)
        client_bytes = client_renderer.render(logs, footer, cursor, cur_client_cols)
        host_stream.feed(host_bytes)
        client_stream.feed(client_bytes)
        host_total += len(host_bytes)
        client_total += len(client_bytes)

        h_text = _screen_text(host_screen)
        c_text = _screen_text(client_screen)
        # 幅が違うと折り返し位置が変わるので、画面全体を flat 化して
        # 空白を圧縮した「論理コンテンツ」で比較する。
        # これでも diverge していれば、内容そのものが分岐している = 真のバグ候補。
        h_flat = _flat_content(h_text)
        c_flat = _flat_content(c_text)
        if h_flat != c_flat:
            divergence_cycles.append(cycle_n)

    return {
        "cycles": cycle_n,
        "divergence_cycles": divergence_cycles,
        "host_final": _screen_text(host_screen),
        "client_final": _screen_text(client_screen),
        "host_bytes_total": host_total,
        "client_bytes_total": client_total,
        "resize_applied": resize_applied,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bytes_file", type=Path)
    ap.add_argument("--rows", type=int, default=24)
    ap.add_argument("--host-cols", type=int, default=80)
    ap.add_argument("--client-cols", type=int, default=120)
    ap.add_argument("--chunk-size", type=int, default=4096)
    ap.add_argument("--resize-at", type=str, default="",
                    help="comma-separated 'offset:rowsxhost_colsxclient_cols'")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--diff-screens", action="store_true",
                    help="host_final / client_final の divergence を行ごとに表示")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    data = args.bytes_file.read_bytes()
    resize_events: list[tuple[int, int, int, int]] = []
    for part in (s.strip() for s in args.resize_at.split(",") if s.strip()):
        off_s, dims_s = part.split(":")
        rr, hc, cc = (int(x) for x in dims_s.lower().split("x"))
        resize_events.append((int(off_s), rr, hc, cc))

    result = pipeline_replay(
        data, rows=args.rows,
        host_cols=args.host_cols, client_cols=args.client_cols,
        chunk_size=args.chunk_size,
        resize_events=resize_events or None,
    )

    body = [
        _format_screen(f"HOST ({args.host_cols}x{args.rows})", result["host_final"]),
        "",
        _format_screen(f"CLIENT ({args.client_cols}x{args.rows})", result["client_final"]),
        "",
        f"divergence_cycles: {len(result['divergence_cycles'])} / {result['cycles']}",
    ]
    out_str = "\n".join(body) + "\n"

    if args.out:
        args.out.write_text(out_str)
    else:
        sys.stdout.write(out_str)

    if args.stats:
        sys.stderr.write(json.dumps({
            "cycles": result["cycles"],
            "divergence_cycles": len(result["divergence_cycles"]),
            "host_bytes": result["host_bytes_total"],
            "client_bytes": result["client_bytes_total"],
            "resize_applied": result["resize_applied"],
        }) + "\n")

    if args.diff_screens:
        h, c = result["host_final"], result["client_final"]
        h_set = set(l.strip() for l in h if l.strip())
        c_set = set(l.strip() for l in c if l.strip())
        only_host = h_set - c_set
        only_client = c_set - h_set
        if only_host:
            print(f"\n## host only ({len(only_host)} lines):")
            for l in list(only_host)[:8]:
                print(f"  {l[:160]}")
        if only_client:
            print(f"\n## client only ({len(only_client)} lines):")
            for l in list(only_client)[:8]:
                print(f"  {l[:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
