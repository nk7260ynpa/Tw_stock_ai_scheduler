"""台股 AI 摘要排程器。

使用 Claude Agent SDK 搭配 schedule 套件，定期執行：
- 19:15 — YT 逐字稿精華摘要（/yt-summary skill）
- 20:03 — 每日新聞摘要（/news-summary skill）

認證方式：Max/Pro 訂閱（透過 ~/.claude/ 憑證）。
"""

import asyncio
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import schedule as schedule_lib
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

# 路徑設定
BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Claude Code 工作目錄（skill 定義與檔案路徑皆基於此）
CLAUDE_CWD = str(BASE_DIR.parent)

# 允許的工具（與原 crontab 的 --allowedTools 一致）
ALLOWED_TOOLS = [
    "Bash", "Read", "Write", "Glob", "Grep", "Edit", "Skill",
]

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


def _build_options() -> ClaudeAgentOptions:
    """建立 Agent SDK 選項。"""
    return ClaudeAgentOptions(
        allowed_tools=ALLOWED_TOOLS,
        permission_mode="acceptEdits",
        cwd=CLAUDE_CWD,
    )


async def _run_skill(skill_name: str) -> dict:
    """執行指定的 Claude Code skill。

    Args:
        skill_name: skill 名稱（如 "/yt-summary"）。

    Returns:
        dict: 包含 result、cost、is_error 欄位。
    """
    result_data = {
        "result": None,
        "cost": None,
        "is_error": False,
    }

    async for message in query(
        prompt=skill_name, options=_build_options()
    ):
        if isinstance(message, ResultMessage):
            result_data["result"] = message.result
            result_data["cost"] = message.total_cost_usd
            result_data["is_error"] = bool(message.is_error)

    return result_data


def _run_skill_sync(skill_name: str, task_label: str):
    """同步包裝 async skill 執行（供 schedule callback 使用）。

    Args:
        skill_name: skill 名稱（如 "/yt-summary"）。
        task_label: 任務標籤（用於 log）。
    """
    logger.info("排程觸發：%s", task_label)
    start = datetime.now()

    try:
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(_run_skill(skill_name))
        finally:
            loop.close()

        elapsed = (datetime.now() - start).total_seconds()

        if result["is_error"]:
            logger.error(
                "%s 失敗（%.1f 秒）：%s",
                task_label, elapsed, result["result"],
            )
        else:
            logger.info(
                "%s 完成（%.1f 秒），花費 $%.4f",
                task_label, elapsed, result["cost"] or 0,
            )

    except Exception:
        elapsed = (datetime.now() - start).total_seconds()
        logger.exception("%s 發生例外（%.1f 秒）", task_label, elapsed)


def job_yt_summary():
    """YT 精華摘要排程任務。"""
    _run_skill_sync("/yt-summary", "YT 精華摘要")


def job_news_summary():
    """每日新聞摘要排程任務。"""
    _run_skill_sync("/news-summary", "每日新聞摘要")


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
    logger.info("工作目錄：%s", CLAUDE_CWD)
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
