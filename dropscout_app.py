"""
Drop Scout — Desktop Application Wrapper
Launches the backend server + native fullscreen window + system tray icon.
"""

import sys
import os
import time
import threading
import signal
import subprocess

# ── Ensure we're running from the correct directory ──────────────────────────
APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_DIR)

# ── Version ──────────────────────────────────────────────────────────────────
APP_VERSION = "2.0.0"
APP_NAME = "Drop Scout"
PORT = 7890

# ── Globals ──────────────────────────────────────────────────────────────────
_server_thread = None
_tray_icon = None
_webview_window = None
_shutting_down = False


def resource_path(relative):
    """Get absolute path to resource, works for dev and PyInstaller bundle."""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative)
    return os.path.join(APP_DIR, relative)


# ── Server Management ───────────────────────────────────────────────────────

def start_server():
    """Start the backend server in a background thread."""
    global _server_thread

    # Import the server module and run its main() in a thread
    # We modify it to not open a browser (we use pywebview instead)
    import discord_monitor_server as server

    # Patch: prevent browser auto-open when running as desktop app
    original_main = server.main

    def patched_main():
        """Run server.main() but skip the webbrowser.open() call."""
        import webbrowser as wb
        original_open = wb.open
        wb.open = lambda *a, **k: None  # no-op
        try:
            original_main()
        finally:
            wb.open = original_open

    _server_thread = threading.Thread(target=patched_main, daemon=True)
    _server_thread.start()


def wait_for_server(timeout=15):
    """Wait for the HTTP server to be ready."""
    import urllib.request
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(f"http://localhost:{PORT}/api/status", timeout=2)
            return True
        except Exception:
            time.sleep(0.3)
    return False


# ── System Tray ──────────────────────────────────────────────────────────────

def create_tray_icon():
    """Create system tray icon with menu."""
    global _tray_icon

    try:
        import pystray
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("[Tray] pystray or Pillow not installed — skipping tray icon")
        return

    # Create tray icon image (32x32 blue/purple gradient-like with arrow)
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background rounded rect (approximate with filled shapes)
    draw.rounded_rectangle([2, 2, 62, 62], radius=12, fill=(88, 101, 242))

    # White arrow/drop shape
    draw.polygon([(32, 8), (46, 28), (32, 22), (18, 28)], fill=(255, 255, 255))
    draw.polygon([(18, 28), (32, 22), (46, 28), (38, 48), (32, 40), (26, 48)], fill=(220, 220, 255))

    # Green dot (live indicator)
    draw.ellipse([26, 52, 38, 60], fill=(59, 165, 92))

    def on_show(icon, item):
        """Show/restore the window."""
        if _webview_window:
            try:
                _webview_window.show()
                _webview_window.restore()
            except Exception:
                pass

    def on_hide(icon, item):
        """Minimize to tray."""
        if _webview_window:
            try:
                _webview_window.hide()
            except Exception:
                pass

    def on_check_updates(icon, item):
        """Check for updates."""
        try:
            from updater import check_for_update
            result = check_for_update(APP_VERSION)
            if result:
                # Show via webview JS
                if _webview_window:
                    ver = result.get("version", "?")
                    _webview_window.evaluate_js(
                        f"toast('Update available: v{ver}', 'info', 5000)"
                    )
            else:
                if _webview_window:
                    _webview_window.evaluate_js(
                        "toast('You are on the latest version!', 'success', 3000)"
                    )
        except Exception as e:
            print(f"[Update] Error checking: {e}")

    def on_quit(icon, item):
        """Quit the application."""
        global _shutting_down
        _shutting_down = True

        # Close the webview window
        if _webview_window:
            try:
                _webview_window.destroy()
            except Exception:
                pass

        # Stop the tray icon
        icon.stop()

        # Force exit after a short delay (daemon threads will die)
        def force_exit():
            time.sleep(1)
            os._exit(0)
        threading.Thread(target=force_exit, daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem("Show Drop Scout", on_show, default=True),
        pystray.MenuItem("Minimize to Tray", on_hide),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Check for Updates", on_check_updates),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"v{APP_VERSION}", None, enabled=False),
        pystray.MenuItem("Quit", on_quit),
    )

    _tray_icon = pystray.Icon(
        "dropscout",
        img,
        "Drop Scout",
        menu
    )

    # Run tray in its own thread
    threading.Thread(target=_tray_icon.run, daemon=True).start()


# ── Webview Window ───────────────────────────────────────────────────────────

def launch_window():
    """Create fullscreen native window with pywebview."""
    global _webview_window

    try:
        import webview
    except ImportError:
        print("[Window] pywebview not installed — falling back to browser")
        import webbrowser
        webbrowser.open(f"http://localhost:{PORT}")
        # Keep main thread alive
        try:
            while not _shutting_down:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return

    # Window close handler — minimize to tray instead of quitting
    def on_closing():
        if _tray_icon and not _shutting_down:
            # Minimize to tray instead of closing
            try:
                _webview_window.hide()
            except Exception:
                pass
            return False  # Prevent actual close
        return True  # Allow close if shutting down or no tray

    def on_closed():
        global _shutting_down
        if not _shutting_down:
            _shutting_down = True
            if _tray_icon:
                try:
                    _tray_icon.stop()
                except Exception:
                    pass
            os._exit(0)

    # Inject version info into the page after load
    def on_loaded():
        if _webview_window:
            try:
                _webview_window.evaluate_js(f"""
                    document.title = 'Drop Scout v{APP_VERSION}';
                    if (typeof window._dsVersion === 'undefined') {{
                        window._dsVersion = '{APP_VERSION}';
                        // Add version chip to header
                        const hdr = document.querySelector('.header-stats');
                        if (hdr) {{
                            const chip = document.createElement('div');
                            chip.className = 'stat-chip';
                            chip.innerHTML = '<span class="stat-label">Version</span><span class="stat-value">v{APP_VERSION}</span>';
                            hdr.appendChild(chip);
                        }}
                    }}
                """)
            except Exception:
                pass

    _webview_window = webview.create_window(
        f"Drop Scout v{APP_VERSION}",
        url=f"http://localhost:{PORT}/?v={int(time.time())}",
        width=1400,
        height=900,
        min_size=(900, 600),
        resizable=True,
        confirm_close=True,
        text_select=True,
    )

    _webview_window.events.closing += on_closing
    _webview_window.events.closed += on_closed
    _webview_window.events.loaded += on_loaded

    # Start fullscreen (user can press F11 or Escape to exit)
    def go_fullscreen():
        time.sleep(1.5)
        try:
            _webview_window.toggle_fullscreen()
        except Exception:
            pass

    threading.Thread(target=go_fullscreen, daemon=True).start()

    # This blocks until all windows are closed
    webview.start(debug=False)


# ── Main Entry Point ─────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print(f"  {APP_NAME} v{APP_VERSION} — Desktop Mode")
    print("=" * 55)

    # 1. Start the backend server
    print("  Starting backend server...")
    start_server()

    # 2. Wait for server to be ready
    print("  Waiting for server...")
    if not wait_for_server():
        print("  ERROR: Server failed to start within 15 seconds!")
        print("  Try running discord_monitor_server.py directly to see errors.")
        input("  Press Enter to exit...")
        sys.exit(1)

    print(f"  Server ready on http://localhost:{PORT}")

    # 3. Check for updates in background (non-blocking)
    def bg_update_check():
        time.sleep(5)  # Wait for UI to settle
        try:
            from updater import check_for_update
            result = check_for_update(APP_VERSION)
            if result and _webview_window:
                ver = result.get("version", "?")
                _webview_window.evaluate_js(
                    f"toast('🆕 Update available: v{ver} — Check system tray to update', 'info', 8000)"
                )
        except Exception:
            pass

    threading.Thread(target=bg_update_check, daemon=True).start()

    # 4. Create system tray icon
    print("  Setting up system tray...")
    create_tray_icon()

    # 5. Launch native window (this blocks until exit)
    print("  Launching window...")
    launch_window()


if __name__ == "__main__":
    main()
