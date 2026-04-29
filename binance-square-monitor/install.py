"""
跨平台一键安装脚本（兜底用，当 install.bat / install.sh 不好用时）

用法:
    python install.py          # 完整安装
    python install.py --skip-playwright   # 只装 pip 依赖，跳过浏览器内核（已装过则用这个）

要求:
    - Python >= 3.10
    - 能访问 pypi.org 和 playwright.azureedge.net
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path


BASE = Path(__file__).resolve().parent
VENV_DIR = BASE / ".venv"
IS_WIN = os.name == "nt"
PY_IN_VENV = VENV_DIR / ("Scripts/python.exe" if IS_WIN else "bin/python")


def step(n: int, total: int, text: str):
    print()
    print(f"[{n}/{total}] {text}")


def run(cmd: list[str], check: bool = True) -> int:
    """运行一个命令，实时输出。"""
    print(f"    $ {' '.join(str(c) for c in cmd)}")
    return subprocess.call(cmd, cwd=str(BASE))


def check_python_version():
    if sys.version_info < (3, 10):
        print(f"[错误] Python 版本过低（需要 3.10+），当前是 {sys.version_info.major}.{sys.version_info.minor}")
        print("       请从 https://www.python.org/downloads/ 安装较新版本")
        sys.exit(1)
    print(f"    Python {sys.version_info.major}.{sys.version_info.minor} ✓")


def create_venv():
    if PY_IN_VENV.exists():
        print(f"    虚拟环境已存在: {VENV_DIR}")
        return
    print(f"    创建虚拟环境: {VENV_DIR}")
    venv.create(str(VENV_DIR), with_pip=True)
    if not PY_IN_VENV.exists():
        print("[错误] 虚拟环境创建失败")
        sys.exit(1)


def upgrade_pip():
    if run([str(PY_IN_VENV), "-m", "pip", "install", "--upgrade", "pip"]) != 0:
        print("[警告] pip 升级失败，继续...")


def install_requirements():
    req = BASE / "requirements.txt"
    if not req.exists():
        print("[错误] 找不到 requirements.txt")
        sys.exit(1)
    if run([str(PY_IN_VENV), "-m", "pip", "install", "-r", str(req)]) != 0:
        print("[错误] 依赖安装失败。检查网络，或手动运行:")
        print(f"       {PY_IN_VENV} -m pip install -r requirements.txt")
        sys.exit(1)


def install_playwright():
    print("    安装 Chromium（约 150MB，首次较慢）...")
    if run([str(PY_IN_VENV), "-m", "playwright", "install", "chromium"]) != 0:
        print("[错误] Playwright 浏览器内核安装失败")
        sys.exit(1)
    # Linux 额外依赖
    if sys.platform.startswith("linux"):
        print("    尝试安装 Chromium 系统依赖（可能需要 sudo）...")
        run([str(PY_IN_VENV), "-m", "playwright", "install-deps", "chromium"], check=False)


def make_scripts_executable():
    if IS_WIN:
        return
    for name in ("start.sh", "stop.sh", "install.sh"):
        p = BASE / name
        if p.exists():
            try:
                p.chmod(0o755)
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-playwright", action="store_true",
                        help="跳过 Playwright 浏览器内核安装（已安装过用这个）")
    args = parser.parse_args()

    print()
    print("=====================================================")
    print("  Binance Square Monitor - 跨平台一键安装")
    print("=====================================================")

    total = 4 if args.skip_playwright else 5

    step(1, total, "检查 Python 版本 (需要 3.10+)")
    check_python_version()

    step(2, total, "创建虚拟环境 .venv")
    create_venv()

    step(3, total, "升级 pip")
    upgrade_pip()

    step(4, total, "安装 Python 依赖")
    install_requirements()

    if not args.skip_playwright:
        step(5, total, "安装 Playwright Chromium 浏览器内核")
        install_playwright()

    make_scripts_executable()

    print()
    print("=====================================================")
    print("  安装完成")
    print("=====================================================")
    print()
    if IS_WIN:
        print("  启动: 双击 start.bat  或运行 start.bat")
        print("  停止: 双击 stop.bat   或运行 stop.bat")
    else:
        print("  启动: ./start.sh")
        print("  停止: ./stop.sh")
    print()
    print("  Web 面板: http://127.0.0.1:8000")
    print()


if __name__ == "__main__":
    main()
