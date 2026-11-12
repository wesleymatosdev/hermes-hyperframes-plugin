"""Shared fixtures for the dashboard tests: an isolated HERMES_HOME, a fake
hyperframes project with a stub CLI bin, and import plumbing for the backend
module (the tests never import the real gateway or call a real LLM).
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = REPO_ROOT / "dashboard"

if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

# The hermes venv installs hermes_cli, but config loading reaches source-root
# modules (hermes_dir_acl, hermes_constants); HERMES_SRC names that root —
# the same convention the gateway's own test suite uses.
_HERMES_SRC = os.environ.get("HERMES_SRC", "")
if _HERMES_SRC and _HERMES_SRC not in sys.path:
    sys.path.insert(0, _HERMES_SRC)

STUB_BIN = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
STATE = PROJECT / ".fake-preview-running"


def tiny_png():
    import struct, zlib
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


args = sys.argv[1:]
cmd = args[0] if args else ""
if cmd == "lint":
    print("STUB-LINT 0 error(s), 1 warning(s)")
elif cmd == "timeline":
    print(json.dumps({"timeline": {"duration": 120.0, "tracks": []}}))
elif cmd == "preview":
    if "--status" in args:
        running = STATE.exists()
        print(json.dumps({"ok": True, "result": {
            "state": "running" if running else "not-running",
            **({"studioUrl": "http://127.0.0.1:3199/#project/fake-cut"} if running else {})}}))
    elif "--stop" in args:
        STATE.unlink(missing_ok=True)
        print(json.dumps({"ok": True, "result": {"state": "stopped"}}))
    else:
        STATE.write_text("")
        print(json.dumps({"ok": True, "result": {
            "state": "started", "ready": True,
            "studioUrl": "http://127.0.0.1:3199/#project/fake-cut"}}))
elif cmd == "snapshot":
    out = PROJECT / "snapshots"
    out.mkdir(exist_ok=True)
    png = tiny_png()
    paths = []
    for i in range(1, 3):
        p = out / ("frame-%d.png" % i)
        p.write_bytes(png)
        paths.append(str(p))
    contact = out / "contact-sheet.png"
    contact.write_bytes(png)
    for p in paths + [str(contact)]:
        print(p)
elif cmd == "render":
    out = Path(args[args.index("--output") + 1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"FAKE-MP4-DATA")
    print(json.dumps({"ok": True, "output": str(out)}))
else:
    print("stub: unknown command %r" % cmd, file=sys.stderr)
    raise SystemExit(2)
"""


class DashboardTestBase(unittest.TestCase):
    """Each test gets a fresh HERMES_HOME and a fake project; the plugin's
    config reads see no real config.yaml (temp home has none)."""

    def setUp(self) -> None:
        self._home_token = tempfile.mkdtemp(prefix="hf-test-home-")
        self._old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = self._home_token
        # Never let a real machine environment leak into lane resolution.
        self._saved_env = {
            k: os.environ.pop(k, None)
            for k in ("HERMES_API_SERVER_KEY", "HERMES_API_SERVER_URL",
                      "HERMES_HYPERFRAMES_HERMES_BIN", "HERMES_HYPERFRAMES_BIN")
        }
        import plugin_api  # noqa: F401  (imported per-test with env isolated)

        self.api = plugin_api

    def tearDown(self) -> None:
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home
        for key, value in self._saved_env.items():
            if value is not None:
                os.environ[key] = value

    # -- fixture builders ---------------------------------------------------

    def make_fake_project(self, name: str = "fake-cut") -> Path:
        root = Path(self._home_token) / "projects"
        project = root / name
        (project / "node_modules" / ".bin").mkdir(parents=True, exist_ok=True)
        (project / "index.html").write_text(
            '<html><div data-composition-id="%s"></div></html>' % name,
            encoding="utf-8")
        stub = project / "node_modules" / ".bin" / "hyperframes"
        stub.write_text(STUB_BIN, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        return project


def make_gateway_stub(handler) -> tuple[str, callable]:
    """A throwaway loopback HTTP server standing in for the gateway api_server.

    ``handler(server_instance, request)`` returns (status, body_dict). Returns
    (base_url, shutdown) — call shutdown() when the test ends.
    """
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Stub(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            try:
                payload = _json.loads(body or b"{}")
            except ValueError:
                payload = {}
            status, out = handler(self, payload)
            data = _json.dumps(out).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # silence per-request stderr
            pass

    server = HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = "http://127.0.0.1:%d" % server.server_address[1]
    return base_url, server.shutdown
