#!/usr/bin/env bash
# fixture を作る:
#   PTY_PROXY_LOG=1 で録画された ~/.claude-master/logs/<pid>/ から、pty_raw.bin と
#   events.log を debug/fixtures/<name>/ にコピーし meta.json を書く。
#
# 使い方:
#   debug/record_session.sh <name> [options]
#
# Options:
#   --pid PID         対象 PID（省略時は最新セッション）
#   --from OFFSET     byte 範囲の開始（省略時 0）
#   --to OFFSET       byte 範囲の終了（省略時 EOF）
#   --width N         meta.json に書く初期幅（省略時 events.log 末尾の client_*_resize 値）
#   --height N        meta.json に書く初期高さ
#   --note TEXT       meta.json の description
set -euo pipefail

NAME=""
PID=""
FROM=0
TO=""
WIDTH=""
HEIGHT=""
NOTE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pid) PID="$2"; shift 2 ;;
        --from) FROM="$2"; shift 2 ;;
        --to) TO="$2"; shift 2 ;;
        --width) WIDTH="$2"; shift 2 ;;
        --height) HEIGHT="$2"; shift 2 ;;
        --note) NOTE="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,18p' "$0"; exit 0 ;;
        *)
            if [[ -z "$NAME" ]]; then NAME="$1"; else
                echo "unknown arg: $1" >&2; exit 1
            fi
            shift ;;
    esac
done

if [[ -z "$NAME" ]]; then
    echo "fixture 名が必要です（例: cogitated-duplicate-on-resize）" >&2
    exit 1
fi

LOGS_DIR="$HOME/.claude-master/logs"
if [[ -z "$PID" ]]; then
    PID=$(ls -t "$LOGS_DIR" | head -1)
    if [[ -z "$PID" ]]; then
        echo "ログが見つかりません: $LOGS_DIR" >&2; exit 1
    fi
    echo "対象セッション: $PID (最新)"
fi
SRC="$LOGS_DIR/$PID"
if [[ ! -d "$SRC" ]]; then
    echo "PID ディレクトリが存在しません: $SRC" >&2; exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DST="$REPO_ROOT/debug/fixtures/$NAME"
mkdir -p "$DST"

# pty_raw.bin の長さを取得
RAW="$SRC/pty_raw.bin"
if [[ ! -f "$RAW" ]]; then
    echo "pty_raw.bin が見つかりません: $RAW" >&2; exit 1
fi
RAW_LEN=$(stat -f%z "$RAW" 2>/dev/null || stat -c%s "$RAW")
if [[ -z "$TO" ]]; then TO=$RAW_LEN; fi

if (( FROM < 0 || TO > RAW_LEN || FROM >= TO )); then
    echo "範囲が不正: from=$FROM to=$TO len=$RAW_LEN" >&2; exit 1
fi

# bytes を切り出し
COUNT=$((TO - FROM))
dd if="$RAW" of="$DST/bytes.bin" bs=1 skip="$FROM" count="$COUNT" status=none

# events.log をそのままコピー（pty_raw の chunk 境界 + resize 履歴）
cp "$SRC/events.log" "$DST/events.log" 2>/dev/null || true

# 初期サイズ: --width/--height で明示指定があればそれを使い、なければ最後の resize から推定
DETECTED_W=""
DETECTED_H=""
if [[ -f "$SRC/client_0_resize.bin" ]]; then
    LAST=$(tr '\n' '\0' < "$SRC/client_0_resize.bin" | tr '\0' '\n' | tail -2 | head -1)
    if [[ "$LAST" =~ ^([0-9]+)x([0-9]+)$ ]]; then
        DETECTED_H="${BASH_REMATCH[1]}"
        DETECTED_W="${BASH_REMATCH[2]}"
    fi
fi
WIDTH="${WIDTH:-${DETECTED_W:-120}}"
HEIGHT="${HEIGHT:-${DETECTED_H:-24}}"

# meta.json を書く
cat > "$DST/meta.json" <<EOF
{
  "name": "$NAME",
  "source_pid": "$PID",
  "byte_range": [$FROM, $TO],
  "width": $WIDTH,
  "height": $HEIGHT,
  "note": "$NOTE"
}
EOF

echo "fixture を保存しました: $DST"
echo "  bytes:  $COUNT bytes (offset $FROM..$TO of $RAW_LEN)"
echo "  events: $(wc -l < "$DST/events.log" 2>/dev/null || echo 0) lines"
echo "  size:   ${HEIGHT}x${WIDTH}"
echo ""
echo "次のステップ: snapshot を作る"
echo "  python debug/replay.py $DST/bytes.bin --width $WIDTH --height $HEIGHT \\"
echo "    --events $DST/events.log --out debug/snapshots/$NAME.expected.txt"
