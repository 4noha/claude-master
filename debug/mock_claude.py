#!/usr/bin/env python3
"""pty_proxy.py が exec する claude のモック。

環境変数:
  MOCK_FIXTURE  bytes.bin のパス（録画済みバイト列）
  MOCK_DELAY    各 chunk 送信前の sleep 秒（default 0、events.log を再現したい場合に使う）
  MOCK_HOLD     全 bytes 出力後に保持する秒数（default 60）
                stdin は読み続けるので tmux からの入力を受け取り続ける

events.log が存在する場合は pty_raw 行のサイズを chunk 境界として使う。
"""
import os
import sys
import time
from pathlib import Path


def main() -> int:
    fixture = os.environ.get("MOCK_FIXTURE")
    if not fixture:
        sys.stderr.write("MOCK_FIXTURE 環境変数で bytes.bin パスを指定してください\n")
        return 1
    fixture_path = Path(fixture)
    if not fixture_path.exists():
        sys.stderr.write(f"fixture が見つかりません: {fixture_path}\n")
        return 1
    delay = float(os.environ.get("MOCK_DELAY", "0"))
    hold = float(os.environ.get("MOCK_HOLD", "60"))

    data = fixture_path.read_bytes()

    # events.log があれば pty_raw 行のサイズで chunk を切る
    events_path = fixture_path.parent / "events.log"
    chunk_sizes: list[int] = []
    if events_path.exists():
        for line in events_path.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[-2] == "pty_raw":
                try:
                    chunk_sizes.append(int(parts[-1]))
                except ValueError:
                    pass

    out = sys.stdout.buffer
    offset = 0
    if chunk_sizes:
        for size in chunk_sizes:
            if offset >= len(data):
                break
            end = min(offset + size, len(data))
            if delay > 0:
                time.sleep(delay)
            out.write(data[offset:end])
            out.flush()
            offset = end
    if offset < len(data):
        out.write(data[offset:])
        out.flush()

    # 全部書き終わったら hold 秒だけ待つ（tmux 側でキャプチャする時間を確保）
    # stdin を読みながら待つことで、入力同期テストの妨げにならないようにする
    deadline = time.monotonic() + hold
    try:
        import select
        while time.monotonic() < deadline:
            r, _, _ = select.select([sys.stdin.buffer], [], [], 0.1)
            if r:
                sys.stdin.buffer.read1(4096)  # 捨てる
    except Exception:
        time.sleep(hold)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
