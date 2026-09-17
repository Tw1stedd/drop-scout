#!/bin/bash
cd "$(dirname "$0")"
echo ""
echo "  ======================================"
echo "    Drop Scout 2.0 - Dashboard"
echo "  ======================================"
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "  ERROR: Python3 not found."
    echo "  Install with: sudo apt install python3  (Linux)"
    echo "             or: brew install python3     (Mac)"
    exit 1
fi

# Install discord.py-self if missing
python3 -c "import discord" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "  Installing discord.py-self... (first run only)"
    pip3 install discord.py-self
    if [ $? -ne 0 ]; then
        echo ""
        echo "  Install failed. Try: pip3 install discord.py-self"
        exit 1
    fi
fi

# Kill old server process so launch always starts clean/offline
pkill -f "discord_monitor_server.py" >/dev/null 2>&1 || true

echo "  Starting Drop Scout 2.0 (dashboard UI)..."
echo "  Browser: http://localhost:7890"
echo "  Press Ctrl+C to stop."
echo ""

python3 discord_monitor_server.py
