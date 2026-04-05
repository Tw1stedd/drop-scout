@echo off
echo ============================================================
echo  FIXING DISCORD LIBRARY - CRITICAL FIX
echo ============================================================
echo.

echo [1/4] Uninstalling WRONG discord library...
pip uninstall -y discord discord.py
echo.

echo [2/4] Installing CORRECT discord.py-self...
pip install discord.py-self
echo.

echo [3/4] Verifying installation...
python -c "import discord; print('Discord version:', discord.__version__)"
echo.

echo [4/4] Done!
echo.
echo ============================================================
echo  NOW RUN START.bat
echo ============================================================
pause
