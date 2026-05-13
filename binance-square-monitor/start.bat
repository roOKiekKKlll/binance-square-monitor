@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual env not found. Run install.bat first.
    pause
    exit /b 1
)

echo [INFO] Restarting monitor processes to reload latest config...
.venv\Scripts\python.exe manage_processes.py restart
if errorlevel 1 (
    echo [ERROR] Failed to restart processes.
    pause
    exit /b 1
)

echo [INFO] Current process status:
.venv\Scripts\python.exe manage_processes.py status
