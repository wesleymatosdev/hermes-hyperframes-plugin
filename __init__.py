"""hermes-hyperframes — hyperframes_* tools for Hermes.

Wraps the pinned local hyperframes CLI (Apache-2.0, installed per-project via
npm with a lockfile — never unpinned npx) so a Hermes session can:

- lint a composition before touching it
- start/status/stop the managed Studio preview (the frame-true "stage" a raw
  ffmpeg pipeline lacks) and hand back the Studio project URL
- read the timeline back as JSON
- snapshot frames as PNGs for visual verification (pair with vision analysis)
- read the Studio selection context so deictic human edits ("move this",
  "cut here") round-trip back into the agent conversation
- render the composition to MP4

Bin resolution order (first hit wins): env HERMES_HYPERFRAMES_BIN, `hyperframes`
on PATH, <project>/node_modules/.bin/hyperframes. Default project dir comes from
the caller or env HERMES_HYPERFRAMES_PROJECT. Stdlib only (gateway has no
pytest/PyYAML). Handlers return {"success": bool, ...} JSON strings.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

TOOLSET = "hyperframes"
DEFAULT_TIMEOUT_S = 120
RENDER_TIMEOUT_S = 1800


def _resolve_bin(project_dir: str) -> str | None:
    env_bin = os.environ.get("HERMES_HYPERFRAMES_BIN")
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin
    on_path = shutil.which("hyperframes")
    if on_path:
        return on_path
    local = os.path.join(project_dir, "node_modules", ".bin", "hyperframes")
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return local
    return None


def _resolve_project(args: dict) -> str | None:
    project = args.get("project_dir") or os.environ.get("HERMES_HYPERFRAMES_PROJECT") or ""
    if project and os.path.isdir(project):
        return os.path.abspath(project)
    return None


def _run(argv: list[str], cwd: str, timeout: int) -> tuple[int, str]:
    # hyperframes derives the project name from $PWD, not the process cwd —
    # normalize it or reuse-resolution picks the wrong project fragment.
    env = dict(os.environ, PWD=cwd)
    proc = subprocess.run(
        argv, cwd=cwd, env=env, timeout=timeout, check=False,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "")[-20_000:]


def _json_out(stdout: str) -> dict | None:
    """Extract a JSON object from CLI --json output (single-line or pretty multi-line)."""
    lines = stdout.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().startswith("{"):
            try:
                return json.loads("\n".join(lines[i:]))
            except ValueError:
                continue
    return None


def _fail(message: str, **extra) -> str:
    return json.dumps({"success": False, "error": message, **extra})


def _fail(message: str, **extra) -> str:
    return json.dumps({"success": False, "error": message, **extra})


def _check() -> bool:
    """Visible only when node exists (hyperframes projects are npm-based)."""
    return bool(shutil.which("node"))


# ---------------------------------------------------------------- handlers --

def h_lint(args: dict, **kw) -> str:
    project = _resolve_project(args)
    if not project:
        return _fail("project_dir not found; pass project_dir or set HERMES_HYPERFRAMES_PROJECT")
    binary = _resolve_bin(project)
    if not binary:
        return _fail("hyperframes CLI not found (env HERMES_HYPERFRAMES_BIN, PATH, or <project>/node_modules/.bin)")
    try:
        rc, out = _run([binary, "lint"], project, DEFAULT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return _fail("lint timed out after 120s")
    return json.dumps({
        "success": rc == 0 and "error(s)" not in out.splitlines()[-1],
        "project": project,
        "summary": out.splitlines()[-1].strip() if out.splitlines() else "",
        "output": out,
    })


def h_preview(args: dict, **kw) -> str:
    project = _resolve_project(args)
    if not project:
        return _fail("project_dir not found; pass project_dir or set HERMES_HYPERFRAMES_PROJECT")
    binary = _resolve_bin(project)
    if not binary:
        return _fail("hyperframes CLI not found (env HERMES_HYPERFRAMES_BIN, PATH, or <project>/node_modules/.bin)")
    action = args.get("action", "start")
    if action not in ("start", "status", "stop"):
        return _fail("action must be start|status|stop")
    argv = {"start": [binary, "preview", "--background", "--json", "--no-open"],
            "status": [binary, "preview", "--status", "--json"],
            "stop": [binary, "preview", "--stop", "--json"]}[action]
    if action == "start" and args.get("port"):
        argv += ["--port", str(args["port"])]
    try:
        rc, out = _run(argv, project, DEFAULT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return _fail(f"preview {action} timed out after 120s")
    data = _json_out(out) or {}
    result = data.get("result", {}) if isinstance(data, dict) else {}
    ok = rc == 0 and data.get("ok") is not False
    if action == "start" and result.get("state") == "started" and result.get("ready"):
        ok = True
    return json.dumps({
        "success": ok, "action": action, "project": project,
        "studioUrl": result.get("studioUrl"),
        "serverUrl": result.get("serverUrl"),
        "state": result.get("state"),
        "raw": data if data else out[-2000:],
    })


def h_preview_context(args: dict, **kw) -> str:
    project = _resolve_project(args)
    if not project:
        return _fail("project_dir not found; pass project_dir or set HERMES_HYPERFRAMES_PROJECT")
    binary = _resolve_bin(project)
    if not binary:
        return _fail("hyperframes CLI not found (env HERMES_HYPERFRAMES_BIN, PATH, or <project>/node_modules/.bin)")
    argv = [binary, "preview", "--context", "--json"]
    fields = args.get("fields")
    if fields:
        argv += ["--context-fields", fields]
    try:
        rc, out = _run(argv, project, DEFAULT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return _fail("preview --context timed out after 120s")
    data = _json_out(out)
    if not data:
        return _fail("no running preview to read context from (start one first)", output=out[-1500:])
    return json.dumps({"success": rc == 0, "project": project, "context": data})


def h_timeline(args: dict, **kw) -> str:
    project = _resolve_project(args)
    if not project:
        return _fail("project_dir not found; pass project_dir or set HERMES_HYPERFRAMES_PROJECT")
    binary = _resolve_bin(project)
    if not binary:
        return _fail("hyperframes CLI not found (env HERMES_HYPERFRAMES_BIN, PATH, or <project>/node_modules/.bin)")
    try:
        rc, out = _run([binary, "timeline", "--json"], project, DEFAULT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return _fail("timeline timed out after 120s")
    data = _json_out(out)
    if not data:
        return _fail("timeline --json returned no JSON", output=out[-1500:])
    return json.dumps({"success": rc == 0, "project": project, "timeline": data.get("timeline", data)})


def h_snapshot(args: dict, **kw) -> str:
    project = _resolve_project(args)
    if not project:
        return _fail("project_dir not found; pass project_dir or set HERMES_HYPERFRAMES_PROJECT")
    binary = _resolve_bin(project)
    if not binary:
        return _fail("hyperframes CLI not found (env HERMES_HYPERFRAMES_BIN, PATH, or <project>/node_modules/.bin)")
    argv = [binary, "snapshot"]
    at = args.get("at")
    if at:
        argv += ["--at", str(at)]
    frames = args.get("frames")
    if frames:
        argv += ["--frames", str(frames)]
    try:
        rc, out = _run(argv, project, DEFAULT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return _fail("snapshot timed out after 120s")
    pngs = [ln.strip() for ln in out.splitlines() if ln.strip().endswith(".png")]
    contact = [ln.strip() for ln in out.splitlines() if "contact-sheet" in ln]
    if rc != 0 or not pngs:
        return _fail("snapshot produced no PNGs", output=out[-1500:])
    return json.dumps({
        "success": True, "project": project,
        "frames": pngs,
        "contactSheet": contact[0] if contact else None,
        "hint": "vision-analyze the contactSheet (or individual frames) to verify what rendered",
    })


def h_render(args: dict, **kw) -> str:
    project = _resolve_project(args)
    if not project:
        return _fail("project_dir not found; pass project_dir or set HERMES_HYPERFRAMES_PROJECT")
    binary = _resolve_bin(project)
    if not binary:
        return _fail("hyperframes CLI not found (env HERMES_HYPERFRAMES_BIN, PATH, or <project>/node_modules/.bin)")
    output = args.get("output")
    if not output:
        return _fail("output path is required (e.g. out.mp4); absolute or relative to the project")
    argv = [binary, "render", "--output", output]
    quality = args.get("quality")
    if quality:
        argv += ["--quality", quality]
    timeout = int(args.get("timeout_s", RENDER_TIMEOUT_S))
    try:
        rc, out = _run(argv, project, timeout)
    except subprocess.TimeoutExpired:
        return _fail(f"render timed out after {timeout}s — render seeks every frame; use --quality draft for iteration")
    out_path = output if os.path.isabs(output) else os.path.join(project, output)
    return json.dumps({
        "success": rc == 0 and os.path.isfile(out_path),
        "output": out_path,
        "sizeBytes": os.path.getsize(out_path) if os.path.isfile(out_path) else None,
        "output_tail": out[-1500:],
    })


# ----------------------------------------------------------------- schemas --

_PROJECT = {"project_dir": {"type": "string", "description": "Absolute path to the hyperframes project dir (contains index.html). Defaults to env HERMES_HYPERFRAMES_PROJECT."}}

SCHEMAS = {
    "hyperframes_lint": {
        "type": "object", "properties": _PROJECT, "required": [],
        "description": "Lint a hyperframes composition for errors/warnings (media missing ids, timing mistakes). Run before preview/render.",
    },
    "hyperframes_preview": {
        "type": "object",
        "properties": {**_PROJECT, "action": {"type": "string", "enum": ["start", "status", "stop"], "description": "start (default) launches the managed background Studio preview and reuses a healthy one; stop terminates it"}, "port": {"type": "integer", "description": "Optional port for start (default 3002)"}},
        "required": [],
        "description": "Managed Hyperframes Studio preview lifecycle. Returns studioUrl — give that URL to the human: it is the full frame-true timeline editor for the composition.",
    },
    "hyperframes_preview_context": {
        "type": "object",
        "properties": {**_PROJECT, "fields": {"type": "string", "description": "Optional comma-separated slices: selection,lint,server,capabilities"}},
        "required": [],
        "description": "Read the running Studio's agent context, especially the human's current selection (element, timeline time, bounding box) — the bridge for deictic edit instructions like 'move this' or 'cut here'. Requires a running preview.",
    },
    "hyperframes_timeline": {
        "type": "object", "properties": _PROJECT, "required": [],
        "description": "Print the composition's tracks and clips as JSON (start, duration, file, track per clip).",
    },
    "hyperframes_snapshot": {
        "type": "object",
        "properties": {**_PROJECT, "at": {"type": "string", "description": "Comma-separated timestamps in seconds (e.g. '5,300,900') for exact instants"}, "frames": {"type": "integer", "description": "Number of evenly-spaced frames (default 5) when --at is omitted"}},
        "required": [],
        "description": "Capture composition frames as PNGs for visual verification. Pair the returned contactSheet with a vision tool — never judge a composition from code alone.",
    },
    "hyperframes_render": {
        "type": "object",
        "properties": {**_PROJECT, "output": {"type": "string", "description": "Output video path (mp4), relative to the project or absolute"}, "quality": {"type": "string", "enum": ["draft", "looks", "delivery"], "description": "draft = fast iteration, looks = default, delivery = final"}, "timeout_s": {"type": "integer", "description": "Render timeout, default 1800s"}},
        "required": ["output"],
        "description": "Render the composition to a video file. Slow by design (frame-by-frame seek); prefer draft quality while iterating, and render only after the human approved the preview.",
    },
}


def register(ctx) -> None:
    handlers = {
        "hyperframes_lint": (h_lint, "✅"),
        "hyperframes_preview": (h_preview, "▶️"),
        "hyperframes_preview_context": (h_preview_context, "🎯"),
        "hyperframes_timeline": (h_timeline, "📏"),
        "hyperframes_snapshot": (h_snapshot, "📸"),
        "hyperframes_render": (h_render, "🎞️"),
    }
    for name, (handler, emoji) in handlers.items():
        ctx.register_tool(
            name=name, toolset=TOOLSET, schema=SCHEMAS[name],
            handler=lambda args, _h=handler, **kw: _h(args, **kw),
            check_fn=_check, emoji=emoji)
