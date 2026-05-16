"""ディスプレイ・オラクル: pyte を「ユーザーが見る端末」としてラップする。

従来のテストは「pty_proxy が何バイト出すか」を検証していたが、二重 footer や
resume 残留のようなバグは「端末の事前状態 × claude のレンダリング」の相互作用で
起こるため、出力バイト検証では構造的に検知できなかった。

DisplayTerminal は pyte.Screen を *再構成器ではなくディスプレイとして* 使う:
  - seed_prior_content() で「接続前に端末に残っていた内容」を再現できる
  - feed() で pty_proxy が送ったバイトを流し込む
  - lines() / contains() で「描画後に画面に何が見えるか」を検証する
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pyte  # noqa: E402


class DisplayTerminal:
    """ユーザーの端末ディスプレイ相当。bytes を食わせ、見える画面を返す。"""

    def __init__(self, rows: int = 40, cols: int = 120,
                 history: bool = False) -> None:
        self._rows = rows
        self._cols = cols
        if history:
            # 端末のネイティブ scrollback を再現（HOST_FLOW_SCROLLBACK 検証用）。
            # 画面外へスクロールアウトした行は screen.history.top に積まれる。
            self._screen = pyte.HistoryScreen(cols, rows, history=5000,
                                              ratio=0.5)
        else:
            self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)

    def scrollback_lines(self) -> list[str]:
        """native scrollback（画面外へ流れた確定行）を古い順で返す。

        history=True で生成した端末のみ意味がある。
        """
        hist = getattr(self._screen, "history", None)
        if hist is None:
            return []
        out: list[str] = []
        for line in list(hist.top):
            if not line:
                out.append("")
                continue
            mx = max(line.keys()) if line else -1
            chars: list[str] = []
            for x in range(mx + 1):
                ch = line.get(x)
                if ch is None:
                    chars.append(" ")
                elif ch.data == "":
                    continue
                else:
                    chars.append(ch.data)
            out.append("".join(chars).rstrip())
        return out

    def scrollback_count(self, needle: str) -> int:
        return sum(1 for ln in self.scrollback_lines() if needle in ln)

    def seed_prior_content(self, text_lines: list[str]) -> None:
        """接続前に端末に残っていた内容を再現する（dirty terminal の事前状態）。

        各行を書いて最後に \\r\\n でカーソルを seed 内容の「下」へ送る。
        これにより、以降のアプリ出力（クリアを伴わない増分描画）は
        seed 内容を *上書きせず* その下に積まれる。クリアが入った場合のみ
        seed 内容が消える、という pty_proxy の責務を切り分けて検証できる。
        """
        payload = "\r\n".join(text_lines).encode("utf-8") + b"\r\n"
        self._stream.feed(payload)

    def feed(self, data: bytes) -> None:
        self._stream.feed(data)

    def lines(self) -> list[str]:
        """現在画面に見えている行（rstrip 済み、全角継続セルは畳む）。"""
        out: list[str] = []
        for y in range(self._screen.lines):
            line = self._screen.buffer[y]
            if not line:
                out.append("")
                continue
            mx = max(line.keys())
            chars: list[str] = []
            for x in range(mx + 1):
                ch = line.get(x)
                if ch is None:
                    chars.append(" ")
                elif ch.data == "":
                    continue
                else:
                    chars.append(ch.data)
            out.append("".join(chars).rstrip())
        return out

    def text(self) -> str:
        return "\n".join(self.lines())

    def contains(self, needle: str) -> bool:
        return any(needle in ln for ln in self.lines())

    def count_lines_with(self, needle: str) -> int:
        return sum(1 for ln in self.lines() if needle in ln)

    def footer_region_count(self, marker: str = "⏵⏵") -> int:
        """footer 構造の数。Claude の入力ボックス下の chrome 行 (⏵⏵ bypass...)
        は 1 footer につき 1 回出る。2 以上なら二重 footer。

        marker が画面に出ない fixture では 0 を返す（その場合は別の検証で
        sentinel 残留をチェックする）。
        """
        return self.count_lines_with(marker)
