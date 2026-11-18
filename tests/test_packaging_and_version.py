"""Regression tests for centralized versioning, entry point, asset resolution, and PyInstaller configuration."""

import importlib
import os
import sys
import tempfile
from pathlib import Path
import pytest

# Ensure src is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import vaani_tracker
from vaani_tracker.constants import APP_VERSION
from vaani_tracker.client.api import VaaniApiClient
from vaani_tracker.ui.icons import get_asset_path


class TestCanonicalVersion:
    """Verifies that all components read version from the canonical definition."""

    def test_package_version_matches_constant(self):
        assert APP_VERSION == "1.0.0"
        assert vaani_tracker.__version__ == APP_VERSION

    def test_api_client_default_version(self):
        client = VaaniApiClient("http://127.0.0.1:8000")
        assert client.client_version == APP_VERSION
        assert f"VaaniDesktopTracker/{APP_VERSION}" in client.user_agent

    def test_dashboard_version_consistency(self, tmp_path):
        from unittest.mock import MagicMock
        from vaani_tracker.config import TrackerConfig
        from vaani_tracker.ui.dashboard import DashboardViewModel
        from vaani_tracker.storage.queue import ActivityQueue
        from vaani_tracker.client.auth import DeviceAuthManager

        config = TrackerConfig(data_dir=tmp_path)
        queue = ActivityQueue(tmp_path / "events.db")
        sm = MagicMock()
        syncer = MagicMock()
        auth = DeviceAuthManager(config, queue)
        vm = DashboardViewModel(config, sm, syncer, queue, auth)

        info = vm.get_device_account()
        assert info["client_version"] == APP_VERSION

    def test_installer_and_version_file_consistency(self):
        iss_path = PROJECT_ROOT / "vaani_tracker.iss"
        if iss_path.exists():
            content = iss_path.read_text(encoding="utf-8")
            assert f'#define MyAppVersion "{APP_VERSION}"' in content

        ver_file = PROJECT_ROOT / "file_version_info.txt"
        if ver_file.exists():
            content = ver_file.read_text(encoding="utf-8")
            assert f"'{APP_VERSION}.0'" in content or f'"{APP_VERSION}.0"' in content


class TestProductionEntryPoint:
    """Verifies the production entry point and CLI parsing."""

    def test_main_module_imports_cleanly(self):
        import vaani_tracker.__main__ as entry_module
        assert hasattr(entry_module, "main")
        assert hasattr(entry_module, "parse_args")
        assert entry_module.APP_VERSION == APP_VERSION

    def test_main_cli_argument_parsing(self):
        from vaani_tracker.__main__ import parse_args
        test_args = [
            "vaani-tracker",
            "--register", "ABC123",
            "--backend", "https://vaaniapp.co.in",
            "--idle-threshold", "90",
            "--headless",
            "--verbose",
        ]
        import sys
        orig_argv = sys.argv
        try:
            sys.argv = test_args
            parsed = parse_args()
            assert parsed.register == "ABC123"
            assert parsed.backend == "https://vaaniapp.co.in"
            assert parsed.idle_threshold == 90
            assert parsed.headless is True
            assert parsed.verbose is True
        finally:
            sys.argv = orig_argv


class TestPyInstallerSpecPortability:
    """Verifies that VaaniTracker.spec is portable and configured for onedir."""

    def test_spec_has_no_hardcoded_paths(self):
        spec_path = PROJECT_ROOT / "VaaniTracker.spec"
        assert spec_path.exists(), "VaaniTracker.spec must exist"
        content = spec_path.read_text(encoding="utf-8")

        # Must not contain hardcoded machine paths
        assert "E:/" not in content
        assert "E:\\" not in content
        assert "C:/" not in content
        assert "C:\\" not in content

        # Must reference SPECPATH
        assert "SPECPATH" in content

    def test_spec_targets_production_entry_point(self):
        spec_path = PROJECT_ROOT / "VaaniTracker.spec"
        content = spec_path.read_text(encoding="utf-8")
        assert "__main__.py" in content
        assert "run_dev.py" not in content

    def test_spec_onedir_configuration(self):
        spec_path = PROJECT_ROOT / "VaaniTracker.spec"
        content = spec_path.read_text(encoding="utf-8")
        # Onedir mode in PyInstaller uses COLLECT
        assert "coll = COLLECT(" in content
        assert "exclude_binaries=True" in content
        assert "name='VaaniTracker'" in content or 'name="VaaniTracker"' in content

    def test_spec_includes_critical_hidden_imports(self):
        spec_path = PROJECT_ROOT / "VaaniTracker.spec"
        content = spec_path.read_text(encoding="utf-8")
        assert "vaani_tracker.security.dpapi" in content
        assert "vaani_tracker.windows.win32" in content
        assert "pystray" in content
        assert "sqlite3" in content


class TestAssetResolution:
    """Verifies asset path resolution across source, onedir, and onefile modes."""

    def test_source_mode_resolution(self):
        # In non-frozen dev mode, assets resolve to repository assets/
        ico_path = get_asset_path("vaani.ico")
        assert ico_path.exists(), f"Asset vaani.ico must exist at {ico_path}"
        png_path = get_asset_path("vaani-icon.png")
        assert png_path.exists(), f"Asset vaani-icon.png must exist at {png_path}"

    def test_simulated_onedir_resolution(self, tmp_path, monkeypatch):
        # Simulate frozen executable in onedir mode: dist/VaaniTracker/VaaniTracker.exe
        fake_exe = tmp_path / "VaaniTracker.exe"
        fake_exe.touch()
        assets_dir = tmp_path / "assets"
        assets_dir.mkdir()
        fake_icon = assets_dir / "test_icon.png"
        fake_icon.write_text("fake_png_data")

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))
        # Ensure _MEIPASS does not point to our test asset
        if hasattr(sys, "_MEIPASS"):
            monkeypatch.delattr(sys, "_MEIPASS")

        resolved = get_asset_path("test_icon.png")
        assert resolved == fake_icon
        assert resolved.exists()

    def test_simulated_onefile_resolution(self, tmp_path, monkeypatch):
        # Simulate frozen executable in onefile mode: %TEMP%/_MEIxxxxxx
        mei_assets = tmp_path / "assets"
        mei_assets.mkdir()
        fake_logo = mei_assets / "test_logo.png"
        fake_logo.write_text("fake_logo_data")

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        # Point executable to an empty directory without assets
        monkeypatch.setattr(sys, "executable", str(tmp_path / "bin" / "app.exe"))

        resolved = get_asset_path("test_logo.png")
        assert resolved == fake_logo
        assert resolved.exists()


class TestBuildRunnerConfiguration:
    """Verifies build_exe.py architecture without executing PyInstaller."""

    def test_build_exe_prerequisites(self):
        import build_exe
        # Prerequisites check should pass on the dev machine
        assert build_exe.check_prerequisites() is True

    def test_build_exe_missing_spec_check(self, monkeypatch, tmp_path):
        import build_exe
        non_existent_spec = tmp_path / "NonExistent.spec"
        monkeypatch.setattr(build_exe, "SPEC_FILE", non_existent_spec)
        assert build_exe.check_prerequisites() is False

    def test_build_exe_invokes_spec_directly(self):
        build_exe_path = PROJECT_ROOT / "build_exe.py"
        content = build_exe_path.read_text(encoding="utf-8")
        # Must invoke SPEC_FILE, not raw PyInstaller --name flags that regenerate spec
        assert "str(SPEC_FILE)" in content
        assert "--name" not in content
        assert "--paths" not in content


class TestInnoSetupConfiguration:
    """Statically verifies Inno Setup configuration (vaani_tracker.iss) rules."""

    @pytest.fixture
    def iss_content(self) -> str:
        iss_path = PROJECT_ROOT / "vaani_tracker.iss"
        assert iss_path.exists(), "vaani_tracker.iss must exist"
        return iss_path.read_text(encoding="utf-8")

    def test_per_user_only_privilege_model_and_no_allusers_override(self, iss_content):
        # 1. Strictly per-user installation without elevation
        assert "PrivilegesRequired=lowest" in iss_content

        # 2. Absence of /ALLUSERS override
        assert "PrivilegesRequiredOverridesAllowed" not in iss_content
        assert "/ALLUSERS" not in iss_content

        # 3. Default directory resolves to {userpf} under lowest
        assert "DefaultDirName={autopf}\\VaaniTracker" in iss_content

        # 4. No machine-wide registry integration
        assert "HKLM" not in iss_content
        assert "HKEY_LOCAL_MACHINE" not in iss_content

    def test_app_mutex_matches_canonical_application_mutex(self, iss_content):
        from vaani_tracker.constants import APP_MUTEX_NAME
        assert f"AppMutex={APP_MUTEX_NAME}" in iss_content
        assert "CloseApplications=no" in iss_content

    def test_onedir_packaging_structure(self, iss_content):
        # Must package onedir recursive distribution
        assert 'Source: "dist\\VaaniTracker\\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs' in iss_content
        # Must package assets directory
        assert 'Source: "assets\\*"; DestDir: "{app}\\assets"; Flags: ignoreversion recursesubdirs createallsubdirs' in iss_content
        # Must package documentation and example config
        assert 'Source: "config.example.json"; DestDir: "{app}"; Flags: ignoreversion' in iss_content
        assert 'Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion' in iss_content
        assert 'Source: "PRIVACY.md"; DestDir: "{app}"; Flags: ignoreversion' in iss_content

        # Must not package active databases, employee tokens, or dev artifacts
        assert "events.db" not in iss_content
        assert "config.json\"" not in iss_content
        assert ".venv" not in iss_content
        assert "credentials" not in iss_content

    def test_dashboard_and_installer_startup_registry_alignment(self, iss_content):
        # Verify exact alignment with dashboard.py key and value name
        expected_subkey = r"Software\Microsoft\Windows\CurrentVersion\Run"
        expected_val = "VaaniTracker"

        assert f'Subkey: "{expected_subkey}"' in iss_content
        assert f'ValueName: "{expected_val}"' in iss_content
        assert 'Root: HKCU;' in iss_content

        # Exactly ONE single auditable [Registry] entry
        lines = [
            line.strip() for line in iss_content.splitlines()
            if line.strip().startswith("Root: HKCU") and expected_val in line
        ]
        assert len(lines) == 1, f"Expected exactly 1 registry entry for {expected_val}, found {len(lines)}: {lines}"

        # Must specify uninsdeletevalue and check function
        entry = lines[0]
        assert "Flags: uninsdeletevalue" in entry
        assert "Check: ShouldSetStartup" in entry

    def test_startup_fresh_install_and_upgrade_preservation_logic(self, iss_content):
        # Task is unchecked by default
        assert 'Name: "startupreg"; Description: "Launch Vaani Tracker automatically when Windows starts"; GroupDescription: "Windows Integration:"; Flags: unchecked' in iss_content

        # Global variable for early detection
        assert "LegacyStartupShortcutFound: Boolean;" in iss_content

        # Early detection in InitializeWizard before [InstallDelete] executes
        assert "procedure InitializeWizard;" in iss_content
        assert "LegacyStartupShortcutFound := FileExists(ExpandConstant('{userstartup}\\{#MyAppName}.lnk'));" in iss_content

        # Fallback in CurStepChanged(ssInstall) immediately before [InstallDelete] executes
        assert "procedure CurStepChanged(CurStep: TSetupStep);" in iss_content
        assert "if CurStep = ssInstall then" in iss_content

        # Logic checks task selection OR existing registry setting OR captured legacy shortcut
        assert "function ShouldSetStartup: Boolean;" in iss_content
        assert "WizardIsTaskSelected('startupreg')" in iss_content
        assert "RegValueExists(HKEY_CURRENT_USER, 'Software\\Microsoft\\Windows\\CurrentVersion\\Run', 'VaaniTracker')" in iss_content
        assert "LegacyStartupShortcutFound" in iss_content

    def test_uninstall_removes_only_vaanitracker_startup_value(self, iss_content):
        # Must clean up the specific value in uninstaller without deleting the Run key
        assert "uninsdeletekey" not in iss_content
        assert "procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);" in iss_content
        assert "usPostUninstall" in iss_content
        assert "RegDeleteValue(HKEY_CURRENT_USER, 'Software\\Microsoft\\Windows\\CurrentVersion\\Run', 'VaaniTracker')" in iss_content

    def test_legacy_startup_shortcut_migration_narrowly_scoped(self, iss_content):
        # [Icons] must NOT create any startup shortcut
        assert "{userstartup}" not in iss_content.split("[Icons]")[1].split("[Registry]")[0]

        # [InstallDelete] must narrowly remove only the old installer shortcut
        assert "[InstallDelete]" in iss_content
        install_delete_section = iss_content.split("[InstallDelete]")[1].split("[Files]")[0]
        assert 'Type: files; Name: "{userstartup}\\{#MyAppName}.lnk"' in install_delete_section

        # No general startup directory deletion
        assert "filesandordirs" not in install_delete_section
        assert "{userstartup}\\*" not in install_delete_section

    def test_runtime_data_directory_remains_outside_installer_cleanup(self, iss_content):
        # Must not contain destructive UninstallDelete targeting localappdata or runtime dir
        assert "[UninstallDelete]" not in iss_content
        assert 'DestDir: "{localappdata}' not in iss_content
        assert 'DestDir: "%LOCALAPPDATA%' not in iss_content
        assert 'Name: "{localappdata}\\VaaniTracker' not in iss_content
        assert 'Name: "%LOCALAPPDATA%\\VaaniTracker' not in iss_content

    def test_stable_identity_and_version(self, iss_content):
        assert "AppId={{5E65D7B2-C1F8-4A11-8075-8D6F39F80A90}" in iss_content
        assert f'#define MyAppVersion "{APP_VERSION}"' in iss_content
        assert 'OutputBaseFilename=VaaniTrackerSetup' in iss_content
        assert "SetupLogging=yes" in iss_content
        assert "Uninstallable=yes" in iss_content


class TestStartupDecisionModel:
    """Tests the mathematical and logical decision model implemented in Inno Setup ShouldSetStartup."""

    @staticmethod
    def should_set_startup(task_selected: bool, reg_exists: bool, legacy_shortcut_found: bool) -> bool:
        """Mirror implementation of Inno Setup ShouldSetStartup Pascal logic."""
        return bool(task_selected or reg_exists or legacy_shortcut_found)

    def test_fresh_install_disabled_by_default(self):
        # Fresh install: task unchecked, no registry entry, no legacy shortcut
        assert self.should_set_startup(task_selected=False, reg_exists=False, legacy_shortcut_found=False) is False

    def test_fresh_install_explicit_task_enabled(self):
        # Fresh install: employee checks startupreg task
        assert self.should_set_startup(task_selected=True, reg_exists=False, legacy_shortcut_found=False) is True

    def test_upgrade_with_legacy_shortcut_only_migrates_preference(self):
        # Upgrade from old installer: task unchecked, no registry entry yet, legacy shortcut was on disk
        assert self.should_set_startup(task_selected=False, reg_exists=False, legacy_shortcut_found=True) is True

    def test_upgrade_with_registry_only_preserves_preference(self):
        # Upgrade from newer installer: task unchecked, registry entry exists, no legacy shortcut
        assert self.should_set_startup(task_selected=False, reg_exists=True, legacy_shortcut_found=False) is True

    def test_upgrade_with_both_registry_and_legacy_shortcut(self):
        # Edge case: both exist
        assert self.should_set_startup(task_selected=False, reg_exists=True, legacy_shortcut_found=True) is True

    def test_upgrade_previously_disabled_remains_disabled(self):
        # Upgrade where startup was previously disabled (no registry, no shortcut, task unchecked)
        assert self.should_set_startup(task_selected=False, reg_exists=False, legacy_shortcut_found=False) is False

    def test_absent_legacy_shortcut_does_not_enable_by_itself(self):
        # Confirm that an absent shortcut cannot trigger startup
        assert self.should_set_startup(task_selected=False, reg_exists=False, legacy_shortcut_found=False) is False

    def test_full_truth_table_invariants(self):
        # Exhaustive 3-variable truth table verification (2^3 = 8 states)
        truth_table = [
            # (task, reg, legacy) -> expected
            (False, False, False, False),
            (False, False, True,  True),
            (False, True,  False, True),
            (False, True,  True,  True),
            (True,  False, False, True),
            (True,  False, True,  True),
            (True,  True,  False, True),
            (True,  True,  True,  True),
        ]
        for task, reg, legacy, expected in truth_table:
            actual = self.should_set_startup(task, reg, legacy)
            assert actual is expected, f"Failed for state (task={task}, reg={reg}, legacy={legacy})"



