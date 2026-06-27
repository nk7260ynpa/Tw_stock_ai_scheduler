"""台股 AI 摘要排程器。

使用 Claude Agent SDK 搭配 schedule 套件，定期執行：
- 19:15 — YT 逐字稿精華摘要（處理「今天」日期）
- 20:03 — 每日新聞摘要（處理「昨天」日期）

認證方式：Max/Pro 訂閱（透過 ~/.claude/ 憑證）。

重要：本排程器**直接餵完整 prompt** 給 Agent SDK，不再以 ``/skill`` slash
觸發。原因是 `/news-summary`、`/yt-summary` 兩個 skill 已不存在於系統，
`query(prompt="/news-summary")` 只會回 ``Unknown skill`` 並以 ``is_error=False``
立即結束（假成功、$0.0000、無產出）。改餵完整 prompt 後，並以「實際產出檔案」
作為成功判準（產出防呆），避免再次空跑卻記成完成。
"""

import asyncio
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import schedule as schedule_lib

import summaries

# 路徑設定
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("ai_scheduler")


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


def job_yt_summary():
    """YT 精華摘要排程任務（處理今天日期）。"""
    date_str = summaries.yt_summary_date()
    label = f"YT 精華摘要（{date_str}）"
    if not summaries.yt_source_available(date_str):
        # 無逐字稿來源 → 略過（不空跑、不誤判為失敗）
        logger.warning(
            "%s 略過：找不到逐字稿來源 %s",
            label, summaries.yt_source_path(date_str),
        )
        return
    _run_summary_sync(
        label,
        summaries.build_yt_prompt(date_str),
        summaries.yt_output_path(date_str),
    )


def job_news_summary():
    """每日新聞摘要排程任務（處理昨天日期）。"""
    date_str = summaries.news_summary_date()
    label = f"每日新聞摘要（{date_str}）"
    if not summaries.news_sources_available(date_str):
        logger.warning(
            "%s 略過：四個新聞來源於 %s 皆無檔案",
            label, date_str,
        )
        return
    _run_summary_sync(
        label,
        summaries.build_news_prompt(date_str),
        summaries.news_output_path(date_str),
    )


def setup_schedule():
    """設定每日排程。"""
    schedule_lib.every().day.at("19:15").do(job_yt_summary)
    schedule_lib.every().day.at("20:03").do(job_news_summary)
    logger.info("排程已設定：YT 精華摘要 19:15、每日新聞摘要 20:03")


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

    while True:
        schedule_lib.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
