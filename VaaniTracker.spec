# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification file for Vaani Desktop Tracker.

Builds a self-contained onedir distribution under dist/VaaniTracker/
containing the application executable, assets, and required runtime dependencies.
"""

from pathlib import Path

# SPECPATH is automatically provided by PyInstaller as the directory containing this spec file
ROOT_DIR = Path(SPECPATH).resolve()
SRC_DIR = ROOT_DIR / "src"
ASSETS_DIR = ROOT_DIR / "assets"
VERSION_FILE = ROOT_DIR / "file_version_info.txt"
ICON_PATH = ASSETS_DIR / "vaani.ico"
ENTRY_POINT = SRC_DIR / "vaani_tracker" / "__main__.py"

a = Analysis(
    [str(ENTRY_POINT)],
    pathex=[str(SRC_DIR)],
    binaries=[],
    datas=[
        (str(ASSETS_DIR), "assets"),
    ],
    hiddenimports=[
        # Core & Security
        "vaani_tracker",
        "vaani_tracker.__main__",
        "vaani_tracker.app",
        "vaani_tracker.config",
        "vaani_tracker.constants",
        "vaani_tracker.models",
        "vaani_tracker.privacy",
        "vaani_tracker.security",
        "vaani_tracker.security.dpapi",
        "vaani_tracker.storage.queue",
        "vaani_tracker.client.api",
        "vaani_tracker.client.auth",
        "vaani_tracker.core.aggregator",
        "vaani_tracker.core.state_machine",
        "vaani_tracker.core.syncer",
        "vaani_tracker.windows.win32",
        "vaani_tracker.windows.session",
        "vaani_tracker.windows.power",
        "vaani_tracker.windows.apps",
        "vaani_tracker.windows.idle",
        # UI & System Tray
        "vaani_tracker.ui",
        "vaani_tracker.ui.dashboard",
        "vaani_tracker.ui.first_run",
        "vaani_tracker.ui.icons",
        "vaani_tracker.ui.summary",
        "vaani_tracker.ui.tray",
        # 3rd-Party & Standard Libraries
        "pystray",
        "pystray._win32",
        "PIL",
        "PIL._imagingtk",
        "PIL.ImageTk",
        "PIL.ImageDraw",
        "tkinter",
        "tkinter.ttk",
        "tkinter.scrolledtext",
        "tkinter.messagebox",
        "sqlite3",
        "ctypes",
        "ctypes.wintypes",
        "winreg",
        "urllib.request",
        "urllib.error",
        "urllib.parse",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VaaniTracker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(VERSION_FILE) if VERSION_FILE.exists() else None,
    icon=str(ICON_PATH) if ICON_PATH.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VaaniTracker",
)
