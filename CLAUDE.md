# claude-master プロジェクト

Claude Code セッション監視・tmux 自動同期デーモン。

## 自律的な課題対応

`README.md` の `### 未修正` セクションはライブバグトラッカーとして機能している。
ユーザーが Claude Code の端末出力をそのまま貼り付けることでバグを報告する。

会話開始時に README を確認し、未修正課題があれば:
1. ゴミ（ツール出力の断片・テーブルの多重描画等）を除去して課題を特定する
2. `pty_emulator.py` / `pty_constants.py` / `pty_proxy.py` を調査・修正する
3. `### 未修正` を「現在未修正の課題はありません。」に戻す
4. `### 修正済み` に HEAD エントリを追加する
5. CLAUDE.md の関連セクションを更新してから commit する

### proactive バグ調査（debug 環境）

`debug/` 配下のオフラインリプレイ環境で snapshot を網羅検査することで、
ユーザー未報告のバグも見つけられる。手順:

```bash
# 全 fixture を再生して LOG セクションを抽出
for f in debug/snapshots/*.expected.txt; do
  # フッターキーワード候補（ctrl+/shift+/Tab/Esc/Tip:）が LOG に流れていれば漏れバグ
done

# 修正後は fixture を再生して snapshot を更新し pytest 緑を確認
python debug/replay.py debug/fixtures/<name>/bytes.bin \
    --width 164 --height 50 --chunk-size 4096 \
    --out debug/snapshots/<name>.expected.txt
python -m pytest debug/tests/
```

検出された false positive パターン（バグではない）:

| パターン | 理由 |
|---------|------|
| `├──...┤` の連続 | 複数行テーブルの行区切り |
| `… +N lines (ctrl+o to expand)` | ツール結果の省略行（LOG 出力で正しい） |
| `Read N files (ctrl+o to expand)` | `⎿` 配下のサマリ（LOG 出力で正しい） |
| `❯ <user prompt>` | `pty_emulator.py:106-115` で意図的に LOG 出力 |

## アーキテクチャ

- **monitor.py** — メインループ。`ProcessScanner` で差分検出 → `TmuxManager` で同期
- **process_scanner.py** — `ps`/`lsof` で `claude` プロセスを列挙し構造化データを返す
- **tmux_manager.py** — `tmux` コマンドを薄くラップ。セッション・ウィンドウの CRUD
- **dashboard.py** — `curses` または tmux の rename-window で一覧を更新
- **config.py** — 定数のみ。優先度 環境変数 > 設定ファイル
  (`~/.claude-master.toml`、TOML、`CLAUDE_MASTER_CONFIG` で位置変更可) >
  既定値。`_get_str/_get_bool/_get_int` で解決。雛形 `claude-master.toml.example`

## 実装ルール

- 外部プロセス呼び出しは `subprocess.run(..., check=True)` で統一。stdout は `capture_output=True`
- tmux が未起動の場合は `tmux new-session -d -s claude-master` で自動作成
- Claude プロセスの識別キーは PID ではなく `session_id`（`--resume` フラグ値）。PID は再起動で変わる
- VS Code 拡張セッション（`--output-format stream-json`）は TTY を持たないため tmux 同期対象外。`INCLUDE_VSCODE=False` がデフォルト。検出はするがウィンドウは作らない
- ポーリングは asyncio ベースにして将来的に inotify/kqueue に切り替えられるよう設計

## コマンド実行例

```bash
# プロセス一覧取得
ps aux | grep -E 'claude' | grep -v grep

# 作業ディレクトリ取得 (macOS)
lsof -a -p <pid> -d cwd -Fn | grep '^n' | cut -c2-

# tmux 操作
tmux list-windows -t claude-master
tmux new-window -t claude-master -n "<name>"
tmux kill-window -t claude-master:<window>
```

## 権限・前提

- `tmux` がインストール済みであること（`brew install tmux`）
- `lsof` は macOS 標準同梱
- Python 3.11+ / `uv` 環境。依存: `psutil`
- デーモンとして動かす場合は `launchd` plist を `~/Library/LaunchAgents/` に配置

## 端末キー到達性（Mac JIS キーボード / VSCode セットアップ）

nav-mode / PAGEKEY / WHEEL スクロールは「キーが pty_proxy まで届く」のが
前提。届かない事象は claude-master のコードでは直せない（上位レイヤが
横取り）。**切り分けは常に `sed -n l` を実行してキーを押し、エコーが
出るか**で行う（cooked モードゆえ届けば即エコー。出なければ横取り）。

到達性の実測結果（この環境）:

| キー | 到達 | 対処 |
|------|------|------|
| 文字 / `↑↓` / `Ctrl-\`(=`\x1c`) | ✅ 届く | そのまま使える（nav-mode は確実な経路） |
| JIS の `_` キー (`Ctrl-_`) で `\x1c` を出したい | △ 要 Karabiner | 下記ルール |
| `PageUp`/`PageDown` | ❌ VSCode が横取り | 下記 keybindings.json |

### Karabiner: JIS で `Ctrl-_` → `Ctrl-\`(`\x1c` = NAV_KEY)

JIS keycode 実測: `_`=`international1`(Ctrl→`\x1f`)、ANSI`\`=`backslash`
(Ctrl→`\x1d`)、`¥`=`international3`(Ctrl→**`\x1c`** ✅)。よって
`~/.config/karabiner/assets/complex_modifications/` に置いて有効化、
または `karabiner.json` の有効プロファイルに直接（自動リロード）:

```json
{ "type": "basic",
  "from": { "key_code": "international1",
            "modifiers": { "mandatory": ["control"],
                           "optional": ["shift","caps_lock"] } },
  "to":   [ { "key_code": "international3", "modifiers": ["left_control"] } ],
  "conditions": [ { "type": "frontmost_application_if",
    "bundle_identifiers": ["^com\\.microsoft\\.VSCode$",
      "^com\\.apple\\.Terminal$","^com\\.googlecode\\.iterm2$"] } ] }
```
素のシェルで `\x1c` は SIGQUIT だが pty_proxy は raw モードなので
nav-mode トグルとして届く（claude 内でのみ検証すること）。

### VSCode: PageUp/PageDown をターミナルへ送る

VSCode 統合ターミナルは既定で PageUp 等を自分のスクロールに割当て pty へ
送らない。`~/Library/Application Support/Code/User/keybindings.json` に追記
（VSCode 自動リロード・再起動不要）:

```json
{ "key": "pageup",   "command": "workbench.action.terminal.sendSequence",
  "args": { "text": "\u001b[5~" }, "when": "terminalFocus" },
{ "key": "pagedown", "command": "workbench.action.terminal.sendSequence",
  "args": { "text": "\u001b[6~" }, "when": "terminalFocus" }
```
確認: `sed -n l` で PageUp 押下時に `^[[5~` が出れば到達。出ない場合は
無理せず nav-mode（`Ctrl-\`→`↑↓`/`j`/`k`）を使う（設定変更不要・確実）。

### Terminal.app: PageUp/PageDown をターミナルへ送る

Terminal.app も既定で PageUp 等を「自分のスクロールバック1ページ送り」に
割当て pty へ送らない。**Terminal → 設定 → プロファイル → 使用中プロ
ファイル → キーボード**タブで:

1. リストの「+」（既存の `⇞ page up` 行があればダブルクリックで上書き）
2. キー=`page up`、修飾=なし、アクション=**「テキストを送信」**
3. 入力欄で **`Esc` を押す**（`\033` と表示）→ 続けて `[5~` → 確定
   （送出値 `\033[5~`）
4. 同様に `page down`→`\033[6~`、必要なら `home`→`\033[H`・
   `end`→`\033[F`

設定は即反映（Terminal 再起動不要）。確認は VSCode と同じく
`sed -n l`→PageUp で `^[[5~`。`defaults write com.apple.Terminal` での
plist 直編集はプロファイル別・バイナリ plist・キーコード難解で破損
リスクが高く非推奨（GUI 手順を使う）。

どの端末でも横取りされるのは Page/Home/End 等のスクロール系のみ。
`↑↓`/文字/`Ctrl-\`(=`\x1c`) は素で pty に届くので nav-mode は常に確実。

## テスト方法

```bash
# 単体: プロセス検出
python process_scanner.py

# 単体: tmux 操作（claude-master-test セッションで確認）
TMUX_SESSION=claude-master-test python tmux_manager.py

# 統合: デーモン起動して別ターミナルで claude を起動
python monitor.py start
claude  # 別ターミナル
python monitor.py status
```

## pty_proxy.py — ミニ tmux 設計（忠実画面モデル + per-client 再描画）

claude の出力を **忠実な pyte 画面モデル**（1 つ）に食わせ、host と各 socket
client は **自分の端末サイズ**で画面モデルの viewport を再描画して受け取る。
tmux のサーバ側スクリーン + per-client レンダリングと同じ方式。

経緯（重要・同じ轍を踏まないため）:
1. 元: pyte で log/footer を**ヒューリスティック分類**して再構成 →
   footer キーワード誤判定・ダイアログ分断・取りこぼし等の温床（脆い）。
2. raw passthrough（生 verbatim 中継）→ 分類バグは消えたが、claude は
   絶対座標描画なので **サイズの違う複数端末を同時に正しく出せない**
   （host + tmux で二段化／真っ白／resize 崩れ）。これは raw passthrough の
   構造的限界（サーバ側画面モデルが無い）。
3. 現: **ミニ tmux**。分類は一切しない（脆さの根を断つ）が、忠実な画面
   モデルを持ち per-client にそのサイズで再描画する（tmux が「完璧」な理由
   そのもの）。サイズ差・二段化・真っ白・遅延接続を構造的に解決。

### データフロー

```
PTY (claude 出力, 単一 SIZE_POLICY サイズ=client 既定=tmux ウィンドウ)
  → IOLogger.log("pty_raw")
  → TuiEmulator.feed_screen_only(data)   # 忠実 pyte 画面モデル更新（分類しない）
  → _render_all():
       host:   ScrollRenderer.render_viewport(screen, host行, host列)
       client: 各 ScrollRenderer.render_viewport(screen, client行, client列)
  → _maybe_write_status()                # extract_usage()/is_active()
```

### 重要な不変条件

- **分類は一切しない**。log/footer 推測・キーワード判定・lazy emission・
  dedup は存在しない（脆さの根）。あるのは「忠実な端末エミュレート
  （pyte HistoryScreen）＋ viewport 再描画」だけ。だから tmux 同様に堅牢。
- **モード**（`SIZE_POLICY` で決まる。`_host_raw_mode = (SIZE_POLICY=="host")`）。
  claude-master は本来 tmux ウィンドウサイズを正とする設計なので**既定は
  `client`**:
  - `client`（既定）: 最後に resize した tmux クライアントを正とし PTY を
    そのサイズに追従（claude が tmux サイズで再描画。client 不在時は host
    fallback）。host/他 client は `ScrollRenderer.render_viewport` で
    自分サイズで再描画（ミニ tmux）。
  - `host`（オプトイン）: host は claude 生バイトを verbatim 中継 →
    VSCode のネイティブスクロールバックで過去ログを読める（簡単・確実）。
    成立条件 PTY==host サイズ。client は viewport 再描画。
  - `largest`/`smallest`/`latest`: host も client も
    `ScrollRenderer.render_viewport` で各自サイズで再描画（ミニ tmux）。
- **managed scroll（host 以外モード）**: `ScrollRenderer._scrollback`
  （0=最下部 live 追従、N>0=N 行遡り）。`scroll(dy)` で history を pan
  （dy<0=古い方/上、dy>0=新しい方/下、0 で follow に復帰）。render は
  `history.top + 可視 buffer` を連結した論理 canvas から viewport を切る。
  follow 時は history を materialize しない高速パス。
  - client: `socket_client` が nav-mode（Ctrl-\）中の ↑↓/PgUp/PgDn/Home/
    End/jk を `SCROLL_MAGIC + !h(dy)` で proxy へ。proxy が該当 fd の
    ScrollRenderer を pan し即再描画。
  - host: `pty_proxy` の stdin 経路で Ctrl-\ で `_host_nav_mode` トグル、
    スクロールキーで `_host_scroll` を pan（claude へ転送しない）。
  - nav トグルキーは `config.NAV_KEY`（既定 `\x1c`=Ctrl-\）。JIS/VSCode で
    `\x1c` が `\x1d`(Ctrl-]) 等に化けて反応しないとき `NAV_KEY=ctrl-]` 等で
    変更。host/client 双方が config 経由で同じ値を読む。
  - スクロール速度: ↑↓/jk が `config.NAV_SCROLL_STEP` 行（既定 1）、
    PageUp/Dn が `config.NAV_PAGE_STEP` 行（既定 10、step とは独立）、
    Home/End は速度非依存（±1000000 固定）。`_SCROLL_KEYS`/
    `_HOST_SCROLL_KEYS` を起動時に生成。不正値は既定へフォールバック。
  - **`PAGEKEY_SCROLL`（既定 off）**: nav-mode に入らず PageUp/PageDown
    単独で managed scroll。host は `_host_pagekey()`（`_loop` の raw/flow
    判定の直後・`_NAV_KEY` 判定の前で呼ぶ。PageUp/Dn は消費、他キーは
    scrollback>0 なら `follow_bottom()`＋再描画して False→通常転送＝
    カーソル移動/文字入力で live 復帰）、client は socket_client が
    `_PGUP/_PGDN`→`SCROLL_MAGIC`、他キーで `_FOLLOW_DY`(=32767) を送って
    live 復帰。nav-mode とは独立に常時併用可。
  - **`WHEEL_SCROLL`（既定 off）**: マウスホイールを同様に managed scroll。
    `pty_scroll.classify_wheel()` が SGR(`\x1b[<Cb;..[Mm]`)/レガシー
    (`\x1b[M b0..`) を判定（Cb bit6=拡張・bit5=motion、修飾無視。
    クリック/ドラッグは None＝claude 透過）。host は `_host_wheel()` を
    `_host_pagekey()` より先に呼ぶ。claude がマウスレポート未有効ならバイトが
    届かず無害に no-op。`NAV_WHEEL_STEP`（既定 3）行/ノッチ。
  - nav の遡り上限は `(len(history.top)+screen.lines)−viewport rows`。
    `SIZE_POLICY=client` で host>モデル(tmux)だと host はこの差分だけ
    遡れる量が減る（tmux は viewport==モデルで満杯。構造的・バグでない）。
    host で全 history 遡るには `largest`/`host`（モデル=host サイズ）。
- **`HOST_FLOW_SCROLLBACK`（実験的オプトイン・既定 off。有効化は `=true`）**:
  `SIZE_POLICY!=host` でも host で端末ネイティブスクロールバックを得る。
  `HistoryFlusher`
  が pyte `HistoryScreen.history.top` の伸び（= 端末エミュレータ自身が
  確定スクロールアウトと判定した行。identity-tracking で delta 抽出。
  **footer キーワード等のヒューリスティック分類はしない**＝「分類は
  一切しない」不変条件を破らない）を取り出し、`ScrollRenderer.render_flow`
  が確定行を host へ plain text で流して 1 行ずつ native scrollback へ
  送り、live 領域は `\x1b[2J` 無しで in-place 再描画する。flow モード時は
  managed scroll(nav-mode) は無効（native scrollback に委譲＝stdin は
  raw 同様 claude へ全転送）。
  **quiescence ゲート**（遷移中の崩れを native scrollback に混ぜない）:
  `_broadcast`（ストリーミング中）は `HistoryFlusher.capture()` で確定行を
  内部 `_pending` に蓄積するだけ（毎フレーム必須＝pyte history.top の
  maxlen 取りこぼし防止。dict 参照を list に足すだけで安価）、live は安全な
  `render_viewport`（全消去）で見せ scrollback には書かない。`_loop` の
  idle tick（select タイムアウト＝claude 静止）で `_flush_host_flow()` が
  `drain()`→`render_flow` し、**静止画面の確定行だけ**を scrollback へ
  書き出す。リサイズ時は `_apply_pty_size` がサイズ実変化を検出して
  `HistoryFlusher.reset()`（identity 再 arm・`_pending` は保持）＋
  `_host_flow_first=True`（次 flush で clean baseline）。回帰検知は
  display-oracle（`debug/tests/test_host_flow_scrollback.py`。
  「ストリーミング中 scrollback 空 → idle で確定行のみ・重複/欠落/二重
  footer 無し」「リサイズ跨ぎ非破壊」を機械検証）。
  **既知の制約**: 実 `claude --resume` 録画（`fixtures/resume-burst`）検証で
  Claude が会話を再ストリームし確定行が最大 ×5 重複・入力枠流入が判明。
  除去には内容比較 dedup（禁じた分類）が必要で flow は実 Claude では不完全。
  host で確実にログを残すなら `SIZE_POLICY=host` か下記 `SESSION_LOG`。
- **`SESSION_LOG`（ファイル転写・描画非依存で完全安全）**: `config.SESSION_LOG`
  が真なら `_open_session_log()` がファイルを追記オープン。`_broadcast` 毎に
  `_session_log_capture()` が `HistoryFlusher.capture/drain` ＋ `line_to_text`
  で確定行をプレーンテキスト追記、`run()` finally の `_finalize_session_log()`
  が最終可視 buffer まで書いて閉じる。ターミナル/native scrollback/live に
  一切触れない（忠実モデルをそのまま書くだけ）ので flow のような破綻が
  構造的に起こらない。Claude の再ストリームは実出力どおり残す（dedup
  しない＝禁手回避）。回帰は実録画で `debug/tests/test_session_log.py`。
- 毎フレーム全画面再描画（先頭 `\x1b[?2026h` synchronized + `\x1b[2J`）。
  前画面/前セッション残留は構造的に消える（二段化・真っ白なし）。
- `_catchup_new_clients`: 新規接続 client にその場で画面モデルを
  client サイズで再描画して送る（tmux の attach replay 相当）。
- pyte 無し環境のみ `_fallback_raw_broadcast`（生中継）に退避。

### legacy: 旧ヒューリスティック再構成

`pty_emulator._extract()` / `pty_renderer.py` / `pty_constants` の footer
判定群（`_is_footer_text`/`_find_footer_start` 等）は **本番未使用**。
`debug/replay.py` と回帰 fixture の検証用に残置。**新機能でこれらに
依存しないこと**（分類は二度と本番に入れない＝脆さの再来防止）。
`pty_scroll.ScrollRenderer` は legacy ではなく**ミニ tmux のコア（本番）**。

## テストの本番経路 / legacy 区分

`debug/tests/` は 2 系統。`pytest -m "not legacy"` で本番経路だけ回せる。

| 種別 | ファイル | 検証対象 |
|------|---------|---------|
| **本番** | `test_display_oracle.py` | 実 PtyProxy + socket + pyte ディスプレイ。dirty terminal × attach クリアを *描画結果* で検証（出力バイトではなく「画面に何が見えるか」） |
| **本番** | `test_multi_client.py` | raw passthrough verbatim 中継・複数 client・drop |
| **本番** | `test_pty_proxy_size.py` | SIZE_POLICY 適用・pyte screen resize |
| **本番** | `test_size_policy.py` | resolve_pty_size 純関数 |
| legacy | 上記以外（`@pytest.mark.legacy`） | 旧 reconstruction（_extract/snapshot/footer 判定）。本番未使用 |

ディスプレイ・オラクル（`debug/display_oracle.py`）の要点: pyte を
*再構成器ではなくユーザーの端末ディスプレイ* として使い、
`seed_prior_content()` で「接続前に端末に残っていた内容」を再現してから
pty_proxy 出力を流し、**描画後の画面**を検証する。出力バイト検証では
構造的に取れなかった「端末事前状態 × レンダリング」系バグ（二重 footer 等）を
機械検知できる。

### claude --resume 二重 footer の真因（重要）

`resume-burst` 録画の生バイトには `\x1b[2J` クリアが **1 つも無い**。
claude --resume は画面クリアせず**絶対カーソル位置**で会話履歴を再描画する。
そのため:
- 表示端末サイズが claude の想定（PTY サイズ）と違うと絶対座標がずれ
  アイドル footer と最終 footer が両方残って二重に見える
- 接続前の前セッション残留も、claude が自分でクリアしないので残る

対策は 3 段（すべて one-time、毎フレーム掃除ではない＝tmux の挙動と同じ）:
1. **起動レース**: `main()` が fork 前に host サイズを取得し、子の execv
   *前* に slave PTY を TIOCSWINSZ。claude が default 24x80 で初回描画する
   のを防ぐ（完全レースフリー）
2. **attach**: 新規 client 接続時にクリア + **直近生出力の replay**。
   raw passthrough にはサーバ側画面モデルが無く、遅れて接続した
   クライアント（monitor が後から socket_client 起動）は claude の出力を
   取り逃して真っ白になる。`_recent_raw`（直近 `_RECENT_RAW_CAP`=256KB の
   リング）を保持し attach 時に replay。SIGWINCH は PTY サイズ不変だと
   claude が再描画しないので当てにできず、replay が主・SIGWINCH は保険。
3. **PTY サイズ変化**: `_apply_pty_size` がサイズ実変化を検知したら
   `_pending_clear` を立て、メインループの `_flush_pending_clear()` が
   host + 全 client を 1 回クリア + claude へ SIGWINCH。tmux client が
   resize して PTY サイズが変わったとき旧サイズ footer が残るのを防ぐ。

signal ハンドラ文脈で socket 送信は危険なので、サイズ変化はフラグだけ立て
ループ側で送出する。

### クリアシーケンスは `_CLEAR_SEQ = \x1b[2J\x1b[9999;1H`（重要）

クリアは `\x1b[H\x1b[2J`（カーソル最上部）ではなく
`\x1b[2J\x1b[9999;1H`（全消去後カーソルを画面**最下部**左端へ）を使う。

claude（`--resume` 含む）は **カーソルが最下部にある前提**で
「下から描画 + スクロールアップ」する。クリア後にカーソルが最上部だと
絶対座標/相対描画がずれて画面が崩れる（ユーザー実測で「最下部に
カーソルが来ていればきれいに表示された」と確定）。行番号 9999 は各端末が
自分の実サイズへ clamp するので host と tmux でサイズが違っても各々の
最下部に正しく落ちる。3 箇所（host 起動 / attach / サイズ変化）すべて
この共通定数を使う。変更時は必ず「最下部カーソル」を維持すること。

## よくある問題

| 問題 | 原因 | 対処 |
|------|------|------|
| `lsof` が cwd を返さない | SIP で保護されたプロセス | `ps -p <pid> -o command=` からパスを推定 |
| tmux セッションが二重作成 | 起動チェック漏れ | `tmux has-session -t claude-master` を必ず先行実行 |
| VS Code セッションが混入する | フィルタ漏れ | `--output-format stream-json` の有無でフィルタ |
