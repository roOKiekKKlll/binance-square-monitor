#!/usr/bin/env bash
# =====================================================
#  Binance Square Monitor - macOS/Linux 一键安装脚本
# =====================================================
set -e

cd "$(dirname "$0")"

echo ""
echo "====================================================="
echo "  Binance Square Monitor 一键安装"
echo "====================================================="
echo ""

# ---------- 1. 检查 Python ----------
echo "[1/5] 检查 Python 环境..."
if ! command -v python3 &>/dev/null; then
    echo ""
    echo "[错误] 没检测到 python3。请先安装 Python 3.10 或更新版本"
    echo "       macOS: brew install python@3.11"
    echo "       Ubuntu/Debian: sudo apt install python3 python3-venv python3-pip"
    exit 1
fi

PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "    已找到 Python $PYVER"

# 检查版本 >= 3.10
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || {
    echo ""
    echo "[错误] Python 版本过低 (需要 3.10+)，当前是 $PYVER"
    exit 1
}
echo "    Python 版本满足要求 (>= 3.10)"

# ---------- 2. 创建虚拟环境 ----------
echo ""
echo "[2/5] 创建虚拟环境 .venv ..."
if [ -f ".venv/bin/python" ]; then
    echo "    虚拟环境已存在，跳过创建"
else
    python3 -m venv .venv || {
        echo "[错误] 创建虚拟环境失败"
        echo "       Ubuntu/Debian 用户可能需要: sudo apt install python3-venv"
        exit 1
    }
    echo "    虚拟环境创建成功"
fi

# ---------- 3. 升级 pip ----------
echo ""
echo "[3/5] 升级 pip ..."
.venv/bin/python -m pip install --upgrade pip --quiet || echo "[警告] pip 升级失败，继续..."

# ---------- 4. 安装依赖 ----------
echo ""
echo "[4/5] 安装 Python 依赖 (可能需要几分钟) ..."
.venv/bin/python -m pip install -r requirements.txt || {
    echo ""
    echo "[错误] 依赖安装失败"
    exit 1
}
echo "    依赖安装完成"

# ---------- 5. 安装 Playwright Chromium ----------
echo ""
echo "[5/5] 安装 Playwright Chromium 浏览器内核 (约 150MB，首次安装较慢) ..."
.venv/bin/python -m playwright install chromium || {
    echo ""
    echo "[错误] Playwright 浏览器内核安装失败"
    exit 1
}
echo "    Playwright Chromium 安装完成"

# Linux 可能需要额外依赖
if [ "$(uname)" = "Linux" ]; then
    echo ""
    echo "[提示] Linux 用户如果运行时遇到浏览器缺少库，可运行："
    echo "       .venv/bin/python -m playwright install-deps chromium"
fi

# ---------- 给启动脚本加可执行权限 ----------
chmod +x start.sh stop.sh 2>/dev/null || true

# ---------- 完成 ----------
echo ""
echo "====================================================="
echo "  安装完成！"
echo "====================================================="
echo ""
echo "  启动命令："
echo "    ./start.sh                    <-- 一键启动"
echo "    .venv/bin/python manage_processes.py start"
echo ""
echo "  停止命令："
echo "    ./stop.sh"
echo "    .venv/bin/python manage_processes.py stop"
echo ""
echo "  Web 面板: http://127.0.0.1:8000"
echo ""
