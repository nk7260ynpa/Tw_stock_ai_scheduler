# Tw_stock_ai_scheduler

台股 AI 摘要排程器，使用 Claude Agent SDK 搭配 Max 訂閱，定期產生 YT 精華摘要與每日新聞摘要。

## 功能說明

- **YT 精華摘要**（每日 19:15，處理今天日期）：讀取游庭皓的財經皓角逐字稿，直接餵完整 prompt 給 Agent SDK 產生精華摘要
- **每日新聞摘要**（每日 20:03，處理昨天日期）：讀取四來源新聞全文，直接餵完整 prompt 給 Agent SDK 產生每日新聞摘要

> **設計重點（直接餵 prompt + 產出防呆）**：早期版本以 `query(prompt="/yt-summary")`
> 觸發 slash skill，但該 skill 已不存在於系統，SDK 只會回 `Unknown skill` 並以
> `is_error=False` 立即結束（假成功、$0.0000、無產出）。現改為**直接餵完整 prompt**，
> 並以**實際產出檔案**作為成功判準：任務後若預期輸出檔未被建立／更新即記為 ERROR，
> 杜絕空跑卻記成完成。

## 架構

```
Host macOS（conda env）
  └── ai_scheduler.py（schedule 主迴圈，背景 daemon）
      ├── 19:15 → 檢查逐字稿來源 → Agent SDK（完整 prompt）→ 產出防呆
      └── 20:03 → 檢查新聞來源   → Agent SDK（完整 prompt）→ 產出防呆
          （prompt 組裝、來源檢查、產出防呆皆共用 summaries.py）

認證：~/.claude/（Max/Pro 訂閱）
工作目錄：本專案的上層目錄（Tw_stock/）
輸入：Tw_stock_DB/NewsContents/（逐字稿、新聞全文）
輸出：Tw_stock_news/YTNews/、Tw_stock_news/DailyNews/
```

## 專案結構

```
Tw_stock_ai_scheduler/
├── ai_scheduler.py        # 主程式（schedule + 產出防呆）
├── summaries.py           # 共用邏輯（prompt 組裝、日期、來源檢查、產出防呆、SDK 執行）
├── batch_news_summary.py  # 批次補抓每日新聞摘要
├── batch_yt_summary.py    # 批次補抓 YT 精華摘要
├── run.sh                 # 啟動/停止/狀態腳本
├── pyproject.toml         # Python 專案定義（PEP 621）
├── requirements.txt       # 依賴
├── test/                  # 單元測試（純函式，不真打 SDK）
├── logs/                  # 日誌資料夾
├── .gitignore
└── README.md
```

## 批次補抓

```bash
# 補抓每日新聞摘要區間（無來源的日子會自動略過並列出）
python batch_news_summary.py 2026-06-10 2026-06-26

# 補抓 YT 精華摘要區間（無逐字稿的日子會自動略過並列出）
python batch_yt_summary.py 2026-06-10 2026-06-22
```

## 測試

```bash
pip install ".[dev]"   # 或 pip install pytest
python -m pytest
```

## 使用方式

### 1. 安裝依賴

```bash
# 使用指定的 conda 環境安裝
conda activate Torch
pip install claude-agent-sdk schedule
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

## 設定

### Conda 環境

`run.sh` 預設使用 `Torch` conda 環境。可透過環境變數覆蓋：

```bash
# 使用其他 conda 環境
CONDA_ENV=myenv bash run.sh start

# 指定 conda 安裝路徑
CONDA_BASE=/opt/conda CONDA_ENV=myenv bash run.sh start
```

## 認證方式

使用 Claude Max/Pro 訂閱認證，透過 `~/.claude/` 目錄中的憑證自動登入。不需要 `ANTHROPIC_API_KEY`。

## 環境需求

- macOS
- Python 3.12+（conda 環境）
- Claude Code CLI 已登入 Max/Pro 訂閱
- claude-agent-sdk、schedule 套件

## CI/CD 與 Git Remote

本專案以自架 GitLab 為開發主線，GitHub 作為對外鏡像：

- **雙 remote**：`origin` 指向 GitLab（預設推送目標），`github` 指向 GitHub。
- **鏡像管線**（`.gitlab-ci.yml`）：feature 分支開 Merge Request 合併進 `main` **不會**
  立即鏡像；需在 `main` 打上 `vX.Y.Z` 版本 tag，才會於該 tag 觸發 `mirror-to-github`，
  將 `main` 與該版本 tag 一併推送（鏡像）到 GitHub。
- **認證**：SSH 私鑰由 GitLab Runner 注入，名稱 `GITHUB_SSH_KEY`（其值可為金鑰檔路徑
  或金鑰內容，管線兩者皆支援）；對應公鑰需加到 GitHub repo → Settings → Deploy keys
  並勾選 Allow write access。
