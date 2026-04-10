# Tw_stock_ai_scheduler

台股 AI 摘要排程器，使用 Claude Agent SDK 搭配 Max 訂閱，定期產生 YT 精華摘要與每日新聞摘要。

## 功能說明

- **YT 精華摘要**（每日 19:15）：呼叫 `/yt-summary` skill，讀取游庭皓的財經皓角逐字稿並產生精華摘要
- **每日新聞摘要**（每日 20:03）：呼叫 `/news-summary` skill，查詢 MySQL 新聞資料並產生每日新聞摘要

## 架構

```
Host macOS（Torch conda env）
  └── ai_scheduler.py（schedule 主迴圈，背景 daemon）
      ├── 19:15 → Agent SDK → /yt-summary skill
      └── 20:03 → Agent SDK → /news-summary skill

認證：~/.claude/（Max 訂閱）
工作目錄：/Users/chen/AI/Tw_stock/
輸入：Tw_stock_DB/NewsContents/（逐字稿、新聞全文）
輸出：Tw_stock_news/YTNews/、Tw_stock_news/DailyNews/
```

## 專案結構

```
Tw_stock_ai_scheduler/
├── ai_scheduler.py       # 主程式（schedule + Agent SDK）
├── run.sh                # 啟動/停止/狀態腳本
├── pyproject.toml        # Python 專案定義（PEP 621）
├── requirements.txt      # 依賴
├── logs/                 # 日誌資料夾
├── .gitignore
└── README.md
```

## 使用方式

### 1. 安裝依賴

```bash
/Users/chen/miniconda3/envs/Torch/bin/pip install claude-agent-sdk schedule
```

### 2. 啟動排程器

```bash
bash run.sh start
```

### 3. 管理排程器

```bash
# 查看狀態
bash run.sh status

# 停止
bash run.sh stop

# 重啟
bash run.sh restart
```

### 4. 查看日誌

```bash
tail -f logs/ai_scheduler.log
```

## 認證方式

使用 Claude Max/Pro 訂閱認證，透過 `~/.claude/` 目錄中的憑證自動登入。不需要 `ANTHROPIC_API_KEY`。

## 環境需求

- macOS
- Python 3.12+（Torch conda 環境）
- Claude Code CLI 已登入 Max/Pro 訂閱
- claude-agent-sdk、schedule 套件
