"""Tests for TrackerConfig loading, saving, and environment variable overrides."""

import json
import os
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.config import TrackerConfig


class TestTrackerConfig(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_default_config_creation(self):
        config = TrackerConfig.load(custom_data_dir=self.data_dir)
        self.assertIsNotNone(config.device_uuid)
        self.assertEqual(config.idle_threshold_seconds, 60)
        self.assertEqual(config.sync_interval_seconds, 60)
        self.assertTrue(config.track_apps)
        self.assertTrue(config.config_path.exists())

    def test_persistence_and_reload(self):
        config = TrackerConfig.load(custom_data_dir=self.data_dir)
        config.backend_url = "https://custom.vaani.internal"
        config.idle_threshold_seconds = 120
        config.auth_token = "saved_token_xyz"
        config.registered_employee_id = 99
        config.registered_employee_name = "Video Editor 1"
        config.save()

        # Reload
        reloaded = TrackerConfig.load(custom_data_dir=self.data_dir)
        self.assertEqual(reloaded.backend_url, "https://custom.vaani.internal")
        self.assertEqual(reloaded.idle_threshold_seconds, 120)
        self.assertEqual(reloaded.auth_token, "saved_token_xyz")
        self.assertEqual(reloaded.registered_employee_id, 99)
        self.assertEqual(reloaded.registered_employee_name, "Video Editor 1")

    def test_environment_variable_overrides(self):
        os.environ["VAANI_BACKEND_URL"] = "https://env.vaani.internal"
        os.environ["VAANI_IDLE_THRESHOLD"] = "90"
        os.environ["VAANI_TRACK_APPS"] = "false"

        try:
            config = TrackerConfig.load(custom_data_dir=self.data_dir)
            self.assertEqual(config.backend_url, "https://env.vaani.internal")
            self.assertEqual(config.idle_threshold_seconds, 90)
            self.assertFalse(config.track_apps)
        finally:
            os.environ.pop("VAANI_BACKEND_URL", None)
            os.environ.pop("VAANI_IDLE_THRESHOLD", None)
            os.environ.pop("VAANI_TRACK_APPS", None)


if __name__ == "__main__":
    unittest.main()

