"""Native chat-lane auto-derivation (feedback round 2) + secret-scope API
(feedback round 3, catalog review on d49838bc).

The plugin must resolve the native lane with ZERO manual configuration — URL
from ``<home>/gateway_state.json`` (platforms.api_server.listener_base) and
the key through Hermes's secret API (``agent.secret_scope.get_secret``).
Plugin settings and env keep precedence for other machines. The key is NEVER
parsed out of ``.env`` by hand anymore — that test fails against v0.3.1.
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

    def test_key_derives_through_hermes_secret_scope(self) -> None:
        """The sanctioned path: agent.secret_scope (what the gateway installs
        from the profile .env / process env — the plugin never reads .env)."""
        from agent.secret_scope import reset_secret_scope, set_secret_scope

        token = set_secret_scope({"API_SERVER_KEY": "scoped-secret"})
        try:
            self.assertEqual(self.api.api_server_key(), "scoped-secret")
        finally:
            reset_secret_scope(token)

    def test_dotenv_is_never_parsed_by_hand(self) -> None:
        """A .env file alone must NOT satisfy the lane anymore: without the
        Hermes secret scope (or an explicit setting/env override) there is no
        credential. This fails against v0.3.1, which parsed .env directly."""
        self._write_dotenv("API_SERVER_KEY", "auto-derived-secret")
        self.assertEqual(self.api.api_server_key(), "")

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
        # Zero manual config: the credential arrives the way the running
        # gateway supplies it — through the profile secret scope (built from
        # the profile .env / process env by Hermes itself).
        from agent.secret_scope import reset_secret_scope, set_secret_scope

        token = set_secret_scope({"API_SERVER_KEY": "auto-derived-secret"})
        try:
            self.assertEqual(self.api.health()["lane"], "native")
        finally:
            reset_secret_scope(token)

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
        from agent.secret_scope import reset_secret_scope, set_secret_scope

        token = set_secret_scope({"API_SERVER_KEY": "derived-key"})
        try:
            self._write_gateway_state(base_url)
            out = self.api.send_message("are you there?")
            self.assertEqual(out["lane"], "native")
            self.assertEqual(out["reply"], "real reply")
        finally:
            reset_secret_scope(token)
            shutdown()


if __name__ == "__main__":
    unittest.main()
