@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

echo.
echo =====================================================
echo   Binance Square Monitor - Auto Installer
echo =====================================================
echo.

REM ---- Step 1: ensure Python 3.10+ ----
echo [1/4] Ensuring Python 3.10+ is installed...
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0install_python.ps1"
if errorlevel 1 (
    echo.
    echo [ERROR] Python setup failed. See messages above.
    pause
    exit /b 1
)

set "PYCMD_FILE=%~dp0.python_cmd.txt"
if not exist "%PYCMD_FILE%" (
    echo [ERROR] Helper did not record Python command path.
    pause
    exit /b 1
)
set /p PYTHON_CMD=<"%PYCMD_FILE%"
del "%PYCMD_FILE%" >nul 2>&1

if "%PYTHON_CMD%"=="" (
    echo [ERROR] Python command empty.
    pause
    exit /b 1
)
echo     Using Python: %PYTHON_CMD%

REM ---- Step 2: create virtual env ----
echo.
echo [2/4] Creating virtual environment .venv ...
if exist ".venv\Scripts\python.exe" (
    echo     Virtual env already exists, skipping
) else (
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual env.
        pause
        exit /b 1
    )
    echo     Virtual env created
)

REM ---- Step 3: install Python dependencies ----
REM Use TUNA mirror first (China-friendly), fall back to default PyPI
echo.
echo [3/4] Installing Python dependencies ^(may take a few minutes^)...
echo     Trying Tsinghua mirror first...
call .venv\Scripts\python.exe -m pip install --upgrade pip --quiet -i https://pypi.tuna.tsinghua.edu.cn/simple
call .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo     Tsinghua mirror failed, trying default PyPI...
    call .venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERROR] Dependency install failed. Check network and retry.
        pause
        exit /b 1
    )
)
echo     Dependencies installed

REM ---- Step 4: install Playwright Chromium ----
echo.
echo [4/4] Installing Playwright Chromium ^(~150MB, slow on first run^)...
echo     Using Taobao npm mirror for Playwright...
set "PLAYWRIGHT_DOWNLOAD_HOST=https://registry.npmmirror.com/-/binary/playwright"
call .venv\Scripts\python.exe -m playwright install chromium
if errorlevel 1 (
    echo     Taobao mirror failed, trying default...
    set "PLAYWRIGHT_DOWNLOAD_HOST="
    call .venv\Scripts\python.exe -m playwright install chromium
    if errorlevel 1 (
        echo.
        echo [ERROR] Playwright Chromium install failed.
        echo         Try manually: .venv\Scripts\python.exe -m playwright install chromium
        pause
        exit /b 1
    )
)
echo     Playwright Chromium installed

echo.
echo =====================================================
echo   Installation complete!
echo =====================================================
echo.
echo   To start: double-click start.bat
echo   To stop:  double-click stop.bat
echo   Web panel: http://127.0.0.1:8000
echo.
echo   IMPORTANT:
echo   - Program needs to access binance.com
echo   - In mainland China, enable VPN/proxy in GLOBAL or TUN mode
echo.
pause
