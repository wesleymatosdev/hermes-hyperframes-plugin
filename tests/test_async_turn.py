"""Async chat turn (hotfix round 3) — RED-first suite.

The transport contract: POST /messages must return BEFORE the gateway turn
completes (the server owns the turn; the fetch no longer carries minutes of
work). GET /chat/status exposes the server-side turn state; a second send
while a turn is pending answers 409 turn_in_progress; a failed turn flips
status to a typed error instead of hanging.

These tests FAIL against the old synchronous route (POST blocked until the
turn finished; no /chat/status; no 409 dedupe) — that is the red proof.
"""

from __future__ import annotations

import os
import threading
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from support import DashboardTestBase, make_gateway_stub

PREFIX = "/api/plugins/hermes-hyperframes"

# How long the fake gateway's chat route blocks before answering. Long enough
# that the POST must have returned before the turn does; short enough that a
# red run against the old sync route fails fast.
TURN_BLOCK_S = 10.0


class AsyncTurnTests(DashboardTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.make_fake_project()  # composition context has a project to name
        self.release = threading.Event()
        self.chat_calls: list[str] = []

        def handler(request, payload):
            if request.headers.get("Authorization") != "Bearer k":
                return (401, {"error": {"message": "bad key"}})
            if request.path == "/api/sessions":
                return (201, {"session": {"id": "async-test-1"}})
            if "/chat" in request.path:
                self.chat_calls.append(str(payload.get("message", "")))
                # Controllable turn duration: the test releases the turn.
                if not self.release.wait(timeout=TURN_BLOCK_S):
                    return (200, {"status": "completed", "session_id": "async-test-1",
                                  "message": {"role": "assistant",
                                              "content": "TIMED-OUT-STUB"}})
                return (200, {"status": "completed", "session_id": "async-test-1",
                              "message": {"role": "assistant", "content": "async reply"}})
            if request.path.endswith("/messages"):
                return (200, {"data": []})
            return (404, {"error": {"message": "unknown"}})

        self._shutdown = make_gateway_stub(handler)
        os.environ["HERMES_API_SERVER_URL"] = self._shutdown[0]
        os.environ["HERMES_API_SERVER_KEY"] = "k"
        app = FastAPI()
        app.include_router(self.api.router, prefix=PREFIX)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        # Drain this test's turn BEFORE tearing the env down: the chat worker
        # is a global single-thread executor, and an orphaned turn that loses
        # its gateway env falls to the (slow) CLI lane, starving the next
        # test's turn.
        self.release.set()
        import time as _time

        deadline = _time.time() + 15
        while _time.time() < deadline:
            if not self.api._CHAT_STATE.get("pending"):
                break
            _time.sleep(0.05)
        self._shutdown[1]()
        os.environ.pop("HERMES_API_SERVER_URL", None)
        os.environ.pop("HERMES_API_SERVER_KEY", None)
        super().tearDown()

    # -- helpers ------------------------------------------------------------

    def _await_idle(self, timeout: float = 15.0) -> dict:
        """Poll /chat/status until the turn is no longer pending."""
        import time

        deadline = time.time() + timeout
        status = {}
        while time.time() < deadline:
            status = self.client.get(PREFIX + "/chat/status").json()
            if not status.get("pending"):
                return status
            time.sleep(0.1)
        return status


class PostReturnsBeforeTurnTests(AsyncTurnTests):
    def test_post_answers_while_turn_still_running(self) -> None:
        resp = self.client.post(PREFIX + "/messages",
                                json={"text": "slow turn smoke"})
        # The route must answer BEFORE the gateway turn completes.
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertEqual(body["status"], "accepted")
        self.assertIn("queuedAt", body)
        self.assertFalse(self.release.is_set(),
                         "turn finished before the POST returned — transport is synchronous")

    def test_reply_lands_after_release(self) -> None:
        self.client.post(PREFIX + "/messages", json={"text": "slow turn smoke"})
        self.release.set()
        status = self._await_idle()
        self.assertFalse(status["pending"])
        history = self.client.get(PREFIX + "/messages").json()
        texts = [m["text"] for m in history["messages"]]
        self.assertIn("async reply", texts)


class StatusEndpointTests(AsyncTurnTests):
    def test_status_transitions_accepted_pending_done(self) -> None:
        accepted = self.client.post(PREFIX + "/messages",
                                    json={"text": "status smoke"}).json()
        self.assertEqual(accepted["status"], "accepted")

        status = self.client.get(PREFIX + "/chat/status")
        self.assertEqual(status.status_code, 200)
        pending = status.json()
        self.assertTrue(pending["pending"])
        self.assertEqual(pending["lane"], "native")

        self.release.set()
        done = self._await_idle()
        self.assertFalse(done["pending"])
        self.assertEqual(done["sessionId"], "async-test-1")

    def test_failed_turn_flips_status_to_typed_error(self) -> None:
        def failing_handler(request, payload):
            if "/chat" in request.path:
                return (500, {"error": {"message": "boom"}})
            if request.path == "/api/sessions":
                return (201, {"session": {"id": "async-test-2"}})
            return (404, {"error": {"message": "unknown"}})

        base_url, shutdown = make_gateway_stub(failing_handler)
        os.environ["HERMES_API_SERVER_URL"] = base_url
        try:
            resp = self.client.post(PREFIX + "/messages", json={"text": "will fail"})
            self.assertEqual(resp.status_code, 202)
            status = self._await_idle()
            self.assertFalse(status["pending"])
            self.assertIsNotNone(status["lastError"])
            self.assertTrue(str(status["lastError"].get("message", "")))
        finally:
            del os.environ["HERMES_API_SERVER_URL"]
            shutdown()


class ConcurrentSendTests(AsyncTurnTests):
    def test_second_send_while_pending_answers_409(self) -> None:
        first = self.client.post(PREFIX + "/messages", json={"text": "turn one"})
        self.assertEqual(first.status_code, 202)

        second = self.client.post(PREFIX + "/messages", json={"text": "turn two"})
        self.assertEqual(second.status_code, 409)
        detail = second.json()["detail"]
        self.assertEqual(detail["kind"], "turn_in_progress")

        self.release.set()
        self._await_idle()

    def test_reset_while_pending_refused(self) -> None:
        self.client.post(PREFIX + "/messages", json={"text": "turn one"})
        resp = self.client.post(PREFIX + "/messages/reset")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"]["kind"], "turn_in_progress")
        self.release.set()
        self._await_idle()

    def test_send_after_completion_is_accepted(self) -> None:
        self.client.post(PREFIX + "/messages", json={"text": "turn one"})
        self.release.set()
        self._await_idle()
        # Re-arm the gate for the second turn.
        self.release.clear()
        try:
            second = self.client.post(PREFIX + "/messages", json={"text": "turn two"})
            self.assertEqual(second.status_code, 202)
            self.release.set()
            self._await_idle()
        finally:
            self.release.set()


if __name__ == "__main__":
    unittest.main()
