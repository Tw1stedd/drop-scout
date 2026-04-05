"""
Drop Scout — Auto-Update Module
Checks GitHub releases (or a custom URL) for new versions and downloads them.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────────────

# Change this to your actual GitHub repo or update server URL
# For GitHub: "https://api.github.com/repos/YOUR_USER/dropscout/releases/latest"
# For custom: any URL returning JSON with {"version": "2.1.0", "download_url": "..."}
UPDATE_CHECK_URL = os.environ.get(
    "DROPSCOUT_UPDATE_URL",
    ""  # Set this when you have a release server
)

# Local version file (written at build time)
VERSION_FILE = Path(__file__).parent / "version.json"

APP_DIR = Path(__file__).parent


def get_current_version():
    """Get the currently installed version."""
    if VERSION_FILE.exists():
        try:
            with open(VERSION_FILE) as f:
                data = json.load(f)
            return data.get("version", "2.0.0")
        except Exception:
            pass
    return "2.0.0"


def write_version(version):
    """Write version to disk."""
    with open(VERSION_FILE, "w") as f:
        json.dump({"version": version, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f, indent=2)


def parse_version(v):
    """Parse version string to comparable tuple."""
    try:
        parts = v.lstrip("v").split(".")
        return tuple(int(p) for p in parts)
    except Exception:
        return (0, 0, 0)


def check_for_update(current_version=None):
    """
    Check if a newer version is available.
    Returns dict with {version, download_url, notes} if update available, else None.
    """
    if not UPDATE_CHECK_URL:
        return None

    current = current_version or get_current_version()

    try:
        req = urllib.request.Request(UPDATE_CHECK_URL, headers={
            "User-Agent": "DropScout-Updater",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        # Support GitHub releases format
        if "tag_name" in data:
            latest_version = data["tag_name"].lstrip("v")
            download_url = ""
            for asset in data.get("assets", []):
                if asset["name"].endswith(".exe") and "Setup" in asset["name"]:
                    download_url = asset["browser_download_url"]
                    break
            notes = data.get("body", "")
        else:
            # Custom JSON format
            latest_version = data.get("version", "").lstrip("v")
            download_url = data.get("download_url", "")
            notes = data.get("notes", "")

        if not latest_version:
            return None

        if parse_version(latest_version) > parse_version(current):
            return {
                "version": latest_version,
                "download_url": download_url,
                "notes": notes,
            }

    except Exception as e:
        print(f"[Updater] Check failed: {e}")

    return None


def download_update(download_url, progress_callback=None):
    """
    Download the update installer to a temp directory.
    Returns the path to the downloaded file, or None on failure.
    """
    if not download_url:
        return None

    try:
        filename = download_url.split("/")[-1]
        if not filename.endswith(".exe"):
            filename = "DropScout-Update.exe"

        temp_dir = tempfile.mkdtemp(prefix="dropscout_update_")
        dest_path = os.path.join(temp_dir, filename)

        req = urllib.request.Request(download_url, headers={
            "User-Agent": "DropScout-Updater",
        })

        with urllib.request.urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 65536

            with open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total:
                        progress_callback(downloaded / total)

        return dest_path

    except Exception as e:
        print(f"[Updater] Download failed: {e}")
        return None


def apply_update(installer_path):
    """
    Launch the downloaded installer and exit the current app.
    The installer (Inno Setup) handles closing the old version.
    """
    if not installer_path or not os.path.exists(installer_path):
        return False

    try:
        # Launch installer with /SILENT for auto-install
        subprocess.Popen(
            [installer_path, "/SILENT", "/CLOSEAPPLICATIONS"],
            creationflags=subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0,
        )

        # Exit current app so installer can replace files
        time.sleep(0.5)
        os._exit(0)

    except Exception as e:
        print(f"[Updater] Apply failed: {e}")
        return False


# ── Quick-update for development (no installer needed) ──────────────────────

def hot_update_files(files_url):
    """
    For development: download updated .py and .html files directly.
    files_url should return JSON: {"files": {"filename": "download_url", ...}}
    """
    try:
        req = urllib.request.Request(files_url, headers={"User-Agent": "DropScout-Updater"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        updated = []
        for filename, url in data.get("files", {}).items():
            # Safety: only allow .py and .html files
            if not filename.endswith((".py", ".html")):
                continue

            dest = APP_DIR / filename
            backup = APP_DIR / f"{filename}.bak"

            # Backup existing file
            if dest.exists():
                shutil.copy2(dest, backup)

            # Download new version
            req2 = urllib.request.Request(url, headers={"User-Agent": "DropScout-Updater"})
            with urllib.request.urlopen(req2, timeout=30) as resp2:
                content = resp2.read()

            with open(dest, "wb") as f:
                f.write(content)

            updated.append(filename)

        return updated

    except Exception as e:
        print(f"[Updater] Hot update failed: {e}")
        return []


if __name__ == "__main__":
    # Quick test
    print(f"Current version: {get_current_version()}")
    result = check_for_update()
    if result:
        print(f"Update available: v{result['version']}")
        print(f"Download: {result['download_url']}")
    else:
        print("No update available (or no update URL configured)")
