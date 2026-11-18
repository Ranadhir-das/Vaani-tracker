"""Dynamic tray icon generation for various tracker states."""

import sys
from pathlib import Path
from typing import Tuple, Optional
from PIL import Image, ImageDraw

from ..constants import TrackerState


STATE_COLORS = {
    TrackerState.ACTIVE: (16, 185, 129),       # Emerald Green
    TrackerState.IDLE: (245, 158, 11),         # Amber Yellow
    TrackerState.PAUSED: (234, 88, 12),        # Orange
    TrackerState.LOCKED: (59, 130, 246),       # Electric Blue
    TrackerState.SLEEPING: (139, 92, 246),     # Purple
    TrackerState.DISCONNECTED: (100, 116, 139),# Slate
    TrackerState.STOPPED: (148, 163, 184),     # Muted Gray
}


def get_asset_path(filename: str) -> Path:
    """Returns absolute path to an asset file in dev or frozen PyInstaller environment."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent if sys.executable else Path.cwd()

        # Check next to executable (standard onedir distribution)
        candidate_exe = exe_dir / "assets" / filename
        if candidate_exe.exists():
            return candidate_exe

        # Check PyInstaller extraction directory (_MEIPASS, used by onefile or _internal)
        if hasattr(sys, "_MEIPASS"):
            candidate_mei = Path(sys._MEIPASS) / "assets" / filename
            if candidate_mei.exists():
                return candidate_mei
            if (Path(sys._MEIPASS) / filename).exists():
                return Path(sys._MEIPASS) / filename
            return Path(sys._MEIPASS) / "assets" / filename

        return exe_dir / "assets" / filename

    # Source execution: PROJECT_ROOT / assets
    base = Path(__file__).resolve().parents[3] / "assets"
    return base / filename


def get_status_icon_image(state: TrackerState, size: int = 64) -> Image.Image:
    """Generates a high-DPI colored status badge image for the system tray icon."""
    color = STATE_COLORS.get(state, (148, 163, 184))

    icon_path = get_asset_path("vaani-icon.png")
    if icon_path.exists():
        try:
            base = Image.open(icon_path).convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
            draw = ImageDraw.Draw(base)

            # Draw status dot in bottom-right corner
            dot_radius = int(size * 0.22)
            margin = int(size * 0.04)
            cx = size - margin - dot_radius
            cy = size - margin - dot_radius

            # Dark outline for contrast
            outline_width = max(1, int(size * 0.04))
            draw.ellipse(
                [cx - dot_radius, cy - dot_radius, cx + dot_radius, cy + dot_radius],
                fill=color,
                outline=(15, 23, 42, 255),
                width=outline_width,
            )
            return base
        except Exception:
            pass

    # Fallback circular badge
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Outer circle border (dark translucent ring for contrast)
    margin = 4
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=(15, 23, 42, 200),  # Dark navy background
        outline=(255, 255, 255, 60),
        width=2,
    )

    # Inner vibrant indicator dot
    inner_margin = size // 4
    draw.ellipse(
        [inner_margin, inner_margin, size - inner_margin, size - inner_margin],
        fill=color,
    )

    return image

