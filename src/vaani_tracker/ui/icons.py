"""Dynamic tray icon generation for various tracker states."""

from typing import Tuple
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


def get_status_icon_image(state: TrackerState, size: int = 64) -> Image.Image:
    """Generates a high-DPI colored status badge image for the system tray icon."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    color = STATE_COLORS.get(state, (148, 163, 184))

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

