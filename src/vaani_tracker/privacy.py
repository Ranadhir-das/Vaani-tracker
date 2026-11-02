"""Privacy and transparency enforcement for Vaani Desktop Tracker.

STRICT PRIVACY GUARANTEES:
1. NEVER collect keystrokes or typed text.
2. NEVER capture screenshots or video.
3. NEVER access webcam or microphone.
4. NEVER read clipboard contents.
5. NEVER inspect document contents, form inputs, or passwords.
6. NEVER record web browser URLs or web page titles.
7. Only collect foreground executable process names (e.g., 'code.exe') and active durations
   when application tracking is explicitly enabled by policy.
"""

import os
import re
from typing import Dict, Any, List


# Disallowed field names that must never be present in any event or database record
PROHIBITED_DATA_KEYS = {
    "keystroke", "keystrokes", "keys", "typed_text", "text", "screenshot",
    "screen_capture", "image", "frame", "webcam", "camera", "microphone",
    "clipboard", "clipboard_text", "password", "passwords", "url", "urls",
    "uri", "window_title", "title", "document_name", "filename", "tab_title",
}

# Regex to detect URLs or browser web page titles accidentally embedded
URL_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)


class PrivacyViolationError(Exception):
    """Raised if an unpermitted data field or forbidden content is detected."""
    pass


class PrivacyGuard:
    """Enforces strict privacy and transparency filtering on all captured metrics."""

    @staticmethod
    def sanitize_process_name(raw_name: str) -> str:
        """Sanitizes an executable process name.

        - Checks if string is a URL (returns 'browser')
        - Extracts basename only (e.g. 'C:\\Program Files\\VS Code\\Code.exe' -> 'code.exe')
        - Handles spaces in executable names (e.g. 'Premiere Pro.exe')
        - Normalizes to lowercase
        - Strips any command-line flags or arguments
        - Replaces unknown/empty with 'unknown'
        """
        if not raw_name:
            return "unknown"

        raw_str = raw_name.strip()

        # If a URL is passed, sanitize to generic 'browser'
        if URL_PATTERN.search(raw_str):
            return "browser"

        # Normalize path separators and get basename
        basename = raw_str.replace("\\", "/").split("/")[-1].strip()

        # If command-line arguments follow .exe (e.g. "code.exe --arg"), isolate executable
        lower_base = basename.lower()
        if ".exe " in lower_base:
            idx = lower_base.find(".exe")
            basename = basename[:idx + 4]

        cleaned = basename.strip().lower()
        return cleaned if cleaned else "unknown"

    @classmethod
    def audit_payload(cls, payload: Dict[str, Any]) -> None:
        """Audits a dictionary payload to ensure zero privacy violations exist.

        Raises PrivacyViolationError if any prohibited keys or URL-like strings are found.
        """
        for key, value in payload.items():
            lower_key = key.lower()
            for prohibited in PROHIBITED_DATA_KEYS:
                if prohibited in lower_key:
                    raise PrivacyViolationError(
                        f"Strict privacy check failed: Prohibited key '{key}' found in payload."
                    )

            if isinstance(value, dict):
                cls.audit_payload(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        cls.audit_payload(item)
                    elif isinstance(item, str) and URL_PATTERN.search(item):
                        raise PrivacyViolationError(
                            f"Strict privacy check failed: Potential URL detected in value: '{item}'"
                        )
            elif isinstance(value, str) and URL_PATTERN.search(value):
                raise PrivacyViolationError(
                    f"Strict privacy check failed: Potential URL detected in value: '{value}'"
                )

    @classmethod
    def filter_app_usages(cls, raw_apps: List[Dict[str, Any]], track_apps: bool = True) -> List[Dict[str, Any]]:
        """Filters a list of app usage items, discarding them entirely if track_apps is False."""
        if not track_apps:
            return []

        clean_apps = []
        for app in raw_apps:
            raw_proc = app.get("process_name", "")
            clean_name = cls.sanitize_process_name(raw_proc)
            duration = int(app.get("duration_seconds", 0))
            if duration > 0:
                clean_apps.append({
                    "process_name": clean_name,
                    "duration_seconds": duration,
                })
        return clean_apps
