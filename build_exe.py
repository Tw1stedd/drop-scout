"""
Drop Scout — Build Script
Creates the standalone .exe using PyInstaller.

Usage:
    python build_exe.py

Prerequisites:
    pip install -r requirements_desktop.txt
    pip install pyinstaller

Output:
    dist/DropScout/DropScout.exe  (folder mode — needed for the installer)
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

APP_DIR = Path(__file__).parent
DIST_DIR = APP_DIR / "dist"
BUILD_DIR = APP_DIR / "build"
SPEC_FILE = APP_DIR / "DropScout.spec"

# Icon file (we'll generate it if missing)
ICON_FILE = APP_DIR / "dropscout.ico"


def create_icon():
    """Generate a .ico file from the Drop Scout logo."""
    if ICON_FILE.exists():
        print(f"  Icon already exists: {ICON_FILE}")
        return

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("  [WARN] Pillow not installed — using default icon")
        return

    # Create 256x256 icon
    sizes = [16, 32, 48, 64, 128, 256]
    images = []

    for size in sizes:
        img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Scale factor
        s = size / 64.0

        # Background rounded rect
        pad = int(2 * s)
        rad = int(12 * s)
        draw.rounded_rectangle(
            [pad, pad, size - pad, size - pad],
            radius=rad,
            fill=(88, 101, 242)  # Discord-like purple/blue
        )

        # White arrow/drop shape (top)
        cx = size // 2
        draw.polygon([
            (cx, int(8 * s)),
            (int(46 * s), int(28 * s)),
            (cx, int(22 * s)),
            (int(18 * s), int(28 * s)),
        ], fill=(255, 255, 255))

        # Lower part (slightly transparent)
        draw.polygon([
            (int(18 * s), int(28 * s)),
            (cx, int(22 * s)),
            (int(46 * s), int(28 * s)),
            (int(38 * s), int(48 * s)),
            (cx, int(40 * s)),
            (int(26 * s), int(48 * s)),
        ], fill=(200, 200, 255))

        # Green dot
        dot_r = int(4 * s)
        draw.ellipse([
            cx - dot_r, int(52 * s) - dot_r,
            cx + dot_r, int(52 * s) + dot_r,
        ], fill=(59, 165, 92))

        images.append(img)

    # Save as .ico with multiple sizes
    images[-1].save(str(ICON_FILE), format='ICO', sizes=[(s, s) for s in sizes])
    print(f"  Icon created: {ICON_FILE}")


def build():
    """Run PyInstaller to create the exe."""

    print("=" * 55)
    print("  Drop Scout — Building Executable")
    print("=" * 55)

    # 1. Generate icon
    print("\n[1/4] Generating icon...")
    create_icon()

    # 2. Clean previous builds
    print("\n[2/4] Cleaning previous builds...")
    for d in [DIST_DIR / "DropScout", BUILD_DIR]:
        if d.exists():
            shutil.rmtree(d)
            print(f"  Removed: {d}")

    # 3. Build PyInstaller command
    print("\n[3/4] Running PyInstaller...")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "DropScout",
        "--noconfirm",
        "--windowed",              # No console window
        "--onedir",                # Folder mode (faster startup, easier patching)
    ]

    # Add icon if available
    if ICON_FILE.exists():
        cmd.extend(["--icon", str(ICON_FILE)])

    # Add data files
    data_files = [
        ("discord_monitor_ui.html", "."),
        ("discord_monitor_server.py", "."),
        ("updater.py", "."),
        ("version.json", "."),
    ]

    # Optional: include config if exists (user might want defaults)
    if (APP_DIR / "monitor_config.json").exists():
        data_files.append(("monitor_config.json", "."))

    for src, dest in data_files:
        src_path = APP_DIR / src
        if src_path.exists():
            cmd.extend(["--add-data", f"{src_path}{os.pathsep}{dest}"])

    # Hidden imports that PyInstaller might miss
    hidden_imports = [
        "discord",
        "discord.ext",
        "discord.ext.commands",
        "aiohttp",
        "multidict",
        "yarl",
        "async_timeout",
        "pystray",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
        "webview",
        "webview.platforms.winforms",  # Windows backend for pywebview
        "clr_loader",
        "pythonnet",
    ]

    for imp in hidden_imports:
        cmd.extend(["--hidden-import", imp])

    # Entry point
    cmd.append(str(APP_DIR / "dropscout_app.py"))

    print(f"  Command: {' '.join(cmd[:6])}...")

    result = subprocess.run(cmd, cwd=str(APP_DIR))

    if result.returncode != 0:
        print("\n  ERROR: PyInstaller failed!")
        print("  Check the output above for errors.")
        sys.exit(1)

    # 4. Copy extra files to dist
    print("\n[4/4] Copying extra files...")

    dist_app = DIST_DIR / "DropScout"
    if dist_app.exists():
        # Copy HTML file (in case --add-data didn't work)
        for f in ["discord_monitor_ui.html", "discord_monitor_server.py",
                   "updater.py", "version.json"]:
            src = APP_DIR / f
            dst = dist_app / f
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
                print(f"  Copied: {f}")

    print("\n" + "=" * 55)
    print("  BUILD COMPLETE!")
    print(f"  Output: {dist_app / 'DropScout.exe'}")
    print("")
    print("  Next steps:")
    print("  1. Test: run dist/DropScout/DropScout.exe")
    print("  2. Build installer: compile dropscout_installer.iss with Inno Setup")
    print("=" * 55)


if __name__ == "__main__":
    build()
