"""Start/stop all monitor processes from one command.

Usage:
    python manage_processes.py start
    python manage_processes.py stop
    python manage_processes.py restart
    python manage_processes.py status
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
PID_FILE = BASE_DIR / ".monitor_processes.json"
WEB_URL = "http://127.0.0.1:8000"
PROGRAMS = [
    ("worker", "worker.py"),
    ("market_realtime", "market_realtime.py"),
    ("web", "web.py"),
    ("auto_trader", "auto_trader.py"),
]


def _load_state() -> dict:
    if not PID_FILE.exists():
        return {}
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict):
    PID_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _matching_script_pids(script: str) -> list[int]:
    """Find running python PIDs whose command line targets this project script."""
    script_path = str((BASE_DIR / script).resolve())
    pids: list[int] = []
    if os.name == "nt":
        # PowerShell 输出: "<pid>|<commandline>"
        ps = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -eq 'python.exe' } | "
            "ForEach-Object { "
            "$pid=$_.ProcessId; $cmd=$_.CommandLine; "
            "if ($cmd) { Write-Output ($pid.ToString() + '|' + $cmd) } }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        # 宽松匹配：路径分隔符、大小写、引号差异都允许
        pattern = re.escape(script_path).replace(r"\\", r"[\\/]")
        rx = re.compile(pattern, re.IGNORECASE)
        for line in result.stdout.splitlines():
            if "|" not in line:
                continue
            pid_text, cmd = line.split("|", 1)
            try:
                pid = int(pid_text.strip())
            except ValueError:
                continue
            if rx.search(cmd):
                pids.append(pid)
        return pids

    result = subprocess.run(
        ["pgrep", "-f", script_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    for line in result.stdout.splitlines():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            continue
    return pids


def _find_running_program_pid(script: str) -> int | None:
    """Pick one running pid for this script in this project."""
    pids = [pid for pid in _matching_script_pids(script) if _pid_running(pid)]
    return pids[0] if pids else None


def _start_one(name: str, script: str) -> dict:
    path = BASE_DIR / script
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_CONSOLE

    proc = subprocess.Popen(
        [sys.executable, str(path)],
        cwd=str(BASE_DIR),
        creationflags=creationflags,
    )
    return {
        "pid": proc.pid,
        "script": script,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def start(open_browser: bool = True):
    state = _load_state()
    changed = False

    for name, script in PROGRAMS:
        old = state.get(name) or {}
        if _pid_running(old.get("pid")):
            print(f"{name}: already running pid={old['pid']}")
            continue
        existing_pid = _find_running_program_pid(script)
        if existing_pid:
            state[name] = {
                "pid": existing_pid,
                "script": script,
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            changed = True
            print(f"{name}: detected existing pid={existing_pid} (adopted)")
            continue
        info = _start_one(name, script)
        state[name] = info
        changed = True
        print(f"{name}: started pid={info['pid']}")
        time.sleep(0.8)

    if changed:
        _save_state(state)

    if open_browser:
        webbrowser.open(WEB_URL)
        print(f"browser: opened {WEB_URL}")


def _stop_pid(pid: int) -> bool:
    if not _pid_running(pid):
        return False
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        os.kill(pid, signal.SIGTERM)
    return True


def stop():
    state = _load_state()
    if not state:
        print("no saved processes")
        return

    for name, _script in reversed(PROGRAMS):
        info = state.get(name) or {}
        pid = info.get("pid")
        if not pid:
            print(f"{name}: no pid")
            continue
        if _stop_pid(int(pid)):
            print(f"{name}: stopped pid={pid}")
        else:
            print(f"{name}: not running pid={pid}")

    # 兜底清理：即使不在 PID_FILE，也把本项目脚本残留进程杀掉，避免双实例。
    for name, script in reversed(PROGRAMS):
        for pid in _matching_script_pids(script):
            if _stop_pid(int(pid)):
                print(f"{name}: cleaned residual pid={pid}")

    if PID_FILE.exists():
        PID_FILE.unlink()


def status():
    state = _load_state()
    if not state:
        print("no saved processes")
        return
    for name, script in PROGRAMS:
        info = state.get(name) or {}
        pid = info.get("pid")
        running = _pid_running(pid)
        print(f"{name}: {'running' if running else 'stopped'} pid={pid} script={script}")


def restart(open_browser: bool = True):
    stop()
    time.sleep(1)
    start(open_browser=open_browser)


def main():
    parser = argparse.ArgumentParser(description="Manage Binance monitor processes.")
    parser.add_argument(
        "command",
        choices=("start", "stop", "restart", "status"),
        help="Action to perform.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the web page after start/restart.",
    )
    args = parser.parse_args()

    if args.command == "start":
        start(open_browser=not args.no_browser)
    elif args.command == "stop":
        stop()
    elif args.command == "restart":
        restart(open_browser=not args.no_browser)
    elif args.command == "status":
        status()


if __name__ == "__main__":
    main()
