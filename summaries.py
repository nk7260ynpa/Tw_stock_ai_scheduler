"""AI 摘要共用邏輯：prompt 組裝、路徑、來源檢查、產出防呆與 SDK 執行。

本模組由 daemon（`ai_scheduler.py`）與批次補抓腳本
（`batch_news_summary.py`、`batch_yt_summary.py`）共用，集中：

- 兩種摘要的「日期邏輯」（新聞用昨天、YT 用今天）。
- 直接餵給 Claude Agent SDK 的「完整 prompt」組裝（不再依賴 `/skill` slash 觸發）。
- 預期輸出檔路徑與「產出防呆」判斷（以實際產出檔案為成功判準）。
- 來源資料是否存在的檢查（無來源就略過、不空跑）。

設計重點：除了 `run_prompt()` 真正呼叫 SDK 外，其餘皆為純函式，方便單元測試。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

# ── 路徑常數 ────────────────────────────────────────────────────────────
# 本檔所在目錄即本 repo；其上層為 Tw_stock/（Agent SDK 的工作目錄）。
BASE_DIR = Path(__file__).resolve().parent
WORKSPACE = BASE_DIR.parent

# 輸出目錄（相對於 WORKSPACE）
NEWS_OUTPUT_DIR = WORKSPACE / "Tw_stock_news" / "DailyNews"
YT_OUTPUT_DIR = WORKSPACE / "Tw_stock_news" / "YTNews"

# 來源資料根目錄
NEWS_SOURCE_ROOT = WORKSPACE / "Tw_stock_DB" / "NewsContents"

# 新聞範本（相對路徑，餵進 prompt 給 SDK 讀取）
NEWS_TEMPLATE_REL = "Tw_stock_news/DailyNews/2026-04-10.md"

# 新聞四來源：來源名稱 → 副檔名萬用字元
NEWS_SOURCES = {
    "CTEE": "*.txt",
    "CNYES": "*.md",
    "PTT": "*.md",
    "MoneyUDN": "*.md",
}

# 允許的工具（與歷史可正常產出的批次腳本一致；不含 Skill，因不再走 slash）
ALLOWED_TOOLS = ["Read", "Write", "Glob", "Grep", "Bash"]

# 星期中文對照（datetime.weekday()：週一=0 … 週日=6）
_WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]


# ── 日期邏輯 ────────────────────────────────────────────────────────────
def news_summary_date(today: date | None = None) -> str:
    """回傳每日新聞摘要要處理的日期（昨天），格式 ``YYYY-MM-DD``。

    Args:
        today: 基準日期，預設為今天（便於測試注入）。

    Returns:
        str: 昨天的日期字串。
    """
    today = today or date.today()
    return (today - timedelta(days=1)).strftime("%Y-%m-%d")


def yt_summary_date(today: date | None = None) -> str:
    """回傳 YT 精華摘要要處理的日期（今天），格式 ``YYYY-MM-DD``。

    Args:
        today: 基準日期，預設為今天（便於測試注入）。

    Returns:
        str: 今天的日期字串。
    """
    today = today or date.today()
    return today.strftime("%Y-%m-%d")


def date_range(start_date: str, end_date: str) -> list[str]:
    """產生 ``start_date`` 到 ``end_date``（含）的日期字串列表。

    Args:
        start_date: 起始日 ``YYYY-MM-DD``。
        end_date: 結束日 ``YYYY-MM-DD``（含）。

    Returns:
        list[str]: 連續日期字串列表。
    """
    sy, sm, sd = map(int, start_date.split("-"))
    ey, em, ed = map(int, end_date.split("-"))
    cur, last = date(sy, sm, sd), date(ey, em, ed)
    out: list[str] = []
    while cur <= last:
        out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


def _weekday_zh(date_str: str) -> str:
    """將 ``YYYY-MM-DD`` 轉成中文星期（如「週五」）。"""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return f"週{_WEEKDAY_ZH[d.weekday()]}"


# ── 輸出路徑與產出防呆 ──────────────────────────────────────────────────
def news_output_path(date_str: str) -> Path:
    """每日新聞摘要的預期輸出檔路徑。"""
    return NEWS_OUTPUT_DIR / f"{date_str}.md"


def yt_output_path(date_str: str) -> Path:
    """YT 精華摘要的預期輸出檔路徑。"""
    return YT_OUTPUT_DIR / f"{date_str}.md"


def output_is_fresh(path: Path, since_ts: float, min_size: int = 1) -> bool:
    """判斷輸出檔是否在 ``since_ts`` 之後被建立／更新且非空。

    這是「產出防呆」的核心：SDK 即使空跑（回 ``is_error=False``）也不會
    產生／更新檔案，故以「檔案存在、非空、且 mtime 不早於任務開始時間」作為
    真正的成功判準。

    Args:
        path: 預期輸出檔路徑。
        since_ts: 任務開始的時間戳（``time.time()``）。
        min_size: 最小檔案大小（位元組），預設 1（即非空）。

    Returns:
        bool: 檔案確實被本次任務產出則為 True。
    """
    if not path.exists():
        return False
    stat = path.stat()
    if stat.st_size < min_size:
        return False
    # 留 1 秒容差，避免檔案系統 mtime 解析度造成誤判。
    return stat.st_mtime >= since_ts - 1


# ── 來源資料檢查 ────────────────────────────────────────────────────────
def news_source_counts(date_str: str) -> dict[str, int]:
    """回傳該日各新聞來源的檔案數量。

    Args:
        date_str: ``YYYY-MM-DD``。

    Returns:
        dict: ``{來源名稱: 檔案數}``，來源目錄不存在時計為 0。
    """
    counts: dict[str, int] = {}
    for source, pattern in NEWS_SOURCES.items():
        src_dir = NEWS_SOURCE_ROOT / source / date_str
        if src_dir.is_dir():
            counts[source] = len(list(src_dir.glob(pattern)))
        else:
            counts[source] = 0
    return counts


def news_sources_available(date_str: str) -> bool:
    """該日是否有任一新聞來源具有檔案。"""
    return any(c > 0 for c in news_source_counts(date_str).values())


def yt_source_path(date_str: str) -> Path:
    """YT 逐字稿來源檔路徑。"""
    return NEWS_SOURCE_ROOT / "YT" / date_str / f"{date_str}.md"


def yt_source_available(date_str: str) -> bool:
    """該日是否有 YT 逐字稿來源檔（存在且非空）。"""
    p = yt_source_path(date_str)
    return p.is_file() and p.stat().st_size > 0


# ── Prompt 組裝 ─────────────────────────────────────────────────────────
def build_news_prompt(date_str: str) -> str:
    """組裝每日新聞摘要的完整 prompt（直接餵給 SDK，不走 slash skill）。"""
    out_path = f"Tw_stock_news/DailyNews/{date_str}.md"
    return f"""請為 {date_str} 產出台股每日新聞摘要。

新聞原始檔案位置（皆在 `Tw_stock_DB/NewsContents/` 下）：
- `CTEE/{date_str}/*.txt`     — 工商時報
- `CNYES/{date_str}/*.md`     — 鉅亨網
- `PTT/{date_str}/*.md`       — 批踢踢股版
- `MoneyUDN/{date_str}/*.md`  — 經濟日報

請：
1. 讀取四個來源 {date_str} 該日所有檔案的內容
2. 仿照範本 `{NEWS_TEMPLATE_REL}` 的格式撰寫摘要：
   - `# {date_str} 台股每日新聞摘要`
   - `## 市場總覽`（1 段，綜合大盤、外資、產業熱點、重要事件，繁體中文）
   - `## 重點新聞`
     - `### 工商時報（CTEE）` — 條列 10 條（不足則列出全部）
     - `### 鉅亨網（CNYES）` — 條列 10 條
     - `### 批踢踢股版（PTT）` — 條列 10 條
     - `### 經濟日報（MoneyUDN）` — 條列 10 條
     - 每條 bullet 格式：`- **標題**：1-2 句精華`
     - 若某來源該日無檔案，僅寫 `（該日無資料）`
   - `## 統計` — 4 列 Markdown 表格：來源 | 新聞數量，最後一列為合計
3. 將完整 Markdown 寫入 `{out_path}`
4. 完成後回覆「DONE」即可，不需要其他說明
"""


def build_yt_prompt(date_str: str) -> str:
    """組裝 YT 精華摘要的完整 prompt（直接餵給 SDK，不走 slash skill）。"""
    transcript = f"Tw_stock_DB/NewsContents/YT/{date_str}/{date_str}.md"
    out_path = f"Tw_stock_news/YTNews/{date_str}.md"
    weekday = _weekday_zh(date_str)
    return f"""請讀取 `{transcript}` 的「游庭皓的財經皓角」直播逐字稿，
整理成精華摘要並寫入 `{out_path}`。

摘要格式（Markdown，繁體中文，仿照既有 YTNews 檔案）：
- 標題：`# {date_str} 游庭皓的財經皓角 — 精華摘要`（{weekday}）
- `## 今日重點`：1 段綜述當日核心觀點
- `## 市場觀點`：條列 4～6 條重點
- 接著依逐字稿主題自由分 3～5 個 H2 區塊（如：個股分析、中東局勢、央行決策、
  台股觀察、能源、AI…），每區塊條列 3～6 條重點
- `## 操作建議`：條列 2～4 條（若逐字稿有提及）
- `## 其他重點`：條列其餘值得記錄的訊息
- 每條 bullet 盡量以 `- **關鍵字**：說明` 呈現

完成後請回覆「DONE」字樣，不需要額外說明。
"""


# ── SDK 執行 ────────────────────────────────────────────────────────────
def build_options() -> ClaudeAgentOptions:
    """建立 Agent SDK 選項（工作目錄為 Tw_stock/，使用 Max 訂閱認證）。

    注意：**絕不**設定 ``ANTHROPIC_API_KEY``，否則會覆蓋訂閱、改走 API 計費。
    """
    return ClaudeAgentOptions(
        allowed_tools=ALLOWED_TOOLS,
        permission_mode="acceptEdits",
        cwd=str(WORKSPACE),
    )


async def run_prompt(prompt: str) -> dict:
    """以完整 prompt 呼叫 Claude Agent SDK，串流並彙整結果。

    Args:
        prompt: 完整的任務指令（非 ``/skill`` slash）。

    Returns:
        dict: 含 ``result``、``cost``、``is_error``、``num_messages`` 欄位。
    """
    data = {
        "result": None,
        "cost": None,
        "is_error": False,
        "num_messages": 0,
    }
    async for message in query(prompt=prompt, options=build_options()):
        data["num_messages"] += 1
        if isinstance(message, ResultMessage):
            data["result"] = message.result
            data["cost"] = message.total_cost_usd
            data["is_error"] = bool(message.is_error)
    return data


# ── 韌性執行（重試 + 產出防呆）──────────────────────────────────────────
async def run_summary_with_retry(
    prompt: str,
    output_path: Path,
    *,
    max_attempts: int = 3,
    base_delay: float = 15.0,
    runner: Callable[[str], Awaitable[dict]] | None = None,
    sleeper: Callable[[float], Awaitable[None]] | None = None,
    log: logging.Logger | None = None,
) -> dict:
    """執行單筆摘要，含指數退避重試，並以「實際產出檔案」作為成功判準。

    可吸收 Claude Agent SDK 子程序的暫時性失敗（如 ``Command failed with exit
    code 1``，常見於撞到 Max 訂閱滾動用量上限／暫時過載），不讓單次失敗中止整批。

    Args:
        prompt: 完整任務指令。
        output_path: 預期輸出檔；以其是否被本次嘗試產出判定成功。
        max_attempts: 最多嘗試次數（含第一次）。
        base_delay: 首次重試前的等待秒數，之後每次乘 2（指數退避）。
        runner: 實際呼叫 SDK 的協程，預設 :func:`run_prompt`；測試可注入假物件。
        sleeper: 退避等待協程，預設 :func:`asyncio.sleep`；測試可注入假物件。
        log: 選用 logger，用於記錄各次嘗試。

    Returns:
        dict: 含 ``result``/``cost``/``is_error``/``num_messages``/``produced``/
        ``attempts``/``error`` 欄位。``produced`` 為 True 且 ``is_error`` 為 False
        才算成功。
    """
    runner = runner or run_prompt
    sleeper = sleeper or asyncio.sleep

    outcome = {
        "result": None,
        "cost": None,
        "is_error": False,
        "num_messages": 0,
        "produced": False,
        "attempts": 0,
        "error": None,
    }

    total_cost = 0.0  # 累計各次嘗試的等價成本，避免重試時少計
    for attempt in range(1, max_attempts + 1):
        outcome["attempts"] = attempt
        start_ts = time.time()
        outcome["error"] = None
        outcome["is_error"] = False
        try:
            result = await runner(prompt)
            outcome["result"] = result.get("result")
            total_cost += result.get("cost") or 0
            outcome["cost"] = total_cost
            outcome["is_error"] = bool(result.get("is_error"))
            outcome["num_messages"] = result.get("num_messages", 0)
        except Exception as exc:  # SDK 子程序 exit 1／暫時性過載等
            outcome["error"] = repr(exc)
            outcome["is_error"] = True
            if log:
                log.warning(
                    "第 %d/%d 次嘗試拋例外：%s", attempt, max_attempts, exc,
                )

        outcome["produced"] = output_is_fresh(output_path, start_ts)

        if not outcome["is_error"] and outcome["produced"]:
            return outcome

        if attempt < max_attempts:
            delay = base_delay * (2 ** (attempt - 1))
            if log:
                log.warning(
                    "第 %d/%d 次未成功（produced=%s, error=%s），%.0f 秒後重試",
                    attempt, max_attempts, outcome["produced"],
                    outcome["error"], delay,
                )
            await sleeper(delay)

    return outcome


async def backfill_one_day(
    date_str: str,
    *,
    build_prompt: Callable[[str], str],
    output_path_fn: Callable[[str], Path],
    source_available_fn: Callable[[str], bool],
    source_desc_fn: Callable[[str], str] | None = None,
    log: logging.Logger,
    max_attempts: int = 3,
    base_delay: float = 15.0,
) -> dict:
    """補抓單日摘要（可重入 + 容錯），回傳結果摘要 dict。

    處理順序：

    1. **可重入**：輸出檔已存在且非空 → 略過（``status="exists"``）。
    2. **無來源**：來源資料不存在 → 略過（``status="nosource"``）。
    3. 否則以 :func:`run_summary_with_retry` 執行；產出檔成功 → ``status="ok"``，
       否則 ``status="failed"``（已記 ERROR，不拋例外、不中止整批）。

    Args:
        date_str: ``YYYY-MM-DD``。
        build_prompt: 由日期組裝 prompt 的函式。
        output_path_fn: 由日期取得輸出檔路徑的函式。
        source_available_fn: 由日期判斷來源是否存在的函式。
        source_desc_fn: 選用，回傳來源描述字串（如各來源檔數）供開始 log。
        log: logger。
        max_attempts: 單日最多嘗試次數。
        base_delay: 重試退避基準秒數（傳給 :func:`run_summary_with_retry`）。

    Returns:
        dict: 含 ``date``/``status``/``elapsed``/``cost``/``file_size``/``error``。
    """
    # 最外層保險：任何未預期例外（含 build_prompt／來源檢查）都收斂為單日
    # failed，確保絕不逸出而中止整批。
    try:
        out_file = output_path_fn(date_str)

        if out_file.exists() and out_file.stat().st_size > 0:
            log.info("=== %s 略過：輸出檔已存在（可重入）===", date_str)
            return {
                "date": date_str, "status": "exists", "elapsed": 0.0,
                "cost": 0.0, "file_size": out_file.stat().st_size,
                "error": None,
            }

        if not source_available_fn(date_str):
            log.warning("=== %s 略過：來源資料不存在 ===", date_str)
            return {
                "date": date_str, "status": "nosource", "elapsed": 0.0,
                "cost": 0.0, "file_size": 0, "error": None,
            }

        if source_desc_fn:
            log.info(
                "=== %s 開始(來源：%s)===", date_str, source_desc_fn(date_str),
            )
        else:
            log.info("=== %s 開始 ===", date_str)

        start = time.monotonic()
        outcome = await run_summary_with_retry(
            build_prompt(date_str), out_file,
            max_attempts=max_attempts, base_delay=base_delay, log=log,
        )
        elapsed = time.monotonic() - start

        produced = outcome["produced"] and not outcome["is_error"]
        size = out_file.stat().st_size if out_file.exists() else 0
        status = "ok" if produced else "failed"

        log_fn = log.info if produced else log.error
        log_fn(
            "%s %s: 嘗試=%d 訊息=%d 耗時=%.1fs cost=$%.4f "
            "produced=%s error=%s size=%d",
            date_str, "成功" if produced else "失敗", outcome["attempts"],
            outcome["num_messages"], elapsed, outcome["cost"] or 0,
            outcome["produced"], outcome["error"], size,
        )
        return {
            "date": date_str, "status": status, "elapsed": elapsed,
            "cost": outcome["cost"] or 0, "file_size": size,
            "error": outcome["error"],
        }
    except Exception as exc:  # 最後防線：不讓任何例外中止整批
        log.exception("=== %s 失敗：補抓時發生未預期例外 ===", date_str)
        return {
            "date": date_str, "status": "failed", "elapsed": 0.0,
            "cost": 0.0, "file_size": 0, "error": repr(exc),
        }


def summarize_backfill(results: list[dict], log: logging.Logger) -> int:
    """結算批次結果並印出「成功／略過／失敗」明細，回傳行程退出碼。

    Args:
        results: 各日 :func:`backfill_one_day` 回傳的 dict 列表。
        log: logger。

    Returns:
        int: 全部無失敗回 0，否則回 1。
    """
    ok = [r for r in results if r["status"] == "ok"]
    exists = [r for r in results if r["status"] == "exists"]
    nosource = [r for r in results if r["status"] == "nosource"]
    failed = [r for r in results if r["status"] == "failed"]
    total_cost = sum(r["cost"] for r in results)
    total_elapsed = sum(r["elapsed"] for r in results)

    log.info("=" * 50)
    log.info("批次結束")
    log.info("總耗時 %.1f 秒 (%.1f 分鐘)", total_elapsed, total_elapsed / 60)
    log.info("總等價成本 $%.4f", total_cost)
    log.info(
        "成功 %d／略過 %d（已存在 %d、無來源 %d）／失敗 %d",
        len(ok), len(exists) + len(nosource), len(exists), len(nosource),
        len(failed),
    )
    if ok:
        log.info("  成功：%s", ", ".join(r["date"] for r in ok))
    if exists:
        log.info("  略過-已存在：%s", ", ".join(r["date"] for r in exists))
    if nosource:
        log.info("  略過-無來源：%s", ", ".join(r["date"] for r in nosource))
    if failed:
        log.error("  失敗：%s", ", ".join(r["date"] for r in failed))

    return 0 if not failed else 1
