"""批次補抓指定日期區間的每日新聞摘要。

逐日呼叫 Claude Agent SDK（直接餵完整 prompt，不走 slash skill），產出仿照
`Tw_stock_news/DailyNews/2026-04-10.md` 格式的台股新聞摘要。具備：

- **可重入**：輸出檔已存在即略過，重跑不重做已完成日。
- **無來源略過**：該日四來源皆無檔案即略過並列出。
- **per-day 容錯 + 指數退避重試**：單日失敗只記 ERROR 並繼續下一天，不中止整批。
- **產出防呆**：以實際產出檔案作為成功判準。
- 結束印「成功／略過／失敗」明細，有失敗回非 0 退出碼。

用法：
    python batch_news_summary.py 2026-06-10 2026-06-26
"""

import asyncio
import logging
import sys

import summaries


async def main() -> int:
    if len(sys.argv) != 3:
        print("用法: python batch_news_summary.py <start_date> <end_date>")
        return 2
    start_date, end_date = sys.argv[1], sys.argv[2]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("batch_news")

    dates = summaries.date_range(start_date, end_date)
    log.info("批次處理 %d 天：%s ~ %s", len(dates), dates[0], dates[-1])

    results = []
    for d in dates:
        results.append(
            await summaries.backfill_one_day(
                d,
                build_prompt=summaries.build_news_prompt,
                output_path_fn=summaries.news_output_path,
                source_available_fn=summaries.news_sources_available,
                source_desc_fn=lambda x: str(summaries.news_source_counts(x)),
                log=log,
            )
        )

    return summaries.summarize_backfill(results, log)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
