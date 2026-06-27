"""批次補抓指定日期區間的 YT 精華摘要。

逐日呼叫 Claude Agent SDK（直接餵完整 prompt，不走 slash skill），讀取
`Tw_stock_DB/NewsContents/YT/{date}/{date}.md` 逐字稿，產出仿照既有
`Tw_stock_news/YTNews/*.md` 格式的精華摘要。會：

- 先檢查該日是否有逐字稿來源，無來源即略過並列出（YT 並非每日皆有直播）。
- 任務後以「實際產出檔案」作為成功判準（產出防呆）。

用法：
    python batch_yt_summary.py 2026-06-10 2026-06-22
"""

import asyncio
import logging
import sys
import time

import summaries


async def run_one(date_str: str, log: logging.Logger) -> dict:
    """補抓單日 YT 精華摘要，回傳結果摘要 dict。"""
    out_file = summaries.yt_output_path(date_str)

    if not summaries.yt_source_available(date_str):
        log.warning(
            "=== %s 略過：找不到逐字稿來源 %s ===",
            date_str, summaries.yt_source_path(date_str),
        )
        return {
            "date": date_str, "skipped": True, "produced": False,
            "elapsed": 0.0, "cost": 0.0, "file_size": 0,
        }

    log.info("=== %s 開始 ===", date_str)
    start = time.monotonic()
    start_ts = time.time()

    result = await summaries.run_prompt(summaries.build_yt_prompt(date_str))

    elapsed = time.monotonic() - start
    produced = summaries.output_is_fresh(out_file, start_ts)
    size = out_file.stat().st_size if out_file.exists() else 0

    log.info(
        "%s 結束: 訊息=%d 耗時=%.1fs cost=$%.4f is_error=%s produced=%s size=%d",
        date_str, result["num_messages"], elapsed, result["cost"] or 0,
        result["is_error"], produced, size,
    )
    return {
        "date": date_str, "skipped": False, "produced": produced,
        "elapsed": elapsed, "cost": result["cost"] or 0,
        "is_error": result["is_error"], "file_size": size,
    }


async def main() -> int:
    if len(sys.argv) != 3:
        print("用法: python batch_yt_summary.py <start_date> <end_date>")
        return 2
    start_date, end_date = sys.argv[1], sys.argv[2]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("batch_yt")

    dates = summaries.date_range(start_date, end_date)
    log.info("批次處理 %d 天：%s ~ %s", len(dates), dates[0], dates[-1])

    results = []
    for d in dates:
        results.append(await run_one(d, log))

    total_cost = sum(r["cost"] for r in results)
    total_elapsed = sum(r["elapsed"] for r in results)

    log.info("=" * 50)
    log.info("批次完成")
    log.info("總耗時 %.1f 秒 (%.1f 分鐘)", total_elapsed, total_elapsed / 60)
    log.info("總等價成本 $%.4f", total_cost)
    log.info("各日狀態：")
    for r in results:
        if r["skipped"]:
            mark = "—略過"
        elif r["produced"]:
            mark = "✓"
        else:
            mark = "✗"
        log.info(
            "  %s %s  $%.4f  %.1fs  size=%d",
            mark, r["date"], r["cost"], r["elapsed"], r["file_size"],
        )

    failed = [r for r in results if not r["skipped"] and not r["produced"]]
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
