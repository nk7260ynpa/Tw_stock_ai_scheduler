"""台股 AI 摘要排程器。

使用 Claude Agent SDK 搭配 schedule 套件，定期執行（兩種摘要皆改為
**事件驅動／冪等補產的輪詢**，不再依賴單一固定時刻）：

- YT 逐字稿精華摘要：每 ``YT_POLL_MINUTES`` 分鐘檢查一次（處理「今天」日期）——
  今天逐字稿一出現且尚未產摘要就立即產生。
- 每日新聞摘要：每 ``NEWS_POLL_MINUTES`` 分鐘檢查一次（處理「昨天」日期）——
  到達當日「新聞就緒時刻」(``NEWS_READY_TIME``，預設 20:03) 後，昨天的摘要若
  尚未產出就立即補產。

認證方式：Max/Pro 訂閱（透過 ~/.claude/ 憑證）。

重要：本排程器**直接餵完整 prompt** 給 Agent SDK，不再以 ``/skill`` slash
觸發。原因是 `/news-summary`、`/yt-summary` 兩個 skill 已不存在於系統，
`query(prompt="/news-summary")` 只會回 ``Unknown skill`` 並以 ``is_error=False``
立即結束（假成功、$0.0000、無產出）。改餵完整 prompt 後，並以「實際產出檔案」
作為成功判準（產出防呆），避免再次空跑卻記成完成。

兩種摘要都改為輪詢的理由（同一病灶）：固定單一時刻觸發過於脆弱——資料延遲、
或 daemon 該刻剛好沒在跑就整天錯過（``schedule`` 遇錯過的時刻會直接跳到隔天）。
改成每隔幾分鐘檢查，條件達成且尚未產出就立即補產（catch-up）；搭配 launchd
KeepAlive 守護，daemon 死掉會自動復活。新聞摘要另以 ``NEWS_READY_TIME`` 為
「就緒時刻」下限，確保四來源（當晚 21:00–22:30 上傳、隔日 06:30 重排）皆已
落檔，維持與原固定 20:03 相同的來源完整度。
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, time as dt_time
from pathlib import Path

import schedule as schedule_lib

import summaries

# 路徑設定
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("ai_scheduler")

# YT 精華摘要輪詢間隔（分鐘），可透過環境變數覆蓋。
YT_POLL_MINUTES = int(os.environ.get("YT_POLL_MINUTES", "2"))

# 每日新聞摘要輪詢間隔（分鐘），可透過環境變數覆蓋。
# 輪詢成本極低（僅幾次 stat），故間隔僅決定 daemon (重)啟動後 catch-up 的延遲。
NEWS_POLL_MINUTES = int(os.environ.get("NEWS_POLL_MINUTES", "5"))

# 每日新聞「就緒時刻」(HH:MM)：此刻之前不嘗試產出昨天的摘要。
# 確保四來源（當晚 21:00–22:30 上傳、隔日 06:30 重排）皆已落檔，維持與原固定
# 20:03 相同的來源完整度。可透過環境變數覆蓋（須為零補位的 HH:MM）。
NEWS_READY_TIME = os.environ.get("NEWS_READY_TIME", "20:03")

# 同一日摘要最多嘗試產出的次數（失敗成本防護）。
# 達上限後當日停止重試、只記一次 ERROR，隔日（date_str 改變）自動歸零。
YT_MAX_DAILY_ATTEMPTS = 5
NEWS_MAX_DAILY_ATTEMPTS = 5

# 記憶體內「每日嘗試次數」計數：{date_str: 次數}。
# 僅保留當日鍵值（每次嘗試前清掉舊日鍵），故隨 daemon 常駐也不會無限增長。
_yt_attempt_counts: dict[str, int] = {}
_news_attempt_counts: dict[str, int] = {}

# 記憶體內「已就『無來源』警示過的日期」：避免每個 tick 重複洗版（同樣只留當日）。
_news_nosource_warned: dict[str, bool] = {}


def _parse_hhmm(value: str) -> dt_time:
    """將 ``HH:MM`` 字串解析為 :class:`datetime.time`。"""
    hour, minute = value.split(":")
    return dt_time(int(hour), int(minute))


def _resolve_ready_time(value: str) -> dt_time:
    """解析就緒時刻字串，格式非法時 fallback 預設 20:03。

    於模組載入時解析一次（環境變數本就只讀一次），避免每個 tick 重複解析；
    更重要的是：非法值不會在輪詢 gate（位於主迴圈 ``run_pending`` 之外、
    無 try/except）拋例外，導致 daemon 反覆崩潰、由 launchd KeepAlive 不停重啟。

    Args:
        value: ``HH:MM`` 字串（須零補位）。

    Returns:
        datetime.time: 解析結果；格式非法時回預設 20:03 並記 WARNING。
    """
    try:
        return _parse_hhmm(value)
    except (ValueError, TypeError):
        logger.warning(
            "NEWS_READY_TIME=%r 格式非法（需零補位 HH:MM），改用預設 20:03",
            value,
        )
        return dt_time(20, 3)


# 模組載入時解析一次就緒時刻（env var 本就只讀一次，避免每個 tick 重複解析）。
_NEWS_READY_TIME = _resolve_ready_time(NEWS_READY_TIME)


def setup_logging():
    """設定 logger，同時輸出至檔案與 stderr。"""
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(
        LOG_DIR / "ai_scheduler.log", encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)


def _run_summary_sync(task_label: str, prompt: str, output_path: Path):
    """同步執行一次摘要任務，並以「實際產出檔案」作為成功判準。

    Args:
        task_label: 任務標籤（用於 log）。
        prompt: 餵給 SDK 的完整 prompt。
        output_path: 預期輸出檔路徑；任務後若未被建立／更新即記為 ERROR。
    """
    logger.info("排程觸發：%s", task_label)
    start = datetime.now()

    try:
        loop = asyncio.new_event_loop()
        try:
            # 含指數退避重試，可吸收暫時性 SDK 失敗（如 exit code 1／過載）
            outcome = loop.run_until_complete(
                summaries.run_summary_with_retry(
                    prompt, output_path, log=logger,
                )
            )
        finally:
            loop.close()

        elapsed = (datetime.now() - start).total_seconds()
        produced = outcome["produced"] and not outcome["is_error"]

        if produced:
            size = output_path.stat().st_size
            logger.info(
                "%s 完成（%.1f 秒，嘗試 %d 次），產出 %s（%d bytes），花費 $%.4f",
                task_label, elapsed, outcome["attempts"], output_path.name,
                size, outcome["cost"] or 0,
            )
        else:
            # 產出防呆：重試後仍未產出預期檔案（含空跑／SDK 錯誤情形）
            logger.error(
                "%s 失敗（%.1f 秒，嘗試 %d 次）：預期輸出檔未產出 %s"
                "（is_error=%s，error=%s，result=%s）",
                task_label, elapsed, outcome["attempts"], output_path,
                outcome["is_error"], outcome["error"], outcome["result"],
            )

    except Exception:
        elapsed = (datetime.now() - start).total_seconds()
        logger.exception("%s 發生例外（%.1f 秒）", task_label, elapsed)


def job_yt_summary_poll():
    """輪詢式 YT 精華摘要：今天逐字稿一出現且尚未產摘要就立即產生。

    本任務每隔 ``YT_POLL_MINUTES`` 分鐘被觸發一次。為避免一天數百次輪詢洗版
    log，下列兩種「正常未達條件」狀況一律**安靜 return、不記 log**：

    - 該日摘要已存在（冪等）。
    - 逐字稿尚未出現。

    僅在真正嘗試產出時，才由 :func:`_run_summary_sync` 記錄。

    失敗成本防護：以模組級 :data:`_yt_attempt_counts` 記錄同一日的嘗試次數，
    達 :data:`YT_MAX_DAILY_ATTEMPTS` 後當日停止重試、只記一次 ERROR；隔日
    （``date_str`` 改變）自動歸零。失敗時輸出檔不會被建立，下個 tick 會自然
    重試（跨 tick 免費重試是優點），上限則防止持續失敗使 SDK 成本暴衝。
    """
    date_str = summaries.yt_summary_date()  # 今天
    if summaries.yt_summary_already_exists(date_str):
        return  # 冪等：已產生 → 安靜跳過（不記 log）
    if not summaries.yt_source_available(date_str):
        return  # 逐字稿尚未出現 → 安靜跳過（不記 log）

    label = f"YT 精華摘要（{date_str}）"

    attempts = _yt_attempt_counts.get(date_str, 0)
    if attempts >= YT_MAX_DAILY_ATTEMPTS:
        return  # 已達當日上限（先前已記過一次 ERROR）→ 安靜跳過

    # 僅保留當日計數，避免常駐期間 dict 無限增長；隔日舊鍵自然被清掉並歸零。
    _yt_attempt_counts.clear()
    _yt_attempt_counts[date_str] = attempts + 1

    _run_summary_sync(
        label,
        summaries.build_yt_prompt(date_str),
        summaries.yt_output_path(date_str),
    )

    # 本次嘗試後仍未產出且剛好達上限 → 記一次 ERROR 提示當日停止重試。
    # （後續 tick 會在上方上限檢查處安靜跳過，不再重複記錄。）
    if (
        not summaries.yt_summary_already_exists(date_str)
        and _yt_attempt_counts[date_str] >= YT_MAX_DAILY_ATTEMPTS
    ):
        logger.error(
            "%s 連續 %d 次嘗試仍未產出，今日停止重試（隔日自動歸零）",
            label, YT_MAX_DAILY_ATTEMPTS,
        )


def _past_news_ready_time() -> bool:
    """目前本地時間是否已達每日新聞就緒時刻 :data:`_NEWS_READY_TIME`。

    抽成獨立函式以便單元測試 monkeypatch（避免依賴真實時鐘）。

    Returns:
        bool: 現在時刻 >= 就緒時刻則為 True。
    """
    return datetime.now().time() >= _NEWS_READY_TIME


def job_news_summary_poll():
    """輪詢式每日新聞摘要：昨天的摘要缺漏即補產（冪等、可 catch-up）。

    本任務每隔 :data:`NEWS_POLL_MINUTES` 分鐘被觸發一次，與 YT 輪詢同構，
    解決「daemon 在固定 20:03 沒活著、或啟動時已過 20:03」就整天靜默漏掉的
    根因——只要 daemon 於當日就緒時刻後仍存活，下一個 tick 就會把昨天的摘要
    補出來。

    為避免一天數十次輪詢洗版 log，下列「正常未達條件」狀況一律**安靜 return**：

    - 尚未到當日就緒時刻 :data:`NEWS_READY_TIME`（確保四來源完整才產出）。
    - 該日摘要已存在（冪等）。
    - 四來源皆無檔（每日僅警示一次後安靜跳過，不洗版）。

    僅在真正嘗試產出時，才由 :func:`_run_summary_sync` 記錄。

    失敗成本防護：以模組級 :data:`_news_attempt_counts` 記錄同一日的嘗試次數，
    達 :data:`NEWS_MAX_DAILY_ATTEMPTS` 後當日停止重試、只記一次 ERROR；隔日
    （``date_str`` 改變）自動歸零。失敗時輸出檔不會被建立，下個 tick 會自然
    重試，上限則防止持續失敗使 SDK 成本暴衝。
    """
    if not _past_news_ready_time():
        return  # 今日尚未到就緒時刻 → 安靜跳過（確保四來源已落檔）

    date_str = summaries.news_summary_date()  # 昨天
    if summaries.news_summary_already_exists(date_str):
        return  # 冪等：已產生 → 安靜跳過（不記 log）

    label = f"每日新聞摘要（{date_str}）"

    if not summaries.news_sources_available(date_str):
        # 四來源皆無檔：每日僅警示一次，避免每個 tick 洗版。
        if not _news_nosource_warned.get(date_str):
            _news_nosource_warned.clear()
            _news_nosource_warned[date_str] = True
            logger.warning(
                "%s 略過：四個新聞來源於 %s 皆無檔案", label, date_str,
            )
        return

    attempts = _news_attempt_counts.get(date_str, 0)
    if attempts >= NEWS_MAX_DAILY_ATTEMPTS:
        return  # 已達當日上限（先前已記過一次 ERROR）→ 安靜跳過

    # 僅保留當日計數，避免常駐期間 dict 無限增長；隔日舊鍵自然被清掉並歸零。
    _news_attempt_counts.clear()
    _news_attempt_counts[date_str] = attempts + 1

    _run_summary_sync(
        label,
        summaries.build_news_prompt(date_str),
        summaries.news_output_path(date_str),
    )

    # 本次嘗試後仍未產出且剛好達上限 → 記一次 ERROR 提示當日停止重試。
    # （後續 tick 會在上方上限檢查處安靜跳過，不再重複記錄。）
    if (
        not summaries.news_summary_already_exists(date_str)
        and _news_attempt_counts[date_str] >= NEWS_MAX_DAILY_ATTEMPTS
    ):
        logger.error(
            "%s 連續 %d 次嘗試仍未產出，今日停止重試（隔日自動歸零）",
            label, NEWS_MAX_DAILY_ATTEMPTS,
        )


def setup_schedule():
    """設定排程：YT 精華摘要與每日新聞摘要皆採事件驅動／冪等補產輪詢。"""
    schedule_lib.every(YT_POLL_MINUTES).minutes.do(job_yt_summary_poll)
    schedule_lib.every(NEWS_POLL_MINUTES).minutes.do(job_news_summary_poll)
    logger.info(
        "排程已設定：YT 精華摘要輪詢（每 %d 分鐘）、"
        "每日新聞摘要輪詢（每 %d 分鐘，就緒時刻 %s）",
        YT_POLL_MINUTES, NEWS_POLL_MINUTES,
        _NEWS_READY_TIME.strftime("%H:%M"),
    )


def startup_catchup():
    """daemon 啟動時立即做一次補產 sweep。

    ``schedule`` 套件註冊後第一次觸發要等一個輪詢間隔，故啟動時主動各跑一次
    輪詢任務，讓「啟動已過就緒時刻」的當日缺漏在啟動幾秒內就補上，而非等到
    下一個 tick。各任務本身冪等且自帶例外處理，重啟亦不會重複產出。
    """
    for job in (job_yt_summary_poll, job_news_summary_poll):
        try:
            job()
        except Exception:  # 啟動補產失敗不可影響主迴圈進入
            logger.exception("啟動補產 %s 發生例外", job.__name__)


def main():
    """主程式：設定排程並進入無限迴圈。"""
    setup_logging()
    logger.info("=" * 50)
    logger.info("台股 AI 摘要排程器啟動")
    logger.info("工作目錄：%s", summaries.WORKSPACE)
    logger.info("=" * 50)

    setup_schedule()

    # 列出下次排程時間
    for job in schedule_lib.get_jobs():
        logger.info("下次執行：%s", job.next_run)

    # 啟動即補產，避免「啟動已過就緒時刻」的當日缺漏等到下一個 tick 才補。
    startup_catchup()

    while True:
        schedule_lib.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
