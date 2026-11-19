#!/bin/sh
# ============================================================================
# Bot-Hosting.net / Pterodactyl 容器启动脚本
# ----------------------------------------------------------------------------
# 在 Bot-Hosting.net 面板里把启动命令设为:  bash start.sh
# 平台会注入端口到 PORT（或 SERVER_PORT），脚本自动识别。
# 免费档 256MB 内存 / 25% CPU，所以只开 1 个 worker，自愈间隔拉长。
# ============================================================================
set -e

cd "$(dirname "$0")"

# 端口：Pterodactyl 各家注入的变量名不一，逐个兜底
PORT="${PORT:-${SERVER_PORT:-${DELTA_PORT:-8080}}}"

# 依赖只在第一次装（venv 或 site-packages 里有 requests 就跳过）
if ! python -c "import requests, fastapi, uvicorn, Crypto" >/dev/null 2>&1; then
    echo "==> 安装依赖..."
    pip install --no-cache-dir -q -r requirements.txt
fi

# 状态文件放自家目录（容器重建后工作目录内容还在）
mkdir -p state
export DELTA_CACHE_FILE="${DELTA_CACHE_FILE:-$(pwd)/state/keys.json}"
export DELTA_RETRYQ_FILE="${DELTA_RETRYQ_FILE:-$(pwd)/state/retryq.json}"
export DELTA_LINK_STATE="${DELTA_LINK_STATE:-$(pwd)/state/link_state.json}"
export DELTA_NO_PROXY="${DELTA_NO_PROXY:-1}"
export DELTA_WORKERS="${DELTA_WORKERS:-1}"
export DELTA_HEAL_INTERVAL="${DELTA_HEAL_INTERVAL:-600}"
export PYTHONUNBUFFERED=1
export LANG=C.UTF-8

echo "==> 启动 Delta 服务 port=$PORT workers=$DELTA_WORKERS"
exec python -u delta.py --host 0.0.0.0 --port "$PORT" --workers "$DELTA_WORKERS"
