"""PyInstaller automated build runner for Vaani Desktop Tracker.

Executes the authoritative VaaniTracker.spec to compile the desktop tracker
into a standalone Windows onedir distribution under dist/VaaniTracker/.

Usage:
    python build_exe.py [--clean]
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"
ASSETS_DIR = PROJECT_ROOT / "assets"
SPEC_FILE = PROJECT_ROOT / "VaaniTracker.spec"
ENTRY_POINT = SRC_DIR / "vaani_tracker" / "__main__.py"
ICON_PATH = ASSETS_DIR / "vaani.ico"
VERSION_FILE = PROJECT_ROOT / "file_version_info.txt"


def check_prerequisites() -> bool:
    """Verifies that PyInstaller and required source files are present."""
    # 1. Verify PyInstaller is installed
    try:
        import PyInstaller
    except ImportError:
        print("[ERROR] PyInstaller is not installed in the active Python environment.", file=sys.stderr)
        print("Please install requirements: pip install -r requirements-dev.txt", file=sys.stderr)
        return False

    # 2. Verify authoritative spec file exists
    if not SPEC_FILE.exists():
        print(f"[ERROR] Authoritative spec file not found: {SPEC_FILE}", file=sys.stderr)
        return False

    # 3. Verify entry point exists
    if not ENTRY_POINT.exists():
        print(f"[ERROR] Application entry point not found: {ENTRY_POINT}", file=sys.stderr)
        return False

    # 4. Verify assets directory exists
    if not ASSETS_DIR.exists() or not ICON_PATH.exists():
        print(f"[ERROR] Required assets not found in: {ASSETS_DIR}", file=sys.stderr)
        return False

    return True


def build_windows_executable(clean: bool = True) -> int:
    """Compiles the Windows executable by invoking the authoritative VaaniTracker.spec."""
    print("=== Building Vaani Windows Desktop Tracker ===")
    print(f"Project root: {PROJECT_ROOT}")
    print(f"Spec file:    {SPEC_FILE}")

    if not check_prerequisites():
        return 1

    # Clean previous build artifacts
    if clean:
        print("Cleaning previous build and distribution directories...")
        if DIST_DIR.exists():
            shutil.rmtree(DIST_DIR, ignore_errors=True)
        if BUILD_DIR.exists():
            shutil.rmtree(BUILD_DIR, ignore_errors=True)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
    ]
    if clean:
        cmd.append("--clean")
    cmd.append(str(SPEC_FILE))

    print(f"Executing: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        print(f"\n[ERROR] PyInstaller build failed with exit code {result.returncode}", file=sys.stderr)
        return result.returncode

    app_dir = DIST_DIR / "VaaniTracker"
    exe_path = app_dir / "VaaniTracker.exe"
    print("\n[OK] Standalone Onedir Build Completed Successfully!")
    print(f"Distribution Directory: {app_dir}")
    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print(f"Primary Executable:    {exe_path} ({size_mb:.2f} MB)")
    return 0


if __name__ == "__main__":
    clean_flag = "--no-clean" not in sys.argv
    sys.exit(build_windows_executable(clean=clean_flag))
