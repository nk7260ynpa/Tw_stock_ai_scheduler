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


def test_yt_summary_date_is_yesterday():
    """YT 摘要用昨天（晨間節目約 08:30 才播，07:54 抓到的是昨天那集）。"""
    assert summaries.yt_summary_date(date(2026, 6, 27)) == "2026-06-26"


def test_yt_summary_date_crosses_month():
    """YT 昨天邏輯跨月邊界正確。"""
    assert summaries.yt_summary_date(date(2026, 7, 1)) == "2026-06-30"


def test_summary_dates_cross_year():
    """跨年邊界：元旦當天，新聞與 YT 皆回前一年最後一天。"""
    assert summaries.news_summary_date(date(2027, 1, 1)) == "2026-12-31"
    assert summaries.yt_summary_date(date(2027, 1, 1)) == "2026-12-31"


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


# ── YT 摘要冪等檢查 yt_summary_already_exists ──────────────────────────────
def test_yt_summary_already_exists_false_when_missing(tmp_path, monkeypatch):
    """輸出檔不存在 → False。"""
    monkeypatch.setattr(summaries, "YT_OUTPUT_DIR", tmp_path)
    assert summaries.yt_summary_already_exists("2026-06-29") is False


def test_yt_summary_already_exists_false_when_empty(tmp_path, monkeypatch):
    """輸出檔存在但為空 → False。"""
    monkeypatch.setattr(summaries, "YT_OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-06-29.md").touch()  # 空檔
    assert summaries.yt_summary_already_exists("2026-06-29") is False


def test_yt_summary_already_exists_true_when_present(tmp_path, monkeypatch):
    """輸出檔存在且非空 → True。"""
    monkeypatch.setattr(summaries, "YT_OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-06-29.md").write_text("# 摘要", encoding="utf-8")
    assert summaries.yt_summary_already_exists("2026-06-29") is True


# ── 每日新聞摘要冪等檢查 news_summary_already_exists ────────────────────────
def test_news_summary_already_exists_false_when_missing(tmp_path, monkeypatch):
    """輸出檔不存在 → False。"""
    monkeypatch.setattr(summaries, "NEWS_OUTPUT_DIR", tmp_path)
    assert summaries.news_summary_already_exists("2026-06-28") is False


def test_news_summary_already_exists_false_when_empty(tmp_path, monkeypatch):
    """輸出檔存在但為空 → False。"""
    monkeypatch.setattr(summaries, "NEWS_OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-06-28.md").touch()  # 空檔
    assert summaries.news_summary_already_exists("2026-06-28") is False


def test_news_summary_already_exists_true_when_present(tmp_path, monkeypatch):
    """輸出檔存在且非空 → True。"""
    monkeypatch.setattr(summaries, "NEWS_OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-06-28.md").write_text("# 摘要", encoding="utf-8")
    assert summaries.news_summary_already_exists("2026-06-28") is True


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
    # 成本應跨三次嘗試累加（每次 0.1）
    assert res["cost"] == pytest.approx(0.3)


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
    """即使檔案確實被產出，is_error=True 仍不算成功（需重試/最終失敗）。"""
    out = tmp_path / "o.md"

    async def runner(prompt):
        out.write_text("# 有檔但 SDK 回報錯誤", encoding="utf-8")
        return {"result": "ERR", "cost": 0.1,
                "is_error": True, "num_messages": 2}

    sleeper, delays = _recording_sleeper()
    res = asyncio.run(summaries.run_summary_with_retry(
        "p", out, runner=runner, sleeper=sleeper,
        max_attempts=2, base_delay=5.0,
    ))
    assert res["produced"] is True   # 檔案確實存在
    assert res["is_error"] is True   # 但 is_error → 不算成功
    assert res["attempts"] == 2      # 故用盡重試
    assert delays == [5.0]


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


# ── A1：build_options 的 stderr callback 與 debug 開關 ───────────────────
def test_build_options_passes_stderr_callback():
    """提供 stderr_callback 時應塞進 options.stderr（讓 SDK pipe CLI stderr）。"""
    def cb(line):  # noqa: ANN001, D401
        pass

    opts = summaries.build_options(stderr_callback=cb)
    assert opts.stderr is cb


def test_build_options_default_no_stderr_and_no_debug(monkeypatch):
    """預設不帶 callback、SDK_DEBUG 關 → stderr 為 None、無 debug-to-stderr。"""
    monkeypatch.setattr(summaries, "SDK_DEBUG", False)
    opts = summaries.build_options()
    assert opts.stderr is None
    assert "debug-to-stderr" not in (opts.extra_args or {})


def test_build_options_debug_toggle_adds_extra_arg(monkeypatch):
    """SDK_DEBUG 開 → extra_args 帶 debug-to-stderr，讓 CLI 輸出詳盡除錯。"""
    monkeypatch.setattr(summaries, "SDK_DEBUG", True)
    opts = summaries.build_options()
    assert "debug-to-stderr" in (opts.extra_args or {})


# ── A2：run_prompt 收集 rate-limit / stop_reason / stderr 尾段 ───────────
class _FakeAgen:
    """假 async generator：依序吐訊息，可選在收尾拋例外，並記錄是否 aclose。"""

    def __init__(self, messages, *, raise_exc=None):
        self._it = iter(messages)
        self._raise_exc = raise_exc
        self.aclosed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            if self._raise_exc is not None:
                exc, self._raise_exc = self._raise_exc, None
                raise exc
            raise StopAsyncIteration

    async def aclose(self):
        self.aclosed = True


def _fake_result_message(**kw):
    from claude_agent_sdk import ResultMessage

    defaults = dict(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="s", stop_reason="end_turn",
        total_cost_usd=0.5, result="DONE", errors=None,
    )
    defaults.update(kw)
    return ResultMessage(**defaults)


def _fake_rate_limit_event(**kw):
    from claude_agent_sdk import RateLimitEvent, RateLimitInfo

    info = RateLimitInfo(
        status=kw.get("status", "rejected"),
        rate_limit_type=kw.get("rate_limit_type", "five_hour"),
        utilization=kw.get("utilization", 0.99),
        resets_at=kw.get("resets_at", 1234567890),
        overage_status=kw.get("overage_status", "disabled"),
    )
    return RateLimitEvent(rate_limit_info=info, uuid="u", session_id="s")


def test_run_prompt_collects_rate_limit_and_stop_reason(monkeypatch):
    """run_prompt 應從 RateLimitEvent / ResultMessage 收診斷欄位並 aclose。"""
    agen = _FakeAgen([
        _fake_rate_limit_event(status="rejected", utilization=0.99),
        _fake_result_message(stop_reason="end_turn", errors=["boom"]),
    ])
    monkeypatch.setattr(summaries, "query", lambda **kw: agen)

    data = asyncio.run(summaries.run_prompt("p"))

    assert data["rate_limit"]["status"] == "rejected"
    assert data["rate_limit"]["utilization"] == 0.99
    assert data["rate_limit"]["rate_limit_type"] == "five_hour"
    assert data["stop_reason"] == "end_turn"
    assert data["errors"] == ["boom"]
    assert data["result"] == "DONE"
    assert data["is_error"] is False
    assert agen.aclosed is True  # 顯式 aclose（孤兒行程防護）


def test_run_prompt_captures_sdk_exception_without_propagating(monkeypatch):
    """SDK 串流拋例外（如 exit code 1）應收斂為 error、不外拋，並保留 stderr。"""
    agen = _FakeAgen(
        [_fake_rate_limit_event(status="rejected")],
        raise_exc=RuntimeError("Command failed with exit code 1"),
    )
    monkeypatch.setattr(summaries, "query", lambda **kw: agen)

    data = asyncio.run(summaries.run_prompt("p"))  # 不應拋例外

    assert data["is_error"] is True
    assert data["error"] is not None and "exit code 1" in data["error"]
    assert data["rate_limit"]["status"] == "rejected"  # 例外前已收到的資訊保留
    assert data["stderr_tail"] == []  # 假 query 未觸發 stderr callback
    assert agen.aclosed is True


# ── B：run_summary_with_retry 單次呼叫逾時（含孤兒行程防護）───────────────
def test_retry_times_out_and_cancels_runner(tmp_path):
    """呼叫超過 call_timeout → 取消該次呼叫、記 timeout、視為失敗。"""
    out = tmp_path / "o.md"
    cancelled = {"yes": False}

    async def slow_runner(prompt):
        try:
            await asyncio.sleep(5)  # 遠超 call_timeout
            return {"result": "DONE", "cost": 0.1,
                    "is_error": False, "num_messages": 1}
        except asyncio.CancelledError:
            cancelled["yes"] = True  # 逾時確實取消了呼叫（孤兒行程防護）
            raise

    sleeper, _ = _recording_sleeper()
    res = asyncio.run(summaries.run_summary_with_retry(
        "p", out, runner=slow_runner, sleeper=sleeper,
        max_attempts=1, call_timeout=0.05,
    ))
    assert res["is_error"] is True
    assert res["produced"] is False
    assert res["error"] is not None and "timeout" in res["error"]
    assert cancelled["yes"] is True


def test_retry_timeout_then_retries(tmp_path):
    """首次逾時、第二次成功：驗證逾時走既有失敗/重試路徑。"""
    out = tmp_path / "o.md"
    state = {"calls": 0}

    async def runner(prompt):
        state["calls"] += 1
        if state["calls"] == 1:
            await asyncio.sleep(5)  # 首次逾時
        out.write_text("# 摘要", encoding="utf-8")
        return {"result": "DONE", "cost": 0.2,
                "is_error": False, "num_messages": 3}

    sleeper, delays = _recording_sleeper()
    res = asyncio.run(summaries.run_summary_with_retry(
        "p", out, runner=runner, sleeper=sleeper,
        max_attempts=2, base_delay=1.0, call_timeout=0.05,
    ))
    assert res["produced"] is True
    assert res["attempts"] == 2
    assert delays == [1.0]  # 首次逾時後退避一次


def test_retry_propagates_diagnostics(tmp_path):
    """runner 回傳的 stderr_tail/rate_limit/stop_reason/errors 應併入 outcome。"""
    out = tmp_path / "o.md"

    async def runner(prompt):
        out.write_text("x", encoding="utf-8")
        return {
            "result": "DONE", "cost": 0.1, "is_error": False, "num_messages": 1,
            "stderr_tail": ["boom line 1", "boom line 2"],
            "rate_limit": {"status": "rejected"}, "stop_reason": "refusal",
            "errors": ["e1"],
        }

    sleeper, _ = _recording_sleeper()
    res = asyncio.run(summaries.run_summary_with_retry(
        "p", out, runner=runner, sleeper=sleeper, max_attempts=1,
    ))
    assert res["rate_limit"] == {"status": "rejected"}
    assert res["stop_reason"] == "refusal"
    assert res["errors"] == ["e1"]
    assert res["stderr_tail"] == ["boom line 1", "boom line 2"]


def test_retry_default_call_timeout_uses_module_constant(tmp_path, monkeypatch):
    """call_timeout=None 時應採模組級 SDK_CALL_TIMEOUT_SEC。"""
    out = tmp_path / "o.md"
    monkeypatch.setattr(summaries, "SDK_CALL_TIMEOUT_SEC", 0.05)
    cancelled = {"yes": False}

    async def slow_runner(prompt):
        try:
            await asyncio.sleep(5)
            return {"result": "DONE", "cost": 0.1,
                    "is_error": False, "num_messages": 1}
        except asyncio.CancelledError:
            cancelled["yes"] = True
            raise

    sleeper, _ = _recording_sleeper()
    res = asyncio.run(summaries.run_summary_with_retry(
        "p", out, runner=slow_runner, sleeper=sleeper, max_attempts=1,
    ))  # 未傳 call_timeout → 用模組常數 0.05
    assert res["is_error"] is True
    assert "timeout" in res["error"]
    assert cancelled["yes"] is True
