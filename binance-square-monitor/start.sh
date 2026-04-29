#!/usr/bin/env bash
cd "$(dirname "$0")"

if [ ! -f ".venv/bin/python" ]; then
    echo "[错误] 虚拟环境不存在。请先运行 ./install.sh"
    exit 1
fi

.venv/bin/python manage_processes.py start
