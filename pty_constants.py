"""TUI 解析・描画で共通使用する定数・正規表現・純粋関数。"""
import re
import unicodedata

# ── カーソル移動 ANSI 変換 ─────────────────────────────────────────────────
# cursor right (`\x1b[<n>C`) は単語間スペース代わりに使われるためスペース展開
_CURSOR_RIGHT_RE = re.compile(rb"\x1b\[(\d*)C")

_CURSOR_OPS_RE = re.compile(
    rb"(?:"
    rb"\x1b\[(?:"
    rb"[0-9;]*[ABDEFGHJKSTf]"    # カーソル移動 / erase / scroll（C は別処理）
    rb"|[su]"                      # カーソル save/restore
    rb"|\?[0-9;]*[hl]"            # プライベートモード（代替スクリーン等）
    rb"|[0-9;]*[@P`]"             # 文字挿入/削除
    rb")"
    rb"|\x1b[78MDH]"              # VT100 カーソル/スクロール
    rb")\r?\n?"                    # 直後の構造的 \r\n も除去
)


def _expand_cursor_right(data: bytes) -> bytes:
    """\\x1b[<n>C を n 個のスペースに展開（Claude Code が単語間スペースとして使う）。"""
    def _repl(m: re.Match) -> bytes:
        n_str = m.group(1)
        n = int(n_str) if n_str else 1
        return b" " * min(n, 200)
    return _CURSOR_RIGHT_RE.sub(_repl, data)


# 水平 box-drawing 文字の UTF-8 バイト列（─ ═ ━ ╌ ╍ ┄ ┈）の連続にマッチ
_H_BOX_RE = re.compile(
    rb"(?:\xe2\x94\x80|\xe2\x95\x90|\xe2\x94\x81"
    rb"|\xe2\x95\x8c|\xe2\x95\x8d|\xe2\x94\x84|\xe2\x94\x88){3,}"
)

# 行全体が水平罫線・空白だけかどうか
_H_BOX_LINE_RE = re.compile(
    rb"^(?:\xe2\x94\x80|\xe2\x95\x90|\xe2\x94\x81"
    rb"|\xe2\x95\x8c|\xe2\x95\x8d|\xe2\x94\x84|\xe2\x94\x88|\s)+$"
)

_ANSI_COLOR_RE = re.compile(rb"\x1b\[[0-9;]*m")

# Claude Code の TUI chrome 行を識別するためのキーワード
_CHROME_KEYWORDS = (
    b"bypass permissions",
    b"shift+tab to cycle",
    b"ctrl+t to show",
    b"esc to interrupt",
    b"accept edits",
    b"plan mode",
    b"\xe2\x8f\xb5\xe2\x8f\xb5",   # ⏵⏵
)

# Thinking アニメーション/進捗 status 行を識別するキーワード
_STATUS_KEYWORDS = (
    b"tokens",
    b"thought for",
    b"context left",
    b"Running",
    b"Tinkering",
    b"Brewed",
    b"Working",
    b"Transfiguring",
    b"Pondering",
    b"Cogitating",
    b"thinking",
)


def _is_status_line(line: bytes) -> bool:
    """Thinking アニメーション・進捗・spinner 行を検出（footer 扱いで in-place 表示）。"""
    plain = _ANSI_COLOR_RE.sub(b"", line).strip()
    if not plain or len(plain) < 3:
        return False
    for kw in _STATUS_KEYWORDS:
        if kw in plain:
            return True
    # 先頭が dingbat 範囲（U+2700-U+27BF）の短い行はスピナー類
    if len(plain) < 100 and plain[0] == 0xE2 and plain[1] in (0x9C, 0x9D, 0x9E):
        if not plain.startswith(b"\xe2\x9d\xaf"):  # ❯（入力プロンプト）は除外
            return True
    return False


# footer 行の安全な最大幅（端末折り返しによる cursor 制御のズレを防止）
_FOOTER_MAX_COLS = 80

_ANSI_ALL_RE = re.compile(rb"\x1b\[[0-9;:?<>]*[\x40-\x7e]|\x1b\][^\x07]*\x07|\x1b.")


def _truncate_visible(line: bytes, max_cols: int) -> bytes:
    """ANSI 制御列を保持したまま可視文字数を max_cols に収める。
    全角文字（East Asian Width W/F）は 2 カラム幅として計上する。
    """
    out = bytearray()
    visible = 0
    i = 0
    n = len(line)
    while i < n:
        m = _ANSI_ALL_RE.match(line, i)
        if m:
            out.extend(m.group())
            i = m.end()
            continue
        b = line[i]
        if b < 0x80:
            char_len = 1
            char_width = 1
        elif b & 0xE0 == 0xC0:
            char_len = 2
            char_width = 1
        elif b & 0xF0 == 0xE0:
            char_len = 3
            try:
                ch = line[i:i + 3].decode("utf-8")
                char_width = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            except (UnicodeDecodeError, IndexError):
                char_width = 1
        elif b & 0xF8 == 0xF0:
            char_len = 4
            try:
                ch = line[i:i + 4].decode("utf-8")
                char_width = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            except (UnicodeDecodeError, IndexError):
                char_width = 1
        else:
            char_len = 1
            char_width = 1
        if visible + char_width > max_cols:
            break
        if i + char_len > n:  # バッファ末尾の不完全シーケンスは emit しない
            break
        out.extend(line[i : i + char_len])
        i += char_len
        visible += char_width
    if i < n:
        out.extend(b"\x1b[0m")
    return bytes(out)


def _is_chrome_line(line: bytes) -> bool:
    """status / 区切り / 入力プロンプト等の TUI chrome 行を検出。"""
    plain = _ANSI_COLOR_RE.sub(b"", line).strip()
    if not plain:
        return False
    if plain.startswith(b"\xe2\x9d\xaf"):  # ❯
        return True
    if _H_BOX_LINE_RE.match(plain):
        return True
    for kw in _CHROME_KEYWORDS:
        if kw in plain:
            return True
    return False


# footer 候補 prefix（spinner 等の dingbat 行）
# "·" (U+00B7 MIDDLE DOT) は Claude Code が "· Percolating…" / "· Crunched for 3m 50s" 等で使用
_FOOTER_SPINNER_CHARS = frozenset("✻✽✶✷✸✹✺✢✣✤✥✦✧✩✪✫✬✭✮✯✰✱✲✳✴✵·")

# アクティブ作業を示す行の先頭文字（spinner + ⏺ サブエージェント実行中マーク）
_ACTIVE_PREFIX_CHARS = _FOOTER_SPINNER_CHARS | frozenset("⏺")

# spinner を伴わないがアクティブ状態を示すキーワード
_ACTIVE_FOOTER_KEYWORDS = (
    "esc to interrupt",
    "Do you want to proceed?",
    "accept edits",
    "don't ask again for:",
)

_FOOTER_KEYWORDS_TEXT = (
    "bypass permissions", "shift+tab to cycle", "ctrl+t to show",
    "esc to interrupt", "accept edits", "plan mode", "⏵⏵",
    "context left",
    "session limit", "You've used", "/upgrade to keep",
    "You've hit your limit", "What do you want to do",
    "Enter to confirm", "Esc to cancel",
    "Stop and wait for limit", "Upgrade your plan",
    "Tip: Use /btw", "/btw to ask",
    "Press up to edit queued",
    "Press Ctrl-C again",
    "Do you want to proceed?",
    "don't ask again for:",
    "Tab to amend", "↑↓ to explain",
    "ctrl+x to view diff",
    "ctrl+r to search",      # 履歴検索インジケータ（↑↓ 履歴ナビゲーション中に表示）
    "Tip: ctrl+",            # 操作ヒント Tip 行（"Tip: ctrl+s to ..." 等）
)

# 選択肢ダイアログの option 行（"1. Yes" / "❯ 2. No" / "  3. Yes, and don't ..."）
_CHOICE_OPTION_RE = re.compile(r"^\s*(?:❯\s*)?\d+\.\s+\S")

# 選択肢ダイアログの question 行を示すマーカー（このどれかが近傍にあれば
# 数字 option 行群は footer ブロックとして扱う）
_DIALOG_QUESTION_MARKERS = (
    "Do you want to proceed?",
    "What do you want to do",
    "Do you want to make this edit",
    "Do you want to create",
    "don't ask again for:",
    "Would you like",
)

# diff / コード行パターン: 行番号 + 任意の +/- + 本文
#   " 276 -    \"You've hit your limit\", ..."  (削除行)
#   "  48 +        parts: list[bytes] = []"     (追加行)
#   "  63          for line in log_lines:"      (文脈行: 行番号 + 空白のみ)
# これらはツール diff のソースコード表示であり、たとえ行内に footer
# キーワード文字列（"You've hit your limit" 等）を含んでも footer chrome では
# ない。これを footer 判定すると pty_constants.py 自身の diff 等で
# ログ消失が起こる。
_DIFF_LINE_RE = re.compile(r"^\s*\d+\s+(?:[-+]\s|\s{2,}|[-+]?\t)")

# 進行中 status の末尾パターン（"… (ctrl+o to expand)" や "…" で終わる）
_TRANSIENT_TAIL_RE = re.compile(r"…\s*(\([^)]*\))?\s*$")

# ⏺ agent 行でアクティブ状態を示す語（… なしで使われることがある）
_AGENT_ACTIVE_WORDS = (
    "Running", "Tinkering", "Brewed", "Working",
    "Transfiguring", "Pondering", "Cogitating",
)


def _is_footer_text(text: str) -> bool:
    """text が footer chrome（区切り・status・入力プロンプト・spinner 等）か判定。"""
    s = text.strip()
    if not s:
        return False
    if all(c in "─━═╌╍┄┈" or c == " " for c in s):
        return True
    if s.startswith("❯"):
        return True
    if s[0] in _FOOTER_SPINNER_CHARS:
        return True
    # ⏺ (agent/tool indicator): アクティブ状態のみ footer（完了行はログへ流す）
    if s.startswith("⏺"):
        if _TRANSIENT_TAIL_RE.search(s):
            return True
        return any(w in s for w in _AGENT_ACTIVE_WORDS)
    # diff / コード行は、行内に footer キーワード文字列を含んでいても
    # footer chrome ではない（ツールの diff 表示なのでログに流す）。
    # キーワード照合より前に弾く。
    if _DIFF_LINE_RE.match(text):
        return False
    for kw in _FOOTER_KEYWORDS_TEXT:
        if kw in s:
            return True
    if _TRANSIENT_TAIL_RE.search(s):
        return True
    return False


def _to_log_view(data: bytes, host_cols: int) -> bytes:
    """TUI カーソル操作を除去し、スクロールログ表示に変換する。色コードは保持。"""
    import re as _re
    out = _expand_cursor_right(data)
    out = _CURSOR_OPS_RE.sub(b"", out)
    out = _re.sub(rb"\r(?!\n)", b"", out)
    def _repl(m: re.Match) -> bytes:
        char3 = m.group()[:3]
        count = len(m.group()) // 3
        return char3 * min(count, host_cols)
    out = _H_BOX_RE.sub(_repl, out)
    if not _re.sub(rb"\x1b\[[0-9;]*m|\s", b"", out):
        return b""
    return out
