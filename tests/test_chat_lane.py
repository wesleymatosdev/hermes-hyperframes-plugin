"""Chat lane: session-line parsing, CLI fallback via a stub binary, and the
native lane against a fake loopback gateway (never the real one)."""

from __future__ import annotations

import json
import os
import unittest

from support import DashboardTestBase, make_gateway_stub


class SessionLineParsingTests(DashboardTestBase):
    def test_quiet_path_shape(self) -> None:
        stderr = "final answer\n\nsession_id: local-abc123\n"
        self.assertEqual(self.api._parse_session_line(stderr), "local-abc123")

    def test_banner_path_shape(self) -> None:
        self.assertEqual(self.api._parse_session_line("Session: local-deadbeef"),
                         "local-deadbeef")

    def test_no_session_line(self) -> None:
        self.assertEqual(self.api._parse_session_line("no session info here"), "")

    def test_hermes_chat_argv_resume_and_override(self) -> None:
        os.environ["HERMES_HYPERFRAMES_HERMES_BIN"] = "/fake/hermes"
        try:
            argv = self.api._hermes_chat_argv("hi", "local-1")
            self.assertEqual(argv, ["/fake/hermes", "chat", "-q", "hi", "-Q", "--resume", "local-1"])
            argv = self.api._hermes_chat_argv("hi", None)
            self.assertEqual(argv[-1], "-Q")
            self.assertNotIn("--resume", argv)
        finally:
            del os.environ["HERMES_HYPERFRAMES_HERMES_BIN"]


class CliLaneTests(DashboardTestBase):
    def _write_stub_hermes(self, reply: str, session: str, exit_code: int = 0) -> str:
        import stat

        stub = os.path.join(self._home_token, "stub-hermes")
        script = "\n".join([
            "#!/bin/sh",
            "cat >&2 <<'EOF'",
            "session_id: %s" % session,
            "EOF",
            "cat <<'EOF'",
            reply,
            "EOF",
            "exit %d" % exit_code,
        ])
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write(script)
        st = os.stat(stub)
        os.chmod(stub, st.st_mode | stat.S_IEXEC)
        return stub

    def test_cli_send_parses_session_and_persists_history(self) -> None:
        stub = self._write_stub_hermes("stub reply", "local-fake123")
        os.environ["HERMES_HYPERFRAMES_HERMES_BIN"] = stub
        try:
            out = self.api.send_message("hello there")
            self.assertEqual(out["lane"], "cli")
            self.assertEqual(out["sessionId"], "local-fake123")
            self.assertEqual(out["reply"], "stub reply")
            history = self.api.messages_payload()
            self.assertEqual(history["sessionId"], "local-fake123")
            self.assertEqual([m["role"] for m in history["messages"]], ["user", "assistant"])

            # A follow-up resumes the parsed session id.
            argv = self.api._hermes_chat_argv("again", self.api.chat_session_id())
            self.assertIn("--resume", argv)
            self.assertEqual(argv[argv.index("--resume") + 1], "local-fake123")
        finally:
            del os.environ["HERMES_HYPERFRAMES_HERMES_BIN"]

    def test_cli_failure_maps_to_typed_error(self) -> None:
        stub = self._write_stub_hermes("", "", exit_code=3)
        os.environ["HERMES_HYPERFRAMES_HERMES_BIN"] = stub
        try:
            from fastapi import HTTPException

            with self.assertRaises(HTTPException) as ctx:
                self.api.send_message("hello")
            self.assertEqual(ctx.exception.status_code, 502)
            self.assertEqual(ctx.exception.detail["kind"], "gateway_unreachable")
            self.assertIn("exit 3", ctx.exception.detail["message"])
        finally:
            del os.environ["HERMES_HYPERFRAMES_HERMES_BIN"]

    def test_empty_message_refused(self) -> None:
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self.api.send_message("   ")
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["kind"], "validation")


class NativeLaneTests(DashboardTestBase):
    def test_native_send_creates_session_chats_and_persists(self) -> None:
        calls: list[tuple[str, str]] = []

        def handler(request, payload):
            auth = request.headers.get("Authorization", "")
            if auth != "Bearer stub-key":
                return (401, {"error": {"message": "bad key"}})
            if request.path == "/api/sessions":
                calls.append(("create", payload.get("title", "")))
                return (201, {"session": {"id": "stub-session-1"}})
            if "/chat" in request.path:
                calls.append(("chat", payload.get("message", "")))
                return (200, {"status": "completed", "session_id": "stub-session-1",
                              "message": {"role": "assistant", "content": "PONG"}})
            return (404, {"error": {"message": "unknown route"}})

        base_url, shutdown = make_gateway_stub(handler)
        try:
            os.environ["HERMES_API_SERVER_URL"] = base_url
            os.environ["HERMES_API_SERVER_KEY"] = "stub-key"
            out = self.api.send_message("health probe")
            self.assertEqual(out["lane"], "native")
            self.assertEqual(out["sessionId"], "stub-session-1")
            self.assertEqual(out["reply"], "PONG")
            self.assertEqual(calls, [("create", "Hyperframes dashboard"), ("chat", "health probe")])
            # Follow-up reuses the stored session: no second create.
            out2 = self.api.send_message("again")
            self.assertEqual([c[0] for c in calls], ["create", "chat", "chat"])
            self.assertEqual(out2["reply"], "PONG")
            history = self.api.messages_payload()
            self.assertEqual(len(history["messages"]), 4)
        finally:
            del os.environ["HERMES_API_SERVER_URL"]
            del os.environ["HERMES_API_SERVER_KEY"]
            shutdown()

    def test_native_title_conflict_retries_untitled(self) -> None:
        """The gateway session store enforces unique titles; a leftover session
        carrying our title must not wedge the lane (found live: 400 title)."""
        creates: list[dict] = []

        def handler(request, payload):
            if request.path == "/api/sessions":
                creates.append(payload)
                if payload.get("title"):
                    return (400, {"error": {"message": "Title 'Hyperframes dashboard' "
                                                    "is already in use by session old-1"}})
                return (201, {"session": {"id": "untitled-1"}})
            if "/chat" in request.path:
                return (200, {"status": "completed", "session_id": "untitled-1",
                              "message": {"role": "assistant", "content": "ok"}})
            return (404, {"error": {"message": "unknown"}})

        base_url, shutdown = make_gateway_stub(handler)
        try:
            os.environ["HERMES_API_SERVER_URL"] = base_url
            os.environ["HERMES_API_SERVER_KEY"] = "k"
            out = self.api.send_message("hi")
            self.assertEqual(out["sessionId"], "untitled-1")
            self.assertEqual(len(creates), 2)
            self.assertTrue(creates[0].get("title"))
            self.assertNotIn("title", creates[1])
        finally:
            del os.environ["HERMES_API_SERVER_URL"]
            del os.environ["HERMES_API_SERVER_KEY"]
            shutdown()

    def test_native_auth_failure_is_typed_and_retryable_false(self) -> None:
        def handler(request, payload):
            return (401, {"error": {"message": "bad key"}})

        base_url, shutdown = make_gateway_stub(handler)
        try:
            os.environ["HERMES_API_SERVER_URL"] = base_url
            os.environ["HERMES_API_SERVER_KEY"] = "wrong-key"
            from fastapi import HTTPException

            with self.assertRaises(HTTPException) as ctx:
                self.api.send_message("hi")
            self.assertEqual(ctx.exception.status_code, 401)
            self.assertFalse(ctx.exception.detail["retryable"])
            self.assertEqual(ctx.exception.detail["upstreamStatus"], 401)
        finally:
            del os.environ["HERMES_API_SERVER_URL"]
            del os.environ["HERMES_API_SERVER_KEY"]
            shutdown()

    def test_reset_drops_session_and_history(self) -> None:
        stub_key_env = {"HERMES_API_SERVER_URL": "", "HERMES_API_SERVER_KEY": "k"}

        def handler(request, payload):
            if request.path == "/api/sessions":
                return (201, {"session": {"id": "sess-x"}})
            return (200, {"status": "completed", "session_id": "sess-x",
                          "message": {"role": "assistant", "content": "ok"}})

        base_url, shutdown = make_gateway_stub(handler)
        try:
            os.environ["HERMES_API_SERVER_URL"] = base_url
            os.environ["HERMES_API_SERVER_KEY"] = "k"
            self.api.send_message("one")
            self.assertEqual(self.api.chat_session_id(), "sess-x")
            result = self.api.reset_session()
            self.assertTrue(result["ok"])
            self.assertEqual(result["previousSessionId"], "sess-x")
            self.assertEqual(self.api.chat_session_id(), "")
            self.assertEqual(self.api.messages_payload()["messages"], [])
        finally:
            del os.environ["HERMES_API_SERVER_URL"]
            del os.environ["HERMES_API_SERVER_KEY"]
            shutdown()


if __name__ == "__main__":
    unittest.main()
