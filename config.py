"""claude-master 設定。

優先度: 環境変数 > 設定ファイル(~/.claude-master.toml) > 既定値。
環境変数キーは大文字（例 SIZE_POLICY）、ファイルキーは小文字
（例 size_policy）。ファイルはフラット、または [claude-master] テーブル
どちらでも可。ファイルが無い/壊れている場合は黙って既定にフォールバック
（依存追加なし: tomllib は Python 3.11+ 標準）。
"""
import os
from pathlib import Path

try:
    import tomllib  # Python 3.11+ 標準
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

# 設定ファイルパス（CLAUDE_MASTER_CONFIG で位置だけ上書き可）
CONFIG_FILE: str = os.environ.get(
    "CLAUDE_MASTER_CONFIG", os.path.expanduser("~/.claude-master.toml"))


def _load_file() -> dict:
    p = Path(CONFIG_FILE)
    if tomllib is None or not p.is_file():
        return {}
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("claude-master"), dict):
        data = data["claude-master"]
    return {str(k).lower(): v for k, v in data.items()}


_FILE = _load_file()


def _raw(key: str, default):
    """env(大文字) > 設定ファイル(小文字) > default。型は変換しない。"""
    ev = os.environ.get(key.upper())
    if ev is not None:
        return ev
    lk = key.lower()
    if lk in _FILE:
        return _FILE[lk]
    return default


def _get_str(key: str, default: str) -> str:
    return str(_raw(key, default))


def _get_bool(key: str, default: bool) -> bool:
    v = _raw(key, default)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() == "true"


def _get_int(key: str, default: int, lo: int, hi: int) -> int:
    v = _raw(key, default)
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


POLL_INTERVAL: int = _get_int("POLL_INTERVAL", 1, 0, 86400)
TMUX_SESSION: str = _get_str("TMUX_SESSION", "claude-master")
INCLUDE_VSCODE: bool = _get_bool("INCLUDE_VSCODE", False)
AUTO_ATTACH: bool = _get_bool("AUTO_ATTACH", False)

LOG_FILE: str = os.path.expanduser("~/.claude-master.log")
STATUS_FILE: str = os.path.expanduser("~/.claude-master.status.json")
PID_FILE: str = os.path.expanduser("~/.claude-master.pid")

LIMIT_WARN_PERCENT: int = _get_int("LIMIT_WARN_PERCENT", 80, 0, 100)
LIMIT_INTERRUPT_PERCENT: int = _get_int("LIMIT_INTERRUPT_PERCENT", 90, 0, 100)

# PTY（= pyte 画面モデル）のサイズ決定方針。
# claude-master は本来 tmux ウィンドウサイズを正とする設計なので、既定は
# client（最後に resize した tmux クライアント = tmux ウィンドウ基準）。
# host（生パススルー＝VSCode 端末のネイティブスクロールバック）は明示的に
# size_policy=host 指定したときだけのオプトイン。
#   client (default): 最後に resize した tmux クライアントを正とする。
#                      claude が tmux サイズで再描画。client 不在時は host
#                      fallback。host/他 client は画面モデルから自分サイズで
#                      viewport 再描画（ミニ tmux。nav-mode で遡れる）
#   host:     host stdin TTY サイズ固定。host=生パススルー=端末ネイティブ
#             スクロールバックで過去ログを読める（PTY==host サイズが条件）
#   largest:  最大端末サイズ。host も client も nav-mode で history を遡れる
#   smallest: 最小サイズ（全端末が全体を見られる安全モード）
#   latest:   host/client 問わず最新の resize
SIZE_POLICY: str = _get_str("SIZE_POLICY", "client").lower()

# 実験的オプトイン: SIZE_POLICY!=host でも host で「端末ネイティブ
# スクロールバック」を得る。pyte HistoryScreen からスクロールアウト確定
# した行（history.top の伸び＝端末エミュレータ自身のグラウンドトゥルース。
# キーワード分類等のヒューリスティックは一切しない）を host へ plain text
# で流し、live 領域は全消去なしで in-place 再描画する。
# 既定 off。SIZE_POLICY=host の生パススルーが構造的に最も確実。
HOST_FLOW_SCROLLBACK: bool = _get_bool("HOST_FLOW_SCROLLBACK", False)


def _parse_nav_key(spec: str) -> bytes:
    """nav-mode トグルキーの指定を 1 バイトの制御コードに変換する。

    既定 Ctrl-\\ (\\x1c) は端末/VSCode/JIS で衝突・誤変換しやすいため
    nav_key で別キーに変更できる。対応形式（大文字小文字無視）:
      - "ctrl-]" / "c-]" / "^]"   → そのキーの制御コード（Ctrl-] = \\x1d）
      - "\\x1d" / "0x1d" / "29"    → 生のコード値
      - 1 文字の生制御文字
    不正・空なら既定 \\x1c。
    """
    import re
    s = (spec or "").strip()
    if not s:
        return b"\x1c"
    low = s.lower()
    m = re.fullmatch(r"(?:ctrl-|c-|\^)(.)", low)
    if m:
        return bytes([ord(m.group(1).upper()) & 0x1F])
    try:
        if low.startswith("\\x"):
            return bytes([int(low[2:], 16) & 0xFF])
        if low.startswith("0x"):
            return bytes([int(low, 16) & 0xFF])
        if low.isdigit():
            return bytes([int(low) & 0xFF])
    except ValueError:
        pass
    if len(s) == 1:
        return s.encode("latin-1", "ignore") or b"\x1c"
    return b"\x1c"


# nav-mode トグルキー（host stdin / socket_client 共通）。既定 Ctrl-\\
# (\\x1c)。JIS/VSCode で \\x1c を出しにくい場合は nav_key="ctrl-]" 等。
NAV_KEY: bytes = _parse_nav_key(_get_str("NAV_KEY", "\\x1c"))

# nav-mode のスクロール速度（host / socket_client 共通）。
# nav_scroll_step: 1 回の ↑↓/j/k で動く行数（既定 1）。例 3 で 3 倍速。
# nav_page_step:   PageUp/PageDown で動く行数（既定 10）。nav_scroll_step
#                  とは独立（矢印を速くしても PageUp/Dn は過剰にならない）。
# Home/End（最古/最新へジャンプ）は速度に依存せず一定。
NAV_SCROLL_STEP: int = _get_int("NAV_SCROLL_STEP", 1, 1, 1000)
NAV_PAGE_STEP: int = _get_int("NAV_PAGE_STEP", 10, 1, 100000)

# PageUp/PageDown を nav-mode に入らずに managed scroll に使う。
# 有効時: PageUp/PageDown 単独で過去ログを nav_page_step 行ずつスクロール
# （claude へ転送しない）。カーソル移動・文字入力など他キーで自動的に
# live(最下部) へ復帰。host(pty_proxy) / client(socket_client) 共通。
# 既定 off（従来は Ctrl-\ / nav キーで nav-mode に入る必要があった）。
PAGEKEY_SCROLL: bool = _get_bool("PAGEKEY_SCROLL", False)

# マウスホイールを nav-mode に入らず managed scroll に使う。
# claude がマウスレポート（?1000h/?1006h 等）を有効化していてホイールの
# エスケープシーケンスが stdin に届く端末でのみ機能（届かなければ無害に
# 何もしない）。有効時はホイール上で過去へ・下で新しい方へ
# nav_wheel_step 行スクロール（claude へは転送しない＝claude 自身の
# ホイール用途は無効になる点に注意。クリック/ドラッグは透過）。
# カーソル移動・文字入力で live 復帰。host/client 共通。既定 off。
WHEEL_SCROLL: bool = _get_bool("WHEEL_SCROLL", False)
NAV_WHEEL_STEP: int = _get_int("NAV_WHEEL_STEP", 3, 1, 1000)

# セッション全文のプレーンテキスト書き出し。
# claude のスクロールアウト確定行（pyte history.top）を逐次ファイルへ
# 追記し、終了時に最終可視画面を flush する。ターミナル描画には一切
# 触れないので live 画面破壊・native scrollback・dedup の問題が無い
# （忠実モデルをそのまま書くだけ。Claude --resume の再ストリームは
# Claude の実出力どおり残る＝内容比較 dedup はしない）。
#   ""/false (既定): 無効
#   true:            自動パス ~/.claude-master/logs/session-<pid>.log
#   <パス>:          そのファイルへ追記
SESSION_LOG: str = _get_str("SESSION_LOG", "").strip()
