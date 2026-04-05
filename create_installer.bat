@echo off
title Drop Scout — Create Portable Package
echo =============================================
echo   Drop Scout — Package Creator
echo =============================================
echo.

:: Create a self-extracting zip-like package
:: Since Inno Setup is not installed, we'll create a portable zip

set "DIST=dist\DropScout"
set "OUT=Output"

if not exist "%DIST%\DropScout.exe" (
    echo ERROR: dist\DropScout\DropScout.exe not found!
    echo Run BUILD.bat first to create the executable.
    pause
    exit /b 1
)

:: Create output directory
if not exist "%OUT%" mkdir "%OUT%"

echo Creating portable package...
echo.

:: Use PowerShell to create a zip
powershell -Command "Compress-Archive -Path '%DIST%\*' -DestinationPath '%OUT%\DropScout-v2.0-Portable.zip' -Force"

if errorlevel 1 (
    echo ERROR: Failed to create zip package!
    pause
    exit /b 1
)

echo.
echo =============================================
echo   PACKAGE READY!
echo =============================================
echo.
echo   Portable zip: Output\DropScout-v2.0-Portable.zip
echo.
echo   To use:
echo     1. Extract the zip to any folder
echo     2. Run DropScout.exe
echo.
echo   For a proper installer with Start Menu shortcuts:
echo     1. Download Inno Setup 6 from https://jrsoftware.org/isinfo.php
echo     2. Open dropscout_installer.iss
echo     3. Click Build ^> Compile
echo     4. Get DropScout-Setup-v2.0.exe in Output folder
echo.
pause
