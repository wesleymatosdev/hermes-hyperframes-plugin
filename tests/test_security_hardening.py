"""Security hardening (catalog review feedback on d49838bc).

RED-first against v0.3.1, one test per review requirement:

- project_dir must be confined to the configured ``projects_roots`` — the
  project-local ``node_modules/.bin/hyperframes`` fallback runs a repo's own
  binary on the host, so a cloned, untrusted repo must never qualify just
  because the model named its directory.
- the CLI subprocess gets a minimal environment (PATH/HOME/locale/TMPDIR) —
  never ``dict(os.environ)`` with every provider key in it.
- ``hyperframes_render`` output must resolve INSIDE the project dir (``..``,
  outside absolute paths and symlink escapes refused) and must never
  overwrite an existing non-video file.
- ``API_SERVER_KEY`` is read through Hermes's secret API
  (``agent.secret_scope.get_secret``), never by parsing ``.env`` by hand
  (covered in test_native_autoderive).
"""

from __future__ import annotations

import json
import os
import stat
import sys
import unittest
from pathlib import Path

from support import DashboardTestBase


def _write_settings(settings: dict) -> None:
    """Give the plugin config a real ``<home>/config.yaml`` (test home is
    isolated; hermes_cli reads HERMES_HOME when set)."""
    import hermes_cli.config as hc  # noqa: F401  (importability probe)

    from hermes_constants import get_hermes_home  # type: ignore

    home = Path(get_hermes_home())
    config = {"plugins": {"entries": {"hermes-hyperframes": {"settings": settings}}}}
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")


def _make_project(path: Path, name: str = "cut") -> Path:
    """A qualifying hyperframes project with a project-local stub bin, at an
    arbitrary path (support.make_fake_project pins <home>/projects)."""
    (path / "node_modules" / ".bin").mkdir(parents=True, exist_ok=True)
    (path / "index.html").write_text(
        '<html><div data-composition-id="%s"></div></html>' % name, encoding="utf-8")
    stub = path / "node_modules" / ".bin" / "hyperframes"
    from support import STUB_BIN

    stub.write_text(STUB_BIN, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return path


class ProjectDirConfinementTests(DashboardTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.allowed_root = Path(self._home_token) / "allowed-roots"
        self.allowed_root.mkdir(parents=True, exist_ok=True)
        _write_settings({"projects_roots": [str(self.allowed_root)]})

    def test_project_outside_roots_refused(self) -> None:
        outside = _make_project(Path(self._home_token) / "untrusted-clone" / "cut")
        result = json.loads(self.api._TOOLS.h_lint({"project_dir": str(outside)}))
        self.assertFalse(result["success"])
        self.assertIn("projects_roots", result["error"])

    def test_project_inside_roots_still_works(self) -> None:
        inside = _make_project(self.allowed_root / "trusted-cut")
        result = json.loads(self.api._TOOLS.h_lint({"project_dir": str(inside)}))
        # The stub lint ran against the allowed project (summary text proves
        # the project-local bin executed; rc-0-with-warnings is data, not a gate).
        self.assertIn("STUB-LINT", result.get("output", ""))
        self.assertEqual(result.get("project"), str(inside))

    def test_project_local_bin_untouched_outside_roots(self) -> None:
        """The refusal happens before any binary lookup: the untrusted clone's
        own stub bin must never execute (its lint prints STUB-LINT)."""
        outside = _make_project(Path(self._home_token) / "untrusted-clone2" / "cut")
        result = json.loads(self.api._TOOLS.h_lint({"project_dir": str(outside)}))
        self.assertNotIn("STUB-LINT", result.get("output", ""))

    def test_dashboard_select_refuses_outside_roots(self) -> None:
        outside = _make_project(Path(self._home_token) / "untrusted-clone3" / "cut")
        from fastapi import HTTPException

        from plugin_api import SelectBody, projects_select

        with self.assertRaises(HTTPException) as ctx:
            projects_select(SelectBody(path=str(outside)))
        self.assertEqual(ctx.exception.status_code, 422)


class MinimalSubprocessEnvTests(DashboardTestBase):
    def test_canary_env_var_does_not_reach_cli(self) -> None:
        project = self.make_fake_project()
        os.environ["HF_CANARY"] = "do-not-leak"
        try:
            rc, out = self.api._TOOLS._run(
                [sys.executable, "-c", "import os; print(os.environ.get('HF_CANARY', 'ABSENT'))"],
                str(project), 30)
        finally:
            os.environ.pop("HF_CANARY", None)
        self.assertEqual(rc, 0)
        self.assertIn("ABSENT", out)
        self.assertNotIn("do-not-leak", out)

    def test_cli_env_keeps_path_home_and_tmpdir(self) -> None:
        project = self.make_fake_project()
        rc, out = self.api._TOOLS._run(
            [sys.executable, "-c",
             "import os; print(os.environ.get('PATH', 'NO-PATH'),"
             " os.environ.get('HOME', 'NO-HOME'),"
             " os.environ.get('TMPDIR', 'NO-TMPDIR'))"],
            str(project), 30)
        self.assertEqual(rc, 0)
        self.assertNotIn("NO-PATH", out)
        self.assertNotIn("NO-HOME", out)
        self.assertNotIn("NO-TMPDIR", out)


class RenderOutputConfinementTests(DashboardTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.project = self.make_fake_project()

    def _render(self, output: str) -> dict:
        return json.loads(self.api._TOOLS.h_render({"project_dir": str(self.project),
                                                    "output": output, "timeout_s": 30}))

    def test_parent_escape_refused(self) -> None:
        result = self._render("../escape.mp4")
        self.assertFalse(result["success"])
        self.assertIn("inside the project", result["error"])
        self.assertFalse((self.project.parent / "escape.mp4").exists())

    def test_absolute_outside_path_refused(self) -> None:
        target = Path(self._home_token) / "outside.mp4"
        result = self._render(str(target))
        self.assertFalse(result["success"])
        self.assertFalse(target.exists())

    def test_symlink_escape_refused(self) -> None:
        link = self.project / "sneaky-dir"
        link.symlink_to(self._home_token, target_is_directory=True)
        result = self._render("sneaky-dir/escape.mp4")
        self.assertFalse(result["success"])
        self.assertFalse((Path(self._home_token) / "escape.mp4").exists())

    def test_inside_render_allowed(self) -> None:
        result = self._render("renders/out.mp4")
        self.assertTrue(result["success"], result)
        self.assertTrue((self.project / "renders" / "out.mp4").is_file())

    def test_existing_non_video_file_refused(self) -> None:
        victim = self.project / "notes.txt"
        victim.write_text("do not clobber", encoding="utf-8")
        result = self._render("notes.txt")
        self.assertFalse(result["success"])
        self.assertIn("non-video", result["error"])
        self.assertEqual(victim.read_text(encoding="utf-8"), "do not clobber")


if __name__ == "__main__":
    unittest.main()
