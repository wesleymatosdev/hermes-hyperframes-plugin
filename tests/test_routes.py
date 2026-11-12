"""Route behavior over TestClient with the fake project + stub bin."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from support import DashboardTestBase

PREFIX = "/api/plugins/hermes-hyperframes"


class RouteTestsBase(DashboardTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.project = self.make_fake_project()
        app = FastAPI()
        app.include_router(self.api.router, prefix=PREFIX)
        self.client = TestClient(app)


class HealthTests(RouteTestsBase):
    def test_health_reports_import_time_version(self) -> None:
        resp = self.client.get(PREFIX + "/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["plugin_version"], "0.3.2")


class ProjectRouteTests(RouteTestsBase):
    def test_projects_lists_fake_project_with_facts(self) -> None:
        os.environ["HERMES_HYPERFRAMES_BIN"] = str(
            self.project / "node_modules" / ".bin" / "hyperframes")
        try:
            resp = self.client.get(PREFIX + "/projects")
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(len(body["projects"]), 1)
            row = body["projects"][0]
            self.assertEqual(row["name"], self.project.name)
            self.assertEqual(row["path"], str(self.project))
            self.assertFalse(row["isCurrent"])
            self.assertEqual(row["duration"], 120.0)
            self.assertIn("0 error(s)", row["lint_summary"])
            self.assertIn("has_preview_running", row)
        finally:
            del os.environ["HERMES_HYPERFRAMES_BIN"]

    def test_select_requires_existing_directory(self) -> None:
        resp = self.client.post(PREFIX + "/projects/select",
                                json={"path": "/definitely/not/a/dir"})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["detail"]["kind"], "validation")

    def test_select_requires_composition_qualifiers(self) -> None:
        bare = Path(self._home_token) / "bare"
        bare.mkdir(parents=True)
        resp = self.client.post(PREFIX + "/projects/select", json={"path": str(bare)})
        self.assertEqual(resp.status_code, 422)
        self.assertIn("hyperframes project", resp.json()["detail"]["message"])

    def test_select_and_current_roundtrip(self) -> None:
        resp = self.client.post(PREFIX + "/projects/select",
                                json={"path": str(self.project)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["current"], str(self.project))
        current = self.client.get(PREFIX + "/projects/current").json()
        self.assertEqual(current["current"], str(self.project))


class CliRouteTests(RouteTestsBase):
    """CLI-backed routes with the current project set."""

    def setUp(self) -> None:
        super().setUp()
        os.environ["HERMES_HYPERFRAMES_BIN"] = str(
            self.project / "node_modules" / ".bin" / "hyperframes")
        self.addCleanup(os.environ.pop, "HERMES_HYPERFRAMES_BIN", None)
        self.client.post(PREFIX + "/projects/select", json={"path": str(self.project)})

    def test_cli_routes_without_project_answer_typed_409(self) -> None:
        self.api.kv_delete("current_project")
        for method, path, body in (
            ("get", "/preview/status", None),
            ("get", "/timeline", None),
            ("post", "/snapshot", {}),
            ("post", "/preview", {"action": "start"}),
        ):
            resp = getattr(self.client, method)(PREFIX + path, json=body) if body is not None \
                else getattr(self.client, method)(PREFIX + path)
            self.assertEqual(resp.status_code, 409, path)
            detail = resp.json()["detail"]
            self.assertEqual(detail["kind"], "no_project")

    def test_preview_start_returns_studio_url(self) -> None:
        resp = self.client.post(PREFIX + "/preview", json={"action": "start"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["state"], "started")
        self.assertIn("studioUrl", body)

    def test_preview_status_running_mapping(self) -> None:
        self.client.post(PREFIX + "/preview", json={"action": "start"})
        body = self.client.get(PREFIX + "/preview/status").json()
        self.assertTrue(body["running"])
        self.assertEqual(body["state"], "running")

    def test_preview_stop(self) -> None:
        self.client.post(PREFIX + "/preview", json={"action": "start"})
        body = self.client.post(PREFIX + "/preview", json={"action": "stop"}).json()
        self.assertTrue(body["ok"])
        self.assertFalse(self.client.get(PREFIX + "/preview/status").json()["running"])

    def test_preview_action_validated(self) -> None:
        resp = self.client.post(PREFIX + "/preview", json={"action": "restart"})
        self.assertEqual(resp.status_code, 422)

    def test_timeline_returns_duration(self) -> None:
        body = self.client.get(PREFIX + "/timeline").json()
        self.assertEqual(body["timeline"]["duration"], 120.0)

    def test_snapshot_returns_allowed_png_paths(self) -> None:
        body = self.client.post(PREFIX + "/snapshot", json={}).json()
        self.assertTrue(body["ok"])
        self.assertGreaterEqual(len(body["frames"]), 2)
        self.assertTrue(body["contactSheet"])
        for path in body["frames"] + [body["contactSheet"]]:
            self.assertTrue(Path(path).is_file())

    def test_snapshot_file_serves_png_and_refuses_non_png(self) -> None:
        snap = self.client.post(PREFIX + "/snapshot", json={}).json()
        ok = self.client.get(PREFIX + "/snapshot/file", params={"path": snap["frames"][0]})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.headers["content-type"], "image/png")
        db = self.api.plugin_db_path()
        refused = self.client.get(PREFIX + "/snapshot/file", params={"path": str(db)})
        self.assertEqual(refused.status_code, 404)


class DiscoveryTests(DashboardTestBase):
    def _write_settings_file(self, roots: list) -> None:
        import hermes_cli.config as hc  # type: ignore

        config = {"plugins": {"entries": {self.api.PLUGIN_ID: {
            "settings": {"projects_roots": roots}}}}}
        from hermes_constants import get_hermes_home  # type: ignore

        (Path(get_hermes_home()) / "config.yaml").write_text(json.dumps(config), encoding="utf-8")

    def test_discovery_requires_composition_and_bin(self) -> None:
        root = Path(self._home_token) / "roots"
        good = self.make_fake_project()
        # rebuild `good` under the scan root the settings will name
        root.mkdir(parents=True, exist_ok=True)
        bare = root / "bare"
        bare.mkdir()
        (bare / "index.html").write_text("<html></html>", encoding="utf-8")
        self._write_settings_file([str(root)])
        found = self.api.discover_projects()
        self.assertEqual(found, [])

    def test_discovery_finds_qualifying_dir(self) -> None:
        root = Path(self._home_token) / "roots"
        root.mkdir(parents=True, exist_ok=True)
        good = self.make_fake_project("good-cut")
        moved = root / "good-cut"
        good.rename(moved)
        self._write_settings_file([str(root)])
        found = self.api.discover_projects()
        self.assertEqual([Path(p) for p in found], [moved])


if __name__ == "__main__":
    unittest.main()
