@echo off
title Entertainment Earth Checkout Bot
color 0A
cd /d "%~dp0"

echo ============================================================
echo   Entertainment Earth Checkout Bot
echo ============================================================
echo.
echo   Usage flags you can append:
echo     --guest              Use guest checkout (no login)
echo     --headless           Invisible browser
echo     --no-auto-checkout   Fill cart but don't place order
echo     --proxy http://...   Override proxy
echo     --interval 3         Monitor every 3 seconds
echo     --url https://...    One-shot checkout for a URL
echo.
echo   Example: python ee_bot.py --guest --interval 3
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install from https://python.org
    pause
    exit /b 1
)

:: Install dependencies if needed
pip show playwright >nul 2>&1
if errorlevel 1 (
    echo [SETUP] Installing dependencies...
    pip install -r ee_requirements.txt
    playwright install chromium
    echo.
)

:: Run bot (pass any CLI args through)
echo [BOT] Starting...
echo.
python ee_bot.py %*

echo.
echo [BOT] Exited.
pause
