# Tw_stock_ai_scheduler

台股 AI 摘要排程器，使用 Claude Agent SDK 搭配 Max 訂閱，定期產生 YT 精華摘要與每日新聞摘要。

## 功能說明

- **YT 精華摘要**（**事件驅動輪詢**，處理今天日期）：每 `YT_POLL_MINUTES` 分鐘（預設 2 分鐘）檢查一次今天逐字稿是否出現，出現且尚未產摘要就立即讀取游庭皓的財經皓角逐字稿，直接餵完整 prompt 給 Agent SDK 產生精華摘要
- **每日新聞摘要**（每日 20:03，處理昨天日期）：讀取四來源新聞全文，直接餵完整 prompt 給 Agent SDK 產生每日新聞摘要

> **為何 YT 改輪詢**：固定 19:15 觸發過於脆弱——逐字稿延遲、或 daemon 該刻剛好沒在跑
> 就整天錯過。改成每隔幾分鐘檢查今天逐字稿是否出現，出現且尚未產摘要就立即補產；
> 並以 launchd `KeepAlive` 守護讓 daemon 死掉自動復活。輪詢時「逐字稿尚未出現」與
> 「摘要已存在」皆**安靜跳過、不記 log**（避免一天數百則洗版）；只有真正嘗試產出
> 才會記錄。失敗時輸出檔不存在，下個輪詢 tick 會自動重試；另設**每日嘗試上限**
> （同一日最多 5 次 SDK 嘗試，達上限只記一次 ERROR 並停止重試、隔日歸零）防止持續
> 失敗使成本暴衝。每日新聞摘要維持固定 20:03，不受影響。

> **設計重點（直接餵 prompt + 產出防呆）**：早期版本以 `query(prompt="/yt-summary")`
> 觸發 slash skill，但該 skill 已不存在於系統，SDK 只會回 `Unknown skill` 並以
> `is_error=False` 立即結束（假成功、$0.0000、無產出）。現改為**直接餵完整 prompt**，
> 並以**實際產出檔案**作為成功判準：任務後若預期輸出檔未被建立／更新即記為 ERROR，
> 杜絕空跑卻記成完成。

## 架構

```
launchd LaunchAgent（RunAtLoad + KeepAlive，死掉自動復活）
  └── Host macOS（conda env）
      └── ai_scheduler.py（schedule 主迴圈，背景 daemon）
          ├── 每 N 分鐘 → 冪等檢查 + 逐字稿來源檢查 → Agent SDK（完整 prompt）→ 產出防呆
          └── 20:03     → 檢查新聞來源 → Agent SDK（完整 prompt）→ 產出防呆
              （prompt 組裝、來源檢查、冪等檢查、產出防呆皆共用 summaries.py）

認證：~/.claude/（Max/Pro 訂閱，憑證在 user Keychain）
工作目錄：本專案的上層目錄（Tw_stock/）
輸入：Tw_stock_DB/NewsContents/（逐字稿、新聞全文）
輸出：Tw_stock_news/YTNews/、Tw_stock_news/DailyNews/
```

## 專案結構

```
Tw_stock_ai_scheduler/
├── ai_scheduler.py        # 主程式（YT 輪詢 + 新聞固定排程 + 產出防呆）
├── summaries.py           # 共用邏輯（prompt 組裝、日期、來源/冪等檢查、產出防呆、SDK 執行）
├── batch_news_summary.py  # 批次補抓每日新聞摘要
├── batch_yt_summary.py    # 批次補抓 YT 精華摘要
├── run.sh                 # 以 launchctl 管理 daemon（start/stop/status/restart）
├── launchd/               # launchd LaunchAgent plist（守護用）
│   └── com.twstock.ai-scheduler.plist
├── pyproject.toml         # Python 專案定義（PEP 621）
├── requirements.txt       # 依賴
├── test/                  # 單元測試（純函式 + 輪詢邏輯，不真打 SDK）
├── logs/                  # 日誌資料夾
├── .gitignore
└── README.md
```

## 批次補抓

```bash
# 補抓每日新聞摘要區間（無來源的日子會自動略過並列出）
python batch_news_summary.py 2026-06-10 2026-06-26

# 補抓 YT 精華摘要區間（無逐字稿的日子會自動略過並列出）
python batch_yt_summary.py 2026-04-10 2026-06-22
```

兩支腳本具備韌性，適合大批量補抓：

- **可重入（idempotent）**：輸出檔已存在即略過，重跑不重做已完成日。
- **per-day 容錯**：單日失敗只記 ERROR 並繼續下一天，**不中止整批**。
- **指數退避重試**：單日最多嘗試 3 次，吸收暫時性 SDK 失敗（如撞到訂閱用量上限的 `exit code 1`）。
- 結束印「成功／略過／失敗」明細；若中途撞限，待用量重置後**重跑同指令**即可續補。

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

`run.sh` 以 **launchd LaunchAgent** 管理 daemon（取代舊的 `nohup` + PID 檔）：

- `start`：環境檢查 → 清掉殘留 PID 檔 → 複製 plist 到 `~/Library/LaunchAgents/` →
  `launchctl bootstrap` 載入（`RunAtLoad` 立即啟動）。
- `stop`：`launchctl bootout` 卸載。
- `status`：`launchctl print` / `list` 查詢運行狀態。
- `restart`：`launchctl kickstart -k` 強制重啟（未載入時改走 `start`）。

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
# Python 排程器日誌
tail -f logs/ai_scheduler.log

# launchd 捕捉的 stdout / stderr
tail -f logs/launchd.out.log logs/launchd.err.log
```

## launchd 守護（自動重啟）

daemon 由 launchd LaunchAgent（`launchd/com.twstock.ai-scheduler.plist`）守護：

- `RunAtLoad=true`：登入 / 載入時自動啟動。
- `KeepAlive=true`：daemon 異常結束時自動復活（無需人工介入）。

安裝步驟（`bash run.sh start` 已自動完成，亦可手動執行）：

```bash
# 1. 複製 plist 到使用者 LaunchAgents 目錄
cp launchd/com.twstock.ai-scheduler.plist ~/Library/LaunchAgents/

# 2. 載入（gui/$UID 網域）
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.twstock.ai-scheduler.plist

# 3. 確認載入並取得 PID
launchctl list | grep ai-scheduler
```

> **重要約束**
>
> - 必須安裝為 **LaunchAgent**（`~/Library/LaunchAgents/`、使用者登入 session 載入），
>   才能存取 user Keychain 的 Max 訂閱憑證；**不可**改成 system LaunchDaemon。
> - plist 的 `EnvironmentVariables` **絕對不可**設 `ANTHROPIC_API_KEY`，否則會覆蓋訂閱、
>   改走 API 計費。

## 設定

### Conda 環境

`run.sh` 預設使用 `Torch` conda 環境。可透過環境變數覆蓋：

```bash
# 使用其他 conda 環境
CONDA_ENV=myenv bash run.sh start

# 指定 conda 安裝路徑
CONDA_BASE=/opt/conda CONDA_ENV=myenv bash run.sh start
```

`run.sh` 安裝 plist 時，會依 `CONDA_ENV` / `CONDA_BASE` 自動覆寫 plist 中的
python 路徑與 PATH 中對應的 conda bin 目錄，故切換環境毋須手動改 plist。

### YT 輪詢間隔

YT 精華摘要的輪詢間隔（分鐘）可透過 `YT_POLL_MINUTES` 環境變數覆蓋（預設 2）：

```bash
# 改為每 5 分鐘輪詢一次
YT_POLL_MINUTES=5 bash run.sh restart
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
