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

Project confinement: ``project_dir`` must resolve under a configured projects
root (config ``plugins.entries.hermes-hyperframes.settings.projects_roots``,
env ``HERMES_HYPERFRAMES_PROJECTS_ROOTS``, then the workspace ``projects`` dir
/ the plugin.yaml tilde defaults). The project-local
``node_modules/.bin/hyperframes`` fallback runs the PROJECT'S OWN binary on
the host, so a cloned, untrusted repo outside the roots never qualifies.

Bin resolution order (first hit wins): env/settings ``bin``, `hyperframes`
on PATH, <project>/node_modules/.bin/hyperframes. Default project comes from
the caller or settings/env HERMES_HYPERFRAMES_PROJECT.

Subprocess environment: minimal on purpose (PATH, HOME, PWD, locale, TMPDIR,
Windows equivalents) — never ``os.environ``, so no provider key reaches the
Node binary. Extra names can be opted in per deployment via the
``env_passthrough`` setting (a list of env var names).

Stdlib only (gateway has no pytest/PyYAML); hermes_cli/agent imports are
best-effort so bare test harnesses can load the module too. Handlers return
{"success": bool, ...} JSON strings.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

TOOLSET = "hyperframes"
DEFAULT_TIMEOUT_S = 120
RENDER_TIMEOUT_S = 1800

# Same tilde defaults as plugin.yaml config_schema.projects_roots.
DEFAULT_PROJECTS_ROOTS = ["~/projects/personal", "~/projects/clients"]

# Env vars the CLI subprocess may inherit. Anything else — provider keys
# above all — stays out (catalog review: minimal environment for the CLI).
_CLI_ENV_ALLOW = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
    "TMPDIR", "TEMP", "TMP",
    # Node children on Windows need these to even start
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PROGRAMFILES",
)

VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}


# ------------------------------------------------------- config accessors --
# Best-effort everywhere: outside a Hermes process (bare test harness, the
# validator) settings resolve empty and the built-in defaults apply.

def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def _plugin_settings() -> dict:
    try:
        from hermes_cli.config import load_config_readonly  # type: ignore
        config = load_config_readonly() or {}
        return (config.get("plugins", {}).get("entries", {})
                .get("hermes-hyperframes", {}).get("settings", {}) or {})
    except Exception:
        return {}


def _setting(name: str) -> str | None:
    value = _plugin_settings().get(name)
    return str(value).strip() if value not in (None, "") else None


def _setting_list(name: str) -> list[str]:
    raw = _plugin_settings().get(name)
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, list):
        items = [str(part) for part in raw]
    else:
        return []
    return [part.strip() for part in items if part.strip()]


def projects_roots() -> list[Path]:
    """Allowed project roots, resolved: settings projects_roots > env
    HERMES_HYPERFRAMES_PROJECTS_ROOTS > workspace <home>/projects > the
    plugin.yaml tilde defaults. Existing dirs only; ``~`` expanded."""
    items = _setting_list("projects_roots")
    if not items:
        env_raw = (os.environ.get("HERMES_HYPERFRAMES_PROJECTS_ROOTS") or "").strip()
        items = [part.strip() for part in env_raw.split(",") if part.strip()]
    if not items:
        workspace = _hermes_home() / "projects"
        items = [str(workspace)] if workspace.is_dir() else list(DEFAULT_PROJECTS_ROOTS)
    roots: list[Path] = []
    for item in items:
        path = Path(os.path.expanduser(item)).resolve()
        if path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def _project_roots_error(project: str) -> str:
    return (
        f"project_dir {project} is outside the allowed projects roots — the "
        "project-local hyperframes binary would run, so unlisted directories "
        "are refused. Add the dir (or its parent) to "
        "plugins.entries.hermes-hyperframes.settings.projects_roots in "
        "config.yaml or set HERMES_HYPERFRAMES_PROJECTS_ROOTS. "
        f"Allowed roots: {', '.join(str(r) for r in projects_roots()) or '(none configured)'}"
    )


# ------------------------------------------------------------ resolution --

def _resolve_bin(project_dir: str) -> str | None:
    env_bin = _setting("bin") or os.environ.get("HERMES_HYPERFRAMES_BIN")
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin
    on_path = shutil.which("hyperframes")
    if on_path:
        return on_path
    local = os.path.join(project_dir, "node_modules", ".bin", "hyperframes")
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return local
    return None


def _resolve_project(args: dict) -> tuple[str | None, str | None]:
    """(project_dir, error) — an error when unset, missing, or OUTSIDE the
    configured projects roots (see module docstring for why that gate is
    load-bearing)."""
    project = (args.get("project_dir") or _setting("project")
               or os.environ.get("HERMES_HYPERFRAMES_PROJECT") or "")
    if not project:
        return None, ("project_dir not found; pass project_dir or set "
                      "settings/env HERMES_HYPERFRAMES_PROJECT")
    if not os.path.isdir(project):
        return None, f"project_dir not found: {project}"
    resolved = os.path.abspath(project)
    real = Path(resolved).resolve()
    for root in projects_roots():
        try:
            real.relative_to(root)
            return resolved, None
        except ValueError:
            continue
    return None, _project_roots_error(resolved)


def _cli_env(cwd: str) -> dict:
    # hyperframes derives the project name from $PWD, not the process cwd —
    # normalize it or reuse-resolution picks the wrong project fragment.
    env = {name: os.environ[name] for name in _CLI_ENV_ALLOW if os.environ.get(name)}
    env["PWD"] = cwd
    # Explicit opt-in passthrough (settings env_passthrough: list of names) —
    # never a wildcard, never os.environ.
    for name in _setting_list("env_passthrough"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


def _run(argv: list[str], cwd: str, timeout: int) -> tuple[int, str]:
    proc = subprocess.run(
        argv, cwd=cwd, env=_cli_env(cwd), timeout=timeout, check=False,
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


def _check() -> bool:
    """Visible only when node exists (hyperframes projects are npm-based)."""
    return bool(shutil.which("node"))


def _project_and_bin(args: dict) -> tuple[str, str, str | None]:
    """(project, binary, error) — error is set (and the first two None) when
    the project is unresolvable/outside the roots or no binary was found."""
    project, err = _resolve_project(args)
    if err or project is None:
        return None, None, err or "unresolvable project_dir"  # type: ignore[return-value]
    binary = _resolve_bin(project)
    if not binary:
        return None, None, ("hyperframes CLI not found (settings/env bin, PATH, "  # type: ignore[return-value]
                            "or <project>/node_modules/.bin)")
    return project, binary, None


# ---------------------------------------------------------------- handlers --

# "◇ 0 error(s), 4 warning(s)" and per-composition "N error(s)" lines.
_LINT_ERROR_COUNT = re.compile(r"(\d+)\s+error", re.IGNORECASE)


def h_lint(args: dict, **kw) -> str:
    project, binary, err = _project_and_bin(args)
    if err:
        return _fail(err)
    try:
        rc, out = _run([binary, "lint"], project, DEFAULT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return _fail("lint timed out after 120s")
    # Count errors, never substring-match: every CLI summary line contains
    # the literal "error(s)", so `"error(s)" not in line` was always False.
    counts = [int(g) for g in _LINT_ERROR_COUNT.findall(out)]
    return json.dumps({
        "success": rc == 0 and not any(counts),
        "project": project,
        "summary": out.splitlines()[-1].strip() if out.splitlines() else "",
        "output": out,
    })


def h_preview(args: dict, **kw) -> str:
    project, binary, err = _project_and_bin(args)
    if err:
        return _fail(err)
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
    project, binary, err = _project_and_bin(args)
    if err:
        return _fail(err)
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
    project, binary, err = _project_and_bin(args)
    if err:
        return _fail(err)
    try:
        rc, out = _run([binary, "timeline", "--json"], project, DEFAULT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return _fail("timeline timed out after 120s")
    data = _json_out(out)
    if not data:
        return _fail("timeline --json returned no JSON", output=out[-1500:])
    return json.dumps({"success": rc == 0, "project": project, "timeline": data.get("timeline", data)})


def h_snapshot(args: dict, **kw) -> str:
    project, binary, err = _project_and_bin(args)
    if err:
        return _fail(err)
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


def _confine_render_output(project: str, output: str) -> tuple[str | None, str | None]:
    """(resolved_output, error): the output must resolve INSIDE the project
    dir (``..``, outside absolute paths and symlink escapes refused) and an
    existing non-video file is never overwritten."""
    project_real = Path(project).resolve()
    candidate = Path(output)
    raw = candidate if candidate.is_absolute() else project_real / candidate
    resolved = Path(os.path.realpath(raw))
    try:
        resolved.relative_to(project_real)
    except ValueError:
        return None, ("output must resolve inside the project dir — '..' segments, "
                      "paths outside it and symlink escapes are refused")
    if resolved.exists() and resolved.suffix.lower() not in VIDEO_EXTS:
        return None, (f"refusing to overwrite existing non-video file {resolved} — "
                      "pick a video filename (e.g. out.mp4) or a new path inside the project")
    return str(resolved), None


def h_render(args: dict, **kw) -> str:
    project, binary, err = _project_and_bin(args)
    if err:
        return _fail(err)
    output = args.get("output")
    if not output:
        return _fail("output path is required (e.g. out.mp4), inside the project dir")
    out_path, err = _confine_render_output(project, output)
    if err:
        return _fail(err)
    argv = [binary, "render", "--output", output]
    quality = args.get("quality")
    if quality:
        argv += ["--quality", quality]
    timeout = int(args.get("timeout_s", RENDER_TIMEOUT_S))
    try:
        rc, out = _run(argv, project, timeout)
    except subprocess.TimeoutExpired:
        return _fail(f"render timed out after {timeout}s — render seeks every frame; use --quality draft for iteration")
    return json.dumps({
        "success": rc == 0 and os.path.isfile(out_path),
        "output": out_path,
        "sizeBytes": os.path.getsize(out_path) if os.path.isfile(out_path) else None,
        "output_tail": out[-1500:],
    })


# ----------------------------------------------------------------- schemas --

_PROJECT = {"project_dir": {"type": "string", "description": "Absolute path to the hyperframes project dir (contains index.html). Must sit under a configured projects root (plugins.entries.hermes-hyperframes.settings.projects_roots). Defaults to settings/env HERMES_HYPERFRAMES_PROJECT."}}

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
        "properties": {**_PROJECT, "output": {"type": "string", "description": "Output video path (mp4), relative to the project or absolute inside it — paths resolving outside the project dir are refused"}, "quality": {"type": "string", "enum": ["draft", "looks", "delivery"], "description": "draft = fast iteration, looks = default, delivery = final"}, "timeout_s": {"type": "integer", "description": "Render timeout, default 1800s"}},
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
