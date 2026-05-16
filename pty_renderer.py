"""端末への「流れるログ + 固定フッター」描画を管理する TerminalRenderer。"""
from pty_constants import _FOOTER_MAX_COLS, _truncate_visible


class TerminalRenderer:
    """端末ごとに 1 インスタンス保持し、render() の度に必要最小限の ANSI を返す。

    - 既存フッター行数だけ上に戻り `\\x1b[J` で消去
    - 新規ログ行を `\\r\\n` 改行で追記
    - 新フッターを描画（最終行末で停止し、次回 in-place 上書き可能にする）
    """

    def __init__(self) -> None:
        self._footer_height: int = 0
        self._max_footer_height: int = 0
        self._last_footer: list[bytes] = []
        self._last_cursor: tuple[int, int] | None = None

    def reset(self) -> None:
        """端末リサイズ後にカーソル追跡をリセットする。
        _footer_height は保持し、次の render() が正確な行数で消去できるようにする。
        screen_rows を設定すると go_up が過大になってドリフトするため設定しない。
        _max_footer_height はリセットして古い最大値によるパディング肥大を防ぐ。

        viewport の全クリア (\\x1b[H\\x1b[2J) はしない。フッター位置が
        いったん画面先頭にジャンプして以降の出力が下方向に「流れる」形になり
        スクロールバックを汚染するため。次の render は通常の go_up + \\x1b[J で
        旧フッター位置だけクリアし、その上の折返し残骸はスクロールに任せる。
        """
        self._last_footer = []
        self._last_cursor = None
        self._max_footer_height = 0  # リサイズ後は最大値を再計測
        # _footer_height は保持（ドリフト防止のため）

    def render(
        self,
        log_lines: list[bytes],
        footer_lines: list[bytes],
        cursor_pos: tuple[int, int] | None = None,
        cols: int = _FOOTER_MAX_COLS,
    ) -> bytes:
        """cursor_pos = (footer 内の行 index, column)。指定されると最後にカーソルを移動。
        cols = 出力先端末の表示幅（折り返しを起こさないように 1 行をこの幅に切り詰める）。
        """
        max_cols = max(20, cols)
        truncated_footer = [_truncate_visible(l, max_cols) for l in footer_lines]
        # 行数を最大値でパディング（spinner 有無で行数が揺れて累積ドリフトしないように）。
        # padding は先頭に入れて、実 footer 内容を常に画面下端に固定する。
        # pad_count は 3 行以内に制限して大きな空白が生じないようにする。
        self._max_footer_height = max(self._max_footer_height, len(truncated_footer))
        pad_count = min(self._max_footer_height - len(truncated_footer), 3)
        if pad_count > 0:
            truncated_footer = [b""] * pad_count + truncated_footer
        if cursor_pos is not None and pad_count > 0:
            cursor_pos = (cursor_pos[0] + pad_count, cursor_pos[1])

        if not log_lines and truncated_footer == self._last_footer:
            if cursor_pos is None or cursor_pos == self._last_cursor:
                return b""
            cursor_seq = self._cursor_move_from_current(cursor_pos)
            self._last_cursor = cursor_pos
            return cursor_seq

        parts: list[bytes] = []
        new_H = len(truncated_footer)
        if self._footer_height > 0:
            current_line_idx = (
                self._last_cursor[0] if self._last_cursor is not None
                else self._footer_height - 1
            )
            # フッターが伸びた場合（履歴ナビ等）、旧フッター上端より上にも古いログ行が残るため
            # 差分だけ余分に上に戻って消去する。
            extra_up = max(0, new_H - self._footer_height)
            go_up = current_line_idx + extra_up
            parts.append(b"\r")
            if go_up > 0:
                parts.append(f"\x1b[{go_up}A".encode())
            parts.append(b"\x1b[J")
        for line in log_lines:
            parts.append(line)  # ログ行は端末の折り返しに任せる（truncate すると情報が欠ける）
            parts.append(b"\r\n")
        for i, line in enumerate(truncated_footer):
            parts.append(line)
            if i + 1 < len(truncated_footer):
                parts.append(b"\r\n")

        self._footer_height = len(truncated_footer)
        self._last_footer = list(truncated_footer)
        if cursor_pos is not None:
            parts.append(self._cursor_move_from_end(cursor_pos, len(truncated_footer)))
        self._last_cursor = cursor_pos
        return b"".join(parts)

    @staticmethod
    def _cursor_move_from_end(target: tuple[int, int], footer_height: int) -> bytes:
        """フッター末尾末（最終行 col 末）から target=(行, 列) へ移動。"""
        line_idx, col = target
        up = (footer_height - 1) - line_idx
        out = bytearray(b"\r")
        if up > 0:
            out.extend(f"\x1b[{up}A".encode())
        if col > 0:
            out.extend(f"\x1b[{col}C".encode())
        return bytes(out)

    def _cursor_move_from_current(self, target: tuple[int, int]) -> bytes:
        """現在のカーソル位置（_last_cursor）から target へ相対移動。"""
        if self._last_cursor is None:
            return self._cursor_move_from_end(target, self._footer_height)
        cur_line, _ = self._last_cursor
        new_line, new_col = target
        out = bytearray(b"\r")
        if new_line < cur_line:
            out.extend(f"\x1b[{cur_line - new_line}A".encode())
        elif new_line > cur_line:
            out.extend(f"\x1b[{new_line - cur_line}B".encode())
        if new_col > 0:
            out.extend(f"\x1b[{new_col}C".encode())
        return bytes(out)
