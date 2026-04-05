# Discord Monitor — Setup Guide

## Files in this folder
- `discord_monitor_server.py`  — Backend server (runs the monitor)
- `discord_monitor_ui.html`    — The UI (opens automatically in browser)
- `START.bat`                  — Windows launcher (double-click)
- `start.sh`                   — Mac/Linux launcher
- `monitor_config.json`        — Your settings (auto-created on first save)

---

## Step 1 — Install Python
- **Windows**: https://www.python.org/downloads/ — check "Add to PATH" during install
- **Mac**: Python3 is usually already installed
- **Linux (Mint)**: `sudo apt install python3 python3-pip`

---

## Step 2 — Launch
- **Windows**: Double-click `START.bat`
- **Mac/Linux**: Run `bash start.sh` in terminal, OR just run `python3 discord_monitor_server.py`

Your browser opens automatically to `http://localhost:7890`

---

## Step 3 — Get Your Discord Token
1. Open **Discord in your browser** (discord.com — not the app)
2. Press **F12** to open DevTools
3. Click the **Application** tab
4. In the left panel: **Local Storage → https://discord.com**
5. Find the key `token` — copy the value (no quotes)
6. Paste it into the **Config tab → Your Token** field

---

## Step 4 — Get Your User ID
1. In Discord: **Settings → Advanced → Developer Mode** → turn ON
2. Right-click your own username anywhere → **Copy User ID**
3. Paste into **Config tab → Your User ID**

---

## Step 5 — Add Channels to Monitor
1. Right-click any channel in Discord → **Copy Channel ID**
2. Go to the **Channels tab** in the UI
3. Paste the ID, give it a label, click **Add Channel**
4. **Click the mode badge to toggle between:**
   - **Keywords Only** (blue) — alerts only when your keywords match
   - **All Posts** (orange) — alerts on EVERY message in that channel
   
**Reseller tip:** Use "All Posts" mode for exclusive deal channels where every message could be a flip opportunity, and "Keywords Only" for general channels where you want filtered alerts.

---

## Step 6 — Set Up Phone Notifications (Ntfy)
1. Download the **Ntfy app** on your phone (free, Android & iOS)
2. Tap the **+** button and subscribe to any topic name you make up
   - Example: `adolfo-deals-7x3k9` (make it unique so nobody else sees it)
3. In the UI **Control tab**, enter that same topic name in the **Ntfy Topic** field
4. Click **Send Test Notification** — you should get a ping on your phone

---

## Step 7 — Start Monitoring
1. Make sure your keywords are set in the **Keywords tab**
2. Click **Save Settings** in the Config tab
3. Click **▶ Start Monitor** in the Control tab
4. The status indicator turns green — you're live

---

## Keeping It Running 24/7
To monitor while your computer is off, run it on a VPS:
- **Hetzner CX11**: ~€4/month (cheapest option)
- Upload the 2 files, run `start.sh`
- Use `screen` or `tmux` to keep it alive after you disconnect

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Login failed" | Double-check your Discord token — get it fresh from browser |
| No alerts firing | Make sure bot is a member of the server with that channel |
| Ntfy not working | Check topic name matches exactly between app and UI |
| Port already in use | Change `PORT = 7890` in `discord_monitor_server.py` |
| discord.py-self install fails | Try `pip3 install discord.py-self --break-system-packages` (Linux) |
