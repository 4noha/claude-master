# claude-master

Claude Code セッションを一元管理するプロセス監視・tmux 自動同期ツール。

**PTY プロキシ**経由で Claude を起動することで、複数のターミナルや tmux ペインから同一セッションに双方向接続できます。プロキシは claude の生 ANSI 出力を**そのまま全端末へ中継**（raw passthrough）し、各端末がネイティブに TUI を描画します。

## 機能

| 機能 | 説明 |
|------|------|
| PTY プロキシ | `claude` を仮想端末でラップし、複数クライアントへ I/O を多重化 |
| セッション検出 | `claude` プロセスを定期ポーリングで自動検出 |
| tmux 自動同期 | 新規セッションに対応する tmux ウィンドウを自動作成 |
| 双方向接続 | tmux ウィンドウからも同一 Claude セッションに入力・表示が可能 |
| ダッシュボード | 全セッションのステータスを一覧表示 |
| 自動クリーンアップ | セッション終了時に対応 tmux ウィンドウを削除 |

## 仕組み

```
ターミナル (VSCode / iTerm 等)
  └─ claude-wrap (ラッパースクリプト)
       └─ pty_proxy.py  ←→  ~/.claude-master/sessions/<pid>.sock
            └─ claude (本体)

tmux: claude-master セッション
  ├─ dashboard  … 全セッションのステータス表示
  ├─ <dir-1>    … socket_client.py → 上記ソケットに接続 (双方向)
  └─ <dir-2>    …
```

## セットアップ

### 1. リポジトリをクローン

```bash
git clone git@github.com:4noha/claude-master.git ~/works/claude-master
cd ~/works/claude-master
```

### 2. 仮想環境と依存ライブラリ

```bash
uv venv
uv pip install psutil
```

### 3. tmux のインストール（未インストールの場合）

```bash
brew install tmux
```

### 4. `claude` コマンドをプロキシ経由にする

```bash
# ~/.zshrc (または ~/.bashrc) に追加
echo "alias claude='$HOME/works/claude-master/claude-wrap'" >> ~/.zshrc
echo "alias claude-real='$HOME/.local/bin/claude'" >> ~/.zshrc
source ~/.zshrc
```

| エイリアス | 説明 |
|---|---|
| `claude` | PTY プロキシ経由（tmux 同期・使用量監視が有効） |
| `claude-real` | 本物のバイナリを直接呼び出す（プロキシなし） |

### 5. 監視デーモンを起動

```bash
python monitor.py start
```

これだけで完了です。以降は `claude` と打つと自動的にプロキシ経由で起動し、tmux の `claude-master` セッションに対応ウィンドウが作成されます。

## 使い方

```bash
# Claude を起動（プロキシ経由）
claude

# 監視デーモンの操作
python monitor.py start   # バックグラウンド起動
python monitor.py stop    # 停止
python monitor.py status  # セッション一覧

# tmux ダッシュボードを開く
tmux attach -t claude-master
```

## tmux 構造

```
tmux session: claude-master
├── window 0: dashboard    # 全セッション一覧 (3秒ごと更新)
├── window 1: <dir-name>   # 各 Claude セッションに対応
├── window 2: <dir-name>
└── ...
```

各ウィンドウでは `socket_client.py` が PTY プロキシに接続しており、**元のターミナルと同じ Claude セッション**に対して入力・出力が可能です。

## ファイル構成

```
claude-master/
├── claude-wrap         # ラッパースクリプト (alias の向き先)
├── pty_proxy.py        # PTY プロキシ本体・UNIX ソケットサーバー
├── socket_client.py    # PTY プロキシへの接続クライアント (tmux ウィンドウ用)
├── monitor.py          # セッション監視デーモン
├── process_scanner.py  # Claude プロセス検出ロジック
├── tmux_manager.py     # tmux セッション・ウィンドウ管理
├── dashboard.py        # ダッシュボード表示
└── config.py           # 設定値
```

## 設定 (設定ファイル / 環境変数)

設定は **`~/.claude-master.toml`**（設定ファイル）で行えます。優先度は
**環境変数 > 設定ファイル > 既定値**。ファイルが無い/壊れていれば黙って
既定値で動作します（依存追加なし: `tomllib` は Python 3.11+ 標準）。

```bash
cp claude-master.toml.example ~/.claude-master.toml   # 雛形をコピーして編集
```

設定ファイルのキーは**小文字**（例 `nav_scroll_step = 3`）、同名の環境
変数は**大文字**（例 `NAV_SCROLL_STEP=3`）。フラット記法でも
`[claude-master]` テーブルでも可。位置を変えたいときだけ
`CLAUDE_MASTER_CONFIG` でパス指定。下表の「設定」は環境変数名（小文字に
すると設定ファイルのキー）。反映には claude-master のプロセス再起動が必要。

| 設定 | デフォルト | 説明 |
|------|-----------|------|
| `POLL_INTERVAL` | `1` 秒 | プロセスポーリング間隔 |
| `TMUX_SESSION` | `claude-master` | 管理用 tmux セッション名 |
| `INCLUDE_VSCODE` | `False` | VS Code 拡張セッションも対象にするか |
| `AUTO_ATTACH` | `False` | 起動時に tmux セッションへ自動アタッチするか |
| `REAL_CLAUDE` | `~/.local/bin/claude` | 本物の claude バイナリパス |
| `SIZE_POLICY` | `client` | PTY/画面モデルサイズの決定方針（下記） |
| `HOST_FLOW_SCROLLBACK` | `False` | 実験的オプトイン: `SIZE_POLICY!=host` でも host で端末ネイティブスクロールバックを得る（下記）。有効化は `=true` |
| `NAV_KEY` | `\x1c`（Ctrl-\） | nav-mode トグルキー。`ctrl-]` / `\x1d` / `0x1d` / `^]` 等で指定可。JIS や VSCode で `Ctrl-\` が出しにくいとき変更（例 `NAV_KEY=ctrl-]`） |
| `NAV_SCROLL_STEP` | `1` | nav-mode で ↑↓/j/k 1 回に動く行数。例 `=3` で 3 倍速（host/tmux 共通） |
| `NAV_PAGE_STEP` | `10` | nav-mode で PageUp/PageDown 1 回に動く行数。`NAV_SCROLL_STEP` とは独立（矢印を速くしても PageUp/Dn は過剰にならない）。Home/End は速度非依存 |
| `PAGEKEY_SCROLL` | `False` | `true` で nav-mode（Ctrl-\）に入らず **PageUp/PageDown だけ**で過去ログを `NAV_PAGE_STEP` 行ずつスクロール。カーソル移動・文字入力で自動 live 復帰。host/tmux client 共通 |
| `WHEEL_SCROLL` | `False` | `true` で nav-mode に入らず**マウスホイール**で履歴を遡る（上=過去 / 下=新しい方へ `NAV_WHEEL_STEP` 行）。claude がマウスレポートを有効化しホイールのエスケープが届く端末でのみ機能（届かなければ無害）。クリック/ドラッグは claude へ透過。カーソル移動・文字入力で live 復帰 |
| `NAV_WHEEL_STEP` | `3` | `WHEEL_SCROLL` 有効時、ホイール 1 ノッチで動く行数 |
| `SESSION_LOG` | `""`（無効）| セッション全文をプレーンテキストでファイルへ。`true`→`~/.claude-master/logs/session-<pid>.log`、`<パス>`→そのファイル。逐次追記＋終了時に最終可視画面を flush。ターミナル描画に触れないので live 破壊なし（下記） |

### アーキテクチャ: ハイブリッド（client=tmux 基準 / host オプトイン）

`pty_proxy` は claude 出力を **1 つの忠実な pyte 画面モデル**に食わせる。
claude-master は本来 **tmux ウィンドウサイズを正とする**設計なので既定は
`client`。出力の出し方は `SIZE_POLICY` で決まる:

- **`client`（既定）**: 最後に resize した tmux クライアントを正とし
  PTY をそのサイズに追従（claude が tmux サイズで再描画）。host
  （VSCode ターミナル）と他 client は画面モデルから自分サイズで
  viewport 再描画（ミニ tmux）。host も `Ctrl-\` nav-mode +
  ↑↓/PgUp/PgDn/Home/End/jk で過去ログを遡れる（managed scroll）。
- **`host`（オプトイン）**: PTY = host サイズ固定。host へは claude の
  生バイトをそのまま中継 → 端末の**ネイティブスクロールバック**で
  過去ログを読める（claude 直接起動と同じ）。tmux client は viewport
  再描画。「tmux ではなく VSCode 端末を主に使いログを生で流したい」用途。
- **`largest` 等**: host も client も画面モデルを各自サイズで viewport
  再描画。両方 managed scroll（Ctrl-\）で history を遡れる。

どちらのモードでもサイズの違う複数端末が各々正しく表示され、
二段化・真っ白・resize 崩れ・遅延接続が構造的に解消する。

設計の経緯（同じ轍を踏まないため明記）:
1. 旧: log/footer を**ヒューリスティック分類**して再構成 → 誤判定・
   ダイアログ分断・取りこぼし等の温床（脆い）
2. raw passthrough（生 verbatim 中継）→ 分類バグは消えたが claude は
   絶対座標描画なので**サイズの違う複数端末を同時に正しく出せない**
   （host+tmux で二段化/真っ白/resize 崩れ）。サーバ画面モデル無しの限界
3. **ミニ tmux（現行）**: 分類は一切しない（脆さの根を断つ）が、忠実な
   画面モデルを per-client にそのサイズで再描画。tmux が「完璧」な理由そのもの

これにより host と tmux でサイズが違っても**各々正しく表示**され、
二段化・真っ白・resize 崩れ・遅延接続が構造的に解消する。

### SIZE_POLICY（PTY/画面モデルをどのサイズで保持するか）

| 値 | 動作 |
|----|------|
| `client` (default) | 最後に resize した tmux クライアントを正とする（**tmux ウィンドウ基準**）。claude が tmux サイズで再描画。client 不在時は host fallback。host/他 client はミニ tmux 再描画（Ctrl-\ で遡れる） |
| `host` | PTY = host stdin TTY サイズ固定。host へは**生バイト中継**で端末ネイティブスクロールバックが効く。client はミニ tmux 再描画 |
| `largest` | 最大端末サイズ。host も client も画面モデルを viewport 再描画。両方 Ctrl-\ nav-mode で history を遡れる（managed scroll）。情報が一切欠けない |
| `smallest` | 最小サイズ（全端末が全体を見られるが大画面が活きない） |
| `latest` | host/client 問わず最新の resize |

```bash
claude                       # default = client（tmux ウィンドウサイズが正）
SIZE_POLICY=host claude      # VSCode 端末を主に使い生スクロールバックで読みたいとき
```

### 複数 tmux クライアントへの対応

同じセッションへ複数 `socket_client` を同時接続でき、各クライアントは
自分用の `ScrollRenderer` で画面モデルをそのサイズに再描画して受け取る
（`_client_scrolls[fd]`）。1 つを detach/切断しても他に影響しない。
新規接続時はその場で画面モデルをそのサイズで再描画して送る（tmux の
attach replay 相当。空白画面にならない）。

ナビゲーション（managed scroll）: `socket_client` 側、および `host` 以外
の `SIZE_POLICY` では host 側でも `Ctrl-\` で nav-mode に入り、↑↓ /
PgUp/PgDn / Home/End / j k で画面モデルの history を遡れる。
未スクロール時は claude の active 領域（プロンプト/footer）が見えるよう
画面最下部に追従。再度 `Ctrl-\` で解除。`SIZE_POLICY=host` の host は
生中継なので nav-mode 不要（端末ネイティブスクロールを使う）。

`PAGEKEY_SCROLL=true` なら nav-mode に入らず **PageUp/PageDown だけ**で
同等にスクロールできる（`Ctrl-\` 不要）。`WHEEL_SCROLL=true` なら同様に
**マウスホイール**で遡れる（claude がマウスレポートを有効化しホイールの
エスケープが届く端末でのみ。クリック/ドラッグは claude へ透過）。どちらも
カーソル移動・文字入力で自動的に live(最下部) へ復帰し、そのキーは通常
どおり claude へ送られる。host・tmux client 双方で有効。

> 注: nav の遡り可能行数は `(history行数 + モデル可視行) − viewport行数`。
> `SIZE_POLICY=client` で host が tmux より縦に大きいと host 側はこの差分
> だけ遡り量が目減りする（tmux は viewport==モデルなので影響なし）。host
> でフル history を遡るには `SIZE_POLICY=largest`（モデルが host サイズ）。

### HOST_FLOW_SCROLLBACK（実験的）: host で端末ネイティブスクロールバック

**実験的オプトイン（既定 off）。** 既定の `client`（tmux 基準）では host は
viewport 再描画なので過去ログは `Ctrl-\` managed scroll でしか遡れません
（VSCode のスクロールバーは効かない）。`HOST_FLOW_SCROLLBACK=true` を
付けると、`client`（tmux 基準）のまま host で **VSCode のスクロール
バー**で過去ログが普通に読めます:

```bash
claude                              # 既定（host は viewport 再描画 + Ctrl-\ managed scroll）
HOST_FLOW_SCROLLBACK=true claude    # client 基準のまま host で生スクロールバック
```

仕組み: pyte `HistoryScreen` がスクロールアウトを確定した行（`history.top`
の伸び＝**端末エミュレータ自身のグラウンドトゥルース**。footer キーワード
等のヒューリスティック分類は一切しない）を host へ plain text で流し込み、
live 領域は `\x1b[2J` 全消去なしで in-place 再描画する。これにより VSCode
のスクロールバーで過去ログを普通に読めて、かつ `client`（tmux 基準）の
ままにできる。`Ctrl-\` managed scroll は不要になる（native に委譲）。

**quiescence ゲート**: native scrollback への書き出しは **claude が静止
した瞬間だけ**行う。ストリーミング中は確定行を内部に capture（取りこぼし
防止）するだけで scrollback には書かず、live は安全な全消去再描画で見せる。
claude が一息ついた idle 時にまとめて確定行を scrollback へ流す。これに
より出力バースト中の中間フレームやリサイズ途中の崩れた状態が native
scrollback に**構造的に混ざらない**（リサイズ時は再 arm + clean baseline）。
ストリーミング中の scrollback 反映は claude 一時停止まで遅延するが、live
はリアルタイム更新を維持（流れるログを後で遡る用途では問題なし）。

**既知の制約（重要）**: 合成 display-oracle は緑でも、実 `claude --resume`
録画（`fixtures/resume-burst`）で検証すると、Claude が会話を**再ストリーム**
するため確定行が最大 5 回重複し、入力枠（`❯`/`────`）も scrollback へ流入
することが判明した。これを除くには内容比較 dedup（本プロジェクトが脆さの根
として禁じた分類）が必要で、`HOST_FLOW_SCROLLBACK` は**実 Claude では完全に
は機能しない**。host で確実にログを残したい場合は **`SIZE_POLICY=host`**
（生パススルー＝構造的に唯一クリーン）か、下記 **`SESSION_LOG`**
（ファイル転写＝描画非依存で完全に安全）を使うこと。

### SESSION_LOG: セッション全文をファイルへ（推奨・完全安全）

`SESSION_LOG=true`（または任意パス）で、claude のスクロールアウト確定行を
逐次プレーンテキストでファイルに追記し、**終了時に最終可視画面まで吐き出す**。

```bash
SESSION_LOG=true claude                  # ~/.claude-master/logs/session-<pid>.log
SESSION_LOG=~/claude.log claude          # 任意パス
```

ターミナル描画・native scrollback・live 画面に**一切触れない**（pyte 忠実
モデルをそのままファイルに書くだけ）ので、flow のような破壊は構造的に
起こらない。実 `claude --resume` 録画で「忠実転写・ANSI 無し・終了時
flush」を回帰検証済み。なお Claude 自身の `--resume` 再ストリームは実出力
どおりファイルに残る（内容比較 dedup はしない＝禁手回避）。

`REAL_CLAUDE` は環境変数でオーバーライド可能です:

```bash
REAL_CLAUDE=/usr/local/bin/claude claude
```

## tmux ウィンドウ内での操作

`socket_client.py` が開いている tmux ウィンドウには以下のキーバインドがあります。

| キー | 動作 |
|------|------|
| `Ctrl-\` | ナビゲーションモード ON/OFF。ON 中はキー入力を Claude に転送せず、端末スクロールバックが使える |
| その他のキー | そのまま Claude セッションへ転送 |

## デバッグ

```bash
# PTY プロキシの詳細ログを有効化
PTY_PROXY_DEBUG=1 claude

# 各 I/O チャネルの生バイトをファイルへ記録（~/.claude-master/logs/<pid>/）
PTY_PROXY_LOG=1 claude

# ログ確認
cat ~/.claude-master-proxy.log

# デーモンログ確認
cat ~/.claude-master.log
```

## 前提条件

- macOS (lsof・pty を使用)
- Python 3.11+・`uv`
- `tmux`（`brew install tmux`）
- Claude Code CLI（`~/.local/bin/claude`）

## プラン上限監視・自動リスケジュール

`pty_proxy` が抽出した `usage_percent` / `reset_time` を監視し、上限に近づいたらセッションを中断・リセット時刻に自動再開する機能。

### 動作フロー

```
pty_proxy.py → <pid>.status.json (usage_percent, reset_time)
                        ↓
monitor.py run_loop → LimitWatcher.check()
  ├─ ≥80%  → tmux ウィンドウ名に [⚠80%] 表示（作業継続）
  ├─ ≥90%  → ESC + 要約依頼メッセージをセッションに注入
  │           ResumeScheduler に reset_at を登録
  └─ 100%  → 同上 + ウィンドウ名を [PAUSED] に変更

reset_at 到達後（monitor ループ内で検出）
  → "プランがリセットされました。作業を再開してください。" を注入
  → ウィンドウ名を元に戻す
```

### 設定

| 環境変数 | デフォルト | 説明 |
|----------|-----------|------|
| `LIMIT_WARN_PERCENT` | `80` | 警告表示を出す使用率(%) |
| `LIMIT_INTERRUPT_PERCENT` | `90` | セッション中断を行う使用率(%) |

### 実装ファイル

| ファイル | 役割 |
|----------|------|
| `limit_watcher.py` | しきい値判定・LimitEvent 発行 |
| `resume_scheduler.py` | reset_time パース・pending 管理・ソケット送信 |
| `monitor.py` | LimitWatcher/ResumeScheduler を run_loop に統合 |
| `tmux_manager.py` | `rename_window()` を追加 |
| `config.py` | `LIMIT_WARN_PERCENT` / `LIMIT_INTERRUPT_PERCENT` を追加 |

---

## PTY レンダリング既知課題

### 未修正

現在未修正の課題はありません。

### 修正済み

| コミット | 内容 |
|---------|------|
| HEAD | 「遡り続けると複数箇所のバッファが混ざる」真因修正。`ScrollRenderer` のスクロール位置が**最下部基準**(`_scrollback`)で、遡り中に claude が出力し canvas が伸びるたび表示が下へドリフトしていた。**canvas 先頭からの絶対アンカー**(`_anchor`/`_follow`)に作り直し、末尾追記で view が動かないようにした（tmux copy-mode / pager と同じ）。実 `claude --resume` 録画で「遡り中に history+500 行成長してもドリフト 0」を確認。scroll 系テストを内部 int でなく **render 結果(display-oracle)** 検証に作り直し（`_handle_host_stdin` 経由で実順序＋実描画）。全 240 テスト緑 |
| `b854403` | 非 nav scroll が focus(`?1004h`)等 passive 端末レポートで即 live 復帰し「完璧に壊れる」修正（`is_live_reset_key()` で実操作のみリセット）。全静的 offset・aliasing も実録画で検証し render 自体は正常と確認済み |
| `71fb2d3` | host nav「カーソル最下部までしか遡れない」修正（`_host_pagekey` が nav-mode の ↑↓/jk で毎回 follow_bottom していた非対称を是正。`_handle_host_stdin` 抽出＋実順序統合テスト） |
| `5345803` | （関連だが別要因）`_apply_pty_size` が古い `_host_size` を使う問題を修正（実 host 端末サイズを毎回読み直す）。largest/smallest の追従性改善 |
| `d0e6909` | `WHEEL_SCROLL` 追加: nav-mode に入らずマウスホイールで履歴を遡る（`classify_wheel` で SGR/レガシー判定、クリック/ドラッグは claude 透過、マウスレポート未有効端末では無害 no-op）。host `_host_wheel`/client 共通、`NAV_WHEEL_STEP`。`test_default_policy_is_client` を実 config 非依存に隔離修正 |
| `721b6de` | tmux client で nav-mode を抜けても live に戻らない「抜けられない」バグ修正（nav OFF 時 `socket_client` が follow を proxy へ送らず per-fd ScrollRenderer が貼り付き。host `follow_bottom()` と非対称だったのを是正） |
| `5b05182` | `PAGEKEY_SCROLL` 追加: nav-mode（Ctrl-\）に入らず PageUp/PageDown だけで managed scroll、カーソル移動・文字入力で自動 live 復帰。host(`_host_pagekey`)/client(socket_client) 共通 |
| `08e5d91` | `SESSION_LOG` 追加: セッション全文をプレーンテキストでファイルへ逐次追記＋終了時に最終可視画面 flush。描画非依存で live 破壊なし。実 `claude --resume` 録画で回帰検証。`HOST_FLOW_SCROLLBACK` は実 Claude で dedup 不能（禁手回避）と「既知の制約」明記 |
| `f77c6a7` | `HOST_FLOW_SCROLLBACK` に quiescence ゲート導入（ストリーミング中 capture のみ・idle で `_flush_host_flow`）。※実 Claude では再ストリームで破綻、SESSION_LOG/`SIZE_POLICY=host` を推奨 |
| `4d33ac7` | 設定ファイル `~/.claude-master.toml`(TOML) を追加。優先度 環境変数 > ファイル > 既定。`tomllib`(3.11+ 標準) で依存追加なし、不正/不在は既定へフォールバック。雛形 `claude-master.toml.example`、ローダ単体テスト 8 件 |
| `da7f058` | PageUp/PageDown 速度を `NAV_SCROLL_STEP` から分離（`NAV_PAGE_STEP` 既定 10、独立指定） |
| `ccaff7d` | nav-mode スクロール速度を `NAV_SCROLL_STEP` で可変化（既定 1、↑↓/jk が N 行）。Home/End は速度非依存。host/client 共通 |
| `398bdde` | nav-mode トグルキーを `NAV_KEY` 環境変数で設定可能化。既定 `\x1c`(Ctrl-\\) は JIS/VSCode で `\x1d`(Ctrl-]) 等に化けて nav-mode が反応しない事例があったため。`ctrl-]`/`\x1d`/`0x1d`/`^]` 等を解釈（`config._parse_nav_key`）。host(`pty_proxy`)・client(`socket_client`) 共通 |
| `a10603e` | `HOST_FLOW_SCROLLBACK` 既定を off に戻す（実験的オプトイン）。`c557456` で一旦 on にしたが `SIZE_POLICY=host` の生パススルーが最も確実なため既定は従来動作。有効化は `=true` |
| `9ed51c8` | `HOST_FLOW_SCROLLBACK` 追加。`SIZE_POLICY!=host` でも host で端末ネイティブスクロールバックを得る。pyte `HistoryScreen.history.top` の伸び（端末エミュレータ自身の確定判定＝グラウンドトゥルース。ヒューリスティック分類なし）を `HistoryFlusher` が identity-tracking で抽出し host へ plain text で流し、live 領域は `\x1b[2J` 無し in-place 再描画（`ScrollRenderer.render_flow`）。display-oracle テスト（`test_host_flow_scrollback.py`、10 件）で「scrollback に 1 回だけ・二重 footer 無し」を機械検証 |
| `3848bd3` | `SIZE_POLICY` 既定を `host` → `client` に変更。claude-master 本来の設計どおり **tmux ウィンドウサイズを正**とする（client が resize すると claude が tmux サイズで再描画）。`host`（生スクロールバック）はオプトイン |
| `ff34c1d` | 従来設定（`SIZE_POLICY != host`）でもログをスクロールで遡れる managed scroll を実装。`ScrollRenderer` を `_scrollback` モデル（0=最下部追従 / N>0=history 遡り）化し HistoryScreen の `history.top + 可視 buffer` を結合 canvas として pan。`SCROLL_MAGIC` 復活（socket_client の Ctrl-\ nav-mode → ↑↓/PgUp/PgDn/Home/End/jk）。host も非 host モードで nav-mode 対応。client 入力ディスパッチを `_handle_client_data` に分離（testable seam） |
| (legacy) | フッターキーワード追加: `ctrl+r to search`（↑↓ 履歴検索インジケータ）, `Tip: ctrl+`（操作ヒント Tip 行）。debug/snapshots の LOG セクション網羅検査で発見 |
| `77066f9` | 全角文字 2 カラム幅計上、italic/underline ANSI、history overflow 安全策、footer キーワード絞り込み |
| `c325950` | `_find_footer_start` がカーソル位置のログ行をフッターに引き込む問題 |
| `303ac53` | フッタードリフト・プロンプト改行消失・カーソル誤位置・キュープロンプト表示 |
| `002e343` | SIGWINCH 時ホスト renderer 常時リセット、`_find_footer_start` 二重呼び出し削除、自動再開機構 |
| `9784706` | R1: `_truncate_visible` バッファ末尾の不完全 UTF-8 ガード、R2: `reset()` で `_max_footer_height` リセット、R3: `cursor_y` スキップ時の `_vis_pending_blank` クリア、R4: アイドル時の新規クライアントへ初期状態送信（`_catchup_new_clients`） |
| `b08a1de` | 複数行貼り付け時の Enter 誤実行: `\x1b[?2004h` (bracketed paste) を host/clients に転送。Alt+Enter（`\x1b\r`）は入力を raw 転送しているため追加対応不要 |
| `9e48984` | ログ間の空行消失: `_vis_pending_blank` ガードを `new_visible_logs or _emitted_visible` に拡張。同名ツール2回目ヘッダ消失: `⏺`/`⎿` 行を `_recent_emitted` 重複チェックから免除（スクリーンクリア後は除く） |
| `3f7b756` | `⏺` 前の空行消失: `_is_footer_text` スキップ時に `⏺` 行では `_vis_pending_blank` をクリアしないよう修正。テーブル差分流れ: 可視行の即 emit を `⏺`/`⎿` 行限定に絞り込み |
| `458d069` | テーブル幅拡張の二重 emit: in-place 変化を追跡するとき変化後の内容を `_recent_emitted` に追加し、history から流れてきた最終版が再 emit されないよう修正 |
| `3073634` | ログ行のテキスト切れ: `TerminalRenderer.render()` でログ行に `_truncate_visible` を適用していたため端末幅で切り捨てられていた。ログ行は端末折り返しに任せ、truncate はフッター行のみに限定 |
| `e28a1af` | 遅延 emit (Lazy Emission): 新規可視行を `_pending_visible` で 1 サイクル保留し内容が安定したときのみ emit。ストリーミング中に幅が変わるテーブルは最終版だけがログに出る。行がクリアされる前に pending 内容を救出 emit |

### 設計上の制約（バグではない）

| 事象 | 理由 |
|------|------|
| ホストと tmux で折り返し位置が違う | 端末幅が異なるため折り返し位置が変わる（ログ行は `_truncate_visible` を適用せず端末の折り返しに任せるため意図的） |
| ホストと tmux でフッター上部の空白量が違うことがある | `_max_footer_height` は `TerminalRenderer` インスタンスごとに独立保持 |
| `⏺ Running…` の transient 形式はログに出ない | スピナー行はフッター扱いで、最終完了形（`⏺ TaskName`）が Lazy Emission により 1 サイクル後に emit される（意図的） |

---

## ライセンス

MIT
