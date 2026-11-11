"""Native chat-lane auto-derivation (feedback round 2).

RED-first: on a machine with a running gateway, the plugin must resolve the
native lane with ZERO manual configuration — URL from
``<home>/gateway_state.json`` (platforms.api_server.listener_base) and the
key from ``<home>/.env`` API_SERVER_KEY (dotenv-style). Plugin settings and
env keep precedence for other machines. These tests fail against the old
resolution order (settings + env only).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from support import DashboardTestBase


class AutoDeriveTests(DashboardTestBase):
    def _write_gateway_state(self, listener_base: str) -> None:
        from hermes_constants import get_hermes_home  # type: ignore

        home = Path(get_hermes_home())
        (home / "gateway_state.json").write_text(json.dumps({
            "platforms": {"api_server": {"listener_base": listener_base}},
        }), encoding="utf-8")

    def _write_dotenv(self, key: str, value: str) -> None:
        from hermes_constants import get_hermes_home  # type: ignore

        home = Path(get_hermes_home())
        (home / ".env").write_text(
            "# gateway credentials\n"
            "OTHER_KEY=untouched\n"
            f'{key}="{value}"\n',
            encoding="utf-8")

    def test_key_derives_from_home_dotenv_without_settings_or_env(self) -> None:
        self._write_dotenv("API_SERVER_KEY", "auto-derived-secret")
        self.assertEqual(self.api.api_server_key(), "auto-derived-secret")

    def test_url_derives_from_gateway_state_listener_base(self) -> None:
        self._write_gateway_state("http://127.0.0.1:8777")
        self.assertEqual(self.api.api_server_url(), "http://127.0.0.1:8777")

    def test_url_falls_back_to_default_port_without_state(self) -> None:
        self.assertEqual(self.api.api_server_url(), "http://127.0.0.1:8642")

    def test_settings_and_env_still_win_over_derivation(self) -> None:
        self._write_gateway_state("http://127.0.0.1:8777")
        self._write_dotenv("API_SERVER_KEY", "dotenv-key")
        os.environ["HERMES_API_SERVER_KEY"] = "env-key"
        try:
            # env beats derived key; settings beat both (config file test in
            # test_config_and_layout covers the settings tier)
            self.assertEqual(self.api.api_server_key(), "env-key")
            self.assertEqual(self.api.api_server_url(), "http://127.0.0.1:8777")
        finally:
            del os.environ["HERMES_API_SERVER_KEY"]

    def test_lane_is_native_with_zero_configuration(self) -> None:
        self._write_gateway_state("http://127.0.0.1:8642")
        self._write_dotenv("API_SERVER_KEY", "auto-derived-secret")
        # No env, no settings — the lane resolver must still pick native.
        self.assertEqual(self.api.health()["lane"], "native")

    def test_send_routes_native_without_configuration(self) -> None:
        def handler(request, payload):
            if request.path == "/api/sessions":
                return (201, {"session": {"id": "auto-1"}})
            if "/chat" in request.path:
                return (200, {"status": "completed", "session_id": "auto-1",
                              "message": {"role": "assistant", "content": "real reply"}})
            return (404, {"error": {"message": "unknown"}})

        from support import make_gateway_stub

        base_url, shutdown = make_gateway_stub(handler)
        try:
            self._write_gateway_state(base_url)
            self._write_dotenv("API_SERVER_KEY", "derived-key")
            out = self.api.send_message("are you there?")
            self.assertEqual(out["lane"], "native")
            self.assertEqual(out["reply"], "real reply")
        finally:
            shutdown()


if __name__ == "__main__":
    unittest.main()
