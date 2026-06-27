"""`summaries.py` 純函式單元測試。

只測純邏輯（日期計算、輸出路徑、產出防呆、來源檢查、prompt 組裝、SDK 選項），
不真正呼叫 Claude Agent SDK（避免耗用訂閱）。
"""

import os
import time
from datetime import date
from pathlib import Path

import pytest

import summaries


# ── 日期邏輯 ────────────────────────────────────────────────────────────
def test_news_summary_date_is_yesterday():
    """新聞摘要用昨天。"""
    assert summaries.news_summary_date(date(2026, 6, 27)) == "2026-06-26"


def test_news_summary_date_crosses_month():
    """跨月邊界正確。"""
    assert summaries.news_summary_date(date(2026, 7, 1)) == "2026-06-30"


def test_yt_summary_date_is_today():
    """YT 摘要用今天。"""
    assert summaries.yt_summary_date(date(2026, 6, 27)) == "2026-06-27"


def test_weekday_zh():
    """中文星期換算（2026-06-26 為週五）。"""
    assert summaries._weekday_zh("2026-06-26") == "週五"
    assert summaries._weekday_zh("2026-06-22") == "週一"


# ── 輸出路徑 ────────────────────────────────────────────────────────────
def test_news_output_path():
    p = summaries.news_output_path("2026-06-26")
    assert p.name == "2026-06-26.md"
    assert p.parent == summaries.NEWS_OUTPUT_DIR


def test_yt_output_path():
    p = summaries.yt_output_path("2026-06-22")
    assert p.name == "2026-06-22.md"
    assert p.parent == summaries.YT_OUTPUT_DIR


# ── 產出防呆 ────────────────────────────────────────────────────────────
def test_output_is_fresh_true_when_updated_after_start(tmp_path):
    """任務開始後產出非空檔 → 視為成功。"""
    f = tmp_path / "out.md"
    start_ts = time.time()
    f.write_text("# 內容", encoding="utf-8")
    assert summaries.output_is_fresh(f, start_ts) is True


def test_output_is_fresh_false_when_missing(tmp_path):
    """檔案不存在（空跑情形）→ 失敗。"""
    f = tmp_path / "missing.md"
    assert summaries.output_is_fresh(f, time.time()) is False


def test_output_is_fresh_false_when_empty(tmp_path):
    """檔案存在但為空 → 失敗。"""
    f = tmp_path / "empty.md"
    f.touch()
    assert summaries.output_is_fresh(f, time.time()) is False


def test_output_is_fresh_false_when_stale(tmp_path):
    """檔案是任務開始前的舊檔（未被本次更新）→ 失敗。"""
    f = tmp_path / "stale.md"
    f.write_text("# 舊內容", encoding="utf-8")
    old = time.time() - 3600
    os.utime(f, (old, old))  # 把 mtime 設成一小時前
    start_ts = time.time()
    assert summaries.output_is_fresh(f, start_ts) is False


# ── 來源檢查 ────────────────────────────────────────────────────────────
def test_news_source_counts(tmp_path, monkeypatch):
    """正確計數四來源檔案數。"""
    monkeypatch.setattr(summaries, "NEWS_SOURCE_ROOT", tmp_path)
    d = "2026-06-26"
    (tmp_path / "CTEE" / d).mkdir(parents=True)
    (tmp_path / "CTEE" / d / "a.txt").write_text("x")
    (tmp_path / "CTEE" / d / "b.txt").write_text("x")
    (tmp_path / "CNYES" / d).mkdir(parents=True)
    (tmp_path / "CNYES" / d / "c.md").write_text("x")
    # PTT、MoneyUDN 目錄不存在 → 0
    counts = summaries.news_source_counts(d)
    assert counts == {"CTEE": 2, "CNYES": 1, "PTT": 0, "MoneyUDN": 0}


def test_news_source_counts_respects_extension(tmp_path, monkeypatch):
    """CTEE 只算 .txt，CNYES/PTT/MoneyUDN 只算 .md。"""
    monkeypatch.setattr(summaries, "NEWS_SOURCE_ROOT", tmp_path)
    d = "2026-06-26"
    (tmp_path / "CTEE" / d).mkdir(parents=True)
    (tmp_path / "CTEE" / d / "a.txt").write_text("x")
    (tmp_path / "CTEE" / d / "ignore.md").write_text("x")  # 不該被算到
    assert summaries.news_source_counts(d)["CTEE"] == 1


def test_news_sources_available(tmp_path, monkeypatch):
    monkeypatch.setattr(summaries, "NEWS_SOURCE_ROOT", tmp_path)
    d = "2026-06-26"
    assert summaries.news_sources_available(d) is False
    (tmp_path / "PTT" / d).mkdir(parents=True)
    (tmp_path / "PTT" / d / "x.md").write_text("x")
    assert summaries.news_sources_available(d) is True


def test_yt_source_available(tmp_path, monkeypatch):
    monkeypatch.setattr(summaries, "NEWS_SOURCE_ROOT", tmp_path)
    d = "2026-06-22"
    assert summaries.yt_source_available(d) is False
    yt_dir = tmp_path / "YT" / d
    yt_dir.mkdir(parents=True)
    (yt_dir / f"{d}.md").write_text("逐字稿內容")
    assert summaries.yt_source_available(d) is True


def test_yt_source_available_false_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(summaries, "NEWS_SOURCE_ROOT", tmp_path)
    d = "2026-06-22"
    yt_dir = tmp_path / "YT" / d
    yt_dir.mkdir(parents=True)
    (yt_dir / f"{d}.md").touch()  # 空檔
    assert summaries.yt_source_available(d) is False


# ── Prompt 組裝 ─────────────────────────────────────────────────────────
def test_build_news_prompt_contains_key_parts():
    p = summaries.build_news_prompt("2026-06-26")
    assert "2026-06-26 台股每日新聞摘要" in p
    assert "Tw_stock_news/DailyNews/2026-06-26.md" in p
    for src in ("CTEE", "CNYES", "PTT", "MoneyUDN"):
        assert src in p
    # 不可走 slash skill
    assert "/news-summary" not in p


def test_build_yt_prompt_contains_key_parts():
    p = summaries.build_yt_prompt("2026-06-22")
    assert "2026-06-22 游庭皓的財經皓角 — 精華摘要" in p
    assert "Tw_stock_DB/NewsContents/YT/2026-06-22/2026-06-22.md" in p
    assert "Tw_stock_news/YTNews/2026-06-22.md" in p
    assert "週一" in p  # 2026-06-22 為週一
    assert "/yt-summary" not in p


# ── SDK 選項（採直接餵 prompt，不靠 skill 載入）────────────────────────
def test_build_options_cwd_is_workspace():
    """工作目錄須為 Tw_stock/（skill 與相對路徑基準）。"""
    opts = summaries.build_options()
    assert opts.cwd == str(summaries.WORKSPACE)


def test_build_options_allowed_tools():
    opts = summaries.build_options()
    assert opts.allowed_tools == ["Read", "Write", "Glob", "Grep", "Bash"]
    assert opts.permission_mode == "acceptEdits"


def test_build_options_does_not_set_api_key_env():
    """確保模組未在 import / 建立選項時注入 ANTHROPIC_API_KEY。"""
    opts = summaries.build_options()
    assert "ANTHROPIC_API_KEY" not in (opts.env or {})


def test_workspace_layout():
    """WORKSPACE 應為本 repo 的上層（Tw_stock/）。"""
    assert summaries.WORKSPACE == summaries.BASE_DIR.parent
    assert summaries.BASE_DIR.name == "Tw_stock_ai_scheduler"
