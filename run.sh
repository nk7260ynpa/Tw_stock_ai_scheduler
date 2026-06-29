#!/bin/bash
#
# 管理台股 AI 摘要排程器（以 launchd LaunchAgent 守護的背景 daemon）。
# 使用 Torch conda 環境 + Claude Agent SDK（Max 訂閱認證）。
#
# 改用 launchctl 取代舊的 nohup + PID 檔：透過 plist 的 RunAtLoad + KeepAlive
# 達到「開機自動啟動、daemon 死掉自動復活」。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Conda 環境（可透過 CONDA_ENV / CONDA_BASE 環境變數覆蓋）
CONDA_ENV="${CONDA_ENV:-Torch}"
CONDA_BASE="${CONDA_BASE:-${HOME}/miniconda3}"
PYTHON="${CONDA_BASE}/envs/${CONDA_ENV}/bin/python"

# plist 中內建（預設）與本次推導的 conda bin 目錄；安裝時用後者覆寫前者，
# 使 CONDA_ENV / CONDA_BASE 覆寫實際生效。
DEFAULT_CONDA_BIN="${HOME}/miniconda3/envs/Torch/bin"
CONDA_BIN="${CONDA_BASE}/envs/${CONDA_ENV}/bin"

LOG_DIR="${SCRIPT_DIR}/logs"
LEGACY_PID_FILE="${SCRIPT_DIR}/ai_scheduler.pid"

# launchd 服務識別
PLIST_LABEL="com.twstock.ai-scheduler"
PLIST_SRC="${SCRIPT_DIR}/launchd/${PLIST_LABEL}.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"
UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"
SERVICE_TARGET="${DOMAIN}/${PLIST_LABEL}"

mkdir -p "${LOG_DIR}"

# 清掉舊版 nohup 殘留的 PID 檔（已改用 launchd，不再需要）。
cleanup_legacy_pid() {
  if [[ -f "${LEGACY_PID_FILE}" ]]; then
    echo "清除舊版殘留 PID 檔：${LEGACY_PID_FILE}"
    rm -f "${LEGACY_PID_FILE}"
  fi
}

# 安裝 / 更新 LaunchAgent plist 到 ~/Library/LaunchAgents/。
#
# 因 launchd 啟動 daemon 時帶入的是 plist 的 EnvironmentVariables（非執行 run.sh
# 的 shell 環境），故把可調參數於安裝時寫入 plist：
#   - 依 CONDA_ENV / CONDA_BASE 覆寫 python 路徑與 PATH 中對應的 conda bin 目錄。
#   - 依 YT_POLL_MINUTES 覆寫 YT 輪詢間隔（plist 中其值是唯一的
#     `<string>2</string>`，故可安全替換；預設沿用 2）。
# 預設值下兩項替換皆為 no-op，等同直接複製。
install_plist() {
  if [[ ! -f "${PLIST_SRC}" ]]; then
    echo "錯誤：找不到 plist 來源：${PLIST_SRC}"
    exit 1
  fi
  mkdir -p "$(dirname "${PLIST_DST}")" "${LOG_DIR}"
  local poll="${YT_POLL_MINUTES:-2}"
  sed -e "s|${DEFAULT_CONDA_BIN}|${CONDA_BIN}|g" \
      -e "s|<string>2</string>|<string>${poll}</string>|" \
      "${PLIST_SRC}" > "${PLIST_DST}"
}

# 啟動前的環境檢查：python 可執行、claude-agent-sdk 已安裝。
check_python_env() {
  if [[ ! -x "${PYTHON}" ]]; then
    echo "錯誤：找不到 Python 執行檔：${PYTHON}"
    echo "請確認 ${CONDA_ENV} conda 環境已建立。"
    exit 1
  fi
  if ! "${PYTHON}" -c "import claude_agent_sdk" 2>/dev/null; then
    echo "錯誤：claude-agent-sdk 未安裝。"
    echo "請執行：${PYTHON} -m pip install claude-agent-sdk"
    exit 1
  fi
}

case "${1:-start}" in
  start)
    check_python_env
    cleanup_legacy_pid
    install_plist
    # 若已載入，先 bootout 以套用最新 plist，再重新 bootstrap。
    launchctl bootout "${SERVICE_TARGET}" 2>/dev/null || true
    launchctl bootstrap "${DOMAIN}" "${PLIST_DST}"
    echo "已透過 launchd 載入並啟動 ${PLIST_LABEL}"
    echo "日誌：${LOG_DIR}/ai_scheduler.log"
    ;;

  stop)
    cleanup_legacy_pid
    if launchctl bootout "${SERVICE_TARGET}" 2>/dev/null; then
      echo "已停止並卸載 ${PLIST_LABEL}"
    else
      echo "${PLIST_LABEL} 未載入（無需停止）。"
    fi
    ;;

  status)
    if launchctl print "${SERVICE_TARGET}" >/dev/null 2>&1; then
      echo "排程器運行中（${PLIST_LABEL}）"
      launchctl list | grep "${PLIST_LABEL}" || true
    else
      echo "排程器未載入（${PLIST_LABEL}）。"
    fi
    ;;

  restart)
    check_python_env
    cleanup_legacy_pid
    install_plist
    # 已載入則用 kickstart -k 強制重啟；未載入則改走 start 流程。
    if launchctl kickstart -k "${SERVICE_TARGET}" 2>/dev/null; then
      echo "已重啟 ${PLIST_LABEL}"
    else
      bash "${SCRIPT_DIR}/run.sh" start
    fi
    ;;

  *)
    echo "用法：$0 {start|stop|status|restart}"
    exit 1
    ;;
esac
