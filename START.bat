@echo off
title Drop Scout 2.0
cd /d "%~dp0"

echo.
echo  ======================================
echo    Drop Scout 2.0  —  Dashboard
echo  ======================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found.
    echo  Download from: https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Install discord.py-self if missing
python -c "import discord" >nul 2>&1
if errorlevel 1 (
    echo  Installing discord.py-self... ^(first run only^)
    pip install discord.py-self
    if errorlevel 1 (
        echo.
        echo  Install failed. Try running: pip install discord.py-self
        pause
        exit /b 1
    )
)

:: Kill old server processes so launch always starts clean/offline
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*discord_monitor_server.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
:: Also kill any running DropScout.exe (compiled version) to free port 7890
taskkill /F /IM DropScout.exe >nul 2>&1

echo  Starting Drop Scout 2.0 ^(dashboard UI^)...
echo  Browser: http://localhost:7890
echo  Press Ctrl+C in this window to stop.
echo.

python discord_monitor_server.py

pause
