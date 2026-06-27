"""`summaries.py` 純函式與韌性邏輯單元測試。

只測純邏輯（日期計算、輸出路徑、產出防呆、來源檢查、prompt 組裝、SDK 選項、
重試／可重入／結算），不真正呼叫 Claude Agent SDK（避免耗用訂閱）：韌性測試以
注入假 runner／sleeper 取代真實 SDK。
"""

import asyncio
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


def test_date_range_inclusive():
    """日期區間含頭含尾。"""
    assert summaries.date_range("2026-06-10", "2026-06-12") == [
        "2026-06-10", "2026-06-11", "2026-06-12",
    ]


def test_date_range_single_day():
    assert summaries.date_range("2026-06-26", "2026-06-26") == ["2026-06-26"]


def test_date_range_crosses_month():
    assert summaries.date_range("2026-06-29", "2026-07-01") == [
        "2026-06-29", "2026-06-30", "2026-07-01",
    ]


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


# ── 韌性：重試 run_summary_with_retry（注入假 runner／sleeper，不打 SDK）──
def _make_runner(succeed_on, out_file, *, is_error=False, raises=False):
    """產生假 runner：第 succeed_on 次（含）起寫出輸出檔以模擬產出成功。"""
    state = {"calls": 0}

    async def runner(prompt):
        state["calls"] += 1
        if raises:
            raise RuntimeError("Command failed with exit code 1")
        if not is_error and state["calls"] >= succeed_on:
            out_file.write_text("# 摘要", encoding="utf-8")
        return {
            "result": "DONE", "cost": 0.1,
            "is_error": is_error, "num_messages": 5,
        }

    return runner, state


def _recording_sleeper():
    """產生假 sleeper：只記錄退避秒數、不真的睡。"""
    delays = []

    async def sleeper(delay):
        delays.append(delay)

    return sleeper, delays


def test_retry_succeeds_first_attempt(tmp_path):
    out = tmp_path / "o.md"
    runner, state = _make_runner(1, out)
    sleeper, delays = _recording_sleeper()
    res = asyncio.run(summaries.run_summary_with_retry(
        "p", out, runner=runner, sleeper=sleeper, base_delay=15.0,
    ))
    assert res["produced"] is True
    assert res["is_error"] is False
    assert res["attempts"] == 1
    assert state["calls"] == 1
    assert delays == []  # 第一次就成功，無退避


def test_retry_succeeds_after_failures(tmp_path):
    """前兩次未產出、第三次成功；退避序列為 [15, 30]。"""
    out = tmp_path / "o.md"
    runner, state = _make_runner(3, out)
    sleeper, delays = _recording_sleeper()
    res = asyncio.run(summaries.run_summary_with_retry(
        "p", out, runner=runner, sleeper=sleeper,
        max_attempts=3, base_delay=15.0,
    ))
    assert res["produced"] is True
    assert res["attempts"] == 3
    assert state["calls"] == 3
    assert delays == [15.0, 30.0]


def test_retry_exhausts_when_never_produced(tmp_path):
    """始終未產出 → produced False、用盡 max_attempts。"""
    out = tmp_path / "o.md"
    runner, state = _make_runner(99, out)  # 永不寫檔
    sleeper, delays = _recording_sleeper()
    res = asyncio.run(summaries.run_summary_with_retry(
        "p", out, runner=runner, sleeper=sleeper,
        max_attempts=3, base_delay=10.0,
    ))
    assert res["produced"] is False
    assert res["attempts"] == 3
    assert state["calls"] == 3
    assert delays == [10.0, 20.0]


def test_retry_handles_exceptions(tmp_path):
    """runner 拋例外（模擬 exit code 1）→ 記 error、不外漏、用盡重試。"""
    out = tmp_path / "o.md"
    runner, state = _make_runner(1, out, raises=True)
    sleeper, delays = _recording_sleeper()
    res = asyncio.run(summaries.run_summary_with_retry(
        "p", out, runner=runner, sleeper=sleeper, max_attempts=2,
    ))
    assert res["produced"] is False
    assert res["is_error"] is True
    assert res["error"] is not None and "exit code 1" in res["error"]
    assert res["attempts"] == 2
    assert state["calls"] == 2


def test_retry_is_error_true_not_success(tmp_path):
    """即使有檔，is_error=True 也不算成功。"""
    out = tmp_path / "o.md"
    runner, _ = _make_runner(1, out, is_error=True)
    sleeper, _ = _recording_sleeper()
    res = asyncio.run(summaries.run_summary_with_retry(
        "p", out, runner=runner, sleeper=sleeper, max_attempts=1,
    ))
    assert res["is_error"] is True
    assert res["attempts"] == 1


# ── 韌性：backfill_one_day 可重入／無來源／成功 ─────────────────────────
def _log():
    import logging
    return logging.getLogger("test_backfill")


def test_backfill_skips_existing(tmp_path):
    """輸出檔已存在 → status=exists，不執行。"""
    out = tmp_path / "2026-06-10.md"
    out.write_text("已存在", encoding="utf-8")
    r = asyncio.run(summaries.backfill_one_day(
        "2026-06-10",
        build_prompt=lambda d: "p",
        output_path_fn=lambda d: out,
        source_available_fn=lambda d: True,
        log=_log(),
    ))
    assert r["status"] == "exists"


def test_backfill_skips_nosource(tmp_path):
    """來源不存在 → status=nosource。"""
    out = tmp_path / "2026-06-10.md"
    r = asyncio.run(summaries.backfill_one_day(
        "2026-06-10",
        build_prompt=lambda d: "p",
        output_path_fn=lambda d: out,
        source_available_fn=lambda d: False,
        log=_log(),
    ))
    assert r["status"] == "nosource"
    assert not out.exists()


def test_backfill_ok_path(tmp_path, monkeypatch):
    """有來源、輸出檔不存在 → 執行並產出 → status=ok。"""
    out = tmp_path / "2026-06-10.md"

    async def fake_run_prompt(prompt):
        out.write_text("# 摘要內容", encoding="utf-8")
        return {"result": "DONE", "cost": 0.2,
                "is_error": False, "num_messages": 3}

    monkeypatch.setattr(summaries, "run_prompt", fake_run_prompt)
    r = asyncio.run(summaries.backfill_one_day(
        "2026-06-10",
        build_prompt=lambda d: "p",
        output_path_fn=lambda d: out,
        source_available_fn=lambda d: True,
        log=_log(),
    ))
    assert r["status"] == "ok"
    assert r["file_size"] > 0


def test_backfill_failed_path(tmp_path, monkeypatch):
    """有來源但始終未產出 → status=failed（不拋例外）。"""
    out = tmp_path / "2026-06-10.md"

    async def fake_run_prompt(prompt):
        return {"result": None, "cost": 0,
                "is_error": False, "num_messages": 1}

    monkeypatch.setattr(summaries, "run_prompt", fake_run_prompt)
    r = asyncio.run(summaries.backfill_one_day(
        "2026-06-10",
        build_prompt=lambda d: "p",
        output_path_fn=lambda d: out,
        source_available_fn=lambda d: True,
        log=_log(),
        max_attempts=2,
        base_delay=0.0,  # 測試不真的退避等待
    ))
    assert r["status"] == "failed"


# ── 韌性：結算 summarize_backfill ───────────────────────────────────────
def test_summarize_backfill_counts_and_exit_code():
    results = [
        {"date": "d1", "status": "ok", "cost": 1.0, "elapsed": 10.0},
        {"date": "d2", "status": "exists", "cost": 0.0, "elapsed": 0.0},
        {"date": "d3", "status": "nosource", "cost": 0.0, "elapsed": 0.0},
        {"date": "d4", "status": "failed", "cost": 0.0, "elapsed": 5.0},
    ]
    code = summaries.summarize_backfill(results, _log())
    assert code == 1  # 有失敗 → 非 0


def test_summarize_backfill_all_ok_exit_zero():
    results = [
        {"date": "d1", "status": "ok", "cost": 1.0, "elapsed": 10.0},
        {"date": "d2", "status": "nosource", "cost": 0.0, "elapsed": 0.0},
    ]
    assert summaries.summarize_backfill(results, _log()) == 0
