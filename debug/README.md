# debug/ — claude-master 自律デバッグ環境

`pty_emulator.py` / `pty_proxy.py` の TUI レンダリングバグを **claude を起動せず** に再現・修正・回帰テスト化するためのオフライン環境。

## ゴール

ユーザーが README の `### 未修正` に貼ったバグ報告から、`claude` を再起動せずに:

1. 該当バイトを fixture 化
2. `replay.py` で再現
3. 修正を入れて snapshot diff
4. pytest で回帰固定
5. README を `### 修正済み` に移行

までを完結させる。

## 構成

```
debug/
├── replay.py              ★ pty_raw.bin を TuiEmulator に流して emit を決定的に再現
├── pipeline_replay.py     ★ host + client 2 レンダラ + 2 pyte で divergence 検出
├── client_replay.py       ★ client_*_out.bin（proxy → tmux 出力）を pyte で復元
├── record_session.sh      ★ ~/.claude-master/logs/<pid>/ から fixture 化
├── mock_claude.py         tmux ハーネス用 mock claude（録画 bytes を stdout に流す）
├── tmux_harness.py        Layer 3: 実 tmux 環境でのリプロ（resize/scroll/dual-pane/nav-toggle/detach）
├── fixtures/<name>/
│       ├── bytes.bin      pty_raw のスライス
│       ├── events.log     録画時の chunk 境界（任意で再現用）
│       └── meta.json      width/height/note
├── snapshots/<name>.expected.txt   replay.py の期待出力
└── tests/
    ├── test_is_footer_text.py    footer keyword 全網羅
    ├── test_tui_emulator.py      Lazy Emission ほか不変条件
    ├── test_replay_fixtures.py   fixture 全件回帰
    ├── test_log_leak.py          LOG への footer 漏れ自動検出
    └── test_pipeline_replay.py   host vs client divergence 検出
```

## 使い方

### 1. オフライン再現（Layer 1）

PTY_PROXY_LOG=1 で録画済みのセッションがあれば、それを fixture に切り出す:

```bash
# 最新セッション全体を fixture 化
debug/record_session.sh my-bug --note "再現メモ"

# 一部だけ切り出し（offset 単位）
debug/record_session.sh cogitated-issue --pid 76406 --from 250000 --to 360000
```

replay.py で `TuiEmulator` を通す:

```bash
python debug/replay.py debug/fixtures/my-bug/bytes.bin \
    --width 164 --height 50 --chunk-size 4096 \
    --out debug/snapshots/my-bug.expected.txt

# 後で再生して差分検出
python debug/replay.py debug/fixtures/my-bug/bytes.bin \
    --width 164 --height 50 --chunk-size 4096 \
    --diff debug/snapshots/my-bug.expected.txt
```

主要オプション:
- `--width N` / `--height N` — 仮想端末サイズ
- `--chunk-size K` — K バイト境界で feed（フレーム断片化バグの再現に必須）
- `--events FILE` — events.log の pty_raw 行サイズを境界に使う（録画時を完全再現）
- `--resize-at "OFFSET:RxC[,OFFSET:RxC]"` — 指定オフセットで `screen.resize()` 発火
- `--diff FILE` — snapshot と比較し不一致なら exit 1

### 2. pytest 回帰（Layer 2）

```bash
python -m pytest debug/tests/ -v
```

カバー範囲:

| ファイル | 内容 |
|---------|------|
| `test_is_footer_text.py` | `_FOOTER_KEYWORDS_TEXT` の全エントリ + 入力プロンプト / 罫線 / スピナー / 完了 ⏺ 行 |
| `test_tui_emulator.py` | Lazy emission の 1 サイクル保留 / 全角文字 / フッター行非 emit / resize 後の重複防止 / `extract_usage` |
| `test_replay_fixtures.py` | `fixtures/` 全件を chunk-size 4096 で回し snapshot と完全一致 |

新しいバグを修正したら必ず fixture + snapshot を追加し、pytest に固定する。

### 2.5. tmux 側パイプライン検査（Layer 1 の拡張）

ホスト stdout と tmux client の renderer は独立した `TerminalRenderer` インスタンスで、
幅違いから divergence する可能性がある。これを offline で検証:

```bash
# 同幅で divergence 0 を確認（pipeline 自体に渋滞がないか）
python debug/pipeline_replay.py debug/fixtures/<name>/bytes.bin \
    --rows 40 --host-cols 100 --client-cols 100

# 異幅で host が truncate しているか確認
python debug/pipeline_replay.py debug/fixtures/<name>/bytes.bin \
    --rows 40 --host-cols 80 --client-cols 140 --diff-screens
```

`pytest debug/tests/test_pipeline_replay.py` が全 fixture で:
  - 同幅 → divergence 0
  - host_cols(80) < client_cols(140) → host 単語集合は client のサブセット

を自動チェックする。これらが崩れたら `TerminalRenderer` 側のバグ可能性が高い。

さらに、実セッションで記録された renderer 出力そのものをレンダーし戻す:

```bash
# 録画済みの host_out.bin と client_0_out.bin を同条件で比較
python debug/client_replay.py ~/.claude-master/logs/<pid>/client_0_out.bin \
    --compare ~/.claude-master/logs/<pid>/host_out.bin \
    --cols 140 --rows 40
```

### 3. tmux 環境再現（Layer 3, 限定用途）

resize / mouse-wheel / dual-terminal など実環境固有のバグ用:

```bash
# 単一ペインで起動して保持
python debug/tmux_harness.py --fixture footer-recovery --width 120 --height 40 --keep
tmux attach -t claude-master-debug   # 手動で挙動を観察

# resize を途中で発火
python debug/tmux_harness.py --fixture cogitated-render \
    --width 120 --height 40 --resize-to "1.5:24x80"

# dual-pane: pane 0 が pty_proxy (host)、pane 1 が socket_client (tmux client)
python debug/tmux_harness.py --fixture sample-recent --width 140 --height 40 --dual --keep

# スクロールアップ（マウスホイール相当）
python debug/tmux_harness.py --fixture sample-recent --scroll 3 --keep

# socket_client 側 Ctrl-\ ナビゲーションモードの検証
python debug/tmux_harness.py --fixture sample-recent --dual --nav-toggle

# detach 中の状態保持を検証
python debug/tmux_harness.py --fixture sample-recent --detach-test
```

**決定性**: tmux capture-pane は録画 fixture のフレーム/スピナータイミングに依存しがち。
以下 2 つの仕組みで安定化:

1. `--mock-delay 0.02`（default）で mock_claude の chunk 間 sleep を強制
2. `--normalize` で spinner 文字 / 経過時間表記を平準化してから diff

組み合わせて `--diff <golden> --normalize` を使えば連続実行で同一 MD5 が得られる。

## 自律修正ループ

ユーザーが README に貼ったバグ報告から:

```
1. PTY_PROXY_LOG=1 のログから該当 PID/byte 範囲を特定（events.log 参照）
2. debug/record_session.sh <name> --pid <PID> --from <OFF> --to <OFF>
3. python debug/replay.py debug/fixtures/<name>/bytes.bin --width N --height M
   → 出力に修正前のバグが見えるか確認
4. snapshot を broken として保存
   --out debug/snapshots/<name>.broken.txt
5. pty_emulator.py / pty_constants.py を修正
6. 再度 replay → expected として保存
   --out debug/snapshots/<name>.expected.txt
7. tests/test_replay_fixtures.py が自動で pickup
8. pytest 緑を確認 → commit
9. README ### 未修正 → ### 修正済み
```
