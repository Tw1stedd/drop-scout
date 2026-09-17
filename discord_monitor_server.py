"""
Drop Scout - Backend Server
Product drop monitor for resellers.
Run with: python discord_monitor_server.py
"""

import asyncio
import json
import os
import sys
import time
import threading
import webbrowser
import signal
from datetime import datetime, timedelta
from pathlib import Path
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import urllib.request
import urllib.error
import subprocess
import re as _re

# ── Config & data file paths ─────────────────────────────────────────────────
# When running as a PyInstaller bundle, store user data in %APPDATA%/DropScout
# (Program Files is read-only on Windows). In dev mode, use script directory.
if hasattr(sys, '_MEIPASS'):
    _DATA_DIR = Path(os.environ.get("APPDATA", os.path.expanduser("~"))) / "DropScout"
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
else:
    _DATA_DIR = Path(__file__).parent

CONFIG_FILE = _DATA_DIR / "monitor_config.json"
ALERT_HISTORY_FILE = _DATA_DIR / "alert_history.json"

DEFAULT_CONFIG = {
    "token": "",
    "user_id": "",
    "channels": [],
    "keywords": ["pokemon", "hot wheels", "restock", "charizard", "booster box",
                 "elite trainer", "monster high", "final fantasy", "limited",
                 "clearance", "in stock", "target", "walmart"],
    "ntfy_topic": "",
    "ntfy_server": "https://ntfy.sh",
    "discord_dm": True,
    "ntfy_enabled": True,
    "cooldown": 30,
    "keyword_priorities": {},   # keyword -> "high" | "medium" | "low"
    "keyword_groups": {},       # group_name -> {"color": "#hex", "keywords": []}
    "webhooks": [],             # [{"url": "...", "name": "...", "enabled": true}]
    "sync": {
        "enabled": False,
        "firebase_url": "",
        "device_id": "",
        "device_name": "",
        "auto_sync": True,
        "sync_interval": 300,   # seconds (5 min default)
    },
}

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                data = json.load(f)
                # Merge with defaults to ensure all keys exist
                merged = {**DEFAULT_CONFIG, **data}
                return merged
        except:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

# ── Shared state ──────────────────────────────────────────────────────────────
state = {
    "running": False,
    "logs": [],          # list of log dicts
    "recent_alerts": [],  # last N alerts only (Live Drops; not mixed with heartbeats)
    "alert_count": 0,
    "start_time": None,
    "monitor_thread": None,
    "stop_event": None,
    "connected_clients": [],  # list of SSE client queues (lists are not hashable for set)
    "messages_seen": 0,       # total on_message calls
    "last_message_at": None,  # ISO timestamp of most recent message (any channel)
    "last_alert_at": None,    # ISO timestamp of most recent alert
    # ── Persistence & stats ──
    "alert_history": [],       # persistent alert entries (saved to disk)
    "alert_history_dirty": False,
    "stats": {
        "alerts_per_hour": {},  # "YYYY-MM-DD-HH" -> count
        "keyword_hits": {},     # keyword -> count
        "channel_hits": {},     # channel_name -> count
    },
    # ── Remote access ──
    "tunnel_url": None,
    "tunnel_process": None,
    "tunnel_enabled": False,
    "tailscale_ip": None,
    "tailscale_url": None,
}

# ── Cloudflare Tunnel (remote access) ────────────────────────────────────────

_CLOUDFLARED_DIR = _DATA_DIR / "bin"
_CLOUDFLARED_EXE = _CLOUDFLARED_DIR / ("cloudflared.exe" if sys.platform == "win32" else "cloudflared")
_tunnel_lock = threading.Lock()

def _detect_tailscale():
    """Detect Tailscale IP if installed and running. Returns IP string or None."""
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if result.returncode == 0:
            ip = result.stdout.strip().split("\n")[0].strip()
            if ip and _re.match(r'^100\.', ip):
                return ip
    except FileNotFoundError:
        pass  # tailscale CLI not installed
    except Exception:
        pass
    return None

def _tailscale_check_loop():
    """Background thread: check Tailscale status every 30 seconds."""
    while True:
        ip = _detect_tailscale()
        if ip:
            state["tailscale_ip"] = ip
            state["tailscale_url"] = f"http://{ip}:{PORT}"
        else:
            state["tailscale_ip"] = None
            state["tailscale_url"] = None
        time.sleep(30)

def _download_cloudflared():
    """Download cloudflared binary if not already present. Returns path or None."""
    if _CLOUDFLARED_EXE.exists():
        return _CLOUDFLARED_EXE
    _CLOUDFLARED_DIR.mkdir(parents=True, exist_ok=True)
    add_log("info", "⬇️ Downloading cloudflared for remote access...")
    try:
        if sys.platform == "win32":
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
            req = urllib.request.Request(url, headers={"User-Agent": "DropScout/2.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                with open(_CLOUDFLARED_EXE, "wb") as f:
                    f.write(resp.read())
        else:
            # Linux / macOS
            arch = "amd64"
            if sys.platform == "darwin":
                url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz"
            else:
                url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}"
            req = urllib.request.Request(url, headers={"User-Agent": "DropScout/2.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                with open(_CLOUDFLARED_EXE, "wb") as f:
                    f.write(resp.read())
            os.chmod(str(_CLOUDFLARED_EXE), 0o755)
        add_log("success", "✅ cloudflared downloaded successfully")
        return _CLOUDFLARED_EXE
    except Exception as e:
        add_log("error", f"❌ Failed to download cloudflared: {e}")
        return None

def start_tunnel():
    """Start a Cloudflare quick tunnel exposing localhost:PORT."""
    with _tunnel_lock:
        if state.get("tunnel_process"):
            return state.get("tunnel_url")
        # Mark as starting so concurrent calls won't double-start
        state["tunnel_enabled"] = True

    exe = _download_cloudflared()
    if not exe:
        state["tunnel_enabled"] = False
        return None

    add_log("info", "🌐 Starting remote access tunnel...")
    try:
        popen_kwargs = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(
            [str(exe), "tunnel", "--url", f"http://localhost:{PORT}", "--no-autoupdate"],
            **popen_kwargs,
        )
        with _tunnel_lock:
            state["tunnel_process"] = proc

        # cloudflared prints the URL to stderr; read lines until we find it,
        # then keep reading to detect if the process crashes.
        def _tunnel_watcher():
            url_pattern = _re.compile(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com')
            deadline = time.time() + 30  # 30 second timeout for initial URL
            found = False
            for line in proc.stderr:
                if not found:
                    match = url_pattern.search(line)
                    if match:
                        tunnel_url = match.group(0)
                        with _tunnel_lock:
                            state["tunnel_url"] = tunnel_url
                            state["tunnel_enabled"] = True
                        found = True
                        # Verify tunnel is actually reachable
                        try:
                            test_req = urllib.request.Request(
                                f"{tunnel_url}/api/status",
                                headers={"User-Agent": "DropScout-HealthCheck"}
                            )
                            with urllib.request.urlopen(test_req, timeout=10) as tr:
                                if tr.status == 200:
                                    add_log("success", f"🌐 Remote access ready: {tunnel_url}")
                                else:
                                    add_log("warning", f"🌐 Tunnel URL assigned but health check returned {tr.status}: {tunnel_url}")
                        except Exception as hc_err:
                            add_log("warning", f"🌐 Tunnel URL assigned (health check pending): {tunnel_url}")
                        # Send URL via ntfy if configured
                        cfg = load_config()
                        if cfg.get("ntfy_topic") and cfg.get("ntfy_enabled", True):
                            tunnel_details = {"jump_url": tunnel_url}
                            threading.Thread(
                                target=send_ntfy_sync,
                                args=(cfg["ntfy_topic"], cfg.get("ntfy_server", "https://ntfy.sh"),
                                      "Drop Scout Remote URL", f"Tap to open Drop Scout from anywhere:\n{tunnel_url}",
                                      "default", tunnel_details),
                                daemon=True
                            ).start()
                        continue
                    if time.time() > deadline:
                        add_log("error", "❌ Tunnel timed out waiting for URL")
                        stop_tunnel()
                        return
            # stderr closed = process exited
            if state.get("tunnel_process") is proc:
                if found:
                    add_log("warning", "⚠️ Tunnel process exited unexpectedly")
                else:
                    add_log("error", "❌ Tunnel process exited without providing a URL")
                stop_tunnel()

        threading.Thread(target=_tunnel_watcher, daemon=True).start()
        return "starting"
    except Exception as e:
        add_log("error", f"❌ Failed to start tunnel: {e}")
        with _tunnel_lock:
            state["tunnel_process"] = None
            state["tunnel_enabled"] = False
        return None

def stop_tunnel():
    """Stop the Cloudflare tunnel."""
    with _tunnel_lock:
        proc = state.get("tunnel_process")
        was_running = proc is not None or state.get("tunnel_url") is not None
        state["tunnel_process"] = None
        state["tunnel_url"] = None
        state["tunnel_enabled"] = False
    if proc:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    if was_running:
        add_log("info", "🌐 Remote access tunnel stopped")

# ── Alert history persistence ────────────────────────────────────────────────

def load_alert_history():
    """Load alert history from disk and rebuild stats."""
    if ALERT_HISTORY_FILE.exists():
        try:
            with open(ALERT_HISTORY_FILE) as f:
                data = json.load(f)
            if isinstance(data, list):
                state["alert_history"] = data[-5000:]  # cap at 5000
                # Rebuild stats from history
                for entry in state["alert_history"]:
                    details = entry.get("details", {})
                    ts = details.get("timestamp") or entry.get("time", "")
                    try:
                        dt = datetime.fromisoformat(ts)
                        hour_key = dt.strftime("%Y-%m-%d-%H")
                        state["stats"]["alerts_per_hour"][hour_key] = state["stats"]["alerts_per_hour"].get(hour_key, 0) + 1
                    except Exception:
                        pass
                    for kw in details.get("keywords", []):
                        if kw != "[monitor all]":
                            state["stats"]["keyword_hits"][kw] = state["stats"]["keyword_hits"].get(kw, 0) + 1
                    ch = details.get("channel", "")
                    if ch:
                        state["stats"]["channel_hits"][ch] = state["stats"]["channel_hits"].get(ch, 0) + 1
                hist_alerts = [e for e in reversed(state["alert_history"]) if e.get("level") == "alert"]
                state["recent_alerts"] = hist_alerts[:80]
        except Exception:
            pass

def save_alert_history():
    """Flush alert history to disk."""
    try:
        with open(ALERT_HISTORY_FILE, "w") as f:
            json.dump(state["alert_history"][-5000:], f)
        state["alert_history_dirty"] = False
    except Exception:
        pass

def _history_flush_loop():
    """Background thread: flush alert history to disk every 5 seconds if dirty."""
    while True:
        time.sleep(5)
        if state["alert_history_dirty"]:
            save_alert_history()

def add_log(level: str, message: str, details: dict = None):
    """Add a log entry to shared state."""
    entry = {
        "id": state.get("_log_seq", 0) + 1,
        "time": datetime.now().strftime("%H:%M:%S"),
        "level": level,  # info | success | warning | error | alert
        "message": message,
        "details": details or {}
    }
    state["_log_seq"] = entry["id"]
    state["logs"].insert(0, entry)
    # Keep last 200 logs
    if len(state["logs"]) > 200:
        state["logs"] = state["logs"][:200]

    # Persist alerts to history + update stats
    if level == "alert" and details:
        state["recent_alerts"].insert(0, entry)
        if len(state["recent_alerts"]) > 80:
            state["recent_alerts"] = state["recent_alerts"][:80]
        state["alert_history"].append(entry)
        if len(state["alert_history"]) > 5000:
            state["alert_history"] = state["alert_history"][-5000:]
        state["alert_history_dirty"] = True
        # Update stats
        hour_key = datetime.now().strftime("%Y-%m-%d-%H")
        state["stats"]["alerts_per_hour"][hour_key] = state["stats"]["alerts_per_hour"].get(hour_key, 0) + 1
        for kw in details.get("keywords", []):
            if kw != "[monitor all]":
                state["stats"]["keyword_hits"][kw] = state["stats"]["keyword_hits"].get(kw, 0) + 1
        ch = details.get("channel", "")
        if ch:
            state["stats"]["channel_hits"][ch] = state["stats"]["channel_hits"].get(ch, 0) + 1

    # Push to all SSE clients
    for q in list(state["connected_clients"]):
        try:
            q.append(entry)
        except:
            pass

    return entry

# ── Notification senders ──────────────────────────────────────────────────────

def send_ntfy_sync(topic: str, server: str, title: str, body: str,
                   priority: str = "high", details: dict = None):
    """Send Ntfy notification with rich formatting, image, and action buttons."""
    try:
        url = f"{server.rstrip('/')}/{topic}"
        details = details or {}

        # Build a cleaner, more structured body
        _d = details
        lines = []

        # Product title (from parsed content)
        product_title = ""
        content = _d.get("content", "")
        if content:
            # Extract the first meaningful line as product name
            for line in content.split("\n"):
                clean = line.strip().lstrip("#").strip()
                if clean and len(clean) > 10 and not clean.startswith(("<", "http", "```", "SKU", "Price", "Offer", "Seller", "Order", "Add to")):
                    product_title = clean[:120]
                    break

        # Price
        price_m = _re.search(r'Price\s*\n\$?([\d,.]+)', content)
        price_str = f"${price_m.group(1)}" if price_m else ""

        # SKU / ASIN
        asin = _d.get("asin", "")
        offer_id = _d.get("offer_id", "")

        # Channel + server info
        channel = _d.get("channel", "")
        guild = _d.get("server", "")
        keywords = _d.get("keywords", [])

        # Build formatted body
        if product_title:
            lines.append(product_title)
            lines.append("")

        if price_str:
            lines.append(f"💲 Price: {price_str}")
        if asin:
            lines.append(f"📦 ASIN: {asin}")
        if channel:
            lines.append(f"📺 #{channel}" + (f" · {guild}" if guild else ""))
        if keywords and keywords != ["[monitor all]"]:
            lines.append(f"🔑 {', '.join(keywords[:4])}")

        # Truncated raw content as fallback if no structured data
        if not product_title and not price_str:
            # Fall back to the original body (plain text)
            lines.append(body[:600])

        ntfy_body = "\n".join(lines).strip()
        data = ntfy_body.encode("utf-8")

        # Sanitize title for latin-1 headers
        safe_title = title.encode("latin-1", "ignore").decode("latin-1").strip() or "Drop Scout Alert"

        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Title", safe_title)
        req.add_header("Priority", priority)
        req.add_header("Content-Type", "text/plain; charset=utf-8")
        req.add_header("Markdown", "yes")

        # Tags (emoji shown in notification)
        tag_map = {"high": "rotating_light,moneybag", "urgent": "rotating_light,fire",
                   "medium": "bell,moneybag", "default": "bell"}
        req.add_header("Tags", tag_map.get(priority, "bell,moneybag"))

        # Attach product image if available
        image_urls = _d.get("image_urls", [])
        if image_urls:
            # Use first image as notification icon/attachment
            img_url = image_urls[0]
            safe_img = img_url.encode("latin-1", "ignore").decode("latin-1")
            if safe_img:
                req.add_header("Attach", safe_img)

        # Click URL — tap notification to open Discord jump link or ATC
        jump_url = _d.get("jump_url", "")
        atc_url = ""
        # Extract ATC URL from content
        atc_m = _re.search(r'https://www\.amazon\.com/checkout/entry/buynow[^\s\)]+', content)
        if atc_m:
            atc_url = atc_m.group(0)

        if jump_url:
            safe_jump = jump_url.encode("latin-1", "ignore").decode("latin-1")
            if safe_jump:
                req.add_header("Click", safe_jump)

        # Action buttons
        actions = []
        if jump_url:
            actions.append(f"view, Discord, {jump_url}")
        if atc_url:
            actions.append(f"view, 🛒 Buy Now, {atc_url}")
        if asin:
            actions.append(f"view, Amazon, https://www.amazon.com/dp/{asin}")

        if actions:
            safe_actions = "; ".join(actions).encode("latin-1", "ignore").decode("latin-1")
            if safe_actions:
                req.add_header("Actions", safe_actions)

        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                add_log("info", f"📱 Ntfy notification sent: {title}")
            else:
                add_log("warning", f"Ntfy returned status {resp.status}")
    except Exception as e:
        add_log("warning", f"Ntfy failed: {e}")

def send_webhook_sync(url: str, title: str, body: str, details: dict):
    """Send alert to a generic webhook URL (JSON POST)."""
    try:
        payload = json.dumps({
            "text": f"**{title}**\n{body}",
            "title": title,
            "body": body,
            "channel": details.get("channel", ""),
            "server": details.get("server", ""),
            "keywords": details.get("keywords", []),
            "jump_url": details.get("jump_url", ""),
            "timestamp": details.get("timestamp", ""),
            "priority": details.get("priority", "medium"),
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status < 300:
                add_log("info", f"🔗 Webhook sent: {url[:40]}...")
            else:
                add_log("warning", f"Webhook {url[:30]} returned {resp.status}")
    except Exception as e:
        add_log("warning", f"Webhook failed: {e}")


# ── eBay sold-price lookup ────────────────────────────────────────────────────

import re
import urllib.parse
import http.cookiejar

# Persistent cookie jar + opener for eBay requests (reduces blocking)
_ebay_cj = http.cookiejar.CookieJar()
_ebay_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_ebay_cj))
_ebay_browser_available = None  # None = untested, True/False after first attempt

_EBAY_JUNK_TITLE = {
    "new", "drop", "live", "restock", "in", "stock", "alert", "deal", "now",
    "today", "limited", "wow", "everyone", "here", "update", "posted", "match",
    "channel", "discord", "monitor", "keyword", "keywords", "post", "message",
}

_EBAY_CHROME_LINE = _re.compile(
    r"^(?:"
    r"(?:restock|drop|deal|live|stock)\s+alerts?"
    r"|match\s+in\s+#?\S+"
    r"|new\s+post\s+in\s+#?\S+"
    r"|new\s+post"
    r")$",
    _re.I,
)

_EBAY_CHROME_PREFIX = _re.compile(
    r"^(?:[\U0001F300-\U0001FAFF🚨🔔📬⚠✅❌◦]\s*)+"
    r"|(?:restock|drop|deal|live|stock)\s+alerts?\s*[:\-–]?\s*"
    r"|match\s+in\s+#?\S+\s*[:\-–]?\s*"
    r"|new\s+post\s+in\s+#?\S+\s*[:\-–]?\s*",
    _re.I,
)


def _is_ebay_chrome_title(text: str) -> bool:
    t = (text or "").strip()
    t = _re.sub(r"[\U0001F300-\U0001FAFF🚨🔔📬]", " ", t)
    t = _re.sub(r"[#*_`|>]+", " ", t)
    t = _re.sub(r"\s+", " ", t).strip().lower()
    if not t:
        return True
    if _EBAY_CHROME_LINE.match(t):
        return True
    words = [w for w in t.split() if w not in _EBAY_JUNK_TITLE and len(w) > 1]
    return len(words) == 0


def _product_title_from_content(content: str) -> str:
    """First real product line — never Discord/monitor chrome."""
    skip_starts = (
        "http", "<@", "```", "sku", "price", "offer", "seller", "order",
        "add to", "links", "cart", "type", "asin", "quantity", "condition",
    )
    for line in (content or "").split("\n"):
        clean = line.strip().lstrip("#").strip()
        clean = _re.sub(r"<@!?&?\d+>", " ", clean)
        clean = _re.sub(r"https?://\S+", " ", clean)
        clean = _re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", clean)
        for _ in range(3):
            nxt = _EBAY_CHROME_PREFIX.sub("", clean).strip(" -–:|")
            if nxt == clean:
                break
            clean = nxt
        clean = _re.sub(r"\s+", " ", clean).strip(" -–:|")
        if not clean:
            continue
        low = clean.lower()
        if any(low.startswith(s) for s in skip_starts):
            continue
        if _is_ebay_chrome_title(clean):
            continue
        if len(clean) >= 4:
            return clean[:120]
    return ""


def _ebay_search_url(query: str) -> str:
    q = urllib.parse.quote_plus((query or "").strip()[:120])
    return (f"https://www.ebay.com/sch/i.html?_nkw={q}"
            "&LH_Complete=1&LH_Sold=1&_ipg=60&_sop=12")


def _clean_ebay_query(title: str = "", asin: str = "", sku: str = "", content: str = "") -> str:
    """Build an eBay sold-search string from product title + identifiers."""
    import re as _re_clean
    raw = (title or "").strip()
    raw = _re_clean.sub(r"https?://\S+", " ", raw)
    raw = _re_clean.sub(r"<@!?&?\d+>", " ", raw)
    raw = _re_clean.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", raw)
    raw = _EBAY_CHROME_PREFIX.sub("", raw)
    raw = _re_clean.sub(r"[#*_`|>]+", " ", raw)
    raw = _re_clean.sub(r"\s+", " ", raw).strip(" -–:")
    if _is_ebay_chrome_title(raw) and content:
        raw = _product_title_from_content(content) or raw
    tokens = [t for t in raw.split(" ") if len(t) > 1 and t.lower() not in _EBAY_JUNK_TITLE]
    asin = (asin or "").strip().upper()
    sku = (sku or "").strip()
    sku_is_asin = bool(_re_clean.match(r"^B0[A-Z0-9]{8}$", sku, _re_clean.I))
    ident = asin if len(asin) == 10 else (sku if sku_is_asin else "")
    extra_sku = sku if sku and not sku_is_asin and len(sku) >= 6 else ""
    if _is_ebay_chrome_title(" ".join(tokens)):
        tokens = []
    if len(tokens) >= 2:
        q = " ".join(tokens)[:120]
        return q
    if len(tokens) == 1 and len(tokens[0]) >= 4:
        base = tokens[0]
        if ident:
            return f"{base} {ident}"[:120]
        if extra_sku:
            return f"{base} {extra_sku}"[:120]
        return base[:120]
    if ident:
        return ident
    if extra_sku:
        return extra_sku
    if content:
        fallback = _product_title_from_content(content)
        if fallback and not _is_ebay_chrome_title(fallback):
            return fallback[:120]
    return ""


def _ebay_empty_result(query: str, error: str = "", blocked: bool = False) -> dict:
    q = (query or "").strip()
    url = _ebay_search_url(q) if q else "https://www.ebay.com/sch/i.html?LH_Complete=1&LH_Sold=1"
    out = {
        "query": q, "prices": [], "avg": 0, "median": 0, "low": 0, "high": 0,
        "count": 0, "fetched_at": datetime.now().isoformat(), "source": "ebay_sold",
        "search_url": url, "manual_url": url, "blocked": blocked,
        "note": "Open the eBay Sold link to check prices in your browser.",
    }
    if error:
        out["error"] = error
    return out


def _find_ebay_browser() -> str:
    """Installed Chrome/Edge/Chromium, then Playwright's cached Chromium."""
    env = (os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
           or os.environ.get("CHROME_PATH") or "").strip()
    if env and Path(env).exists():
        return env
    named = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path("/usr/bin/google-chrome-stable"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/local/bin/google-chrome"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ]
    for p in named:
        if p and str(p) != "." and p.exists():
            return str(p)
    chrome_paths = [
        Path.home() / "AppData/Local/ms-playwright/chromium-1208/chrome-win64/chrome.exe",
        Path.home() / "AppData/Local/ms-playwright/chromium-1200/chrome-win64/chrome.exe",
        Path.home() / ".cache/ms-playwright/chromium-1208/chrome-linux/chrome",
        Path.home() / ".cache/ms-playwright/chromium-1200/chrome-linux/chrome",
    ]
    for p in chrome_paths:
        if p.exists():
            return str(p)
    for base in [Path.home() / "AppData/Local/ms-playwright", Path.home() / ".cache/ms-playwright"]:
        if not base.exists():
            continue
        for d in sorted(base.iterdir(), reverse=True):
            if not d.name.startswith("chromium-"):
                continue
            for sub in ["chrome-win64/chrome.exe", "chrome-win/chrome.exe",
                        "chrome-linux/chrome", "chrome-mac/Chromium.app/Contents/MacOS/Chromium"]:
                candidate = d / sub
                if candidate.exists():
                    return str(candidate)
    return ""


def _ebay_sold_prices_basic(query: str, html: str = None) -> dict:
    """Extract prices from eBay sold listings HTML.  Returns result dict."""
    query = (query or "").strip()
    result = {"query": query, "prices": [], "avg": 0, "median": 0,
              "low": 0, "high": 0, "count": 0, "fetched_at": datetime.now().isoformat(),
              "source": "ebay_sold", "search_url": _ebay_search_url(query),
              "manual_url": _ebay_search_url(query)}

    if not query:
        result["error"] = "Empty eBay search query — no title, ASIN, or SKU"
        return result

    if html is None:
        url = _ebay_search_url(query)
        result["search_url"] = url
        result["manual_url"] = url
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
            "Referer": "https://www.ebay.com/",
            "DNT": "1",
        }
        req = urllib.request.Request(url)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with _ebay_opener.open(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            code = getattr(e, "code", 0) or 0
            result["error"] = f"eBay blocked automated scrape (HTTP {code} {e.reason})"
            result["blocked"] = True
            result["count"] = 0
            result["note"] = "Open the eBay Sold link to check prices in your browser."
            return result
        except Exception as e:
            result["error"] = f"eBay scrape failed: {e}"
            result["blocked"] = True
            result["count"] = 0
            result["note"] = "Open the eBay Sold link to check prices in your browser."
            return result

    # If we got a CAPTCHA page, mark as blocked — caller may try Playwright
    head = (html or "")[:2000]
    if ("Pardon Our Interruption" in head
            or "captcha" in head.lower()
            or "robot check" in head.lower()
            or "blocked" in html[:800].lower() and "ebay" in html[:400].lower()):
        result["blocked"] = True
        result["error"] = "eBay CAPTCHA / bot check — sold prices were not scraped"
        result["count"] = 0
        result["note"] = "Open the eBay Sold link to check prices in your browser."
        result["search_url"] = _ebay_search_url(query)
        result["manual_url"] = result["search_url"]
        return result

    prices = []
    # Pattern 1 (2024+ layout): s-card__price class (non-strikethrough = actual sold price)
    # Match:  class="su-styled-text positive bold large-1 s-card__price">$XX.XX<
    # Avoid strikethrough (original prices)
    for m in re.finditer(r'(?:positive|primary)[^"]*s-card__price[^>]*>\s*\$\s*([\d,]+\.?\d*)', html):
        try:
            p = float(m.group(1).replace(",", ""))
            if 0.50 < p < 50000:
                prices.append(p)
        except ValueError:
            pass
    # Pattern 2 (legacy layout): s-item__price class
    if len(prices) < 3:
        for m in re.finditer(r's-item__price[^>]*>\s*\$\s*([\d,]+\.?\d*)', html):
            try:
                p = float(m.group(1).replace(",", ""))
                if 0.50 < p < 50000:
                    prices.append(p)
            except ValueError:
                pass
    # Pattern 3: structured data "price":{"value":"XX.XX"}
    if len(prices) < 3:
        for m in re.finditer(r'"price"\s*:\s*\{\s*"value"\s*:\s*"([\d.]+)"', html):
            try:
                p = float(m.group(1))
                if 0.50 < p < 50000:
                    prices.append(p)
            except ValueError:
                pass
    # Pattern 4: generic dollar amounts (last resort, less accurate)
    if len(prices) < 3:
        for m in re.finditer(r'\$([\d,]+\.\d{2})', html):
            try:
                p = float(m.group(1).replace(",", ""))
                if 0.50 < p < 50000:
                    prices.append(p)
            except ValueError:
                pass

    # Deduplicate
    seen = set()
    unique = []
    for p in prices:
        r = round(p, 2)
        if r not in seen:
            seen.add(r)
            unique.append(r)
    prices = unique[:60]

    if prices:
        prices.sort()
        result["prices"] = prices[:30]
        result["avg"]    = round(sum(prices) / len(prices), 2)
        result["median"] = round(prices[len(prices) // 2], 2)
        result["low"]    = round(prices[0], 2)
        result["high"]   = round(prices[-1], 2)
        result["count"]  = len(prices)
    return result


def _ebay_sold_browser(query: str) -> dict:
    """Use Playwright + installed Chrome/Chromium to fetch eBay sold listings."""
    global _ebay_browser_available
    url = _ebay_search_url(query)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _ebay_browser_available = False
        return _ebay_empty_result(
            query,
            "Playwright is not installed — cannot scrape eBay in a browser",
            blocked=True,
        )

    chrome_exe = _find_ebay_browser()
    try:
        with sync_playwright() as p:
            browser = None
            last_err = None
            launch_tries = []
            if chrome_exe:
                launch_tries.append({"headless": True, "executable_path": chrome_exe})
            launch_tries.append({"headless": True, "channel": "chrome"})
            launch_tries.append({"headless": True})
            for kwargs in launch_tries:
                try:
                    browser = p.chromium.launch(**kwargs)
                    break
                except Exception as e:
                    last_err = e
                    browser = None
            if browser is None:
                _ebay_browser_available = False
                return _ebay_empty_result(
                    query,
                    f"No Chromium/Chrome for Playwright ({last_err})",
                    blocked=True,
                )
            try:
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(2500)
                html = page.content()
            finally:
                browser.close()

        _ebay_browser_available = True
        parsed = _ebay_sold_prices_basic(query, html)
        parsed["search_url"] = url
        parsed["manual_url"] = url
        return parsed
    except Exception as e:
        return _ebay_empty_result(query, f"browser scrape timed out or failed: {e}", blocked=True)


def _ebay_sold_prices(query: str) -> dict:
    """HTTP scrape first; Playwright / Chrome if blocked or empty; always keep sold URL."""
    query = (query or "").strip()
    if not query:
        return _ebay_empty_result("", "Empty eBay search query — no title, ASIN, or SKU")

    result = _ebay_sold_prices_basic(query)
    result["query"] = query
    result["search_url"] = result.get("search_url") or _ebay_search_url(query)
    result["manual_url"] = result["search_url"]

    need_browser = result.get("blocked") or result.get("count", 0) == 0
    if need_browser:
        add_log("info", f"🔄 eBay HTTP scrape missed prices ({result.get('error') or 'empty'}), trying browser for: {query[:50]}...")
        browser_result = _ebay_sold_browser(query)
        if browser_result.get("count", 0) > 0:
            add_log("info", f"✅ Browser eBay lookup found {browser_result['count']} prices")
            browser_result["query"] = query
            browser_result["search_url"] = _ebay_search_url(query)
            browser_result["manual_url"] = browser_result["search_url"]
            return browser_result
        err = browser_result.get("error") or result.get("error") or "eBay blocked the automated scrape"
        http_err = result.get("error")
        if http_err and browser_result.get("error") and http_err not in str(browser_result.get("error")):
            err = f"{http_err}. {browser_result.get('error')}"
        result = _ebay_empty_result(query, err, blocked=True)
        add_log("warning", f"eBay sold lookup failed for '{query[:60]}': {err}")

    if result.get("count", 0) == 0:
        if not result.get("error"):
            result["error"] = "No sold prices parsed for this search"
        result["note"] = "Open the eBay Sold link to check prices in your browser."
        result["fetched_at"] = result.get("fetched_at") or datetime.now().isoformat()
        result["search_url"] = _ebay_search_url(query)
        result["manual_url"] = result["search_url"]
        result["query"] = query

    return result


def _lookup_offer_id_from_history(asin: str) -> dict:
    """Look up Offer ID for an ASIN from alert history (extracted from Discord messages)."""
    result = {"asin": asin, "offer_id": None, "error": None}
    for entry in reversed(state.get("alert_history", [])):
        details = entry.get("details", {})
        if details.get("asin") == asin and details.get("offer_id"):
            result["offer_id"] = details["offer_id"]
            return result
    result["error"] = f"No Offer ID found in alert history for {asin}"
    return result


# ── URL Resolver (ASIN / TCIN extraction) ────────────────────────────────────

# In-memory cache for resolved URLs (persists for session)
_resolved_url_cache = {}

def _resolve_url(url: str) -> dict:
    """Resolve a redirect URL (dmflip.com, amzn.to, etc.) to the real retailer URL
    and extract product identifiers (ASIN, TCIN, etc.)."""
    import re

    if url in _resolved_url_cache:
        return _resolved_url_cache[url]

    result = {"original": url, "resolved": None, "asin": None, "tcin": None,
              "retailer": None, "product_url": None, "offer_id": None, "error": None}

    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""

        # ── dmflip.com — affiliate redirect page ──
        # The page HTML contains the actual retailer URL
        if "dmflip.com" in host:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="replace")

            # Look for Amazon URL in the page
            amz = re.search(r'https?://(?:www\.)?amazon\.com/[^\s"\'<>]+', html)
            if amz:
                found_url = amz.group(0).rstrip("\\")
                result["resolved"] = found_url
                result["retailer"] = "amazon"
                result["product_url"] = found_url
                # Extract ASIN from /dp/ASIN or /gp/product/ASIN
                asin_m = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', found_url)
                if asin_m:
                    result["asin"] = asin_m.group(1)

            # Look for Target URL
            if not result["resolved"]:
                tgt = re.search(r'https?://(?:www\.)?target\.com/[^\s"\'<>]+', html)
                if tgt:
                    found_url = tgt.group(0).rstrip("\\")
                    result["resolved"] = found_url
                    result["retailer"] = "target"
                    result["product_url"] = found_url
                    tcin_m = re.search(r'A-(\d{7,9})', found_url)
                    if tcin_m:
                        result["tcin"] = tcin_m.group(1)

            # Look for Walmart URL
            if not result["resolved"]:
                wmt = re.search(r'https?://(?:www\.)?walmart\.com/[^\s"\'<>]+', html)
                if wmt:
                    found_url = wmt.group(0).rstrip("\\")
                    result["resolved"] = found_url
                    result["retailer"] = "walmart"
                    result["product_url"] = found_url

        # ── amzn.to — Amazon short URL (follow redirect) ──
        elif "amzn.to" in host:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            req.method = "HEAD"
            try:
                # Follow redirects manually to get final URL
                opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
                resp = opener.open(req, timeout=10)
                final = resp.url
            except urllib.error.HTTPError as he:
                # Some redirects return 3xx that urllib treats as error
                final = he.headers.get("Location", url)
            except Exception:
                # Fallback: GET request
                req2 = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })
                with urllib.request.urlopen(req2, timeout=10) as resp:
                    final = resp.url

            if final and final != url:
                result["resolved"] = final
                result["retailer"] = "amazon"
                result["product_url"] = final
                asin_m = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', final)
                if asin_m:
                    result["asin"] = asin_m.group(1)

        # ── Direct Amazon URL ──
        elif "amazon.com" in host:
            result["resolved"] = url
            result["retailer"] = "amazon"
            result["product_url"] = url
            asin_m = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url)
            if not asin_m:
                asin_m = re.search(r'[?&]asin=([A-Z0-9]{10})', url)
            if asin_m:
                result["asin"] = asin_m.group(1)
            # Extract Offer ID from checkout/buynow URLs
            from urllib.parse import unquote as _unquote
            offer_m = re.search(r'(?:offeringID|OfferListingId(?:\.\d)?)[=:]([^\s&,\)]+)', url)
            if offer_m:
                result["offer_id"] = _unquote(offer_m.group(1)).strip()

        # ── Direct Target URL ──
        elif "target.com" in host:
            result["resolved"] = url
            result["retailer"] = "target"
            result["product_url"] = url
            tcin_m = re.search(r'A-(\d{7,9})', url)
            if tcin_m:
                result["tcin"] = tcin_m.group(1)

        # ── Direct Walmart URL ──
        elif "walmart.com" in host:
            result["resolved"] = url
            result["retailer"] = "walmart"
            result["product_url"] = url

    except Exception as e:
        result["error"] = str(e)

    _resolved_url_cache[url] = result
    return result


def _extract_ids_from_text(text: str) -> dict:
    """Extract ASINs, TCINs, and Amazon Offer IDs from raw text content."""
    import re
    from urllib.parse import unquote
    ids = {"asins": [], "tcins": [], "skus": [], "offer_ids": []}

    if not text:
        return ids

    # Amazon ASINs (B0 + 8 alphanumeric chars, or standard 10-char ASIN)
    for m in re.finditer(r'\b(B0[A-Z0-9]{8})\b', text):
        asin = m.group(1)
        if asin not in ids["asins"]:
            ids["asins"].append(asin)

    # Amazon Offer IDs from checkout/buynow URLs (offeringID= or OfferListingId=)
    for m in re.finditer(r'(?:offeringID|OfferListingId(?:\.\d)?)[=:]([^\s&,\)]+)', text):
        raw = m.group(1)
        decoded = unquote(raw).strip()
        if decoded and decoded not in ids["offer_ids"]:
            ids["offer_ids"].append(decoded)

    # Also extract from amazon.com/gp/offer-listing/ASIN patterns (marks offer page exists)
    # The actual offer IDs are in the checkout URLs above

    # Target TCINs from URL patterns A-XXXXXXXX
    for m in re.finditer(r'A-(\d{7,9})', text):
        tcin = m.group(1)
        if tcin not in ids["tcins"]:
            ids["tcins"].append(tcin)

    # Numeric SKUs (8+ digit numbers that look like product IDs, not timestamps)
    for m in re.finditer(r'\b(\d{8,12})\b', text):
        sku = m.group(1)
        # Filter out timestamps and common non-SKU numbers
        if len(sku) <= 12 and sku not in ids["skus"] and not sku.startswith("20"):
            ids["skus"].append(sku)

    return ids


# ── Cloud Sync (Firebase Realtime Database) ──────────────────────────────────

import uuid

def _get_device_id():
    """Get or create a persistent device ID."""
    cfg = load_config()
    sync_cfg = cfg.get("sync", {})
    did = sync_cfg.get("device_id", "")
    if not did:
        did = str(uuid.uuid4())[:12]
        sync_cfg["device_id"] = did
        cfg["sync"] = sync_cfg
        save_config(cfg)
    return did

def _firebase_request(firebase_url: str, path: str, method: str = "GET",
                      data: dict = None, timeout: int = 15) -> dict:
    """Make a REST request to Firebase Realtime Database.
    firebase_url: e.g. https://myproject-default-rtdb.firebaseio.com
    path: e.g. /alert_history   (no .json suffix needed, we add it)
    Returns parsed JSON or {"error": "..."}.
    """
    base = firebase_url.rstrip("/")
    url = f"{base}/{path.lstrip('/')}.json"
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "DropScout/1.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}

def _sync_push(firebase_url: str, device_id: str, device_name: str) -> dict:
    """Push local data to Firebase.  Returns summary."""
    result = {"ok": False, "pushed_alerts": 0, "pushed_ebay": 0}
    try:
        # 1. Push alert history (keyed by ID for merge semantics)
        history = state.get("alert_history", [])
        if history:
            # Convert list to dict keyed by alert ID for PATCH merge
            history_dict = {}
            for entry in history[-2000:]:  # Push last 2000 to stay within free tier
                key = str(entry.get("id", ""))
                if key:
                    history_dict[key] = entry
            if history_dict:
                resp = _firebase_request(firebase_url, "/alert_history", "PATCH", history_dict)
                if "error" not in resp:
                    result["pushed_alerts"] = len(history_dict)

        # 2. Push config (EXCLUDE sensitive fields)
        cfg = load_config()
        safe_cfg = {k: v for k, v in cfg.items()
                    if k not in ("token", "user_id", "sync")}
        _firebase_request(firebase_url, f"/config/{device_id}", "PUT", safe_cfg)

        # 3. Update device presence
        device_info = {
            "name": device_name or device_id,
            "last_push": datetime.now().isoformat(),
            "alert_count": len(history),
            "running": state.get("running", False),
        }
        _firebase_request(firebase_url, f"/devices/{device_id}", "PATCH", device_info)

        # 4. Update meta
        _firebase_request(firebase_url, "/meta", "PATCH", {
            "last_sync_at": datetime.now().isoformat(),
            "last_sync_by": device_id,
        })

        result["ok"] = True
        state["sync_last_push"] = datetime.now().isoformat()
    except Exception as e:
        result["error"] = str(e)
    return result

def _sync_pull(firebase_url: str, device_id: str) -> dict:
    """Pull remote data from Firebase and merge into local state."""
    result = {"ok": False, "new_alerts": 0, "total_cloud": 0}
    try:
        # 1. Pull alert history
        remote = _firebase_request(firebase_url, "/alert_history", "GET")
        if isinstance(remote, dict) and "error" not in remote:
            # remote is {id_str: entry_dict, ...}
            local_ids = {str(e.get("id", "")) for e in state.get("alert_history", [])}
            new_entries = []
            for key, entry in remote.items():
                if isinstance(entry, dict) and key not in local_ids:
                    new_entries.append(entry)
            result["total_cloud"] = len(remote)

            if new_entries:
                state["alert_history"].extend(new_entries)
                # Sort by ID (timestamp) and cap
                state["alert_history"].sort(key=lambda e: e.get("id", 0))
                state["alert_history"] = state["alert_history"][-5000:]
                state["alert_history_dirty"] = True
                result["new_alerts"] = len(new_entries)

                # Rebuild stats for new entries
                for entry in new_entries:
                    details = entry.get("details", {})
                    ts = details.get("timestamp") or entry.get("time", "")
                    try:
                        dt = datetime.fromisoformat(ts)
                        hour_key = dt.strftime("%Y-%m-%d-%H")
                        state["stats"]["alerts_per_hour"][hour_key] = \
                            state["stats"]["alerts_per_hour"].get(hour_key, 0) + 1
                    except Exception:
                        pass
                    for kw in details.get("keywords", []):
                        if kw != "[monitor all]":
                            state["stats"]["keyword_hits"][kw] = \
                                state["stats"]["keyword_hits"].get(kw, 0) + 1
                    ch = details.get("channel", "")
                    if ch:
                        state["stats"]["channel_hits"][ch] = \
                            state["stats"]["channel_hits"].get(ch, 0) + 1

        # 2. Pull eBay cache
        ebay_remote = _firebase_request(firebase_url, "/ebay_cache", "GET")
        if isinstance(ebay_remote, dict) and "error" not in ebay_remote:
            result["ebay_cache"] = ebay_remote

        # 3. Update device last_pull
        _firebase_request(firebase_url, f"/devices/{device_id}", "PATCH", {
            "last_pull": datetime.now().isoformat(),
        })

        result["ok"] = True
        state["sync_last_pull"] = datetime.now().isoformat()
    except Exception as e:
        result["error"] = str(e)
    return result

def _sync_backup_full(firebase_url: str, device_id: str) -> dict:
    """Full backup: push everything including ebay cache from frontend."""
    return _sync_push(firebase_url, device_id,
                      load_config().get("sync", {}).get("device_name", ""))

def _sync_auto_loop():
    """Background thread: auto-sync every N seconds if enabled."""
    while True:
        time.sleep(30)  # Check every 30s
        cfg = load_config()
        sync_cfg = cfg.get("sync", {})
        if not sync_cfg.get("enabled") or not sync_cfg.get("auto_sync", True):
            continue
        fb_url = sync_cfg.get("firebase_url", "")
        if not fb_url:
            continue
        interval = max(int(sync_cfg.get("sync_interval", 300)), 60)
        last_push = state.get("sync_last_push")
        if last_push:
            try:
                elapsed = (datetime.now() - datetime.fromisoformat(last_push)).total_seconds()
                if elapsed < interval:
                    continue
            except Exception:
                pass

        device_id = sync_cfg.get("device_id") or _get_device_id()
        device_name = sync_cfg.get("device_name", "")

        # Push if we have dirty data
        if state.get("alert_history_dirty") or not last_push:
            try:
                r = _sync_push(fb_url, device_id, device_name)
                if r.get("ok") and r.get("pushed_alerts", 0) > 0:
                    add_log("info", f"☁️ Auto-sync pushed {r['pushed_alerts']} alerts")
            except Exception:
                pass

        # Pull new data from other devices
        try:
            r = _sync_pull(fb_url, device_id)
            if r.get("ok") and r.get("new_alerts", 0) > 0:
                add_log("info", f"☁️ Auto-sync pulled {r['new_alerts']} new alerts")
        except Exception:
            pass


# ── Discord self-bot monitor ──────────────────────────────────────────────────

def run_monitor(config: dict, stop_event: threading.Event):
    """Run the Discord self-bot in a thread."""
    try:
        import discord
    except ImportError:
        add_log("error", "discord.py-self not installed. Run: pip install discord.py-self")
        state["running"] = False
        return

    token = config.get("token", "")
    user_id_str = config.get("user_id", "")
    raw_channels = config.get("channels", [])
    channel_ids = []
    monitor_all_channels = set()  # Channels set to "all messages" mode
    skipped_config = []

    for c in raw_channels:
        try:
            # Handle both plain IDs (str/int) and {id, name, mode} objects
            if isinstance(c, dict):
                cid = c["id"]
                mode = c.get("mode", "keywords")  # "keywords" or "all"
                if mode == "all":
                    monitor_all_channels.add(int(str(cid).strip()))
            else:
                cid = c
            channel_ids.append(int(str(cid).strip()))
        except (KeyError, ValueError, TypeError) as e:
            skipped_config.append(repr(c))
            add_log("warning", f"⚠ Skipped invalid watched channel {c!r}: {e}")
    channel_id_set = set(channel_ids)  # for fast lookup and thread parent matching
    if skipped_config:
        add_log("warning", f"⚠ {len(skipped_config)} watched channel(s) skipped (bad id/mode): {', '.join(skipped_config)[:300]}")
    keywords = [k.lower().strip() for k in config.get("keywords", []) if k.strip()]
    ntfy_topic = config.get("ntfy_topic", "")
    ntfy_server = config.get("ntfy_server", "https://ntfy.sh")
    ntfy_enabled = config.get("ntfy_enabled", True)
    discord_dm = config.get("discord_dm", True)
    cooldown_secs = int(config.get("cooldown", 30))
    keyword_priorities = config.get("keyword_priorities", {})
    webhooks = config.get("webhooks", [])

    if not token:
        add_log("error", "No Discord token set. Please configure it in Settings.")
        state["running"] = False
        return

    try:
        user_id = int(user_id_str) if user_id_str else None
    except:
        user_id = None

    cooldowns = {}

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    client = discord.Client()

    def _message_text(message):
        """Build searchable/displayable text from content + embeds + attachments."""
        parts = []
        content = getattr(message, "content", None)
        if content:
            parts.append(str(content))
        for emb in getattr(message, "embeds", []) or []:
            try:
                if getattr(emb, "title", None):
                    parts.append(str(emb.title))
                if getattr(emb, "description", None):
                    parts.append(str(emb.description))
                for f in getattr(emb, "fields", []) or []:
                    n = getattr(f, "name", None)
                    v = getattr(f, "value", None)
                    if n:
                        parts.append(str(n))
                    if v:
                        parts.append(str(v))
            except Exception:
                pass
        for a in getattr(message, "attachments", []) or []:
            try:
                if getattr(a, "filename", None):
                    parts.append(str(a.filename))
                if getattr(a, "url", None):
                    parts.append(str(a.url))
            except Exception:
                pass
        for st in getattr(message, "stickers", []) or []:
            try:
                nm = getattr(st, "name", None)
                if nm:
                    parts.append(str(nm))
            except Exception:
                pass
        return "\n".join(p for p in parts if p).strip()

    # Name lookup from config for log messages
    ch_name_map = {}
    for c in raw_channels:
        if isinstance(c, dict):
            try:
                ch_name_map[int(str(c.get("id", "0")).strip())] = c.get("name", "")
            except (ValueError, TypeError):
                pass

    async def _resolve_watched_channel(cid):
        label = ch_name_map.get(cid, str(cid))
        ch = client.get_channel(cid)
        if ch is None:
            last_err = None
            for _try in range(3):
                try:
                    ch = await client.fetch_channel(cid)
                    if ch is not None:
                        break
                except Exception as e:
                    last_err = e
                    ch = None
                    await asyncio.sleep(0.4 * (_try + 1))
            if ch is None:
                add_log("warning", f"  ✗ #{label} ({cid}) — fetch failed: {last_err or 'not found'}")
                return None, label, last_err or "not found"
        return ch, label, None

    async def _subscribe_watched_channels(reason="startup"):
        """Lazy-guild: subscribe each guild, then OP-14 each watched channel. Never silent-skip."""
        skipped = []
        activated = 0
        subscribed_guilds = set()
        by_guild = {}
        dms = []

        for cid in channel_ids:
            ch, label, err = await _resolve_watched_channel(cid)
            if ch is None:
                skipped.append(f"#{label} ({cid}): {err}")
                continue
            guild = getattr(ch, "guild", None)
            if guild is None:
                dms.append((cid, ch, label))
                continue
            by_guild.setdefault(guild.id, []).append((cid, ch, label))

        async def _touch(ch, label, guild_name):
            nonlocal activated
            try:
                async for _msg in ch.history(limit=1):
                    break
                activated += 1
                add_log("info", f"  ✓ #{label} — {guild_name} — active")
            except Exception as e:
                add_log("warning", f"  ⚠ #{label} — {guild_name} — history touch failed (still subscribed): {e}")
                activated += 1

        for gid, items in by_guild.items():
            guild = items[0][1].guild
            guild_name = getattr(guild, "name", str(gid))
            try:
                await guild.subscribe(typing=True, threads=True, activities=True)
                subscribed_guilds.add(gid)
            except Exception as e:
                add_log("warning", f"  ⚠ Guild subscribe failed for {guild_name}: {e}")
                skipped.append(f"{guild_name}: subscribe {e}")

            chan_ranges = {}
            thread_objs = []
            for cid, ch, label in items:
                chan_ranges[str(cid)] = [(0, 99)]
                parent_id = getattr(ch, "parent_id", None)
                if parent_id:
                    chan_ranges[str(int(parent_id))] = [(0, 99)]
                if getattr(discord, "Thread", None) and isinstance(ch, discord.Thread):
                    thread_objs.append(ch)

            conn = getattr(client, "_connection", None) or getattr(client, "_state", None)
            subs = getattr(conn, "subscriptions", None)
            if subs and hasattr(subs, "subscribe_to_channels"):
                try:
                    await subs.subscribe_to_channels(guild, chan_ranges, replace=False)
                except Exception as e:
                    add_log("warning", f"  ⚠ Channel-map subscribe failed for {guild_name}: {e}")
                    skipped.append(f"{guild_name}: channel map {e}")

            if thread_objs:
                try:
                    await guild.subscribe_to(threads=thread_objs)
                except Exception as e:
                    add_log("warning", f"  ⚠ Thread subscribe failed for {guild_name}: {e}")

            for cid, ch, label in items:
                await _touch(ch, label, guild_name)
                await asyncio.sleep(0.25)

        for cid, ch, label in dms:
            await _touch(ch, label, "DM")
            await asyncio.sleep(0.25)

        add_log(
            "success" if not skipped else "warning",
            f"📡 {reason}: {activated}/{len(channel_ids)} watched channels active, "
            f"{len(subscribed_guilds)} guilds subscribed"
            + (f" — SKIPPED: {'; '.join(skipped)[:500]}" if skipped else "")
        )
        return skipped

    @client.event
    async def on_ready():
        add_log("success", f"✅ Logged in as {client.user} — monitoring {len(channel_ids)} channels")
        if not state.get("start_time"):
            state["start_time"] = datetime.now().isoformat()
        await _subscribe_watched_channels("startup")

    @client.event
    async def on_message(message):
        # Resolve channel: in threads, message.channel.id is the thread ID; match by parent channel too
        raw_id = getattr(message.channel, "id", None)
        if raw_id is None:
            return
        raw_id = int(raw_id)
        parent_id = getattr(message.channel, "parent_id", None)
        if parent_id is not None:
            parent_id = int(parent_id)
        if raw_id in channel_id_set:
            effective_channel_id = raw_id
        elif parent_id is not None and parent_id in channel_id_set:
            effective_channel_id = parent_id
        else:
            return

        # Count every message in a monitored channel
        state["messages_seen"] += 1
        state["last_message_at"] = datetime.now().isoformat()

        msg_text = _message_text(message)
        # For attachment-only messages with no text, use the filename or URL as text
        if not msg_text:
            fallback_parts = []
            for a in getattr(message, "attachments", []) or []:
                fn = getattr(a, "filename", None)
                if fn:
                    fallback_parts.append(str(fn))
            msg_text = " ".join(fallback_parts)
        if not msg_text:
            return

        # Check if this channel is in "monitor all" mode (use resolved channel)
        is_monitor_all = effective_channel_id in monitor_all_channels
        
        content_lower = msg_text.lower()
        matched = [kw for kw in keywords if kw in content_lower]
        
        # Skip if no matches AND not in monitor-all mode
        if not matched and not is_monitor_all:
            return

        now = time.time()
        # Cooldown is per watched channel + keyword so a busy channel cannot mute the rest.
        fresh = [kw for kw in matched
                 if now - cooldowns.get(f"{effective_channel_id}:{kw}", 0) >= cooldown_secs]
        
        # For monitor-all channels, always alert (bypass cooldown for non-keyword matches)
        if not fresh and not is_monitor_all:
            return

        # Update cooldowns only for keyword matches
        for kw in fresh:
            cooldowns[f"{effective_channel_id}:{kw}"] = now

        channel_name = getattr(message.channel, "name", "unknown")
        guild_name = getattr(message.guild, "name", "Unknown Server") if message.guild else "DM"
        jump_url = f"https://discord.com/channels/{message.guild.id if message.guild else '@me'}/{message.channel.id}/{message.id}"
        raw_content = (getattr(message, "content", "") or "").strip()

        attachments = []
        image_urls = []
        extracted_links = set()

        for a in getattr(message, "attachments", []) or []:
            try:
                a_url = getattr(a, "url", None)
                a_name = getattr(a, "filename", None)
                a_type = getattr(a, "content_type", None)
                if a_url:
                    extracted_links.add(str(a_url))
                if a_url and any(str(a_url).lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
                    image_urls.append(str(a_url))
                if a_type and str(a_type).startswith("image/") and a_url:
                    image_urls.append(str(a_url))
                attachments.append({
                    "filename": str(a_name) if a_name else None,
                    "url": str(a_url) if a_url else None,
                    "content_type": str(a_type) if a_type else None,
                })
            except Exception:
                pass

        for emb in getattr(message, "embeds", []) or []:
            try:
                emb_url = getattr(emb, "url", None)
                if emb_url:
                    extracted_links.add(str(emb_url))
                emb_img = getattr(getattr(emb, "image", None), "url", None)
                if emb_img:
                    image_urls.append(str(emb_img))
                    extracted_links.add(str(emb_img))
                emb_thumb = getattr(getattr(emb, "thumbnail", None), "url", None)
                if emb_thumb:
                    image_urls.append(str(emb_thumb))
                    extracted_links.add(str(emb_thumb))
            except Exception:
                pass

        # Basic URL extraction from parsed text
        for token in msg_text.split():
            if token.startswith("http://") or token.startswith("https://"):
                extracted_links.add(token.strip("()[]<>.,"))

        image_urls = list(dict.fromkeys([u for u in image_urls if u]))[:8]
        links = list(dict.fromkeys([u for u in extracted_links if u]))[:25]

        state["alert_count"] += 1
        state["last_alert_at"] = datetime.now().isoformat()

        # Extract ASIN and Offer ID from Discord message content
        _asin = None
        _offer_id = None
        _asin_m = _re.search(r'SKU\s*\n([A-Z0-9]{10})', msg_text)
        if _asin_m:
            _asin = _asin_m.group(1)
        else:
            # Try extracting from Amazon URLs
            _asin_m2 = _re.search(r'amazon\.com/(?:dp|gp/product|gp/offer-listing)/([A-Z0-9]{10})', msg_text)
            if _asin_m2:
                _asin = _asin_m2.group(1)
        # Offer ID is in backtick blocks after "Offer ID"
        _oid_m = _re.search(r'Offer ID\s*\n```([^`]+)```', msg_text)
        if _oid_m:
            _offer_id = _oid_m.group(1).strip()
        else:
            # Also try offeringID from ATC URL
            _oid_m2 = _re.search(r'offeringID=([^\s&,\)\]]+)', msg_text)
            if _oid_m2:
                _offer_id = _oid_m2.group(1).strip()

        # Determine alert priority from keyword_priorities config
        priority_order = {"high": 3, "medium": 2, "low": 1}
        alert_priority = "medium"
        for kw in fresh:
            kw_pri = keyword_priorities.get(kw, "medium")
            if priority_order.get(kw_pri, 2) > priority_order.get(alert_priority, 2):
                alert_priority = kw_pri

        # Different log messages for monitor-all vs keyword match
        if is_monitor_all and not matched:
            log_msg = f"📬 New post in #{channel_name}"
        else:
            prefix = "🚨" if alert_priority == "high" else "🔔"
            log_msg = f"{prefix} Match in #{channel_name}"

        product_title = _product_title_from_content(msg_text)

        add_log("alert", log_msg, {
            "server": guild_name,
            "channel": channel_name,
            "author": str(message.author.display_name),
            "keywords": fresh if fresh else ["[monitor all]"],
            "content": msg_text[:3000],
            "raw_content": raw_content[:800],
            "product_title": product_title,
            "jump_url": jump_url,
            "timestamp": datetime.now().isoformat(),
            "monitor_mode": "all" if is_monitor_all else "keywords",
            "message_id": str(getattr(message, "id", "")),
            "channel_id": str(getattr(message.channel, "id", "")),
            "watch_channel_id": str(effective_channel_id),
            "parent_channel_id": str(parent_id) if parent_id is not None else None,
            "created_at": str(getattr(message, "created_at", "")) if getattr(message, "created_at", None) else None,
            "attachments": attachments[:10],
            "attachment_count": len(attachments),
            "image_urls": image_urls,
            "embed_count": len(getattr(message, "embeds", []) or []),
            "links": links,
            "priority": alert_priority,
            "asin": _asin,
            "offer_id": _offer_id,
        })

        # Format notification title based on mode
        if is_monitor_all and not matched:
            title = f"📬 New Post: #{channel_name}"
        else:
            title = f"🔔 Deal: {', '.join(fresh[:2])}"
            
        body = (
            f"Server: {guild_name}\n"
            f"Channel: #{channel_name}\n"
            f"From: {message.author.display_name}\n"
            f"{f'Keywords: {', '.join(fresh)}' if fresh else 'Mode: Monitor All'}\n\n"
            f"{msg_text[:1200]}\n\n"
            f"Jump: {jump_url}"
        )

        # Map priority to ntfy priority level
        ntfy_pri = {"high": "urgent", "medium": "high", "low": "default"}.get(alert_priority, "high")

        # Build details for rich ntfy notification
        _ntfy_details = {
            "channel": channel_name, "server": guild_name,
            "keywords": fresh if fresh else ["[monitor all]"],
            "content": msg_text[:3000],
            "jump_url": jump_url,
            "image_urls": image_urls,
            "asin": _asin,
            "offer_id": _offer_id,
            "priority": alert_priority,
        }

        tasks = []
        if ntfy_enabled and ntfy_topic:
            tasks.append(loop.run_in_executor(None, send_ntfy_sync, ntfy_topic, ntfy_server, title, body, ntfy_pri, _ntfy_details))

        if discord_dm and user_id:
            async def send_dm():
                try:
                    user = await client.fetch_user(user_id)
                    await user.send(f"**{title}**\n```\n{body[:1800]}\n```")
                    add_log("info", "💬 Discord DM sent")
                except Exception as e:
                    add_log("warning", f"Discord DM failed: {e}")
            tasks.append(send_dm())

        # Webhooks
        alert_details = {"channel": channel_name, "server": guild_name,
                         "keywords": fresh, "jump_url": jump_url,
                         "timestamp": datetime.now().isoformat(), "priority": alert_priority}
        for wh in webhooks:
            if wh.get("enabled", True) and wh.get("url"):
                tasks.append(loop.run_in_executor(None, send_webhook_sync, wh["url"], title, body, alert_details))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def heartbeat():
        """Log a heartbeat every 60s and re-activate channels every 10min."""
        await asyncio.sleep(60)
        cycles = 0
        while not stop_event.is_set():
            seen = state["messages_seen"]
            alerts = state["alert_count"]
            last = state.get("last_message_at")
            last_str = ""
            if last:
                try:
                    dt = datetime.fromisoformat(last)
                    last_str = f" | last msg {dt.strftime('%H:%M:%S')}"
                except Exception:
                    pass
            add_log("info", f"💓 Heartbeat — {seen} msgs seen, {alerts} alerts fired{last_str}")

            # Every 10 minutes, re-activate channels to keep subscriptions fresh
            cycles += 1
            if cycles % 10 == 0:
                try:
                    await _subscribe_watched_channels("re-activate")
                except Exception as e:
                    add_log("warning", f"🔄 Re-activate failed: {e}")

            await asyncio.sleep(60)

    async def runner():
        try:
            await client.start(token)
        except discord.LoginFailure:
            add_log("error", "❌ Login failed — check your Discord token")
            state["running"] = False
        except Exception as e:
            add_log("error", f"Monitor error: {e}")
            state["running"] = False

    async def stopper():
        while not stop_event.is_set():
            await asyncio.sleep(0.5)
        try:
            await client.close()
        except:
            pass

    async def main():
        await asyncio.gather(runner(), stopper(), heartbeat(), return_exceptions=True)

    try:
        loop.run_until_complete(main())
    except Exception as e:
        add_log("error", f"Monitor crashed: {e}")
    finally:
        loop.close()
        state["running"] = False
        add_log("info", "Monitor stopped.")


# ── HTTP Server ───────────────────────────────────────────────────────────────

# Read the HTML file once at startup
HTML_FILE = Path(__file__).parent / "discord_monitor_ui.html"

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default HTTP logs

    def handle(self):
        """Silence expected client disconnect errors (WinError 10053, etc.)."""
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except OSError as e:
            # Common disconnect errors across platforms:
            # - Windows: winerror 10053
            # - Unix-like: EPIPE(32), ECONNRESET(104), ESHUTDOWN(108)
            if getattr(e, "winerror", None) == 10053:
                return
            if getattr(e, "errno", None) in (32, 104, 108):
                return
            raise

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, content):
        body = content if isinstance(content, bytes) else content.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/favicon.ico":
            # Return a minimal 1x1 pixel favicon to suppress 404s
            # Lightning bolt SVG as a data URI would be better, but ICO is simplest
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><defs><linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#7289da"/><stop offset="100%" stop-color="#5865f2"/></linearGradient></defs><rect width="32" height="32" rx="7" fill="url(#g)"/><path d="M16 4L22 14l-6-2-6 2z" fill="#fff" opacity=".95"/><path d="M10 14l6-2 6 2-2.5 10L16 19l-3.5 5z" fill="#fff" opacity=".7"/><circle cx="16" cy="27" r="1.5" fill="#3ba55c"/></svg>'
            self.send_header("Content-Length", len(svg))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(svg)
            return

        # ── PWA assets ──
        if path == "/manifest.json":
            mf = Path(__file__).parent / "manifest.json"
            if mf.exists():
                self.send_response(200)
                self.send_header("Content-Type", "application/manifest+json")
                data = mf.read_bytes()
                self.send_header("Content-Length", len(data))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_error(404)
            return

        if path == "/sw.js":
            sw = Path(__file__).parent / "sw.js"
            if sw.exists():
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript")
                self.send_header("Cache-Control", "no-cache")
                data = sw.read_bytes()
                self.send_header("Content-Length", len(data))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_error(404)
            return

        if path in ("/icon-192.svg", "/icon-512.svg"):
            size = 192 if "192" in path else 512
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            icon_svg = (
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}">'
                f'<defs><linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">'
                f'<stop offset="0%" stop-color="#7289da"/>'
                f'<stop offset="100%" stop-color="#5865f2"/></linearGradient></defs>'
                f'<rect width="{size}" height="{size}" rx="{size//5}" fill="url(#g)"/>'
                f'<g transform="translate({size/2},{size/2}) scale({size/32})">'
                f'<path d="M0-12L6-2l-6 2-6-2z" fill="#fff" opacity=".95"/>'
                f'<path d="M-6-2l6 2 6-2-2.5 10L0 3l-3.5 5z" fill="#fff" opacity=".7"/>'
                f'<circle cx="0" cy="11" r="1.5" fill="#3ba55c"/>'
                f'</g></svg>'
            ).encode()
            self.send_header("Content-Length", len(icon_svg))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(icon_svg)
            return

        if path == "/" or path == "/index.html":
            if HTML_FILE.exists():
                self.send_html(HTML_FILE.read_bytes())
            else:
                self.send_html(b"<h1>UI file not found. Place discord_monitor_ui.html next to this script.</h1>")

        elif path == "/api/status":
            self.send_json({
                "running": state["running"],
                "alert_count": state["alert_count"],
                "log_count": len(state["logs"]),
                "start_time": state.get("start_time"),
                "uptime": _uptime(),
                "messages_seen": state.get("messages_seen", 0),
                "last_message_at": state.get("last_message_at"),
                "last_alert_at": state.get("last_alert_at"),
                "tunnel_url": state.get("tunnel_url"),
                "tunnel_enabled": state.get("tunnel_enabled", False),
                "tailscale_ip": state.get("tailscale_ip"),
                "tailscale_url": state.get("tailscale_url"),
            })

        elif path == "/api/config":
            cfg = load_config()
            # Mask token for display
            masked = dict(cfg)
            if masked.get("token"):
                t = masked["token"]
                masked["token"] = t[:6] + "..." + t[-4:] if len(t) > 10 else "***"
            self.send_json(masked)

        elif path == "/api/config/full":
            self.send_json(load_config())

        elif path == "/api/logs":
            qs = parse_qs(urlparse(self.path).query)
            since_id = int(qs.get("since_id", [0])[0])
            logs = state["logs"]
            if since_id:
                logs = [l for l in logs if l["id"] > since_id]
            self.send_json({"logs": logs, "alert_count": state["alert_count"],
                            "alerts": state.get("recent_alerts") or []})

        elif path == "/api/stats":
            stats = state.get("stats", {})
            top_keywords = sorted(stats.get("keyword_hits", {}).items(), key=lambda x: -x[1])[:20]
            top_channels = sorted(stats.get("channel_hits", {}).items(), key=lambda x: -x[1])[:10]
            now = datetime.now()
            hourly = []
            for i in range(24):
                h = now - timedelta(hours=23 - i)
                key = h.strftime("%Y-%m-%d-%H")
                hourly.append({"hour": h.strftime("%H:00"), "count": stats.get("alerts_per_hour", {}).get(key, 0)})
            self.send_json({
                "hourly": hourly,
                "top_keywords": [{"keyword": k, "count": c} for k, c in top_keywords],
                "top_channels": [{"channel": ch, "count": c} for ch, c in top_channels],
                "total_alerts": len(state.get("alert_history", [])),
                "session_alerts": state["alert_count"],
                "session_messages": state.get("messages_seen", 0),
                "uptime": _uptime(),
            })

        elif path == "/api/history":
            qs = parse_qs(urlparse(self.path).query)
            limit = min(int(qs.get("limit", [100])[0]), 500)
            offset = int(qs.get("offset", [0])[0])
            history = state.get("alert_history", [])
            # Return newest first
            reversed_history = list(reversed(history))
            self.send_json({
                "alerts": reversed_history[offset:offset + limit],
                "total": len(history),
            })

        elif path == "/api/sync/status":
            cfg = load_config()
            sync_cfg = cfg.get("sync", {})
            self.send_json({
                "enabled": sync_cfg.get("enabled", False),
                "firebase_url": sync_cfg.get("firebase_url", ""),
                "device_id": sync_cfg.get("device_id", ""),
                "device_name": sync_cfg.get("device_name", ""),
                "auto_sync": sync_cfg.get("auto_sync", True),
                "sync_interval": sync_cfg.get("sync_interval", 300),
                "last_push": state.get("sync_last_push"),
                "last_pull": state.get("sync_last_pull"),
                "local_alerts": len(state.get("alert_history", [])),
            })

        elif path == "/api/sync/devices":
            cfg = load_config()
            fb_url = cfg.get("sync", {}).get("firebase_url", "")
            if not fb_url:
                self.send_json({"devices": [], "error": "No Firebase URL"})
                return
            devices = _firebase_request(fb_url, "/devices", "GET")
            if isinstance(devices, dict) and "error" not in devices:
                device_list = []
                for did, info in devices.items():
                    if isinstance(info, dict):
                        info["id"] = did
                        device_list.append(info)
                self.send_json({"devices": device_list})
            else:
                self.send_json({"devices": [], "error": devices.get("error", "Failed")})

        elif path == "/api/events":
            # Server-Sent Events stream
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            q = []
            state["connected_clients"].append(q)
            try:
                while True:
                    try:
                        if q:
                            entry = q.pop(0)
                            data = json.dumps(entry)
                            self.wfile.write(f"data: {data}\n\n".encode())
                            self.wfile.flush()
                        else:
                            self.wfile.write(b": heartbeat\n\n")
                            self.wfile.flush()
                            time.sleep(1)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        break
                    except OSError as e:
                        # Client closed connection: Windows 10053, or Unix EPIPE/ECONNRESET
                        if getattr(e, "winerror", None) == 10053:
                            break
                        if getattr(e, "errno", None) in (32, 104, 108):
                            break
                        raise
            finally:
                try:
                    state["connected_clients"].remove(q)
                except ValueError:
                    pass

        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/config":
            try:
                data = json.loads(self.read_body())
                current = load_config()
                # If token is masked, keep the real one
                if data.get("token", "").endswith("...") or "***" in data.get("token", ""):
                    data["token"] = current.get("token", "")
                current.update(data)
                save_config(current)
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": str(e)}, 400)

        elif path == "/api/start":
            try:
                payload = json.loads(self.read_body() or b"{}")
            except Exception:
                payload = {}
            if payload.get("manual") is not True:
                self.send_json({"ok": False, "error": "Manual start required"})
                return
            if state["running"]:
                self.send_json({"ok": False, "error": "Already running"})
                return
            config = load_config()
            if not config.get("token"):
                self.send_json({"ok": False, "error": "No token configured"})
                return

            stop_event = threading.Event()
            state["stop_event"] = stop_event
            state["running"] = True
            state["alert_count"] = 0
            state["messages_seen"] = 0
            state["last_message_at"] = None
            state["last_alert_at"] = None
            state["start_time"] = datetime.now().isoformat()
            add_log("info", "🚀 Starting monitor...")

            t = threading.Thread(target=run_monitor, args=(config, stop_event), daemon=True)
            t.start()
            state["monitor_thread"] = t
            self.send_json({"ok": True})

        elif path == "/api/stop":
            if not state["running"]:
                self.send_json({"ok": False, "error": "Not running"})
                return
            if state.get("stop_event"):
                state["stop_event"].set()
            state["running"] = False
            add_log("info", "⏹️  Monitor stopping...")
            self.send_json({"ok": True})

        elif path == "/api/logs/clear":
            state["logs"] = []
            self.send_json({"ok": True})

        elif path == "/api/ebay-lookup":
            try:
                data = json.loads(self.read_body())
                title = (data.get("title") or data.get("query") or "").strip()
                asin = (data.get("asin") or "").strip()
                sku = (data.get("sku") or "").strip()
                content = (data.get("content") or "").strip()
                query = _clean_ebay_query(title, asin=asin, sku=sku, content=content)
                if not query:
                    self.send_json({
                        "error": "No product title, ASIN, or SKU to search on eBay",
                        "query": "",
                        "count": 0,
                        "prices": [],
                        "search_url": "https://www.ebay.com/sch/i.html?LH_Complete=1&LH_Sold=1",
                    }, 400)
                    return
                result = _ebay_sold_prices(query)
                result["query"] = query
                if not result.get("search_url"):
                    result["search_url"] = _ebay_search_url(query)
                result["manual_url"] = result["search_url"]
                self.send_json(result)
            except Exception as e:
                q = ""
                try:
                    q = query
                except NameError:
                    pass
                fail = _ebay_empty_result(q, f"eBay lookup crashed: {e}", blocked=True)
                self.send_json(fail, 500)

        elif path == "/api/sync/configure":
            try:
                data = json.loads(self.read_body())
                cfg = load_config()
                sync_cfg = cfg.get("sync", dict(DEFAULT_CONFIG["sync"]))
                # Update sync fields
                for key in ("enabled", "firebase_url", "device_name", "auto_sync", "sync_interval"):
                    if key in data:
                        sync_cfg[key] = data[key]
                # Ensure device ID exists
                if not sync_cfg.get("device_id"):
                    sync_cfg["device_id"] = str(uuid.uuid4())[:12]
                cfg["sync"] = sync_cfg
                save_config(cfg)
                self.send_json({"ok": True, "device_id": sync_cfg["device_id"]})
            except Exception as e:
                self.send_json({"error": str(e)}, 400)

        elif path == "/api/sync/test":
            try:
                data = json.loads(self.read_body())
                fb_url = (data.get("firebase_url") or "").strip().rstrip("/")
                if not fb_url:
                    self.send_json({"ok": False, "error": "No Firebase URL"})
                    return
                # Validate URL format
                if not (fb_url.startswith("https://") and
                        ("firebaseio.com" in fb_url or "firebasedatabase.app" in fb_url)):
                    self.send_json({"ok": False, "error": "Invalid Firebase URL. Should be https://your-project.firebaseio.com"})
                    return
                # Write test, read back, delete
                test_data = {"test": True, "timestamp": datetime.now().isoformat()}
                w = _firebase_request(fb_url, "/_test", "PUT", test_data)
                if "error" in w:
                    self.send_json({"ok": False, "error": f"Write failed: {w['error']}"})
                    return
                r = _firebase_request(fb_url, "/_test", "GET")
                if "error" in r:
                    self.send_json({"ok": False, "error": f"Read failed: {r['error']}"})
                    return
                _firebase_request(fb_url, "/_test", "DELETE")
                # Check cloud alert count
                meta = _firebase_request(fb_url, "/meta", "GET")
                cloud_info = {}
                if isinstance(meta, dict) and "error" not in meta:
                    cloud_info = meta
                self.send_json({"ok": True, "message": "Connected!", "cloud_meta": cloud_info})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})

        elif path == "/api/sync/push":
            try:
                cfg = load_config()
                sync_cfg = cfg.get("sync", {})
                fb_url = sync_cfg.get("firebase_url", "")
                if not fb_url:
                    self.send_json({"ok": False, "error": "No Firebase URL configured"})
                    return
                device_id = sync_cfg.get("device_id") or _get_device_id()
                device_name = sync_cfg.get("device_name", "")
                # Also push eBay cache from frontend if provided
                body_data = {}
                try:
                    body_data = json.loads(self.read_body())
                except Exception:
                    pass
                ebay_cache = body_data.get("ebay_cache")
                if ebay_cache and isinstance(ebay_cache, dict):
                    _firebase_request(fb_url, "/ebay_cache", "PATCH", ebay_cache)

                result = _sync_push(fb_url, device_id, device_name)
                self.send_json(result)
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})

        elif path == "/api/sync/pull":
            try:
                cfg = load_config()
                sync_cfg = cfg.get("sync", {})
                fb_url = sync_cfg.get("firebase_url", "")
                if not fb_url:
                    self.send_json({"ok": False, "error": "No Firebase URL configured"})
                    return
                device_id = sync_cfg.get("device_id") or _get_device_id()
                result = _sync_pull(fb_url, device_id)
                self.send_json(result)
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})

        elif path == "/api/sync/backup":
            # Full export as downloadable JSON
            try:
                backup = {
                    "version": 1,
                    "exported_at": datetime.now().isoformat(),
                    "alert_history": state.get("alert_history", []),
                    "config": {k: v for k, v in load_config().items()
                               if k not in ("token", "user_id")},
                    "stats": state.get("stats", {}),
                }
                body = json.dumps(backup, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="dropscout_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json"')
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif path == "/api/sync/restore":
            try:
                data = json.loads(self.read_body())
                restored = 0
                # Restore alert history
                if "alert_history" in data and isinstance(data["alert_history"], list):
                    local_ids = {str(e.get("id", "")) for e in state.get("alert_history", [])}
                    for entry in data["alert_history"]:
                        if isinstance(entry, dict) and str(entry.get("id", "")) not in local_ids:
                            state["alert_history"].append(entry)
                            restored += 1
                    state["alert_history"].sort(key=lambda e: e.get("id", 0))
                    state["alert_history"] = state["alert_history"][-5000:]
                    state["alert_history_dirty"] = True
                # Restore config (non-sensitive)
                if "config" in data and isinstance(data["config"], dict):
                    cfg = load_config()
                    safe_keys = ("channels", "keywords", "keyword_priorities",
                                 "keyword_groups", "webhooks", "ntfy_topic",
                                 "ntfy_server", "cooldown")
                    for k in safe_keys:
                        if k in data["config"]:
                            cfg[k] = data["config"][k]
                    save_config(cfg)
                self.send_json({"ok": True, "restored_alerts": restored})
            except Exception as e:
                self.send_json({"error": str(e)}, 400)

        elif path == "/api/resolve-url":
            try:
                data = json.loads(self.read_body())
                url = (data.get("url") or "").strip()
                urls = data.get("urls") or []
                content = data.get("content") or ""

                results = {}

                # Resolve single URL
                if url:
                    results[url] = _resolve_url(url)

                # Resolve batch of URLs
                for u in urls[:20]:  # cap at 20 to prevent abuse
                    u = u.strip()
                    if u and u not in results:
                        results[u] = _resolve_url(u)

                # Extract IDs from text content
                text_ids = _extract_ids_from_text(content)

                # Also extract from all resolved URLs
                for r in results.values():
                    if r.get("asin") and r["asin"] not in text_ids["asins"]:
                        text_ids["asins"].append(r["asin"])
                    if r.get("tcin") and r["tcin"] not in text_ids["tcins"]:
                        text_ids["tcins"].append(r["tcin"])

                self.send_json({
                    "ok": True,
                    "resolved": results,
                    "ids": text_ids,
                })
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif path == "/api/test_alert":
            # Seed demo alerts for Dashboard / UI checks. Does not touch the Discord gateway.
            try:
                now = datetime.now()
                samples = [
                    {
                        "message": "🚨 Match in #amazon-restocks",
                        "details": {
                            "server": "Flip Alerts",
                            "channel": "amazon-restocks",
                            "author": "RestockBot",
                            "keywords": ["pokemon", "restock"],
                            "content": (
                                "Pokemon TCG Prismatic Evolutions Elite Trainer Box\n"
                                "SKU\nB0DH1ZW4MM\n"
                                "Price\n$54.99\n"
                                "Seller\nAmazon.com\n"
                                "Add to Cart\n"
                                "[Add to Cart](https://www.amazon.com/checkout/entry/buynow?asin=B0DH1ZW4MM)\n"
                                "Links\n"
                                "[Amazon](https://www.amazon.com/dp/B0DH1ZW4MM) [eBay](https://www.ebay.com/sch/i.html?_nkw=prismatic+evolutions+etb)"
                            ),
                            "jump_url": "https://discord.com/channels/1/2/3",
                            "timestamp": now.isoformat(),
                            "created_at": now.isoformat(),
                            "image_urls": ["https://m.media-amazon.com/images/I/81Q7rGaLYdL._AC_SL1500_.jpg"],
                            "links": ["https://www.amazon.com/dp/B0DH1ZW4MM"],
                            "priority": "high",
                            "asin": "B0DH1ZW4MM",
                            "product_title": "Pokemon TCG Prismatic Evolutions Elite Trainer Box",
                        },
                    },
                    {
                        "message": "🔔 Match in #target-drops",
                        "details": {
                            "server": "Flip Alerts",
                            "channel": "target-drops",
                            "author": "DropPing",
                            "keywords": ["hot wheels"],
                            "content": (
                                "# Restock Alert\n"
                                "Hot Wheels Premium Boulevard Mix\n"
                                "Just hit Target online — limited per household.\n"
                                "[ATC](https://www.target.com/p/hot-wheels/-/A-12345678)\n"
                                "https://www.target.com/p/hot-wheels/-/A-12345678"
                            ),
                            "jump_url": "https://discord.com/channels/1/4/5",
                            "timestamp": (now - timedelta(minutes=2)).isoformat(),
                            "created_at": (now - timedelta(minutes=2)).isoformat(),
                            "image_urls": ["https://target.scene7.com/is/image/Target/GUEST_hotwheels"],
                            "links": ["https://www.target.com/p/hot-wheels/-/A-12345678"],
                            "priority": "medium",
                        },
                    },
                    {
                        "message": "🔔 Match in #walmart-deals",
                        "details": {
                            "server": "Flip Alerts",
                            "channel": "walmart-deals",
                            "author": "Scout",
                            "keywords": ["lego"],
                            "content": (
                                "LEGO Star Wars UCS set clearance\n"
                                "Price\n$89.00\n"
                                "Recommended Price\n$159.99\n"
                                "Links\n"
                                "[Walmart](https://www.walmart.com/ip/lego-star-wars/123456789) [eBay Sold](https://www.ebay.com/sch/i.html?_nkw=lego+star+wars+ucs&LH_Sold=1)"
                            ),
                            "jump_url": "https://discord.com/channels/1/6/7",
                            "timestamp": (now - timedelta(minutes=8)).isoformat(),
                            "created_at": (now - timedelta(minutes=8)).isoformat(),
                            "image_urls": [],
                            "links": ["https://www.walmart.com/ip/lego-star-wars/123456789"],
                            "priority": "medium",
                        },
                    },
                    {
                        "message": "📬 New post in #exclusive",
                        "details": {
                            "server": "VIP Drops",
                            "channel": "exclusive",
                            "author": "mod",
                            "keywords": ["[monitor all]"],
                            "content": (
                                "Monster High Haunt Couture restock rumor — watch the product page.\n"
                                "https://www.amazon.com/dp/B0CXYZ1234"
                            ),
                            "jump_url": "https://discord.com/channels/8/9/10",
                            "timestamp": (now - timedelta(minutes=18)).isoformat(),
                            "created_at": (now - timedelta(minutes=18)).isoformat(),
                            "image_urls": [],
                            "links": ["https://www.amazon.com/dp/B0CXYZ1234"],
                            "priority": "low",
                            "asin": "B0CXYZ1234",
                        },
                    },
                ]
                last = None
                for s in samples:
                    d = s["details"]
                    d["product_title"] = d.get("product_title") or _product_title_from_content(d.get("content") or "")
                    last = add_log("alert", s["message"], d)
                    state["alert_count"] += 1
                    state["last_alert_at"] = datetime.now().isoformat()
                self.send_json({"ok": True, "count": len(samples), "alert": last})
            except Exception as e:
                self.send_json({"error": str(e)}, 400)

        elif path == "/api/test_ntfy":
            try:
                data = json.loads(self.read_body())
                topic = data.get("topic", "")
                server = data.get("server", "https://ntfy.sh")
                if not topic:
                    self.send_json({"ok": False, "error": "No topic"})
                    return
                test_details = {
                    "channel": "deals-restocks",
                    "server": "Drop Scout",
                    "keywords": ["restock", "pokemon"],
                    "content": "Pokemon TCG Prismatic Evolutions Elite Trainer Box\nSKU\nB0DH1ZW4MM\nPrice\n$54.99\nSeller\nAmazon.com\nOffer ID\n```test123%3D%3D```",
                    "jump_url": "",
                    "image_urls": ["https://m.media-amazon.com/images/I/81Q7rGaLYdL._AC_SL1500_.jpg"],
                    "asin": "B0DH1ZW4MM",
                    "priority": "high",
                }
                threading.Thread(
                    target=send_ntfy_sync,
                    args=(topic, server, "🔔 Test: Pokemon ETB Restock", "Drop Scout test notification", "high", test_details),
                    daemon=True
                ).start()
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": str(e)}, 400)

        elif path == "/api/tunnel/start":
            with _tunnel_lock:
                url = state.get("tunnel_url")
                proc = state.get("tunnel_process")
            if url:
                self.send_json({"ok": True, "url": url, "status": "already_running"})
            elif proc:
                self.send_json({"ok": True, "status": "starting"})
            else:
                result = start_tunnel()
                if result:
                    self.send_json({"ok": True, "status": "starting"})
                else:
                    self.send_json({"ok": False, "error": "Failed to start tunnel"}, 500)

        elif path == "/api/tunnel/stop":
            stop_tunnel()
            self.send_json({"ok": True})

        elif path == "/api/amazon-offer":
            try:
                data = json.loads(self.read_body())
                asin = data.get("asin", "").strip().upper()
                if not asin or len(asin) != 10:
                    self.send_json({"error": "Invalid ASIN"}, 400)
                    return
                result = _lookup_offer_id_from_history(asin)
                self.send_json(result)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        else:
            self.send_json({"error": "Not found"}, 404)


def _uptime():
    if not state.get("start_time"):
        return None
    try:
        start = datetime.fromisoformat(state["start_time"])
        delta = datetime.now() - start
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    except:
        return None


# ── Entry point ───────────────────────────────────────────────────────────────

PORT = 7890

def main():
    print("=" * 55)
    print("  Drop Scout 2.0 — Dashboard")
    print(f"  UI: http://localhost:{PORT}")
    print("  Press Ctrl+C to stop.")
    print("=" * 55)

    # Always boot into a clean, manual-start state.
    state["running"] = False
    state["stop_event"] = None
    state["monitor_thread"] = None
    state["start_time"] = None
    state["alert_count"] = 0

    # Load persistent alert history from disk
    load_alert_history()
    history_count = len(state["alert_history"])
    if history_count:
        print(f"  Loaded {history_count} alerts from history.")

    # Start background flush thread for alert persistence
    threading.Thread(target=_history_flush_loop, daemon=True).start()

    # Start cloud sync background thread
    threading.Thread(target=_sync_auto_loop, daemon=True).start()

    # Start Tailscale detection loop
    threading.Thread(target=_tailscale_check_loop, daemon=True).start()
    # Run initial Tailscale check immediately
    ts_ip = _detect_tailscale()
    if ts_ip:
        state["tailscale_ip"] = ts_ip
        state["tailscale_url"] = f"http://{ts_ip}:{PORT}"
        print(f"  Tailscale: http://{ts_ip}:{PORT}")

    # Threaded server is required because /api/events keeps SSE connections open.
    # Without this, one open SSE stream can block /api/start, /api/stop, etc.
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        print(f"\n  ❌ ERROR: Port {PORT} is already in use!")
        print(f"     Another instance of Drop Scout (or DropScout.exe) may be running.")
        print(f"     Close it first, or run:  taskkill /F /IM DropScout.exe")
        input("\n  Press Enter to exit...")
        sys.exit(1)
    server.timeout = 1

    # Open browser after short delay
    def open_browser():
        time.sleep(0.8)
        # Cache-bust on launch so users don't need Ctrl+F5 to get latest UI logic.
        webbrowser.open(f"http://localhost:{PORT}/?v=2.0.{int(time.time())}")
    threading.Thread(target=open_browser, daemon=True).start()

    add_log("success", f"✅ Server started on http://localhost:{PORT}")
    if history_count:
        add_log("info", f"📊 Loaded {history_count} alerts from history with {len(state['stats']['keyword_hits'])} tracked keywords.")
    add_log("info", "Configure your settings and press Start to begin monitoring.")

    try:
        while True:
            server.handle_request()
    except KeyboardInterrupt:
        print("\nShutting down...")
        if state.get("stop_event"):
            state["stop_event"].set()
        # Stop tunnel if running
        if state.get("tunnel_process"):
            stop_tunnel()
            print("  Tunnel stopped.")
        # Flush alert history before exit
        if state["alert_history_dirty"]:
            save_alert_history()
            print("  Alert history saved.")
        server.server_close()

if __name__ == "__main__":
    main()
