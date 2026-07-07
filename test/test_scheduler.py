"""`ai_scheduler.py` 事件驅動輪詢邏輯單元測試（不真打 SDK）。

聚焦輪詢式 YT 摘要 :func:`ai_scheduler.job_yt_summary_poll` 與每日新聞摘要
:func:`ai_scheduler.job_news_summary_poll` 的分支行為與「每日嘗試上限」防護。
以 monkeypatch 攔截冪等檢查、來源檢查、就緒時刻判斷與 ``_run_summary_sync``，
故完全不會呼叫 Claude Agent SDK。
"""

import datetime as _dt
import logging

import pytest

import ai_scheduler
import summaries


@pytest.fixture(autouse=True)
def _reset_attempt_counts():
    """每個測試前後清空每日嘗試計數，避免測試間互相污染模組級狀態。"""
    ai_scheduler._yt_attempt_counts.clear()
    ai_scheduler._news_attempt_counts.clear()
    ai_scheduler._news_nosource_warned.clear()
    yield
    ai_scheduler._yt_attempt_counts.clear()
    ai_scheduler._news_attempt_counts.clear()
    ai_scheduler._news_nosource_warned.clear()


def _patch_poll(monkeypatch, *, already_exists, source_available):
    """共用：mock 冪等檢查與來源檢查，並以計數器攔截 ``_run_summary_sync``。

    Args:
        monkeypatch: pytest fixture。
        already_exists: ``yt_summary_already_exists`` 的回傳值。
        source_available: ``yt_source_available`` 的回傳值。

    Returns:
        dict: ``{"run": 次數}``，記錄 ``_run_summary_sync`` 被呼叫幾次。
    """
    calls = {"run": 0}

    monkeypatch.setattr(
        summaries, "yt_summary_already_exists", lambda d: already_exists,
    )
    monkeypatch.setattr(
        summaries, "yt_source_available", lambda d: source_available,
    )

    def fake_run(label, prompt, output_path):
        # 模擬失敗：不建立輸出檔（故下個 tick 仍會被視為「尚未產出」）。
        calls["run"] += 1

    monkeypatch.setattr(ai_scheduler, "_run_summary_sync", fake_run)
    return calls


def test_poll_skips_when_summary_exists(monkeypatch, caplog):
    """(a) 摘要已存在 → 不呼叫 SDK runner、且安靜不記 log。"""
    calls = _patch_poll(
        monkeypatch, already_exists=True, source_available=True,
    )
    with caplog.at_level(logging.DEBUG, logger="ai_scheduler"):
        ai_scheduler.job_yt_summary_poll()
    assert calls["run"] == 0
    assert caplog.records == []  # 冪等跳過不可洗版


def test_poll_skips_when_no_source(monkeypatch, caplog):
    """(b) 逐字稿不存在 → 不呼叫 SDK runner、且安靜不記 log。"""
    calls = _patch_poll(
        monkeypatch, already_exists=False, source_available=False,
    )
    with caplog.at_level(logging.DEBUG, logger="ai_scheduler"):
        ai_scheduler.job_yt_summary_poll()
    assert calls["run"] == 0
    assert caplog.records == []  # 來源未到不可洗版


def test_poll_runs_when_source_ready_and_no_summary(monkeypatch):
    """(c) 逐字稿存在且尚無摘要 → 呼叫 _run_summary_sync 恰一次。"""
    calls = _patch_poll(
        monkeypatch, already_exists=False, source_available=True,
    )
    ai_scheduler.job_yt_summary_poll()
    assert calls["run"] == 1


def test_poll_respects_daily_attempt_cap(monkeypatch, caplog):
    """每日嘗試上限：連續失敗達上限後不再呼叫 SDK，且只記一次 ERROR。"""
    calls = _patch_poll(
        monkeypatch, already_exists=False, source_available=True,
    )
    with caplog.at_level(logging.ERROR, logger="ai_scheduler"):
        # 遠超過上限地連續輪詢
        for _ in range(ai_scheduler.YT_MAX_DAILY_ATTEMPTS + 5):
            ai_scheduler.job_yt_summary_poll()

    # 達上限後即不再觸發 SDK
    assert calls["run"] == ai_scheduler.YT_MAX_DAILY_ATTEMPTS
    # 達上限只記一次 ERROR
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1


def test_poll_counts_reset_across_days(monkeypatch):
    """跨日歸零：date_str 改變後，舊日計數不影響新日。"""
    calls = _patch_poll(
        monkeypatch, already_exists=False, source_available=True,
    )
    # 第一天用盡上限
    monkeypatch.setattr(summaries, "yt_summary_date", lambda: "2026-06-29")
    for _ in range(ai_scheduler.YT_MAX_DAILY_ATTEMPTS + 2):
        ai_scheduler.job_yt_summary_poll()
    assert calls["run"] == ai_scheduler.YT_MAX_DAILY_ATTEMPTS

    # 隔日應自動歸零，可再次嘗試
    monkeypatch.setattr(summaries, "yt_summary_date", lambda: "2026-06-30")
    ai_scheduler.job_yt_summary_poll()
    assert calls["run"] == ai_scheduler.YT_MAX_DAILY_ATTEMPTS + 1


# ── 每日新聞摘要就緒時刻判斷 _past_news_ready_time ──────────────────────────
def test_parse_hhmm():
    """HH:MM 正確解析為 datetime.time。"""
    assert ai_scheduler._parse_hhmm("20:03") == _dt.time(20, 3)
    assert ai_scheduler._parse_hhmm("08:00") == _dt.time(8, 0)


def test_resolve_ready_time_valid():
    """合法 HH:MM → 正確解析。"""
    assert ai_scheduler._resolve_ready_time("08:00") == _dt.time(8, 0)


def test_resolve_ready_time_falls_back_on_bad_value(caplog):
    """非法值 → fallback 預設 08:00 並記 WARNING（不拋例外，避免 daemon 崩潰）。"""
    with caplog.at_level(logging.WARNING, logger="ai_scheduler"):
        for bad in ("0800", "08:00:00", "xx:yy", ""):
            assert ai_scheduler._resolve_ready_time(bad) == _dt.time(8, 0)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 4  # 每個非法值各警示一次


def test_past_news_ready_time_gate(monkeypatch):
    """就緒時刻前回 False、之後回 True（以 monkeypatch datetime.now 控制）。"""
    monkeypatch.setattr(ai_scheduler, "_NEWS_READY_TIME", _dt.time(8, 0))

    class _FixedDatetime(_dt.datetime):
        _now = None

        @classmethod
        def now(cls, tz=None):
            return cls._now

    # 就緒時刻之前（新聞尚未於早上 07:46–07:52 落檔完成）
    _FixedDatetime._now = _dt.datetime(2026, 6, 29, 7, 30)
    monkeypatch.setattr(ai_scheduler, "datetime", _FixedDatetime)
    assert ai_scheduler._past_news_ready_time() is False

    # 就緒時刻之後（08:00 後四來源皆已落檔）
    _FixedDatetime._now = _dt.datetime(2026, 6, 29, 8, 15)
    assert ai_scheduler._past_news_ready_time() is True


# ── 輪詢式每日新聞摘要 job_news_summary_poll ───────────────────────────────
def _patch_news_poll(
    monkeypatch, *, ready=True, already_exists=False, sources_available=True,
):
    """共用：mock 就緒時刻、冪等檢查、來源檢查，並攔截 ``_run_summary_sync``。

    Returns:
        dict: ``{"run": 次數}``，記錄 ``_run_summary_sync`` 被呼叫幾次。
    """
    calls = {"run": 0}

    monkeypatch.setattr(ai_scheduler, "_past_news_ready_time", lambda: ready)
    monkeypatch.setattr(summaries, "news_summary_date", lambda: "2026-06-28")
    monkeypatch.setattr(
        summaries, "news_summary_already_exists", lambda d: already_exists,
    )
    monkeypatch.setattr(
        summaries, "news_sources_available", lambda d: sources_available,
    )

    def fake_run(label, prompt, output_path):
        # 模擬失敗：不建立輸出檔（故下個 tick 仍會被視為「尚未產出」）。
        calls["run"] += 1

    monkeypatch.setattr(ai_scheduler, "_run_summary_sync", fake_run)
    return calls


def test_news_poll_skips_before_ready_time(monkeypatch, caplog):
    """尚未到就緒時刻 → 不呼叫 SDK runner、且安靜不記 log。"""
    calls = _patch_news_poll(monkeypatch, ready=False)
    with caplog.at_level(logging.DEBUG, logger="ai_scheduler"):
        ai_scheduler.job_news_summary_poll()
    assert calls["run"] == 0
    assert caplog.records == []  # 未到就緒時刻不可洗版


def test_news_poll_skips_when_summary_exists(monkeypatch, caplog):
    """摘要已存在 → 不呼叫 SDK runner、且安靜不記 log（冪等）。"""
    calls = _patch_news_poll(monkeypatch, already_exists=True)
    with caplog.at_level(logging.DEBUG, logger="ai_scheduler"):
        ai_scheduler.job_news_summary_poll()
    assert calls["run"] == 0
    assert caplog.records == []


def test_news_poll_runs_when_ready_and_missing(monkeypatch):
    """就緒、尚無摘要、有來源 → 呼叫 _run_summary_sync 恰一次（catch-up）。"""
    calls = _patch_news_poll(monkeypatch)
    ai_scheduler.job_news_summary_poll()
    assert calls["run"] == 1


def test_news_poll_warns_once_when_no_source(monkeypatch, caplog):
    """四來源皆無檔 → 不呼叫 SDK，且每日僅警示一次（不洗版）。"""
    calls = _patch_news_poll(monkeypatch, sources_available=False)
    with caplog.at_level(logging.WARNING, logger="ai_scheduler"):
        for _ in range(5):  # 多次輪詢
            ai_scheduler.job_news_summary_poll()
    assert calls["run"] == 0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # 僅警示一次


def test_news_poll_respects_daily_attempt_cap(monkeypatch, caplog):
    """每日嘗試上限：連續失敗達上限後不再呼叫 SDK，且只記一次 ERROR。"""
    calls = _patch_news_poll(monkeypatch)
    with caplog.at_level(logging.ERROR, logger="ai_scheduler"):
        for _ in range(ai_scheduler.NEWS_MAX_DAILY_ATTEMPTS + 5):
            ai_scheduler.job_news_summary_poll()
    assert calls["run"] == ai_scheduler.NEWS_MAX_DAILY_ATTEMPTS
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1


def test_news_poll_counts_reset_across_days(monkeypatch):
    """跨日歸零：date_str 改變後，舊日計數不影響新日。"""
    calls = _patch_news_poll(monkeypatch)
    monkeypatch.setattr(summaries, "news_summary_date", lambda: "2026-06-28")
    for _ in range(ai_scheduler.NEWS_MAX_DAILY_ATTEMPTS + 2):
        ai_scheduler.job_news_summary_poll()
    assert calls["run"] == ai_scheduler.NEWS_MAX_DAILY_ATTEMPTS

    monkeypatch.setattr(summaries, "news_summary_date", lambda: "2026-06-29")
    ai_scheduler.job_news_summary_poll()
    assert calls["run"] == ai_scheduler.NEWS_MAX_DAILY_ATTEMPTS + 1
