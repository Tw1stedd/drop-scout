@echo off
cd /d "%~dp0"

:: Install dependencies silently if needed
pip show customtkinter >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    pip install -r ee_requirements.txt -q
    playwright install chromium -q
)

:: Launch GUI with no console window (pythonw suppresses terminal)
start "" pythonw ee_gui.py
