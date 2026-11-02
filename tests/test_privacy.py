"""Tests for strict privacy guard and data sanitization."""

import unittest
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.privacy import PrivacyGuard, PrivacyViolationError


class TestPrivacyGuard(unittest.TestCase):

    def test_sanitize_process_name_removes_paths(self):
        cases = [
            ("C:\\Program Files\\Microsoft VS Code\\Code.exe", "code.exe"),
            ("C:\\Windows\\System32\\notepad.exe", "notepad.exe"),
            ("E:/Adobe/Premiere Pro.exe", "premiere pro.exe"),
            ("/usr/bin/python3", "python3"),
            ("Slack.exe", "slack.exe"),
        ]
        for raw, expected in cases:
            self.assertEqual(PrivacyGuard.sanitize_process_name(raw), expected)

    def test_sanitize_process_name_strips_urls_and_titles(self):
        # Even if window title or URL is passed by mistake, it is sanitized
        url_input = "https://mail.google.com/mail/u/0/#inbox"
        self.assertEqual(PrivacyGuard.sanitize_process_name(url_input), "browser")

        empty_input = ""
        self.assertEqual(PrivacyGuard.sanitize_process_name(empty_input), "unknown")

    def test_audit_payload_passes_clean_data(self):
        clean_payload = {
            "bucket_id": "test-uuid-1234",
            "active_seconds": 45,
            "idle_seconds": 15,
            "state": "ACTIVE",
            "app_usages": [
                {"process_name": "code.exe", "duration_seconds": 30},
                {"process_name": "chrome.exe", "duration_seconds": 15},
            ]
        }
        # Should not raise any error
        try:
            PrivacyGuard.audit_payload(clean_payload)
        except PrivacyViolationError:
            self.fail("audit_payload unexpectedly raised PrivacyViolationError on clean payload.")

    def test_audit_payload_rejects_prohibited_keys(self):
        forbidden_keys = [
            "keystrokes", "typed_text", "screenshot", "window_title",
            "document_name", "clipboard", "password", "url"
        ]
        for key in forbidden_keys:
            bad_payload = {
                "bucket_id": "123",
                key: "Sensitive customer data or document title",
            }
            with self.assertRaises(PrivacyViolationError, msg=f"Should reject key '{key}'"):
                PrivacyGuard.audit_payload(bad_payload)

    def test_audit_payload_rejects_embedded_urls(self):
        bad_payload = {
            "bucket_id": "123",
            "notes": "User visited https://intranet.company.com/payroll",
        }
        with self.assertRaises(PrivacyViolationError):
            PrivacyGuard.audit_payload(bad_payload)

    def test_filter_app_usages_respects_tracking_switch(self):
        raw_apps = [
            {"process_name": "C:\\VSCode\\code.exe", "duration_seconds": 40},
            {"process_name": "chrome.exe", "duration_seconds": 20},
        ]
        # When track_apps is False, must return empty list
        disabled_result = PrivacyGuard.filter_app_usages(raw_apps, track_apps=False)
        self.assertEqual(disabled_result, [])

        # When track_apps is True, must return sanitized list
        enabled_result = PrivacyGuard.filter_app_usages(raw_apps, track_apps=True)
        self.assertEqual(len(enabled_result), 2)
        self.assertEqual(enabled_result[0]["process_name"], "code.exe")
        self.assertEqual(enabled_result[0]["duration_seconds"], 40)
        self.assertEqual(enabled_result[1]["process_name"], "chrome.exe")


if __name__ == "__main__":
    unittest.main()

