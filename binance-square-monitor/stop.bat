@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual env not found.
    pause
    exit /b 1
)

echo [INFO] Stopping monitor processes...
.venv\Scripts\python.exe manage_processes.py stop

echo [INFO] Current process status:
.venv\Scripts\python.exe manage_processes.py status

pause
