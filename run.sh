#!/bin/bash
#
# 啟動台股 AI 摘要排程器（背景 daemon）
# 使用 Torch conda 環境 + Claude Agent SDK（Max 訂閱認證）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Conda 環境名稱（可透過 CONDA_ENV 環境變數覆蓋）
CONDA_ENV="${CONDA_ENV:-Torch}"
CONDA_BASE="${CONDA_BASE:-${HOME}/miniconda3}"
PYTHON="${CONDA_BASE}/envs/${CONDA_ENV}/bin/python"
PID_FILE="${SCRIPT_DIR}/ai_scheduler.pid"
LOG_DIR="${SCRIPT_DIR}/logs"

mkdir -p "${LOG_DIR}"

# 檢查 Python 環境
if [[ ! -x "${PYTHON}" ]]; then
  echo "錯誤：找不到 Python 執行檔：${PYTHON}"
  echo "請確認 Torch conda 環境已建立。"
  exit 1
fi

# 檢查 claude-agent-sdk 是否已安裝
if ! "${PYTHON}" -c "import claude_agent_sdk" 2>/dev/null; then
  echo "錯誤：claude-agent-sdk 未安裝。"
  echo "請執行：${PYTHON} -m pip install claude-agent-sdk"
  exit 1
fi

case "${1:-start}" in
  start)
    # 若已在運行，先停止
    if [[ -f "${PID_FILE}" ]]; then
      OLD_PID=$(cat "${PID_FILE}")
      if kill -0 "${OLD_PID}" 2>/dev/null; then
        echo "排程器已在運行（PID ${OLD_PID}），先停止..."
        kill "${OLD_PID}" 2>/dev/null || true
        sleep 1
      fi
      rm -f "${PID_FILE}"
    fi

    echo "啟動台股 AI 摘要排程器..."
    nohup "${PYTHON}" "${SCRIPT_DIR}/ai_scheduler.py" \
      > /dev/null 2>&1 &
    echo $! > "${PID_FILE}"
    echo "排程器已啟動（PID $(cat "${PID_FILE}")）"
    echo "日誌：${LOG_DIR}/ai_scheduler.log"
    ;;

  stop)
    if [[ -f "${PID_FILE}" ]]; then
      PID=$(cat "${PID_FILE}")
      if kill -0 "${PID}" 2>/dev/null; then
        echo "停止排程器（PID ${PID}）..."
        kill "${PID}"
        rm -f "${PID_FILE}"
        echo "已停止。"
      else
        echo "排程器未在運行（PID ${PID} 已不存在）。"
        rm -f "${PID_FILE}"
      fi
    else
      echo "排程器未在運行（無 PID 檔案）。"
    fi
    ;;

  status)
    if [[ -f "${PID_FILE}" ]]; then
      PID=$(cat "${PID_FILE}")
      if kill -0 "${PID}" 2>/dev/null; then
        echo "排程器運行中（PID ${PID}）"
      else
        echo "排程器未在運行（PID ${PID} 已不存在）"
        rm -f "${PID_FILE}"
      fi
    else
      echo "排程器未在運行。"
    fi
    ;;

  restart)
    # 以絕對路徑呼叫自身，避免相對檔名不在 PATH 造成 command not found
    bash "${SCRIPT_DIR}/run.sh" stop
    sleep 1
    bash "${SCRIPT_DIR}/run.sh" start
    ;;

  *)
    echo "用法：$0 {start|stop|status|restart}"
    exit 1
    ;;
esac
