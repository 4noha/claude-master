"""TUI エミュレータ: pyte ベースの TuiEmulator と regex フォールバックの TuiParser。"""
import collections
import logging
import re

from pty_constants import (
    _ACTIVE_FOOTER_KEYWORDS,
    _ACTIVE_PREFIX_CHARS,
    _ANSI_ALL_RE,
    _ANSI_COLOR_RE,
    _CHOICE_OPTION_RE,
    _CHROME_KEYWORDS,
    _DIALOG_QUESTION_MARKERS,
    _H_BOX_LINE_RE,
    _TRANSIENT_TAIL_RE,
    _is_chrome_line,
    _is_footer_text,
    _is_status_line,
    _to_log_view,
)

_log = logging.getLogger("pty_proxy")

try:
    import pyte
    _PYTE_AVAILABLE = True
except ImportError:
    _PYTE_AVAILABLE = False


class TuiEmulator:
    """pyte で仮想スクリーンを保持し、log 行と footer 行を取り出す。

    Claude Code は cursor 絶対位置でフレームを部分描画するため、ストリームから
    log/footer を直接抽出しても断片しか得られない。pyte で実画面状態を再構成し、
    history.top（スクロールアウト確定）と可視領域から本文行/フッター行を取り出す。
    """

    def __init__(self, rows: int = 24, cols: int = 80) -> None:
        if not _PYTE_AVAILABLE:
            raise RuntimeError("pyte not installed")
        self._rows = rows
        self._cols = cols
        self._screen = pyte.HistoryScreen(cols, rows, history=5000, ratio=0.5)
        self._stream = pyte.ByteStream(self._screen)
        self._prev_history_count = 0
        # _pending_visible: 可視行を 1 サイクル保留するバッファ。(ansi, had_blank_before) のタプル。
        # 内容が変化しないサイクルで「安定」と判断し emit する（最終版だけをログに流す）。
        self._pending_visible: dict[int, tuple[bytes, bool]] = {}
        self._emitted_visible: dict[int, bytes] = {}  # 可視 row_idx -> emit 済み内容
        # コンテキスト圧縮 / claude --resume の一括再描画でスクリーンクリアや
        # 同一行の複数回描画が起こると重複 emit される。これを防ぐ直近 emit キャッシュ。
        # ★ バイト列ではなく ANSI を剥がした「テキスト」で重複判定する。
        # 同じ文でも選択ハイライト (\x1b[48;2;55;55;55m) や折返し差で ANSI
        # バイト列が変わると、バイト一致では dedup できず resume バーストで
        # 同じ行が 2〜3 回出ていたため。
        # maxlen は resume の大量再描画でも溢れないよう 2000 に拡大。
        self._recent_emitted: collections.deque[str] = collections.deque(maxlen=2000)
        self._last_footer_bytes: list[bytes] = []
        self._last_cursor_pos: tuple[int, int] | None = None  # feed() の度に更新
        # usage と reset は別行・別フレームで現れることがあるので最後に観測した値を保持
        self._last_usage_percent: int | None = None
        self._last_reset_time: str | None = None
        self._last_reset_tz: str | None = None

    def resize(self, rows: int, cols: int) -> None:
        if rows == self._rows and cols == self._cols:
            return
        try:
            self._screen.resize(rows, cols)
        except Exception as e:
            _log.exception("pyte resize error: %s", e)
        self._rows = rows
        self._cols = cols
        self._pending_visible.clear()
        self._emitted_visible.clear()
        # 注: 過去に試した「pre-populate / _post_resize_extract フラグ」アプローチは
        # いずれも resize 直後に同名ツール呼出を skip してしまい取りこぼしを生んだ。
        # 取りこぼしは復旧不能（user が claude --resume するまで見えない）が、
        # 重複は視覚的に冗長なだけで内容は残るため、取りこぼし < 重複 の優先度を取り
        # この関数は state クリアだけに留めて、ツール行の重複は許容する。

    def feed(self, data: bytes) -> tuple[list[bytes], list[bytes], tuple[int, int] | None]:
        """PTY データを受け取り (log_lines, footer_lines, cursor_pos) を返す。
        cursor_pos は footer 内の (行 index, 列) またはフッター外なら None。
        （raw passthrough 移行後は debug/replay 専用。本番経路では使わない）
        """
        try:
            self._stream.feed(data)
        except Exception as e:
            _log.exception("pyte feed error: %s", e)
            return [], [], None
        return self._extract()

    def feed_screen_only(self, data: bytes) -> None:
        """pyte 仮想スクリーンだけ更新する（_extract の log/footer 再構成はしない）。

        raw passthrough 構成での本番経路用。レンダリングは pty_proxy が生バイト
        中継で行い、ここでは extract_usage()/is_active() がスクリーンを走査する
        ためだけに画面状態を維持する。脆弱な _extract / lazy emission /
        _recent_emitted dedup は一切通らない。
        """
        try:
            self._stream.feed(data)
        except Exception as e:
            _log.exception("pyte feed error: %s", e)

    def cursor_in_footer(self) -> tuple[int, int] | None:
        """最後の feed() で計算した cursor_pos を返す（再計算なし）。"""
        return self._last_cursor_pos

    def _extract(self) -> tuple[list[bytes], list[bytes], tuple[int, int] | None]:
        screen = self._screen

        # 1. history.top の新規追加 = スクロールアウトした確定 log 行
        hist_lines = list(screen.history.top)
        n = len(hist_lines)
        if n < self._prev_history_count:
            # リサイズ等で history がクリアされた場合は再処理（_recent_emitted で重複除去）
            self._prev_history_count = 0
        new_hist = hist_lines[self._prev_history_count:]
        self._prev_history_count = n
        history_logs: list[bytes] = []
        _hist_pending_blank = False
        for line in new_hist:
            text = self._line_text(line).rstrip()
            if not text:
                if history_logs:
                    _hist_pending_blank = True
                continue
            if _is_footer_text(text):
                # ❯ 行でプレースホルダー以外の内容はユーザープロンプトなのでログに流す
                if text.startswith("❯"):
                    body = text[1:].strip()
                    if body and body != "Press up to edit queued messages":
                        ansi = self._line_to_ansi(line)
                        key = self._norm_emit(ansi)
                        if key not in self._recent_emitted:
                            if _hist_pending_blank:
                                history_logs.append(b"")
                            self._recent_emitted.append(key)
                            history_logs.append(ansi)
                _hist_pending_blank = False
                continue
            ansi = self._line_to_ansi(line)
            if self._norm_emit(ansi) in self._recent_emitted:
                _hist_pending_blank = False
                continue
            if _hist_pending_blank:
                history_logs.append(b"")
                _hist_pending_blank = False
            self._recent_emitted.append(self._norm_emit(ansi))
            history_logs.append(ansi)

        # スクロール時、可視 row_idx の追跡をシフト
        if new_hist:
            shift = len(new_hist)
            new_emitted: dict[int, bytes] = {}
            for idx, content in self._emitted_visible.items():
                ni = idx - shift
                if ni >= 0:
                    new_emitted[ni] = content
            self._emitted_visible = new_emitted
            # pending もシフト。ni<0 になった行は history.top 経由で step 1 が emit する。
            new_pend: dict[int, tuple[bytes, bool]] = {}
            for idx, entry in self._pending_visible.items():
                ni = idx - shift
                if ni >= 0:
                    new_pend[ni] = entry
            self._pending_visible = new_pend

        # 2. 可視領域
        visible_lines = [screen.buffer[y] for y in range(screen.lines)]
        visible_text = [self._line_text(l).rstrip() for l in visible_lines]

        # 3. footer 開始行
        cursor_y = screen.cursor.y
        footer_start = self._find_footer_start(visible_text, cursor_y)

        # 4. log area: Lazy Emission — 新規行は 1 サイクル _pending_visible に保留し、
        #    内容が変化しなければ「安定」と判断して emit する。
        #    こうすることでストリーミング中に幅が変わるテーブルは最終版だけがログに出る。
        new_visible_logs: list[bytes] = []
        _vis_pending_blank = False
        new_pending: dict[int, tuple[bytes, bool]] = {}

        for y in range(footer_start):
            text = visible_text[y]

            if not text:
                # 行が空になった（クリアされた）
                if y in self._pending_visible:
                    # pending 中の行がクリアされる前に救出 emit する
                    p_ansi, p_had_blank = self._pending_visible.pop(y)
                    if p_had_blank:
                        new_visible_logs.append(b"")
                    new_visible_logs.append(p_ansi)
                    self._emitted_visible[y] = p_ansi
                    self._recent_emitted.append(self._norm_emit(p_ansi))
                else:
                    self._emitted_visible.pop(y, None)
                if new_visible_logs or self._emitted_visible or self._pending_visible or new_pending:
                    _vis_pending_blank = True
                continue

            if y == cursor_y:
                _vis_pending_blank = False
                continue

            if _is_footer_text(text):
                # ⏺ 行は Running/Working 中のみ footer 扱い。blank を消さない。
                if not text.startswith("⏺"):
                    _vis_pending_blank = False
                continue

            ansi = self._line_to_ansi(visible_lines[y])

            # ── 既 emit 済み行 ──────────────────────────────────────────────
            prev = self._emitted_visible.get(y)
            if prev is not None:
                if prev != ansi:
                    self._emitted_visible[y] = ansi
                    self._recent_emitted.append(self._norm_emit(ansi))
                    # ⏺/⎿ 行の完了遷移のみ即 emit（pending を経由しない）
                    if (text.startswith("⏺") or text.startswith("⎿")) and not _TRANSIENT_TAIL_RE.search(text):
                        if _vis_pending_blank:
                            new_visible_logs.append(b"")
                        new_visible_logs.append(ansi)
                _vis_pending_blank = False
                continue

            # ── Pending 行（前サイクルに保留済み）──────────────────────────
            pending_entry = self._pending_visible.get(y)
            if pending_entry is not None:
                prev_ansi, had_blank = pending_entry
                # このサイクルに blank が来ていれば had_blank を更新
                had_blank = had_blank or _vis_pending_blank
                if prev_ansi == ansi:
                    # 安定（前サイクルから変化なし）→ emit
                    if had_blank:
                        new_visible_logs.append(b"")
                    new_visible_logs.append(ansi)
                    self._emitted_visible[y] = ansi
                    self._recent_emitted.append(self._norm_emit(ansi))
                    # new_pending には入れない（emit 済み）
                else:
                    # まだ変化中 → 更新して保留継続
                    new_pending[y] = (ansi, had_blank)
                _vis_pending_blank = False
                continue

            # ── 新規行 → pending に追加 ─────────────────────────────────────
            # tool-row 例外: 同名ツールの再呼出ヘッダ ("⏺ Bash(git status)" 二度目) が
            # _recent_emitted の重複チェックで消えるのを避ける。
            # 重複より取りこぼしを避ける方針なので、resize 直後でも例外は維持する。
            _is_tool_row = (text.startswith("⏺") or text.startswith("⎿")) and self._emitted_visible
            if self._norm_emit(ansi) in self._recent_emitted and not _is_tool_row:
                self._emitted_visible[y] = ansi
                _vis_pending_blank = False
                continue
            new_pending[y] = (ansi, _vis_pending_blank)
            _vis_pending_blank = False

        self._pending_visible = new_pending

        # 5. footer
        footer_visible = visible_lines[footer_start:]
        footer_text = visible_text[footer_start:]
        while footer_visible and not footer_text[-1]:
            footer_visible.pop()
            footer_text.pop()
        footer_bytes = [self._line_to_ansi(l) for l in footer_visible]

        if footer_bytes:
            self._last_footer_bytes = footer_bytes
        else:
            footer_bytes = list(self._last_footer_bytes)

        # cursor_pos: footer 内なら (footer 行 index, 列)、それ以外は None
        footer_last = footer_start + len(footer_visible) - 1
        cx = screen.cursor.x
        if (footer_visible and footer_start <= cursor_y <= footer_last):
            self._last_cursor_pos = (cursor_y - footer_start, cx)
        else:
            self._last_cursor_pos = None

        return history_logs + new_visible_logs, footer_bytes, self._last_cursor_pos

    @staticmethod
    def _line_text(line_dict) -> str:
        if not line_dict:
            return ""
        max_col = max(line_dict.keys())
        out = []
        for k in range(max_col + 1):
            ch = line_dict.get(k)
            if ch is None:
                out.append(" ")
            elif ch.data == "":
                continue  # 全角文字の継続セル
            else:
                out.append(ch.data)
        return "".join(out)

    @staticmethod
    def _norm_emit(ansi: bytes) -> str:
        """_recent_emitted 用の正規化キー。ANSI を剥がし末尾空白を除去。

        同じ可視テキストなら、選択ハイライト・色状態・折返し位置の差で
        ANSI バイト列が変わっても同一キーになり、重複 emit を防げる。
        """
        return _ANSI_ALL_RE.sub(b"", ansi).decode("utf-8", "replace").rstrip()

    @staticmethod
    def _color_seq(color, fore: bool) -> bytes:
        if isinstance(color, str) and len(color) == 6:
            try:
                r = int(color[0:2], 16)
                g = int(color[2:4], 16)
                b = int(color[4:6], 16)
                prefix = "38" if fore else "48"
                return f"\x1b[{prefix};2;{r};{g};{b}m".encode()
            except ValueError:
                pass
        named = {
            "black": "0", "red": "1", "green": "2", "brown": "3",
            "blue": "4", "magenta": "5", "cyan": "6", "white": "7",
        }
        if color in named:
            return f"\x1b[{'3' if fore else '4'}{named[color]}m".encode()
        return b""

    def _line_to_ansi(self, line_dict) -> bytes:
        """1 行を ANSI 付きバイト列に変換（末尾の空セルは省略）。"""
        if not line_dict:
            return b""
        max_col = -1
        for k, ch in line_dict.items():
            if ch.data and ch.data != " " or ch.bg not in ("default", None):
                if k > max_col:
                    max_col = k
        if max_col < 0:
            return b""
        out = bytearray()
        cur_fg = "default"
        cur_bg = "default"
        cur_bold = False
        cur_italic = False
        cur_underline = False
        for k in range(max_col + 1):
            ch = line_dict.get(k)
            if ch is None:
                fg, bg, bold, italic, underline, data = "default", "default", False, False, False, " "
            else:
                if ch.data == "":
                    continue  # 全角文字の継続セル
                fg = ch.fg or "default"
                bg = ch.bg or "default"
                bold = bool(ch.bold)
                italic = bool(getattr(ch, "italics", False))
                underline = bool(getattr(ch, "underscore", False))
                data = ch.data
            if fg != cur_fg or bg != cur_bg or bold != cur_bold or italic != cur_italic or underline != cur_underline:
                out.extend(b"\x1b[0m")
                if bold:
                    out.extend(b"\x1b[1m")
                if italic:
                    out.extend(b"\x1b[3m")
                if underline:
                    out.extend(b"\x1b[4m")
                if fg != "default":
                    out.extend(self._color_seq(fg, fore=True))
                if bg != "default":
                    out.extend(self._color_seq(bg, fore=False))
                cur_fg, cur_bg, cur_bold, cur_italic, cur_underline = fg, bg, bold, italic, underline
            out.extend(data.encode("utf-8"))
        if cur_fg != "default" or cur_bg != "default" or cur_bold or cur_italic or cur_underline:
            out.extend(b"\x1b[0m")
        return bytes(out)

    _USAGE_RE = re.compile(r"used\s+(\d+)%\s+of\s+your\s+session\s+limit")
    _RESET_RE = re.compile(r"resets\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*\(([^)]+)\)")

    def extract_usage(self) -> dict | None:
        """画面全体 + スクロールアウト履歴から usage% と reset time を抽出。"""
        def _scan(line_dict) -> None:
            text = self._line_text(line_dict)
            m_use = self._USAGE_RE.search(text)
            if m_use:
                pct = int(m_use.group(1))
                if self._last_usage_percent is None or pct > self._last_usage_percent:
                    self._last_usage_percent = pct
            m_reset = self._RESET_RE.search(text)
            if m_reset:
                self._last_reset_time = m_reset.group(1).strip()
                self._last_reset_tz = m_reset.group(2).strip()

        for line in self._screen.history.top:
            _scan(line)
        for y in range(self._screen.lines):
            _scan(self._screen.buffer[y])

        result: dict = {}
        if self._last_usage_percent is not None:
            result["usage_percent"] = self._last_usage_percent
        if self._last_reset_time is not None:
            result["reset_time"] = self._last_reset_time
            result["reset_tz"] = self._last_reset_tz
        return result or None

    def is_active(self) -> bool:
        """アクティブなタスク実行中か。可視スクリーン下部を直接走査する。

        旧実装は _extract が設定する _last_footer_bytes に依存していたが、
        raw passthrough 構成では _extract を通さないため、pyte の可視バッファ
        下部数行を直接見る方式に変更。
        """
        n = self._screen.lines
        # 下から最大 6 行を走査（フッター/spinner はここに出る）
        for y in range(n - 1, max(-1, n - 7), -1):
            text = self._line_text(self._screen.buffer[y]).strip()
            if not text:
                continue
            if text[0] in _ACTIVE_PREFIX_CHARS:
                return True
            if _TRANSIENT_TAIL_RE.search(text):
                return True
            for kw in _ACTIVE_FOOTER_KEYWORDS:
                if kw in text:
                    return True
        return False

    def idle_prompt_visible(self) -> bool:
        """可視スクリーン下部に空の入力プロンプト ❯ があり、かつ非アクティブか。

        auto-resume の「アイドルになったらプロンプト注入」判定用。
        旧実装は _extract の footer 解析に依存していた。
        """
        if self.is_active():
            return False
        n = self._screen.lines
        for y in range(n - 1, max(-1, n - 8), -1):
            text = self._line_text(self._screen.buffer[y]).strip()
            if text.startswith("❯"):
                return True
        return False

    @staticmethod
    def _dialog_block_top(text_lines: list[str], break_idx: int) -> int | None:
        """break_idx より上に選択肢ダイアログがあれば、その最上行 index を返す。

        break_idx から上方向に最大 12 行覗き、その範囲に
          - dialog question マーカー（"Do you want to proceed?" 等）
          - または option 行（"1. Yes" / "❯ 2. ..."）が 2 つ以上
        が存在すれば、ダイアログブロックの最上行（question 行か最初の option 行）
        を返す。なければ None。

        ダイアログと最下部 footer の間に "─────  *" のような半端な区切り行が
        挟まって通常スキャンが break してしまうケースを救済する。
        """
        lo = max(0, break_idx - 12)
        has_question = False
        option_rows: list[int] = []
        top_candidate: int | None = None
        for j in range(break_idx, lo - 1, -1):
            tj = text_lines[j].strip()
            if not tj:
                continue
            if any(m in tj for m in _DIALOG_QUESTION_MARKERS):
                has_question = True
                top_candidate = j
            elif _CHOICE_OPTION_RE.match(tj):
                option_rows.append(j)
                top_candidate = j
        if not (has_question or len(option_rows) >= 2):
            return None
        # top_candidate が question マーカー行ならそこがダイアログ上端。
        # これ以上 above は log なので拡張しない。
        if top_candidate is not None:
            tc_text = text_lines[top_candidate].strip()
            if any(m in tc_text for m in _DIALOG_QUESTION_MARKERS):
                return top_candidate
        # option しか無い（question 不在）ケース。説明文（"This session is ... old"
        # 等）が option の上に続く場合があるので、区切り線（全 box-drawing）または
        # ⏺/⎿/log 境界まで更に上方へ拡張する。
        t = top_candidate
        k = t - 1
        scan_floor = max(0, t - 10)
        while k >= scan_floor:
            sk = text_lines[k].strip()
            if not sk:
                k -= 1
                continue
            # 区切り線（罫線のみ）に当たったらそこがダイアログ上端境界
            if all(c in "─━═╌╍┄┈" or c == " " for c in sk):
                t = k
                break
            # ツール行 / ⏺ / ⎿ は明確に log 側 → そこで止める（拡張しない）
            if sk[0] in "⏺⎿" or sk.startswith("❯"):
                break
            # それ以外（説明文）はダイアログの一部とみなして取り込む
            t = k
            k -= 1
        return t

    @staticmethod
    def _is_choice_dialog_block(text_lines: list[str], idx: int) -> bool:
        """text_lines[idx] が選択肢ダイアログ（"Do you want to proceed?" +
        "1. Yes" / "❯ 2. ..." / "3. No"）の一部かどうかを判定する。

        idx 行が option 行・question 行のいずれかで、かつ近傍（上下 8 行以内）に
        dialog question マーカーが存在するときに True。これにより
        "1. Yes" / "3. No" のようにキーワードを含まない option 行も
        footer ブロックの一部として扱える。
        """
        s = text_lines[idx].strip()
        if not s:
            return False
        is_option = bool(_CHOICE_OPTION_RE.match(s))
        is_question = any(m in s for m in _DIALOG_QUESTION_MARKERS)
        if not (is_option or is_question):
            return False
        lo = max(0, idx - 8)
        hi = min(len(text_lines), idx + 9)
        for j in range(lo, hi):
            tj = text_lines[j].strip()
            if any(m in tj for m in _DIALOG_QUESTION_MARKERS):
                return True
            # 連続する option 行が複数あるのもダイアログの特徴
            if j != idx and _CHOICE_OPTION_RE.match(tj):
                # option 行が 2 つ以上連なる + 自身も option なら dialog とみなす
                if is_option:
                    return True
        return False

    @staticmethod
    def _find_footer_start(text_lines: list[str], cursor_y: int = -1) -> int:
        n = len(text_lines)
        i = n - 1
        while i >= 0 and not text_lines[i]:
            i -= 1
        if i < 0:
            return n
        footer_start = i + 1
        in_dialog = False  # 選択肢ダイアログ option を一度でも見たら True
        while i >= 0:
            t = text_lines[i].strip()
            if not t:
                i -= 1
                continue
            if _is_footer_text(t):
                footer_start = i
                # 区切り線（罫線のみ）または question マーカー行に当たったら、
                # ダイアログ上端なので in_dialog 解除（これ以上 above は log）。
                if in_dialog and (
                    all(c in "─━═╌╍┄┈" or c == " " for c in t)
                    or any(m in t for m in _DIALOG_QUESTION_MARKERS)
                ):
                    in_dialog = False
                i -= 1
                continue
            # 選択肢ダイアログの option/question 行（"1. Yes" 等キーワード無しを含む）は
            # footer ブロックとして扱い、スキャンを継続する。
            if TuiEmulator._is_choice_dialog_block(text_lines, i):
                footer_start = i
                # question マーカー行はダイアログ上端。これ以上 above は dialog で
                # ないので in_dialog を立てない（log 行まで吸い込まないため）。
                # option 行のときは上に説明文がある可能性があるので in_dialog=True。
                if any(m in t for m in _DIALOG_QUESTION_MARKERS):
                    in_dialog = False
                else:
                    in_dialog = True
                i -= 1
                continue
            # ダイアログ内なら、option/question の上にある説明文
            # （"This session is ... old" / "Resuming the full session ..."）も
            # footer ブロックに含める。⏺/⎿/❯ の log 境界で打ち切る。
            if in_dialog and t[0] not in "⏺⎿❯":
                footer_start = i
                i -= 1
                continue
            # ⎿ で始まる行はツール結果のサブアイテム（"⎿  file.py" 等）または
            # "⎿  Tip: Use /btw..." のような footer キーワード付き行。
            # ツール結果は footer_start を更新せずスキップ、footer キーワード行は更新する。
            if t.startswith("⎿"):
                if _is_footer_text(t):
                    footer_start = i
                i -= 1
                continue
            # break する前に look-ahead: この行のすぐ上（最大 12 行）に
            # 選択肢ダイアログ（question マーカー or option 行群）があれば、
            # ダイアログ + その間の半端な区切り行ごと footer に含める。
            dialog_top = TuiEmulator._dialog_block_top(text_lines, i)
            if dialog_top is not None:
                footer_start = dialog_top
                i = dialog_top - 1
                continue
            break

        # 入力テキストが PTY 幅で折り返した継続行を footer に含める。
        # ただし上方向スキャンで footer キーワード行が見つかった場合のみ適用する。
        # cursor がログ行（⎿ 出力末尾等）にある場合はログ行を footer に引き込まないようにする。
        if 0 <= cursor_y < footer_start and text_lines[cursor_y]:
            new_fs = cursor_y
            found_footer_text = False
            j = cursor_y - 1
            scan_limit = max(0, cursor_y - 10)
            while j >= scan_limit:
                t = text_lines[j].strip()
                if not t:
                    break
                if _is_footer_text(t):
                    found_footer_text = True
                    new_fs = j
                elif found_footer_text:
                    break
                else:
                    new_fs = j
                j -= 1
            if found_footer_text:
                footer_start = new_fs

        return footer_start


class TuiParser:
    """Claude Code の PTY 出力から「流れるログ行」と「固定 UI フッター」を分離する。

    pyte が利用できない場合のフォールバック。chunk ごとに parse() を呼ぶ。
    """

    def __init__(self) -> None:
        self._recent_lines: list[bytes] = []
        self._max_recent: int = 200
        self._in_input_area: bool = False
        self._current_footer: list[bytes] = []
        self._line_buf: bytes = b""
        self._sync_buf: bytes = b""
        self._in_sync: bool = False

    _SYNC_BEGIN = b"\x1b[?2026h"
    _SYNC_END = b"\x1b[?2026l"

    def _split_sync_blocks(self, data: bytes) -> bytes:
        """\\x1b[?2026h/l に挟まれたフレームは完成するまで buffer に保持。"""
        out_parts: list[bytes] = []
        cur = self._sync_buf + data
        self._sync_buf = b""
        while cur:
            if self._in_sync:
                end_idx = cur.find(self._SYNC_END)
                if end_idx < 0:
                    self._sync_buf = cur
                    break
                out_parts.append(cur[: end_idx + len(self._SYNC_END)])
                cur = cur[end_idx + len(self._SYNC_END) :]
                self._in_sync = False
            else:
                begin_idx = cur.find(self._SYNC_BEGIN)
                if begin_idx < 0:
                    out_parts.append(cur)
                    break
                out_parts.append(cur[:begin_idx])
                cur = cur[begin_idx:]
                self._in_sync = True
        return b"".join(out_parts)

    def parse(self, data: bytes, max_cols: int) -> tuple[list[bytes], list[bytes]]:
        ready = self._split_sync_blocks(data)
        if not ready:
            return [], list(self._current_footer)
        out = _to_log_view(ready, max_cols)
        if not out:
            return [], list(self._current_footer)

        self._in_input_area = False
        log_lines: list[bytes] = []
        footer_buf: list[bytes] = []
        recent = set(self._recent_lines)
        saw_footer_marker = False

        combined = self._line_buf + out
        if combined.endswith(b"\r\n"):
            self._line_buf = b""
            line_iter = combined[:-2].split(b"\r\n")
        else:
            parts = combined.split(b"\r\n")
            self._line_buf = parts[-1]
            line_iter = parts[:-1]

        for line in line_iter:
            if not line:
                continue
            plain = _ANSI_COLOR_RE.sub(b"", line).strip()
            if not plain:
                continue

            if _is_status_line(line):
                saw_footer_marker = True
                footer_buf.append(line)
                continue

            if plain.startswith(b"\xe2\x9d\xaf"):  # ❯
                self._in_input_area = True
                saw_footer_marker = True
                footer_buf.append(line)
                continue

            if self._in_input_area:
                if any(kw in plain for kw in _CHROME_KEYWORDS) or _H_BOX_LINE_RE.match(plain):
                    self._in_input_area = False
                    saw_footer_marker = True
                footer_buf.append(line)
                continue

            if _is_chrome_line(line):
                saw_footer_marker = True
                footer_buf.append(line)
                continue

            if line in recent:
                continue
            log_lines.append(line)
            self._recent_lines.append(line)
            recent.add(line)
            if len(self._recent_lines) > self._max_recent:
                old = self._recent_lines.pop(0)
                recent.discard(old)

        if saw_footer_marker:
            self._current_footer = footer_buf

        return log_lines, list(self._current_footer)
