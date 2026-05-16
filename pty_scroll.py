"""Scroll モード用のレンダラ。

RELAYOUT_MODE=scroll のとき、TuiEmulator の pyte.Screen を「仮想 canvas」とみなし、
各端末は自分のサイズ (vrows x vcols) 分だけの viewport を表示する。

viewport の左上 (offset_row, offset_col) は ScrollRenderer.set_offset() / scroll() で
更新する。canvas より小さい viewport では端末スクロールにより全体を見渡せる。
canvas より大きい viewport（普通はない）では canvas 終端でクリップする。

tmux の window-size=largest + Shift+矢印スクロールに対応する設計。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pyte


import re as _re

_SGR_MOUSE = _re.compile(rb"\x1b\[<(\d+);\d+;\d+[Mm]")

# 端末が自発的に送る passive シーケンス（ユーザー操作ではない）。
# これらで managed scroll を live へ戻してはいけない（フォーカスレポート
# ?1004h / 各種デバイス応答が常時飛んでくるため、戻すと「非 nav scroll が
# 完璧に壊れる」）。
_FOCUS_IN = b"\x1b[I"
_FOCUS_OUT = b"\x1b[O"
# CSI ... 終端が R(カーソル位置)/c(DA)/n(DSR)/y(DECRPM) など = デバイス応答
_DEV_REPORT = _re.compile(rb"\x1b\[[\d;?>=]*[Rcny]\Z")


def is_live_reset_key(data: bytes) -> bool:
    """この入力が「ユーザーの操作（文字入力・カーソル移動等）」で、
    PAGEKEY/WHEEL scroll を live(最下部) へ戻すべきか。

    端末が自発的に送る passive シーケンス（フォーカス入/出、マウス、
    カーソル位置/DA/DSR 応答、OSC/DCS 応答）は **False**（戻さない・
    透過）。それ以外（印字文字・Enter・BS・Tab・矢印・編集キー等の実
    操作）は **True**（live へ戻す＝ユーザー意図）。空入力は False。
    """
    if not data:
        return False
    if data in (_FOCUS_IN, _FOCUS_OUT):
        return False
    if data.startswith(b"\x1b[M"):                 # legacy mouse
        return False
    if _SGR_MOUSE.match(data):                      # SGR mouse
        return False
    if data.startswith(b"\x1b]") or data.startswith(b"\x1bP"):
        return False                                # OSC / DCS 応答
    if _DEV_REPORT.match(data):                     # CSI デバイス応答
        return False
    return True                                     # 実操作 → live 復帰


def classify_wheel(data: bytes):
    """マウスホイール入力を判定。-1=上(過去へ) / 1=下(新しい方へ) / None。

    SGR(?1006h) `\\x1b[<Cb;Cx;Cy[Mm]` とレガシー(?1000h) `\\x1b[M b0 b1 b2`
    の両方に対応。Cb の bit6(64)=拡張ボタン(ホイール)・bit5(32)=motion を
    使い、修飾(shift4/alt8/ctrl16)は無視。クリック/ドラッグ(通常ボタン)は
    None を返す（claude へ透過させるため）。"""
    m = _SGR_MOUSE.search(data)
    if m:
        cb = int(m.group(1))
    else:
        i = data.find(b"\x1b[M")
        if i == -1 or len(data) < i + 4:
            return None
        cb = data[i + 3] - 32
    if (cb & 64) and not (cb & 32):       # 拡張ボタン かつ motion でない
        low = cb & 0b11
        if low == 0:
            return -1                     # ホイール上 = 過去へ
        if low == 1:
            return 1                      # ホイール下 = 新しい方へ
    return None


def line_to_text(line) -> str:
    """pyte の 1 行（{col: Char} 相当）をプレーンテキストへ。

    全角の継続セル（data==""）はスキップ、空セルは空白、末尾空白は除去。
    セッションログのファイル書き出し用（ANSI を含めない）。
    """
    if not line:
        return ""
    mx = max(line.keys())
    out = []
    for x in range(mx + 1):
        ch = line.get(x)
        if ch is None:
            out.append(" ")
        elif ch.data == "":
            continue
        else:
            out.append(ch.data)
    return "".join(out).rstrip()


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


class ScrollRenderer:
    """canvas (pyte.Screen) の viewport を抽出して ANSI bytes を返す。

    各クライアントごとに 1 インスタンス保持し、viewport offset 状態を管理する。
    """

    def __init__(self) -> None:
        self._last_repaint: bytes = b""
        # スクロール位置は **canvas 先頭(最古=index 0)からの絶対 oy** で
        # 保持する（_anchor）。「最下部から N 行」方式だと、遡り中に claude
        # が出力して canvas が伸びるたび表示位置が下へドリフトし「複数箇所の
        # バッファが混ざって」見える（実録画で確認）。先頭基準なら末尾への
        # 追記で view が動かない（tmux copy-mode / pager と同じ正しい挙動）。
        self._follow: bool = True       # True=最下部(live)追従
        self._anchor: int = 0           # not follow 時の viewport 先頭絶対 oy
        self._last_max_oy: int = 0      # 直近 render の max_oy（scroll() 用）
        self._offset_col: int = 0       # 横 pan（通常 0）

    def reset(self) -> None:
        """端末リサイズ時に再描画を強制する。スクロール位置は保持する。"""
        self._last_repaint = b""

    @property
    def follow_bottom_active(self) -> bool:
        return self._follow

    def follow_bottom(self) -> None:
        """最下部（live）追従へ戻す。"""
        self._follow = True
        self._offset_col = 0

    def scroll(self, dy: int, dx: int = 0) -> None:
        """履歴をスクロール。dy<0=上(古い方へ遡る)、dy>0=下(新しい方へ)。
        絶対アンカー方式: follow 中の上スクロールで最下部からアンカーを
        起こし、以降は末尾追記に影響されない。最下部に到達したら follow
        に復帰。clamp は render 時。"""
        if dx:
            self._offset_col = max(0, self._offset_col + dx)
        if dy == 0:
            return
        if self._follow:
            if dy < 0:                  # live から上へ遡り始める
                self._anchor = max(0, self._last_max_oy + dy)
                self._follow = False
            # dy>0（live で下）: 何もしない（最下部のまま）
        else:
            self._anchor += dy
            if self._anchor >= self._last_max_oy:
                self._follow = True     # 最下部に達した → live 追従へ
            elif self._anchor < 0:
                self._anchor = 0

    @property
    def scrollback(self) -> int:
        """互換: 「最下部から何行上か」。0=follow。"""
        if self._follow:
            return 0
        return max(0, self._last_max_oy - self._anchor)

    def render_viewport(self, screen: "pyte.Screen",
                        vrows: int, vcols: int) -> bytes:
        """canvas から (vrows, vcols) の viewport を ANSI で返す。

        論理 canvas = history.top（スクロールアウト済の過去ログ）+ 可視 buffer。
        - follow_bottom（通常）: 最下部 = 可視 buffer の末尾 vrows のみ描画。
          history を materialize しないので高速（ホットパス）。
        - pan 中（nav-mode でスクロール）: history+可視を連結した論理 canvas
          を作り、offset 位置の viewport を描く（過去ログを遡れる）。
        毎回 \\x1b[2J で全消去 → 行ごとに描画（diff はしない）。
        plain pyte.Screen（history 属性なし）でも動く。
        """
        vis_rows = screen.lines
        canvas_cols = screen.columns
        hist = getattr(screen, "history", None)

        if hist is None:
            n_hist = 0
            total = vis_rows
            def _line(L: int):
                return screen.buffer[L] if 0 <= L < vis_rows else None
        else:
            # 論理 canvas = history.top（確定スクロールアウト）+ 可視 buffer。
            # follow でも真の max_oy が要る（先頭基準アンカー / scroll() 用）。
            top = list(hist.top)
            n_hist = len(top)
            total = n_hist + vis_rows
            def _line(L: int):
                if 0 <= L < n_hist:
                    return top[L]
                vi = L - n_hist
                return screen.buffer[vi] if 0 <= vi < vis_rows else None

        max_oy = max(0, total - vrows)
        max_ox = max(0, canvas_cols - vcols)
        self._last_max_oy = max_oy
        if self._follow or hist is None:
            oy = max_oy                      # 最下部 = live 追従
        else:
            # 先頭基準の絶対アンカー。末尾追記(max_oy 増)で動かない。
            oy = self._anchor
            if oy >= max_oy:
                oy = max_oy
                self._follow = True          # 最下部に達した → live
            elif oy < 0:
                oy = 0
            self._anchor = oy
        ox = min(self._offset_col, max_ox)
        self._offset_col = ox

        parts = bytearray()
        parts.extend(b"\x1b[?2026h")  # synchronized output begin
        parts.extend(b"\x1b[H")
        parts.extend(b"\x1b[2J")
        parts.extend(b"\x1b[H")

        for vy in range(vrows):
            L = oy + vy
            if L >= total:
                break
            self._append_row(parts, _line(L), ox, vcols)
            if vy + 1 < vrows:
                parts.extend(b"\r\n")

        # カーソル論理位置 = n_hist + screen.cursor.y（live は可視 buffer 内）
        cy_canvas = n_hist + screen.cursor.y
        cx_canvas = screen.cursor.x
        if oy <= cy_canvas < oy + vrows and ox <= cx_canvas < ox + vcols:
            cy_view = cy_canvas - oy + 1  # ANSI は 1-indexed
            cx_view = cx_canvas - ox + 1
            parts.extend(f"\x1b[{cy_view};{cx_view}H".encode())
            parts.extend(b"\x1b[?25h")
        else:
            parts.extend(b"\x1b[?25l")

        parts.extend(b"\x1b[?2026l")  # synchronized output end
        result = bytes(parts)
        self._last_repaint = result
        return result

    def render_flow(self, screen: "pyte.Screen", vrows: int, vcols: int,
                    committed: list, first: bool) -> bytes:
        """HOST_FLOW_SCROLLBACK 用: 確定行を端末ネイティブ scrollback へ流し、
        live 領域を **全消去なし**で in-place 再描画する。

        committed: 今回新たに history へ確定した行（古い順）。これを画面
        最上部に描いてから画面を len(committed) 行スクロールアップさせる
        ことで、端末は *その確定行* を native scrollback へ送り込む
        （\\x1b[2J を使わないので空フレームで scrollback を汚さない）。
        その後 live 領域（可視 buffer 末尾 vrows）を \\x1b[H + 行毎
        \\x1b[K で上書き再描画する（claude 自身の絶対座標 in-place 描画と
        同じ流儀。二重 footer/にじみは display-oracle で回帰検知）。

        first=True の初回のみ一度だけ全消去してベースラインを作る
        （attach 相当。以降は no-clear）。pyte history 非対応 screen でも
        committed=[] で安全に live 再描画のみ行う。
        """
        vis_rows = screen.lines
        n = len(committed)
        parts = bytearray()
        parts.extend(b"\x1b[?2026h")  # synchronized output begin
        if first:
            parts.extend(b"\x1b[2J\x1b[9999;1H")
        if n:
            # 確定行を 1 行ずつ「最上部に描く → 1 行スクロール」して
            # native scrollback へ送る。端末スクロールは row0 が
            # scrollback へ落ちるので、毎回 row0 を確定行で上書きしてから
            # スクロールする（n と vrows の大小に依存せず正しい）。
            for ln in committed:
                parts.extend(b"\x1b[H")
                self._append_row(parts, ln, 0, vcols)
                parts.extend(b"\x1b[K")
                parts.extend(f"\x1b[{vrows};1H".encode())
                parts.extend(b"\n")
        # live 領域を全消去なしで in-place 再描画（可視 buffer 末尾 vrows）
        parts.extend(b"\x1b[H")
        start = max(0, vis_rows - vrows)
        for vy in range(vrows):
            L = start + vy
            line = screen.buffer[L] if 0 <= L < vis_rows else None
            self._append_row(parts, line, 0, vcols)
            parts.extend(b"\x1b[K")
            if vy + 1 < vrows:
                parts.extend(b"\r\n")
        # claude の論理カーソルを live 領域内に写像
        cy_view = screen.cursor.y - start + 1
        cx_view = screen.cursor.x + 1
        if 1 <= cy_view <= vrows and 1 <= cx_view <= vcols:
            parts.extend(f"\x1b[{cy_view};{cx_view}H".encode())
            parts.extend(b"\x1b[?25h")
        else:
            parts.extend(b"\x1b[?25l")
        parts.extend(b"\x1b[?2026l")  # synchronized output end
        result = bytes(parts)
        self._last_repaint = result
        return result

    @staticmethod
    def _append_row(parts: bytearray, line, col_offset: int, vcols: int) -> None:
        if not line:
            return
        cur_fg = "default"
        cur_bg = "default"
        cur_bold = False
        cur_italic = False
        cur_underline = False
        for vx in range(vcols):
            cx = col_offset + vx
            ch = line.get(cx)
            if ch is None:
                data = " "
                fg, bg = "default", "default"
                bold = italic = underline = False
            else:
                if ch.data == "":
                    # 全角文字の継続セル: 描画しない（直前の全角が 2 カラム占有済み）
                    continue
                data = ch.data
                fg = ch.fg or "default"
                bg = ch.bg or "default"
                bold = bool(ch.bold)
                italic = bool(getattr(ch, "italics", False))
                underline = bool(getattr(ch, "underscore", False))
            if (fg != cur_fg or bg != cur_bg or bold != cur_bold
                    or italic != cur_italic or underline != cur_underline):
                parts.extend(b"\x1b[0m")
                if bold:
                    parts.extend(b"\x1b[1m")
                if italic:
                    parts.extend(b"\x1b[3m")
                if underline:
                    parts.extend(b"\x1b[4m")
                if fg != "default":
                    parts.extend(_color_seq(fg, fore=True))
                if bg != "default":
                    parts.extend(_color_seq(bg, fore=False))
                cur_fg, cur_bg, cur_bold, cur_italic, cur_underline = fg, bg, bold, italic, underline
            parts.extend(data.encode("utf-8"))
        parts.extend(b"\x1b[0m")


class HistoryFlusher:
    """pyte HistoryScreen から「確定してスクロールアウトした行」を
    取り出す（HOST_FLOW_SCROLLBACK 用）。

    原理: claude が改行で 1 行スクロールアウトさせると pyte の
    ``HistoryScreen.index()`` が *その行オブジェクト* を
    ``screen.history.top`` (maxlen 付き deque) に append する。これは
    端末エミュレータ自身の確定判定＝グラウンドトゥルースであり、
    footer キーワード等のヒューリスティック分類は一切しない。

    deque 内の行オブジェクトは（maxlen で押し出されるまで）identity が
    保たれるので、最後に渡した行オブジェクトを identity で覚えておき、
    次回はそれより後ろ（新しい側）に積まれた行だけを返す。
    """

    # capture で溜める pending の安全上限（never-idle の暴走メモリ防止）。
    # 超過したら最古を捨てる（極端時のみ。通常 idle で drain される）。
    _PENDING_CAP = 100000

    def __init__(self) -> None:
        self._armed: bool = False
        self._last = None  # 最後に take_new で返した history.top 末尾行（identity）
        self._pending: list = []  # capture 済み・未 emit の確定行（古い→新しい）

    def reset(self) -> None:
        """identity 追跡だけ再 arm（リサイズ等）。capture 済み _pending は
        既に確定した正しい行なので保持する（scrollback へは後で出す）。"""
        self._armed = False
        self._last = None

    def capture(self, screen) -> None:
        """全フレームで呼ぶ。確定行を内部 _pending に蓄積するだけで端末へは
        書かない。pyte の history.top は maxlen 付き deque なので、毎フレーム
        取り込まないと長いバーストで古い確定行が押し出されて失われる。
        取り込み自体は dict 参照を list に足すだけで安価。"""
        new = self.take_new(screen)
        if new:
            self._pending.extend(new)
            if len(self._pending) > self._PENDING_CAP:
                # 極端な never-idle 時のメモリ防御（通常到達しない）
                self._pending = self._pending[-self._PENDING_CAP:]

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    @property
    def pending_len(self) -> int:
        return len(self._pending)

    def drain(self) -> list:
        """蓄積した確定行を取り出してクリア（idle/静止時に scrollback へ
        書き出すのは呼び出し側）。"""
        out = self._pending
        self._pending = []
        return out

    def take_new(self, screen) -> list:
        """前回以降に history.top へ確定した行を古い→新しい順で返す。

        初回（arm 時）は既存履歴を一切流さず空を返す（接続時点から開始。
        過去ログの大量初期 dump を避ける）。``_last`` が deque から
        落ちている / history がクリアされた等で追跡不能なときは、重複や
        暴発を避けるため何も流さず現在地へ resync する。
        """
        hist = getattr(screen, "history", None)
        if hist is None:
            return []
        top = hist.top
        if not self._armed:
            self._armed = True
            self._last = top[-1] if len(top) else None
            return []
        if self._last is None:
            # 前回時点で空。現在ある top は全て新規。
            new = list(top)
        else:
            new = []
            found = False
            for ln in reversed(top):
                if ln is self._last:
                    found = True
                    break
                new.append(ln)
            if not found:
                # _last が落ちた / history reset。重複防止のため resync only。
                new = []
            new.reverse()
        self._last = top[-1] if len(top) else None
        return new
