#!/usr/bin/env bash
# 股票论坛可视化查看器 —— 一键启动（自动激活 .venv）
# 用法: bash scripts/run_app.sh
set -e
cd "$(dirname "$0")/.."
if [ ! -x ".venv/bin/python" ]; then
    echo "[错误] 未找到 .venv，请先执行: python3 -m venv .venv && .venv/bin/pip install -r requirements.lock.txt"
    exit 1
fi
source .venv/bin/activate
python viewer/server.py
