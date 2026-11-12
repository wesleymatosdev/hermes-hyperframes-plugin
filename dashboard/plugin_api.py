"""hermes-hyperframes dashboard backend — mounted at /api/plugins/hermes-hyperframes/.

Third-party dashboard plugin: the web server imports this module once at startup
(spec_from_file_location) and mounts its ``router``; it is never re-imported
after a plugin update, while the frontend assets and manifest.json are served
from disk per request — ``plugin_version()`` reports the import-time version so
the UI can detect that skew and banner a reload instead of misbehaving.

Every hyperframes route wraps the SAME code paths the hyperframes_* tools use:
the handlers in the plugin's ``__init__.py`` are imported here by file path and
called directly — no drift-prone second implementation of bin resolution, the
PWD-normalizing subprocess runner, or CLI JSON parsing. (The plugin root is
located relative to this file so both layouts work: the repo ships
``plugin/__init__.py`` beside ``dashboard/``; a user install flattens
``__init__.py`` to the plugin root beside ``dashboard/``.)

Chat lane (a HEAVY action — see README): preferred is the gateway's native
api_server (``POST /api/sessions`` + ``POST /api/sessions/{id}/chat``, the same
loopback surface ``hermes peer dm`` drives — one synchronous agent turn per
message), authenticated with the user-pasted API_SERVER_KEY. Fallback when no
key is configured: ``hermes chat -q`` (local LLM call) with ``--resume`` for
follow-ups. Conversation state lives in a per-profile sqlite db under the
Hermes home; nothing is cached across profiles.

Importable standalone: stdlib + fastapi only; ``hermes_cli`` reads are
best-effort so bare test harnesses and the plugin validator can load it too.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

router = APIRouter()
log = logging.getLogger("hermes-hyperframes")

PLUGIN_ID = "hermes-hyperframes"
DEFAULT_TIMEOUT_S = 120
PROJECTS_SCAN_TIMEOUT_S = 30
CHAT_TIMEOUT_S = 300
CHAT_OUTPUT_MAX_CHARS = 16_000
PROJECTS_SCAN_MAX_DEPTH = 1

# Default scan roots for hyperframes projects (overridable via config_schema
# key projects_roots; see plugin.yaml).
DEFAULT_PROJECTS_ROOTS = ["~/projects/personal", "~/projects/clients"]


def _manifest_version() -> str:
    """Version from the manifest NEXT TO THIS FILE, read once at import.

    The dashboard imports this module once and never re-imports after a plugin
    update; the frontend assets and manifest.json are served from disk per
    request. Reporting the import-time version is what lets the frontend detect
    a stale in-process backend and banner "reload" instead of misbehaving.
    """
    try:
        raw = (Path(__file__).resolve().parent / "manifest.json").read_text(encoding="utf-8")
        return str(json.loads(raw).get("version") or "")
    except (OSError, ValueError):  # a broken manifest must not take the tab down
        return ""


PLUGIN_VERSION = _manifest_version()


# ---------------------------------------------------------------------------
# Tools-module bridge: the hyperframes_* handlers ARE the implementation
# ---------------------------------------------------------------------------

def _locate_tools_init() -> Optional[Path]:
    """The plugin's ``__init__.py`` (the hyperframes_* tools), in either layout.

    Repo layout:  <root>/plugin/__init__.py beside <root>/dashboard/.
    User install: <plugin-root>/__init__.py beside <plugin-root>/dashboard/.
    """
    here = Path(__file__).resolve().parent          # .../dashboard
    root = here.parent
    candidates = [root / "__init__.py", root / "plugin" / "__init__.py"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _import_tools_module():
    """Import the tools ``__init__.py`` once under a private module name."""
    path = _locate_tools_init()
    if path is None:
        return None
    try:
        spec = importlib.util.spec_from_file_location("hermes_hyperframes_tools", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules["hermes_hyperframes_tools"] = module
        spec.loader.exec_module(module)
        return module
    except Exception:  # a broken tools module must not kill the tab's import
        log.warning("failed to import hyperframes tools module at %s", path, exc_info=True)
        return None


_TOOLS = _import_tools_module()


def _project_dir_for(args_extra: Optional[dict] = None) -> dict:
    """Handler args with the CURRENT project dir resolved, or a typed error dict.

    The selected project lives in the plugin db; if none is selected (or it
    vanished from disk) every CLI-backed route answers 409-style typed errors
    the UI can render as an honest banner instead of a blank pane.
    """
    project = current_project_path()
    if not project:
        return {"success": False,
                "error": "no project selected — pick one in the Hyperframes tab",
                "kind": "no_project"}
    args = dict(args_extra or {})
    args["project_dir"] = project
    return args


def _call_tools(handler_name: str, args: Optional[dict] = None) -> dict:
    """Run one hyperframes_* handler and return its parsed result dict.

    The handlers return JSON strings ({"success": bool, ...}); failures map to
    typed HTTP errors so the UI degrades honestly (never a blank page).
    """
    if _TOOLS is None:
        raise HTTPException(status_code=503, detail=_error_detail(
            "tools_unavailable",
            "The hyperframes tools module could not be imported — the plugin "
            "install looks broken; reinstall the plugin.", retryable=False))
    args = _project_dir_for(args)
    if not args.get("success", True):
        raise HTTPException(status_code=409, detail=_error_detail(
            args.get("kind", "no_project"), args["error"], retryable=False))
    raw = getattr(_TOOLS, handler_name)(args)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=500, detail=_error_detail(
            "bridge_error", "hyperframes tool returned an unparsable result", retryable=True))
    if not parsed.get("success"):
        raise HTTPException(status_code=502, detail=_error_detail(
            "cli_error", str(parsed.get("error") or "hyperframes CLI failed"),
            retryable=False, output=str(parsed.get("output") or "")[-800:]))
    return parsed


# ---------------------------------------------------------------------------
# Configuration (config.yaml plugins.entries.hermes-hyperframes.settings.*)
# ---------------------------------------------------------------------------

def _load_hermes_config() -> dict:
    """Best-effort config.yaml read; any failure just means "empty config"."""
    try:
        from hermes_cli.config import load_config_readonly  # type: ignore
    except Exception:  # pragma: no cover - exercised only outside the dashboard server
        return {}
    try:
        return load_config_readonly() or {}
    except Exception:  # pragma: no cover - an unreadable config grants no setting
        return {}


def _settings() -> dict:
    """``plugins.entries.hermes-hyperframes.settings`` (best-effort)."""
    try:
        return (_load_hermes_config().get("plugins", {}).get("entries", {})
                .get(PLUGIN_ID, {}).get("settings", {})) or {}
    except Exception:  # pragma: no cover - defensive
        return {}


def _setting(key: str) -> Optional[str]:
    value = _settings().get(key)
    return str(value).strip() if value not in (None, "") else None


def projects_roots() -> list[str]:
    """Scan roots from config (projects_roots), falling back to the defaults.

    Unconfigured: the per-profile workspace ``<hermes_home>/projects`` is the
    default scan root when it exists (keeps temp/multi-profile setups
    hermetic); otherwise the machine-wide tilde defaults from plugin.yaml.
    Accepts a list or a comma-separated string; ``~`` expands; missing dirs are
    skipped at scan time.
    """
    raw = _settings().get("projects_roots")
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, list):
        items = [str(part).strip() for part in raw]
    else:
        home_projects = _hermes_home() / "projects"
        items = ([str(home_projects)] if home_projects.is_dir()
                 else list(DEFAULT_PROJECTS_ROOTS))
    roots = []
    for item in items:
        if not item:
            continue
        path = Path(os.path.expanduser(item))
        if path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def _is_within(path: Path, root: Path) -> bool:
    """True when ``path`` resolves inside ``root`` (symlink-aware)."""
    try:
        Path(path).resolve().relative_to(Path(str(root)).resolve())
        return True
    except (ValueError, OSError):
        return False


def _gateway_state_listener_base() -> Optional[str]:
    """api_server listener URL from ``<home>/gateway_state.json``, best-effort.

    The running gateway records its own api_server bind there
    (``platforms.api_server.listener_base``) — reading it is what makes the
    native chat lane work with ZERO manual configuration.
    """
    try:
        raw = (_hermes_home() / "gateway_state.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        base = ((data.get("platforms") or {}).get("api_server") or {}).get("listener_base")
        return str(base).strip().rstrip("/") or None
    except (OSError, ValueError, AttributeError):
        return None


def _hermes_secret(name: str) -> str:
    """``name`` through Hermes's secret API — the sanctioned path the gateway
    itself uses (profile secret scope, then the process env). Never parses
    ``.env`` by hand; fails closed ("" not an exception) outside a Hermes
    process and under active multiplexing without a scope."""
    try:
        from agent.secret_scope import get_secret  # type: ignore
    except Exception:
        return ""
    try:
        return (get_secret(name) or "").strip()
    except Exception:  # UnscopedSecretError et al: no credential, never a crash
        return ""


def api_server_url() -> str:
    """Native chat lane base URL: plugin settings, env, then the running
    gateway's own listener (gateway_state.json), then the stock loopback port."""
    from_config = _setting("api_server_url")
    if from_config:
        return from_config.rstrip("/")
    from_env = (os.environ.get("HERMES_API_SERVER_URL") or "").strip()
    if from_env:
        return from_env.rstrip("/")
    from_state = _gateway_state_listener_base()
    if from_state:
        return from_state
    return "http://127.0.0.1:8642"


def api_server_key() -> str:
    """Native chat lane credential: the plugin's own ``api_server_key``
    setting, the plugin's ``HERMES_API_SERVER_KEY`` env override, then
    ``API_SERVER_KEY`` through Hermes's secret API
    (``agent.secret_scope.get_secret`` — profile scope, then process env).
    The derived value never leaves the loopback calls and is never logged."""
    from_config = _setting("api_server_key")
    if from_config:
        return from_config
    from_env = (os.environ.get("HERMES_API_SERVER_KEY") or "").strip()
    if from_env:
        return from_env
    return _hermes_secret("API_SERVER_KEY")


# ---------------------------------------------------------------------------
# Per-profile plugin db (selected project + chat session + history)
# ---------------------------------------------------------------------------

def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home())
    except Exception:  # pragma: no cover - bare test harnesses
        return Path.home() / ".hermes"


def plugin_db_path() -> Path:
    """Per-profile by construction: resolved through the active Hermes home."""
    path = _hermes_home() / "plugins" / "data" / PLUGIN_ID
    path.mkdir(parents=True, exist_ok=True)
    return path / "dashboard.db"


_DB_LOCK = threading.Lock()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(plugin_db_path()), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '')
    """)
    return conn


def kv_get(key: str) -> Optional[str]:
    with _DB_LOCK, _db() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def kv_set(key: str, value: str) -> None:
    with _DB_LOCK, _db() as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def kv_delete(key: str) -> None:
    with _DB_LOCK, _db() as conn:
        conn.execute("DELETE FROM kv WHERE key = ?", (key,))


# --- selected project ---

def current_project_path() -> Optional[str]:
    """The persisted project dir, only while it still exists on disk."""
    path = kv_get("current_project")
    if path and Path(path).is_dir():
        return path
    return None


# --- chat history ---

def history_add(role: str, text: str, session_id: str) -> None:
    with _DB_LOCK, _db() as conn:
        conn.execute(
            "INSERT INTO messages (ts, role, text, session_id) VALUES (?, ?, ?, ?)",
            (time.time(), role, text, session_id))


def history_all(session_id: str, limit: int = 200) -> list[dict]:
    with _DB_LOCK, _db() as conn:
        rows = conn.execute(
            "SELECT ts, role, text FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit)).fetchall()
    return [{"ts": ts, "role": role, "text": text} for ts, role, text in reversed(rows)]


def chat_session_id() -> str:
    return kv_get("chat_session_id") or ""


# ---------------------------------------------------------------------------
# Typed error payloads (every failure names the cause; never a bare string)
# ---------------------------------------------------------------------------

def _error_detail(kind: str, message: str, retryable: bool,
                  upstream_status: Optional[int] = None,
                  code: str = "", output: str = "") -> dict:
    detail: dict = {"kind": kind, "retryable": retryable, "message": message,
                    "pluginVersion": PLUGIN_VERSION}
    if upstream_status is not None:
        detail["upstreamStatus"] = upstream_status
    if code:
        detail["code"] = code
    if output:
        detail["output"] = output
    return detail


def _raise_upstream(status: int, message: str, *, retryable: bool,
                    upstream_status: Optional[int] = None, code: str = "",
                    output: str = "") -> None:
    raise HTTPException(status_code=status, detail=_error_detail(
        "upstream_error" if upstream_status else "gateway_unreachable",
        message, retryable=retryable, upstream_status=upstream_status,
        code=code, output=output))


# ---------------------------------------------------------------------------
# Gateway api_server client (stdlib urllib; loopback; the native chat lane)
# ---------------------------------------------------------------------------

class _GatewayApi:
    """Minimal client for the gateway's api_server platform (loopback by default).

    Verified wire contract (gateway/platforms/api_server.py, probed live
    2026-11-11): ``POST /api/sessions`` -> 201 {"session": {...}};
    ``POST /api/sessions/{id}/chat`` {"message": str} -> one SYNCHRONOUS agent
    turn, {"status", "session_id", "message": {"role": "assistant", "content"}}.
    Auth: ``Authorization: Bearer <API_SERVER_KEY>``.
    """

    def __init__(self, base_url: str, key: str, timeout: float = CHAT_TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.timeout = timeout

    def request(self, method: str, path: str, payload: Optional[dict] = None,
                timeout: Optional[float] = None) -> dict:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json",
                   "Authorization": f"Bearer {self.key}"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as err:
            try:
                detail = json.loads(err.read() or b"{}")
                message = str((detail.get("error") or {}).get("message")
                              or detail.get("message") or err.reason)
            except (ValueError, AttributeError):
                message = err.reason
            _raise_upstream(502 if err.code >= 500 else err.code, message,
                            retryable=err.code >= 500 or err.code == 429,
                            upstream_status=err.code)
        except (urllib.error.URLError, OSError) as err:
            _raise_upstream(502, f"gateway api_server unreachable at {self.base_url}: {err}",
                            retryable=True)
        if not body:
            return {}
        try:
            parsed = json.loads(body)
        except ValueError:
            return {"value": body.decode("utf-8", "replace")}
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    def create_session(self, title: str) -> str:
        try:
            resp = self.request("POST", "/api/sessions",
                                {"title": title, "source": "api_server"}, timeout=15)
        except HTTPException as err:
            detail = err.detail if isinstance(getattr(err, "detail", None), dict) else {}
            message = str(detail.get("message") or "")
            if "title" not in message.lower():
                raise
            # The gateway session store enforces unique titles; a leftover
            # session carrying our title must not wedge the lane — retry
            # untitled and let the gateway title it from the first message.
            resp = self.request("POST", "/api/sessions",
                                {"source": "api_server"}, timeout=15)
        session = resp.get("session") or {}
        session_id = str(session.get("id") or "")
        if not session_id:
            _raise_upstream(502, "gateway created no session id", retryable=True)
        return session_id

    def chat(self, session_id: str, text: str) -> str:
        resp = self.request("POST", f"/api/sessions/{urllib.parse.quote(session_id, safe='')}/chat",
                            {"message": text})
        message = resp.get("message") or {}
        content = message.get("content")
        if content is None:
            _raise_upstream(502, "gateway chat returned no reply content", retryable=True)
        return str(content)

    def messages(self, session_id: str) -> list[dict]:
        resp = self.request("GET", f"/api/sessions/{urllib.parse.quote(session_id, safe='')}/messages",
                            timeout=15)
        rows = resp.get("data") if isinstance(resp.get("data"), list) else resp.get("messages")
        return rows if isinstance(rows, list) else []


# ---------------------------------------------------------------------------
# CLI chat fallback (the heavy lane: one local `hermes chat -q` per message)
# ---------------------------------------------------------------------------

def _hermes_chat_argv(text: str, resume: Optional[str]) -> list[str]:
    """Quiet one-shot argv against THIS interpreter's hermes_cli (same venv as
    the dashboard serve — no PATH dependence), or an explicit binary override.

    ``-q`` on a non-TTY answers and exits; ``-Q`` suppresses the banner and
    prints only the final response plus a ``session_id: <id>`` line on stderr.
    """
    override = (os.environ.get("HERMES_HYPERFRAMES_HERMES_BIN") or "").strip()
    if override:
        base = [override]
    else:
        base = [sys.executable, "-m", "hermes_cli.main"]
    argv = base + ["chat", "-q", text, "-Q"]
    if resume:
        argv += ["--resume", resume]
    return argv


# stderr session-line shapes, both accepted (banner path and quiet path):
#   session_id: <id>      (quiet one-shot, cli_single_query.py)
#   Session: <id>         (banner / older prints)
_SESSION_LINE = re.compile(r"^(?:session_id|Session):\s*(\S+)\s*$", re.MULTILINE)


def _parse_session_line(stderr_text: str) -> str:
    match = _SESSION_LINE.search(stderr_text or "")
    return match.group(1) if match else ""


def _cli_chat(text: str, resume: Optional[str]) -> tuple[str, str]:
    """One heavy `hermes chat` turn: (reply, session_id). Timeout + output cap."""
    argv = _hermes_chat_argv(text, resume)
    try:
        proc = subprocess.run(
            argv, timeout=CHAT_TIMEOUT_S, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        _raise_upstream(504, f"hermes chat timed out after {CHAT_TIMEOUT_S}s", retryable=True)
    except OSError as err:
        _raise_upstream(503, f"hermes CLI failed to start: {err}", retryable=False)
    reply = (proc.stdout or "").strip()[:CHAT_OUTPUT_MAX_CHARS]
    session_id = _parse_session_line(proc.stderr or "")
    if proc.returncode != 0 and not reply:
        tail = ((proc.stderr or "") + (proc.stdout or ""))[-800:]
        _raise_upstream(502, f"hermes chat failed (exit {proc.returncode})",
                        retryable=False, output=tail)
    return reply or "(empty reply)", session_id


# ---------------------------------------------------------------------------
# Chat lane orchestration
# ---------------------------------------------------------------------------

def _send_via_native(text: str, session_id: str) -> tuple[str, str]:
    """Native lane: one synchronous gateway turn. Returns (reply, session_id)."""
    client = _GatewayApi(api_server_url(), api_server_key())
    if not session_id:
        session_id = client.create_session("Hyperframes dashboard")
        kv_set("chat_session_id", session_id)
    reply = client.chat(session_id, text)
    return reply[:CHAT_OUTPUT_MAX_CHARS], session_id


def chat_lane() -> str:
    """The lane the next message would take: "native" (gateway api_server,
    one real Hermes session turn per message) or "cli" (local `hermes chat -q`
    fallback — only when the native credential genuinely failed to resolve)."""
    return "native" if api_server_key() else "cli"


def _composition_context() -> str:
    """One context line about the selected composition, so the gateway session
    (which carries the pinned hyperframes_* toolset) can act on the project
    the tab is looking at: path + managed preview state."""
    project = current_project_path()
    if not project:
        return ""
    preview_state = "unknown"
    try:
        data = json.loads(_TOOLS.h_preview({"project_dir": project, "action": "status"}))
    except Exception:  # pragma: no cover - status is best-effort context
        data = {}
    studio_url = data.get("studioUrl")
    if _preview_is_running(data):
        preview_state = f"running at {studio_url}" if studio_url else "running"
    else:
        preview_state = "not running"
    return (f"[Hyperframes dashboard] active composition: {project} "
            f"(managed preview: {preview_state})\n")


def send_message(text: str) -> dict:
    """Run one chat turn through the best configured lane; persist history.

    The heavy path, shared by the async route's worker thread (and tests):
    a real agent turn takes 1-5 minutes, so POST /messages never runs this
    inline — it queues onto ``_CHAT_EXECUTOR`` and returns immediately.
    """
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail=_error_detail(
            "validation", "Message must not be empty", retryable=False))
    session_id = chat_session_id()
    context = _composition_context()
    wire_text = context + text if context else text
    if api_server_key():
        lane = "native"
        reply, session_id = _send_via_native(wire_text, session_id)
    else:
        # Fallback lane: a local `hermes chat -q` per message (HEAVY — see README).
        lane = "cli"
        reply, parsed = _cli_chat(wire_text, session_id or None)
        if parsed:
            session_id = parsed
            kv_set("chat_session_id", parsed)
    if session_id:
        history_add("user", text, session_id)
        history_add("assistant", reply, session_id)
    else:
        # An unparseable CLI session id still shows the exchange in this tab.
        history_add("user", text, "")
        history_add("assistant", reply, "")
    return {"ok": True, "lane": lane, "sessionId": session_id, "reply": reply}


# ---------------------------------------------------------------------------
# Async turn plumbing: the SERVER owns the turn, not the fetch.
#
# A real agent turn is 1-5 minutes; a synchronous POST looks dead through any
# proxy and orphans the UI on reload. POST /messages accepts (202) and runs
# the turn on a dedicated single-worker executor; GET /chat/status exposes
# the server-side state so any page (reload included) can resume waiting.
# One in-flight turn per pinned session: the 409 rejection IS the dedupe.
# ---------------------------------------------------------------------------

_CHAT_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hf-chat")
_CHAT_LOCK = threading.Lock()
_CHAT_STATE: dict = {"pending": False, "queuedAt": None, "startedAt": None,
                     "sessionId": "", "lastError": None}


def _chat_status_payload() -> dict:
    with _CHAT_LOCK:
        return {"pending": bool(_CHAT_STATE["pending"]),
                "queuedAt": _CHAT_STATE["queuedAt"],
                "startedAt": _CHAT_STATE["startedAt"],
                "sessionId": _CHAT_STATE["sessionId"] or chat_session_id(),
                "lane": chat_lane(),
                "lastError": _CHAT_STATE["lastError"]}


def _run_turn_stateful(text: str) -> None:
    """Worker body: run one turn, always clearing the pending flag."""
    with _CHAT_LOCK:
        _CHAT_STATE["startedAt"] = time.time()
    try:
        result = send_message(text)
        with _CHAT_LOCK:
            _CHAT_STATE["sessionId"] = str(result.get("sessionId") or _CHAT_STATE["sessionId"])
    except Exception as exc:  # a failed turn is a typed status, never a hang
        detail = getattr(exc, "detail", None)
        error = detail if isinstance(detail, dict) else _error_detail(
            "turn_failed", str(exc) or type(exc).__name__, retryable=False)
        with _CHAT_LOCK:
            _CHAT_STATE["lastError"] = error
    finally:
        with _CHAT_LOCK:
            _CHAT_STATE["pending"] = False


def queue_message(text: str) -> dict:
    """Accept one message for the async turn; dedupe on the pending flag."""
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail=_error_detail(
            "validation", "Message must not be empty", retryable=False))
    with _CHAT_LOCK:
        if _CHAT_STATE["pending"]:
            raise HTTPException(status_code=409, detail=_error_detail(
                "turn_in_progress",
                "the agent is already working on your previous message",
                retryable=False))
        _CHAT_STATE.update(pending=True, queuedAt=time.time(),
                           startedAt=None, lastError=None)
    _CHAT_EXECUTOR.submit(_run_turn_stateful, text)
    return {"status": "accepted", "sessionId": chat_session_id(),
            "queuedAt": _CHAT_STATE["queuedAt"]}


def reset_session() -> dict:
    """Start a fresh pinned session: drop the id and the stored history rows."""
    with _CHAT_LOCK:
        if _CHAT_STATE["pending"]:
            raise HTTPException(status_code=409, detail=_error_detail(
                "turn_in_progress",
                "wait for the agent to finish the current message first",
                retryable=False))
    old = chat_session_id()
    kv_delete("chat_session_id")
    with _DB_LOCK, _db() as conn:
        conn.execute("DELETE FROM messages")
    return {"ok": True, "previousSessionId": old}


def messages_payload() -> dict:
    session_id = chat_session_id()
    return {"ok": True, "sessionId": session_id,
            "lane": chat_lane(),
            "messages": history_all(session_id)}


# ---------------------------------------------------------------------------
# Project discovery
# ---------------------------------------------------------------------------

def _composition_qualifies(dir_path: Path) -> bool:
    """A dir qualifies when index.html declares a composition id AND the pinned
    hyperframes bin exists in its node_modules (same bin the tools resolve)."""
    index = dir_path / "index.html"
    try:
        if not index.is_file():
            return False
        head = index.read_text(encoding="utf-8", errors="replace")[:8192]
        if "data-composition-id" not in head:
            return False
    except OSError:
        return False
    return (dir_path / "node_modules" / ".bin" / "hyperframes").is_file()


def discover_projects() -> list[dict]:
    """Scan the configured roots (depth 1) for qualifying composition dirs."""
    found: list[dict] = []
    for root in projects_roots():
        try:
            for child in sorted(root.iterdir(), key=lambda p: p.name):
                if child.is_dir() and _composition_qualifies(child):
                    found.append(child)
        except OSError as exc:
            log.warning("skipping unreadable projects root %s: %s", root, exc)
    return found


def _row_facts(dir_path: str) -> dict:
    """Per-project facts (duration + lint summary) via the SAME CLI wrappers.

    Each fact degrades to None on failure — a project that cannot answer must
    still list (with an honest gap), never take the whole scan down.
    """
    facts: dict = {"duration": None, "lint_summary": None}
    try:
        data = json.loads(_TOOLS.h_timeline({"project_dir": dir_path}))
        timeline = data.get("timeline") or {}
        duration = timeline.get("duration") if isinstance(timeline, dict) else None
        if duration is None and isinstance(timeline, dict):
            duration = timeline.get("totalDuration")
        facts["duration"] = duration
    except Exception:  # pragma: no cover - depends on local project state
        pass
    try:
        data = json.loads(_TOOLS.h_lint({"project_dir": dir_path}))
        facts["lint_summary"] = data.get("summary")
    except Exception:  # pragma: no cover - depends on local project state
        pass
    return facts


def _preview_running(dir_path: str) -> Optional[bool]:
    try:
        data = json.loads(_TOOLS.h_preview({"project_dir": dir_path, "action": "status"}))
    except Exception:  # pragma: no cover - depends on local project state
        return None
    return _preview_is_running(data)


def projects_payload() -> dict:
    dirs = discover_projects()
    rows: list[dict] = []
    current = current_project_path()

    def facts_for(path: str) -> tuple[str, dict]:
        return path, _row_facts(path)

    with ThreadPoolExecutor(max_workers=4) as pool:
        facts_by_path = dict(pool.map(facts_for, [str(d) for d in dirs]))

    for dir_path in dirs:
        path = str(dir_path)
        rows.append({
            "name": dir_path.name,
            "path": path,
            "isCurrent": path == current,
            "has_preview_running": _preview_running(path),
            "duration": facts_by_path.get(path, {}).get("duration"),
            "lint_summary": facts_by_path.get(path, {}).get("lint_summary"),
        })
    return {"ok": True, "projects": rows, "current": current,
            "pluginVersion": PLUGIN_VERSION}


# ---------------------------------------------------------------------------
# Snapshot artifact serving (PNGs via the backend; never file:// URLs)
# ---------------------------------------------------------------------------

def _snapshot_file_allowed(path: str) -> bool:
    """Only PNGs the current project's snapshot could have produced.

    Guard: resolved path must be a file inside the current project dir OR the
    system temp dir (the CLI snapshots into either), and must end in .png.
    """
    resolved = Path(path).resolve()
    if resolved.suffix.lower() != ".png" or not resolved.is_file():
        return False
    project = current_project_path()
    if project:
        try:
            resolved.relative_to(Path(project).resolve())
            return True
        except ValueError:
            pass
    tmp = Path(os.environ.get("TMPDIR", "/tmp")).resolve()
    try:
        resolved.relative_to(tmp)
        return True
    except ValueError:
        return False


@router.get("/snapshot/file")
def snapshot_file(path: str):
    """Serve one snapshot PNG. 404 (not 403) on refusal — no filesystem oracle."""
    if not _snapshot_file_allowed(path):
        raise HTTPException(status_code=404, detail="Snapshot file not found")
    return FileResponse(path, media_type="image/png")


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class SelectBody(BaseModel):
    path: str


class PreviewBody(BaseModel):
    action: str = Field("start")


class SnapshotBody(BaseModel):
    at: Optional[str] = None


class MessageBody(BaseModel):
    text: str


# ---------------------------------------------------------------------------
# Routes (mounted at /api/plugins/hermes-hyperframes/)
# ---------------------------------------------------------------------------

@router.get("/health")
def health():
    """Import-time plugin_version: the frontend compares it against the on-disk
    manifest and banners "Plugin updated — reload dashboard" on mismatch."""
    return {"ok": True, "plugin_version": PLUGIN_VERSION,
            "lane": chat_lane(),
            "api_server_url": api_server_url() if api_server_key() else ""}


@router.get("/projects")
def projects():
    return projects_payload()


@router.post("/projects/select")
def projects_select(body: SelectBody):
    path = os.path.abspath(os.path.expanduser(body.path.strip()))
    if not Path(path).is_dir():
        raise HTTPException(status_code=422, detail=_error_detail(
            "validation", f"Not a directory: {body.path}", retryable=False))
    if not _composition_qualifies(Path(path)):
        raise HTTPException(status_code=422, detail=_error_detail(
            "validation",
            "Not a hyperframes project (needs index.html with data-composition-id "
            "and node_modules/.bin/hyperframes)", retryable=False))
    # Same confinement the tools enforce: the project's own
    # node_modules/.bin/hyperframes runs on the host, so only dirs under a
    # configured projects root may be selected.
    roots = projects_roots()
    resolved = Path(path).resolve()
    if not any(_is_within(resolved, root) for root in roots):
        raise HTTPException(status_code=422, detail=_error_detail(
            "validation",
            "Outside the configured projects_roots — add the dir (or its parent) "
            "to plugins.entries.hermes-hyperframes.settings.projects_roots in "
            f"config.yaml. Allowed roots: "
            f"{', '.join(str(r) for r in roots) or '(none configured)'}",
            retryable=False))
    kv_set("current_project", path)
    return {"ok": True, "current": path}


@router.get("/projects/current")
def projects_current():
    project = current_project_path()
    return {"ok": True, "current": project}


@router.post("/preview")
def preview(body: PreviewBody):
    if body.action not in ("start", "stop"):
        raise HTTPException(status_code=422, detail=_error_detail(
            "validation", "action must be start|stop", retryable=False))
    data = _call_tools("h_preview", {"action": body.action})
    return {"ok": True, "action": body.action,
            "studioUrl": data.get("studioUrl"),
            "serverUrl": data.get("serverUrl"),
            "state": data.get("state")}


@router.get("/preview/status")
def preview_status():
    data = _call_tools("h_preview", {"action": "status"})
    return {"ok": True, "studioUrl": data.get("studioUrl"),
            "serverUrl": data.get("serverUrl"),
            "state": data.get("state"),
            "running": _preview_is_running(data)}


def _preview_is_running(data: dict) -> bool:
    """CLI truth (hyperframes 0.9.32 dist): status reports state 'running' or
    'not-running'; start reports 'started'/'reused' with a studioUrl."""
    return str(data.get("state") or "") == "running" or bool(data.get("studioUrl"))


@router.get("/timeline")
def timeline():
    data = _call_tools("h_timeline")
    return {"ok": True, "timeline": data.get("timeline")}


@router.post("/snapshot")
def snapshot(body: SnapshotBody):
    args: dict = {}
    if body.at:
        args["at"] = body.at
    data = _call_tools("h_snapshot", args)
    frames = [p for p in (data.get("frames") or []) if _snapshot_file_allowed(str(p))]
    contact = data.get("contactSheet")
    if contact and not _snapshot_file_allowed(str(contact)):
        contact = None
    return {"ok": True, "frames": frames, "contactSheet": contact,
            "hint": data.get("hint")}


@router.get("/messages")
def messages():
    return messages_payload()


@router.post("/messages", status_code=202)
def messages_send(body: MessageBody):
    """Accept one message; the gateway turn runs server-side (202 immediately).

    A real agent turn is 1-5 minutes — the fetch must not carry it. The UI
    polls GET /chat/status; a reload resumes waiting from server state.
    """
    return queue_message(body.text)


@router.get("/chat/status")
def chat_status():
    return _chat_status_payload()


@router.post("/messages/reset")
def messages_reset():
    return reset_session()
