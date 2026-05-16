"""セッション全文ログ（SESSION_LOG）の本番経路テスト。

実 `claude --resume` 録画（fixtures/resume-burst/bytes.bin）を PtyProxy に
流し、ファイルへ忠実なプレーンテキスト転写が書かれることを検証する。
ターミナル描画には触れないので live 破壊・native scrollback・dedup の
問題は構造的に無い（Claude の再ストリームは実出力どおり残る＝
内容比較 dedup はしない、が本テストの非目標）。
"""
import json
import os
import sys
from pathlib import Path

import pyte
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import pty_proxy  # noqa: E402
from pty_proxy import PtyProxy  # noqa: E402
from pty_scroll import line_to_text  # noqa: E402

FX = Path(__file__).resolve().parent.parent / "fixtures" / "resume-burst"


def test_line_to_text_plain_and_widechar() -> None:
    sc = pyte.Screen(12, 2)
    pyte.ByteStream(sc).feed("ねこ ab  \r\n".encode())
    t = line_to_text(sc.buffer[0])
    assert t == "ねこ ab"                       # 末尾空白除去
    assert "\x1b" not in t                       # ANSI なし
    assert "ね こ" not in t                      # 全角継続セルで割れない
    assert line_to_text(None) == ""              # 空行安全


def _event_chunks() -> list[int]:
    out = []
    for ln in (FX / "events.log").read_text().splitlines():
        p = ln.split()
        if len(p) >= 3 and p[1] == "pty_raw":
            out.append(int(p[2]))
    return out


@pytest.fixture
def proxy_with_log(tmp_path, monkeypatch):
    logfile = tmp_path / "session.log"
    monkeypatch.setattr(pty_proxy, "SESSION_LOG", str(logfile))
    master_fd, slave_fd = os.openpty()
    p = PtyProxy(master_fd, child_pid=4242, child_pgid=4242,
                 sock_path=tmp_path / "x.sock")
    yield p, logfile
    for fd in (master_fd, slave_fd):
        try:
            os.close(fd)
        except OSError:
            pass


def test_session_log_real_resume_recording(proxy_with_log) -> None:
    """実録画を流す → ファイルに会話本文が忠実に転写され、ANSI を含まず、
    終了時に最終可視画面まで書かれて閉じられる。"""
    proxy, logfile = proxy_with_log
    assert proxy._session_fp is not None          # SESSION_LOG で開いた
    meta = json.loads((FX / "meta.json").read_text())
    proxy._emulator.resize(meta["height"], meta["width"])
    data = (FX / "bytes.bin").read_bytes()

    off = 0
    for n in _event_chunks():
        proxy._broadcast(data[off:off + n])       # _session_log_capture 経由
        off += n
    if off < len(data):
        proxy._broadcast(data[off:])

    proxy._finalize_session_log()                 # 終了時の吐き出し
    assert proxy._session_fp is None              # クローズ済み

    text = logfile.read_text(encoding="utf-8", errors="replace")
    assert "\x1b" not in text                     # プレーンテキスト（ANSI 無）
    assert "===== claude-master session" in text  # ヘッダ
    assert "===== session end" in text            # フッタ（終了時 flush）
    # 録画に含まれる実会話本文が忠実に出ている
    assert "Claude" in text
    assert "claude-master" in text
    # 多数行の転写になっている（空ではない）
    body = [l for l in text.splitlines() if l and not l.startswith("=====")]
    assert len(body) > 100, f"転写が少なすぎる: {len(body)} 行"


def test_session_log_disabled_by_default(tmp_path, monkeypatch) -> None:
    """SESSION_LOG 未設定なら何も開かない（既定無効）。"""
    monkeypatch.setattr(pty_proxy, "SESSION_LOG", "")
    master_fd, slave_fd = os.openpty()
    try:
        p = PtyProxy(master_fd, child_pid=1, child_pgid=1,
                     sock_path=tmp_path / "y.sock")
        assert p._session_fp is None
        assert p._session_flusher is None
        p._broadcast(b"hello\r\n")                 # 例外なく no-op
        p._finalize_session_log()                  # no-op
    finally:
        for fd in (master_fd, slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def test_session_log_true_uses_auto_path(tmp_path, monkeypatch) -> None:
    """SESSION_LOG=true は LOGS_DIR/session-<pid>.log を使う。"""
    monkeypatch.setattr(pty_proxy, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(pty_proxy, "SESSION_LOG", "true")
    master_fd, slave_fd = os.openpty()
    try:
        p = PtyProxy(master_fd, child_pid=777, child_pgid=777,
                     sock_path=tmp_path / "z.sock")
        assert p._session_fp is not None
        p._broadcast(b"alpha\r\nbeta\r\n")
        p._finalize_session_log()
        f = tmp_path / "session-777.log"
        assert f.is_file()
        assert "alpha" in f.read_text()
    finally:
        for fd in (master_fd, slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass
