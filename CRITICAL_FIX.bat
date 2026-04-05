@echo off
cls
echo ============================================================
echo  CRITICAL FIX - DISCORD LIBRARY
echo ============================================================
echo.
echo This will completely remove ALL discord libraries
echo and install ONLY the correct one: discord.py-self
echo.
pause
echo.

echo [1/5] Removing ALL discord libraries...
pip uninstall -y discord
pip uninstall -y discord.py
pip uninstall -y py-cord
pip uninstall -y nextcord
pip uninstall -y discord.py-self
echo.

echo [2/5] Clearing pip cache...
pip cache purge
echo.

echo [3/5] Installing discord.py-self (correct version)...
pip install --no-cache-dir discord.py-self
echo.

echo [4/5] Verifying installation...
python -c "import discord; print('Discord version:', discord.__version__); print('Has Intents:', hasattr(discord, 'Intents'))"
echo.

echo [5/5] Testing...
if exist TEST_DISCORD.py (
    python TEST_DISCORD.py
) else (
    echo TEST_DISCORD.py not found - skipping test
    echo But if you see "Has Intents: True" above, you're good!
)
echo.

echo ============================================================
echo  FIX COMPLETE!
echo ============================================================
echo.
echo If you see "Has Intents: True" above,
echo you can now run START.bat
echo.
pause
