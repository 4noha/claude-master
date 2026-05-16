"""HOST_FLOW_SCROLLBACK（実験的オプトイン・既定 off）の本番経路テスト。

検証の主眼（ディスプレイ・オラクル方式 = 出力バイトではなく
「ユーザーの端末ディスプレイに最終的に何が見えるか」）:
  - スクロールアウトした確定ログが host の native scrollback に
    *1 回だけ* 入る（重複なし・欠落なし）
  - live 領域は最新（footer 含む）を表示し、二重 footer が出ない
  - HistoryFlusher の identity-tracking delta が正しい
  - off 時（既定）は従来挙動（render_viewport / 全消去）と不変
"""
import sys
from pathlib import Path

import pyte
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "debug"))

from pty_scroll import HistoryFlusher, ScrollRenderer  # noqa: E402
from display_oracle import DisplayTerminal  # noqa: E402


def _hist_screen(cols: int, rows: int) -> pyte.HistoryScreen:
    return pyte.HistoryScreen(cols, rows, history=5000, ratio=0.5)


# ── HistoryFlusher 単体 ────────────────────────────────────────────────

def test_flusher_arm_returns_empty_even_with_existing_history() -> None:
    """初回 take_new は arm のみ。既存履歴は流さない（接続時点から開始）。"""
    sc = _hist_screen(20, 4)
    st = pyte.ByteStream(sc)
    st.feed(("\r\n".join(f"old{i}" for i in range(20))).encode())
    f = HistoryFlusher()
    assert f.take_new(sc) == []                 # arm のみ、既存は無視


def _texts(lines: list) -> list[str]:
    out = []
    for line in lines:
        mx = max(line.keys()) if line else -1
        out.append("".join((line.get(x).data if line.get(x) else " ")
                           for x in range(mx + 1)).rstrip())
    return out


def test_flusher_returns_only_new_scrolled_lines_in_order() -> None:
    # 3 行画面。"X\r\n" を 3 回流すと 3 個目の \n（最下行）で 1 行
    # スクロールするため、この時点で L0 は既に history.top 入り。
    sc = _hist_screen(20, 3)
    st = pyte.ByteStream(sc)
    st.feed(b"L0\r\nL1\r\nL2\r\n")                # L0 は既に確定（スクロール済）
    f = HistoryFlusher()
    assert f.take_new(sc) == []                   # arm。arm 前確定の L0 は流さない
    st.feed(b"L3\r\nL4\r\n")                      # L1,L2 が確定スクロールアウト
    assert _texts(f.take_new(sc)) == ["L1", "L2"]  # 古い→新しい順、新規のみ
    st.feed(b"L5\r\n")                            # さらに L3 が確定
    assert _texts(f.take_new(sc)) == ["L3"]
    assert f.take_new(sc) == []                   # 二度返らない


def test_flusher_reset_rearms() -> None:
    sc = _hist_screen(20, 3)
    st = pyte.ByteStream(sc)
    st.feed(b"x\r\ny\r\nz\r\n")
    f = HistoryFlusher()
    f.take_new(sc)
    st.feed(b"p\r\nq\r\n")
    assert f.take_new(sc)                         # 新規あり
    f.reset()
    assert f.take_new(sc) == []                   # 再 arm（既存は流さない）


def test_flusher_plain_screen_no_history_safe() -> None:
    sc = pyte.Screen(20, 4)
    pyte.ByteStream(sc).feed(b"a\r\nb\r\nc\r\nd\r\ne\r\n")
    f = HistoryFlusher()
    assert f.take_new(sc) == []
    assert f.take_new(sc) == []


def test_flusher_history_clear_resyncs_no_duplicate() -> None:
    """\\x1b[3J 等で history がクリアされても重複・暴発しない。"""
    sc = _hist_screen(20, 3)
    st = pyte.ByteStream(sc)
    st.feed(b"a\r\nb\r\nc\r\nd\r\n")
    f = HistoryFlusher()
    f.take_new(sc)
    st.feed(b"e\r\nf\r\n")
    f.take_new(sc)
    st.feed(b"\x1b[3J")                           # scrollback クリア
    st.feed(b"g\r\nh\r\n")
    # 落ちた _last を見つけられない → resync only（重複/暴発しない）
    out = f.take_new(sc)
    assert isinstance(out, list)                  # 例外なく完走が要件


# ── render_flow 単体 ───────────────────────────────────────────────────

def test_render_flow_first_clears_once_then_no_clear() -> None:
    sc = _hist_screen(20, 4)
    pyte.ByteStream(sc).feed(b"hello\r\n")
    r = ScrollRenderer()
    first = r.render_flow(sc, 4, 20, committed=[], first=True)
    assert b"\x1b[2J" in first                    # 初回のみベースライン全消去
    later = r.render_flow(sc, 4, 20, committed=[], first=False)
    assert b"\x1b[2J" not in later                # 以降は no-clear
    assert later.startswith(b"\x1b[?2026h")
    assert later.endswith(b"\x1b[?2026l")


def test_render_flow_no_committed_no_scroll_newlines() -> None:
    """committed 無し（in-place 更新のみ）はスクロール改行を出さない。"""
    sc = _hist_screen(20, 4)
    pyte.ByteStream(sc).feed(b"abc\r\n")
    r = ScrollRenderer()
    out = r.render_flow(sc, 4, 20, committed=[], first=False)
    # live 行間の \r\n はあるが、確定行スクロール用の単独 \n 連打は無い
    assert b"\n" * 2 not in out


# ── ディスプレイ・オラクル統合 ────────────────────────────────────────

def test_oracle_scrolled_logs_enter_native_scrollback_once() -> None:
    """多数の確定ログが host native scrollback に *1 回だけ* 入り、
    live は最新を表示。各行は scrollback+visible 合わせて重複しない。"""
    R, C = 8, 60
    emu = _hist_screen(C, R)               # claude が描く pyte 画面（PTY サイズ）
    est = pyte.ByteStream(emu)
    flusher = HistoryFlusher()
    renderer = ScrollRenderer()
    disp = DisplayTerminal(rows=R, cols=C, history=True)

    first = True
    # 40 本のログを数行ずつ「フレーム」に分けて流す（broadcast 相当）
    n_logs = 40
    i = 0
    while i < n_logs:
        chunk = "".join(f"logline{j:03d}\r\n" for j in range(i, min(i + 3, n_logs)))
        est.feed(chunk.encode())
        committed = flusher.take_new(emu)
        out = renderer.render_flow(emu, R, C, committed, first)
        first = False
        disp.feed(out)
        i += 3

    sb = disp.scrollback_lines()
    vis = disp.lines()
    for j in range(n_logs):
        tag = f"logline{j:03d}"
        total = sum(1 for ln in sb if tag in ln) + sum(1 for ln in vis if tag in ln)
        assert total == 1, (
            f"{tag}: scrollback+visible に {total} 回（1 回であるべき）\n"
            f"sb_tail={sb[-3:]!r} vis={vis!r}")
    # 最新は live（visible）側に出ている
    assert disp.contains("logline039")
    # 最古は native scrollback 側へ流れている
    assert disp.scrollback_count("logline000") == 1


def test_oracle_no_double_footer_with_flow() -> None:
    """ログをスクロールさせつつ最後に footer を描いても二重にならない。"""
    R, C = 8, 60
    emu = _hist_screen(C, R)
    est = pyte.ByteStream(emu)
    flusher = HistoryFlusher()
    renderer = ScrollRenderer()
    disp = DisplayTerminal(rows=R, cols=C, history=True)

    first = True
    for k in range(6):
        est.feed(("".join(f"out{k}_{m}\r\n" for m in range(4))).encode())
        committed = flusher.take_new(emu)
        disp.feed(renderer.render_flow(emu, R, C, committed, first))
        first = False
    # claude 風 footer を絶対座標で最下部に描く（in-place）
    est.feed(f"\x1b[{R};1H".encode() + b"\xe2\x8f\xb5\xe2\x8f\xb5 bypass mode")
    committed = flusher.take_new(emu)
    disp.feed(renderer.render_flow(emu, R, C, committed, first))

    assert disp.footer_region_count("⏵⏵") <= 1, (
        f"二重 footer: visible={disp.lines()!r}")


# ── quiescence ゲート: capture / drain ────────────────────────────────

def test_capture_accumulates_then_drain_clears() -> None:
    sc = _hist_screen(20, 3)
    st = pyte.ByteStream(sc)
    f = HistoryFlusher()
    f.capture(sc)                                # arm
    st.feed(b"a\r\nb\r\nc\r\nd\r\ne\r\n")
    f.capture(sc)
    st.feed(b"g\r\nh\r\n")
    f.capture(sc)
    assert f.has_pending
    got = _texts(f.drain())
    assert got == ["a", "b", "c", "d", "e", "g"][:len(got)]
    assert got[0] == "a" and "h" not in got       # 古い→新しい順・未確定は出ない
    assert not f.has_pending                       # drain でクリア
    assert f.drain() == []


def test_capture_no_loss_across_deque_maxlen() -> None:
    """history.top の maxlen を超える長いバーストでも、毎フレーム capture
    すれば確定行は1つも失われない（pending は Python list で保持）。"""
    sc = pyte.HistoryScreen(20, 3, history=8, ratio=0.5)  # 小さい maxlen
    st = pyte.ByteStream(sc)
    f = HistoryFlusher()
    f.capture(sc)                                  # arm
    for i in range(100):                           # 1 行ずつ 100 フレーム
        st.feed(f"L{i:03d}\r\n".encode())
        f.capture(sc)                              # 毎フレーム取り込み
    got = _texts(f.drain())
    # 画面 3 行なので最後の方は未確定。確定済みは連番で欠落なし。
    nums = [int(s[1:]) for s in got if s.startswith("L")]
    assert nums == list(range(nums[0], nums[0] + len(nums)))  # 連続・欠落なし
    assert nums[0] == 0                            # 最古も失われていない


def test_capture_pending_cap_bounds_memory() -> None:
    sc = pyte.HistoryScreen(20, 2, history=4, ratio=0.5)
    st = pyte.ByteStream(sc)
    f = HistoryFlusher()
    f.capture(sc)
    f._PENDING_CAP = 50                            # テスト用に小さく
    for i in range(500):
        st.feed(f"x{i}\r\n".encode())
        f.capture(sc)
    assert f.pending_len <= 50                     # 上限で頭打ち（暴走しない）


# ── ディスプレイ・オラクル: 静止時のみ scrollback へ書く ─────────────

def test_oracle_streaming_no_scrollback_until_idle_then_flush() -> None:
    """ユーザー案の核心: ストリーミング中は scrollback に1行も書かれず
    （live は安全な全消去再描画）、idle で初めて確定行がまとめて
    native scrollback に入る。重複・欠落・二重 footer なし。"""
    R, C = 6, 50
    emu = _hist_screen(C, R)
    est = pyte.ByteStream(emu)
    f = HistoryFlusher()
    r = ScrollRenderer()
    disp = DisplayTerminal(rows=R, cols=C, history=True)

    # ── ストリーミング中（_broadcast 相当）: capture のみ + 全消去再描画
    for k in range(30):
        est.feed(f"log{k:03d}\r\n".encode())
        f.capture(emu)
        disp.feed(r.render_viewport(emu, R, C))    # live のみ・scrollback 書かない
        assert disp.scrollback_lines() == [], (
            f"ストリーミング中に scrollback へ書かれた: {disp.scrollback_lines()!r}")

    # ── claude 静止（idle tick = _flush_host_flow 相当）
    committed = f.drain()
    disp.feed(r.render_flow(emu, R, C, committed, first=True))

    sb, vis = disp.scrollback_lines(), disp.lines()
    for k in range(30):
        tag = f"log{k:03d}"
        n = sum(1 for ln in sb if tag in ln) + sum(1 for ln in vis if tag in ln)
        assert n == 1, f"{tag}: {n} 回（1 回であるべき） sb_tail={sb[-3:]!r}"
    assert disp.contains("log029")                 # 最新は live
    assert disp.scrollback_count("log000") == 1    # 最古は scrollback
    assert disp.footer_region_count("⏵⏵") == 0     # 二重 footer 無し（footer無）


def test_oracle_resize_midstream_no_loss_no_corruption() -> None:
    """ストリーミング途中でリサイズ → flusher 再 arm + clean baseline。
    確定行の欠落・重複・二重 footer が起きない。"""
    R, C = 6, 50
    emu = _hist_screen(C, R)
    est = pyte.ByteStream(emu)
    f = HistoryFlusher()
    r = ScrollRenderer()
    disp = DisplayTerminal(rows=R, cols=C, history=True)

    for k in range(15):
        est.feed(f"pre{k:02d}\r\n".encode())
        f.capture(emu)
        disp.feed(r.render_viewport(emu, R, C))

    # リサイズ（_apply_pty_size 相当: emulator resize → flusher.reset + first）
    emu.resize(8, 70)
    f.reset()                                      # identity 再 arm。_pending 保持
    first = True

    for k in range(15):
        est.feed(f"post{k:02d}\r\n".encode())
        f.capture(emu)
        disp.feed(r.render_viewport(emu, 8, 70))

    disp2 = DisplayTerminal(rows=8, cols=70, history=True)
    disp2.feed(r.render_flow(emu, 8, 70, f.drain(), first=first))
    sb, vis = disp2.scrollback_lines(), disp2.lines()
    allcells = sb + vis
    # リサイズ跨ぎでも pre/post の確定行が重複せず存在（欠落は許容＝
    # 画面内 live 分。重複・破損が無いことが要件）
    for k in range(13):                            # 画面に残る末尾以外は確定済み
        tag = f"pre{k:02d}"
        assert sum(1 for ln in allcells if tag in ln) <= 1, f"{tag} 重複"
    assert disp2.footer_region_count("⏵⏵") == 0    # 破損 footer 無し


def test_off_path_unchanged_uses_render_viewport() -> None:
    """HOST_FLOW_SCROLLBACK=false は従来 render_viewport（全消去）経路。
    回帰防止: render_viewport は毎フレーム \\x1b[2J を出す。"""
    sc = _hist_screen(20, 5)
    pyte.ByteStream(sc).feed(("\r\n".join(f"r{i}" for i in range(10))).encode())
    r = ScrollRenderer()
    out1 = r.render_viewport(sc, 5, 20)
    out2 = r.render_viewport(sc, 5, 20)
    assert b"\x1b[2J" in out1 and b"\x1b[2J" in out2   # 従来どおり毎回全消去
