@echo off
title Drop Scout — Build Executable
echo =============================================
echo   Drop Scout — One-Click Build
echo =============================================
echo.

:: Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH!
    echo Download from https://python.org
    pause
    exit /b 1
)

:: Step 1: Install desktop dependencies
echo [1/3] Installing dependencies...
pip install -r requirements_desktop.txt
if errorlevel 1 (
    echo WARNING: Some dependencies may have failed to install.
    echo Continuing anyway...
)
echo.

:: Step 2: Run PyInstaller build
echo [2/3] Building executable with PyInstaller...
python build_exe.py
if errorlevel 1 (
    echo.
    echo ERROR: Build failed!
    pause
    exit /b 1
)
echo.

:: Step 3: Check if Inno Setup is available
echo [3/3] Creating installer...
set INNO_PATH=
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" (
    set "INNO_PATH=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
) else if exist "C:\Program Files\Inno Setup 6\ISCC.exe" (
    set "INNO_PATH=C:\Program Files\Inno Setup 6\ISCC.exe"
)

if defined INNO_PATH (
    echo Found Inno Setup at: %INNO_PATH%
    "%INNO_PATH%" dropscout_installer.iss
    if errorlevel 1 (
        echo WARNING: Installer build failed.
        echo You can still use dist\DropScout\DropScout.exe directly.
    ) else (
        echo.
        echo =============================================
        echo   INSTALLER READY!
        echo   Output\DropScout-Setup-v2.0.exe
        echo =============================================
    )
) else (
    echo Inno Setup not found — skipping installer creation.
    echo.
    echo To create the installer:
    echo   1. Download Inno Setup 6 from https://jrsoftware.org/isinfo.php
    echo   2. Open dropscout_installer.iss in Inno Setup Compiler
    echo   3. Click Build ^> Compile
    echo.
    echo You can still run the app directly:
    echo   dist\DropScout\DropScout.exe
)

echo.
echo =============================================
echo   BUILD COMPLETE!
echo =============================================
echo.
echo   Standalone exe: dist\DropScout\DropScout.exe
echo   (Run this to test before creating installer)
echo.
pause
