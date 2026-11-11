"""Config resolution, manifest version, and tools-module layout discovery."""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import DashboardTestBase, DASHBOARD_DIR, REPO_ROOT


class ManifestVersionTests(DashboardTestBase):
    def test_version_read_from_manifest_next_to_module(self) -> None:
        self.assertEqual(self.api.PLUGIN_VERSION, "0.3.1")

    def test_broken_manifest_yields_empty_version_not_crash(self) -> None:
        import shutil

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes-hyperframes"
            (root / "dashboard").mkdir(parents=True)
            (root / "dashboard" / "manifest.json").write_text("{not json", encoding="utf-8")
            (root / "__init__.py").write_text("", encoding="utf-8")
            shutil.copy(DASHBOARD_DIR / "plugin_api.py", root / "dashboard" / "plugin_api.py")
            sys.path.insert(0, str(root / "dashboard"))
            try:
                name = "plugin_api_broken_manifest_probe"
                sys.modules.pop(name, None)
                spec = importlib.util.spec_from_file_location(
                    name, root / "dashboard" / "plugin_api.py")
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
                self.assertEqual(module.PLUGIN_VERSION, "")
            finally:
                sys.path.remove(str(root / "dashboard"))
                sys.modules.pop(name, None)


class SettingsTests(DashboardTestBase):
    def _write_settings(self, settings: dict) -> None:
        config = {"plugins": {"entries": {self.api.PLUGIN_ID: {"settings": settings}}}}
        # hermes_cli.config.load_config_readonly reads <home>/config.yaml — the
        # dashboard module wraps it best-effort; when hermes_cli is importable
        # in this venv, give it a real file. Otherwise settings stay empty and
        # the defaults below assert the fallback path.
        import hermes_cli.config as hc  # type: ignore

        try:
            from hermes_constants import get_hermes_home  # type: ignore

            home = Path(get_hermes_home())
        except Exception:
            home = Path.home() / ".hermes"
        (home / "config.yaml").write_text(
            json.dumps(config), encoding="utf-8")

    def test_projects_roots_defaults_when_unconfigured(self) -> None:
        roots = self.api.projects_roots()
        self.assertEqual(
            [str(p) for p in roots],
            [os.path.expanduser(p) for p in self.api.DEFAULT_PROJECTS_ROOTS])

    def test_projects_roots_list_setting_and_tilde_expansion(self) -> None:
        self._write_settings({"projects_roots": ["~/Movies", "/tmp"]})
        roots = self.api.projects_roots()
        self.assertEqual([str(p) for p in roots], [os.path.expanduser("~/Movies"), "/tmp"])

    def test_projects_roots_comma_string_setting(self) -> None:
        self._write_settings({"projects_roots": "/tmp,/var"})
        roots = self.api.projects_roots()
        self.assertEqual([str(p) for p in roots], ["/tmp", "/var"])

    def test_missing_dirs_dropped_from_roots(self) -> None:
        self._write_settings({"projects_roots": ["/tmp", "/definitely/not/here"]})
        roots = self.api.projects_roots()
        self.assertEqual([str(p) for p in roots], ["/tmp"])

    def test_api_server_url_config_then_env_then_default(self) -> None:
        self.assertEqual(self.api.api_server_url(), "http://127.0.0.1:8642")
        os.environ["HERMES_API_SERVER_URL"] = "http://127.0.0.1:9999/"
        try:
            self.assertEqual(self.api.api_server_url(), "http://127.0.0.1:9999")
        finally:
            del os.environ["HERMES_API_SERVER_URL"]

    def test_api_server_key_config_wins_over_env(self) -> None:
        os.environ["HERMES_API_SERVER_KEY"] = "env-key"
        try:
            self.assertEqual(self.api.api_server_key(), "env-key")
            self._write_settings({"api_server_key": "config-key"})
            self.assertEqual(self.api.api_server_key(), "config-key")
        finally:
            del os.environ["HERMES_API_SERVER_KEY"]


class ToolsModuleLayoutTests(unittest.TestCase):
    """The backend imports the tools __init__.py by file path in BOTH layouts."""

    def _import_from_layout(self, dashboard_dir: Path) -> object:
        sys.path.insert(0, str(dashboard_dir))
        try:
            name = "plugin_api_layout_probe"
            sys.modules.pop(name, None)
            spec = importlib.util.spec_from_file_location(name, dashboard_dir / "plugin_api.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module
        finally:
            sys.path.remove(str(dashboard_dir))

    def test_repo_layout_finds_plugin_init(self) -> None:
        module = self._import_from_layout(DASHBOARD_DIR)
        self.assertIsNotNone(module._TOOLS)
        self.assertTrue(hasattr(module._TOOLS, "h_preview"))

    def test_installed_flat_layout_finds_flattened_init(self) -> None:
        import shutil

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes-hyperframes"
            (root / "dashboard" / "dist").mkdir(parents=True)
            (root / "dashboard" / "manifest.json").write_text(
                json.dumps({"version": "9.9.9"}), encoding="utf-8")
            shutil.copy(REPO_ROOT / "plugin" / "__init__.py", root / "__init__.py")
            shutil.copy(REPO_ROOT / "dashboard" / "plugin_api.py",
                        root / "dashboard" / "plugin_api.py")
            module = self._import_from_layout(root / "dashboard")
            self.assertIsNotNone(module._TOOLS)
            self.assertTrue(hasattr(module._TOOLS, "h_snapshot"))


if __name__ == "__main__":
    unittest.main()
