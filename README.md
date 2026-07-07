# Tw_stock_ai_scheduler

台股 AI 摘要排程器，使用 Claude Agent SDK 搭配 Max 訂閱，定期產生 YT 精華摘要與每日新聞摘要。

## 功能說明

- **YT 精華摘要**（**事件驅動輪詢**，處理昨天日期）：每 `YT_POLL_MINUTES` 分鐘（預設 2 分鐘）**全天候**檢查一次昨天逐字稿是否出現，出現且尚未產摘要就立即讀取游庭皓的財經皓角逐字稿，直接餵完整 prompt 給 Agent SDK 產生精華摘要
- **每日新聞摘要**（**冪等補產輪詢**，處理昨天日期）：每 `NEWS_POLL_MINUTES` 分鐘（預設 5 分鐘）輪詢，到達當日就緒時刻 `NEWS_READY_TIME`（預設 08:00）後，昨天的摘要若尚未產出就讀取四來源新聞全文、直接餵完整 prompt 給 Agent SDK 補產

> **時序（2026-07 起，上游抓取移到早上）**：上游 `Tw_stock_DB_Operating` 已把新聞與
> YT 抓取由晚上移到早上並集中於 07:30–08:00——新聞四來源約 07:46–07:52、YT 逐字稿約
> 07:54 落檔（皆為「昨天」的資料），契約為 08:00 前全部完成。故 `NEWS_READY_TIME` 由
> 20:03 改為 **08:00**，且 **YT 日期由今天改為昨天**：游庭皓的「早晨財經速解讀」約
> 08:30（開盤前半小時）才開播，07:54 抓到的必然是**昨天**那集，且上游存成
> `YT/{昨天}/` 資料夾。兩種摘要皆於「今天早上」產出「昨天」的摘要。

> **為何兩種摘要都改輪詢**：固定單一時刻觸發過於脆弱——資料延遲、或 daemon 該刻
> 剛好沒在跑就整天錯過（`schedule` 遇錯過的時刻會直接跳到隔天）。改成每隔幾分鐘
> 檢查條件，達成且尚未產出就立即補產（catch-up）；並以 launchd `KeepAlive` 守護
> 讓 daemon 死掉自動復活。輪詢時「條件未達」與「摘要已存在」皆**安靜跳過、不記 log**
> （避免一天數百則洗版）；只有真正嘗試產出才會記錄。失敗時輸出檔不存在，下個輪詢
> tick 會自動重試；另設**每日嘗試上限**（同一日最多 5 次 SDK 嘗試，達上限只記一次
> ERROR 並停止重試、隔日歸零）防止持續失敗使成本暴衝。
>
> YT 精華摘要**全天候輪詢、不設就緒時刻**：逐字稿約 07:54 才落檔，07:54 前來源檢查
> 自然回 False 而安靜跳過，逐字稿一出現下個 tick 即產出（來源可用性即天然閘門）。
> 每日新聞摘要則另以 `NEWS_READY_TIME`（預設 08:00）為「就緒時刻」下限，確保四來源
> （早上 07:46–07:52 落檔）皆已落檔才產出，維持來源完整度；daemon 啟動時即做一次
> catch-up，故「啟動已過就緒時刻」的當日缺漏會在啟動幾秒內補上，而非等到隔天。
> 此輪詢僅補「昨天」一天，若 daemon 整天死掉漏掉某日，仍須以 `batch_news_summary.py`
> 手動補產。

> **設計重點（直接餵 prompt + 產出防呆）**：早期版本以 `query(prompt="/yt-summary")`
> 觸發 slash skill，但該 skill 已不存在於系統，SDK 只會回 `Unknown skill` 並以
> `is_error=False` 立即結束（假成功、$0.0000、無產出）。現改為**直接餵完整 prompt**，
> 並以**實際產出檔案**作為成功判準：任務後若預期輸出檔未被建立／更新即記為 ERROR，
> 杜絕空跑卻記成完成。

## 架構

```
launchd LaunchAgent（RunAtLoad + KeepAlive，死掉自動復活）
  └── Host macOS（conda env）
      └── ai_scheduler.py（schedule 主迴圈，背景 daemon；啟動即 catch-up sweep）
          ├── YT 每 N 分鐘（全天候）→ 冪等檢查 + 逐字稿來源檢查(昨天) → Agent SDK（完整 prompt）→ 產出防呆
          └── 新聞 每 M 分鐘 → 就緒時刻(08:00)後 + 冪等檢查 + 新聞來源檢查(昨天) → Agent SDK（完整 prompt）→ 產出防呆
              （prompt 組裝、來源檢查、冪等檢查、產出防呆皆共用 summaries.py）

認證：~/.claude/（Max/Pro 訂閱，憑證在 user Keychain）
工作目錄：本專案的上層目錄（Tw_stock/）
輸入：Tw_stock_DB/NewsContents/（逐字稿、新聞全文）
輸出：Tw_stock_news/YTNews/、Tw_stock_news/DailyNews/
```

## 專案結構

```
Tw_stock_ai_scheduler/
├── ai_scheduler.py        # 主程式（YT + 新聞皆冪等補產輪詢 + 啟動 catch-up + 產出防呆）
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

- `start`：環境檢查 → 清掉殘留 PID 檔 → 複製 / 更新 plist 到 `~/Library/LaunchAgents/`
  →（先 `bootout` 再）`launchctl bootstrap` 載入（`RunAtLoad` 立即啟動）。**會重讀 plist**，
  故環境變數（如 `YT_POLL_MINUTES`、conda 路徑）的變更要靠 `start` 才會生效。
- `stop`：`launchctl bootout` 卸載。
- `status`：`launchctl print` / `list` 查詢運行狀態。
- `restart`：`launchctl kickstart -k` 強制重啟行程（未載入時改走 `start`）。
  **僅重啟行程、不會重讀磁碟上的 plist**；要套用 plist / 環境變數變更請改用 `start`。

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

YT 精華摘要的輪詢間隔（分鐘）可透過 `YT_POLL_MINUTES` 環境變數覆蓋（預設 2）。
此值於安裝 plist 時寫入，需用 **`start`**（會重讀 plist）才會對 launchd daemon 生效；
`restart`（`kickstart`）只重啟行程、不會套用新值：

```bash
# 改為每 5 分鐘輪詢一次（用 start 才會生效）
YT_POLL_MINUTES=5 bash run.sh start
```

### 每日新聞輪詢間隔與就緒時刻

每日新聞摘要的輪詢間隔（分鐘）與就緒時刻可分別由 `NEWS_POLL_MINUTES`（預設 5）
與 `NEWS_READY_TIME`（HH:MM，預設 08:00）覆蓋。兩者為程式內預設值，daemon 直接讀取
即生效；若要對 launchd daemon 覆寫，請自行加入 plist 的 `EnvironmentVariables`
（`run.sh` 目前僅以 sed 注入 `YT_POLL_MINUTES` 與 conda 路徑，未注入這兩個變數）：

```bash
# 直接執行（非 launchd）時可用環境變數覆蓋
NEWS_POLL_MINUTES=10 NEWS_READY_TIME=08:30 python ai_scheduler.py
```

> `NEWS_READY_TIME` 之前不會嘗試產出昨天的摘要，確保四來源（早上 07:46–07:52 落檔）
> 皆已落檔才產出，維持來源完整度。其值於模組載入時解析一次；若格式非法（非零補位
> HH:MM）會記一次 WARNING 並 fallback 預設 08:00，不會讓 daemon 崩潰。

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
