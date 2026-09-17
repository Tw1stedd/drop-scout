@echo off
setlocal EnableExtensions
title Drop Scout 2.0
cd /d "%~dp0"

REM Optional UTF-8 console (banners below stay ASCII so OEM code pages don't mojibake)
chcp 65001 >nul 2>&1

echo.
echo  ======================================
echo    Drop Scout 2.0 - Dashboard
echo  ======================================
echo.
echo  Finding Python...

REM Prefer the py launcher (real install). Never invoke the Windows Store python stub
REM (%LocalAppData%\Microsoft\WindowsApps\python.exe) -- it can hang with no output.
set "PY="
set "PYX="
where py >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%I in ('where py 2^>nul') do (
        echo %%I | findstr /I /C:"WindowsApps" >nul
        if errorlevel 1 (
            "%%I" -3 -c "import sys" >nul 2>&1
            if not errorlevel 1 (
                set "PY=%%I"
                set "PYX=-3"
                goto :py_ok
            )
        )
    )
)

if not defined PY (
    for /f "delims=" %%I in ('where python 2^>nul') do (
        echo %%I | findstr /I /C:"WindowsApps" >nul
        if errorlevel 1 (
            "%%I" -c "import sys" >nul 2>&1
            if not errorlevel 1 (
                set "PY=%%I"
                set "PYX="
                goto :py_ok
            )
        )
    )
)

:py_ok
if not defined PY (
    echo  ERROR: Python not found ^(Windows Store stub ignored^).
    echo  Install from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    echo  Or install the "py" launcher and retry.
    pause
    exit /b 1
)

echo  Using: "%PY%" %PYX%

REM discord.py-self if missing (do not use bare "python" - Store stub)
"%PY%" %PYX% -c "import discord" >nul 2>&1
if errorlevel 1 (
    echo  Installing discord.py-self... (first run only)
    "%PY%" %PYX% -m pip install discord.py-self
    if errorlevel 1 (
        echo.
        echo  Install failed. Try: "%PY%" %PYX% -m pip install discord.py-self
        pause
        exit /b 1
    )
)

REM Do not kill old processes here. taskkill and netstat -ano can hang forever
REM on some PCs (same class of stall as Get-CimInstance). The server exits
REM immediately if 7890 is already bound.

echo  Starting Drop Scout 2.0 (dashboard UI)...
echo  Browser: http://localhost:7890
echo  Press Ctrl+C in this window to stop.
echo.

"%PY%" %PYX% discord_monitor_server.py

pause
endlocal
