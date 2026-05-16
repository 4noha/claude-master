"""PTY_PROXY_LOG=1 で記録された client_*_out.bin（proxy → tmux client 出力）を pyte に
流し込み、tmux クライアントが実際に見ていた画面を offline で再現する。

pipeline_replay.py が「入力 bytes から host + client の最終画面を再構築する」のに対し、
本ツールは「proxy が実際に client へ送ったバイト列」を pyte でレンダーする。
両者を比べることで pty_proxy のレンダリング pipeline 自体のバグと、socket 経路で
何かが起きているかを切り分けられる。

使い方:
  python debug/client_replay.py ~/.claude-master/logs/<pid>/client_0_out.bin \
      --cols 140 --rows 40 --out screen.txt

  # 比較用に同じ session の host_out.bin と並べてレンダー
  python debug/client_replay.py ~/.claude-master/logs/<pid>/client_0_out.bin \
      --compare ~/.claude-master/logs/<pid>/host_out.bin \
      --cols 140 --rows 40
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pyte  # noqa: E402

from pipeline_replay import _flat_content, _screen_text  # noqa: E402


def replay_to_screen(data: bytes, rows: int, cols: int,
                     chunk_size: int = 4096) -> pyte.Screen:
    """bytes を chunk 単位で feed して pyte.Screen を返す。"""
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    offset = 0
    while offset < len(data):
        end = min(offset + chunk_size, len(data))
        stream.feed(data[offset:end])
        offset = end
    return screen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bytes_file", type=Path,
                    help="client_*_out.bin / host_out.bin 等の renderer 出力")
    ap.add_argument("--rows", type=int, default=24)
    ap.add_argument("--cols", type=int, default=120)
    ap.add_argument("--chunk-size", type=int, default=4096)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--compare", type=Path, default=None,
                    help="このパスのファイルも同じ条件でレンダーし内容を比較")
    args = ap.parse_args()

    data = args.bytes_file.read_bytes()
    screen = replay_to_screen(data, args.rows, args.cols, args.chunk_size)
    lines = _screen_text(screen)

    out_str = "\n".join(lines) + "\n"
    if args.out:
        args.out.write_text(out_str)
    else:
        sys.stdout.write(out_str)

    if args.compare:
        cmp_data = args.compare.read_bytes()
        cmp_screen = replay_to_screen(cmp_data, args.rows, args.cols, args.chunk_size)
        cmp_lines = _screen_text(cmp_screen)
        a_flat = _flat_content(lines)
        b_flat = _flat_content(cmp_lines)
        print("\n--- COMPARE ---")
        print(f"  {args.bytes_file.name}: {len(data)} bytes, {sum(1 for l in lines if l.strip())} visible lines")
        print(f"  {args.compare.name}: {len(cmp_data)} bytes, {sum(1 for l in cmp_lines if l.strip())} visible lines")
        if a_flat == b_flat:
            print("  ✓ flat content IDENTICAL")
        else:
            a_set = set(s.strip() for s in lines if s.strip())
            b_set = set(s.strip() for s in cmp_lines if s.strip())
            only_a = a_set - b_set
            only_b = b_set - a_set
            print(f"  ⚠ DIVERGENCE  only_a={len(only_a)}  only_b={len(only_b)}")
            for l in list(only_a)[:3]:
                print(f"    only {args.bytes_file.name}: {l[:140]}")
            for l in list(only_b)[:3]:
                print(f"    only {args.compare.name}: {l[:140]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
