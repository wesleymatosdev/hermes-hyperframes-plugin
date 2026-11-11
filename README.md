# hermes-hyperframes

A Hermes community plugin that drives [Hyperframes](https://github.com/ApacheOpenVideo/hyperframes) — Apache-2.0 HTML/JS video compositions — from Hermes: six `hyperframes_*` agent tools plus a dashboard tab with a managed Studio preview, frame snapshots, and an agent chat bound to the composition on screen.

The plugin wraps a **pinned local Hyperframes CLI** (no `npx`, no network fetch): bin resolution is `HERMES_HYPERFRAMES_BIN` > `PATH` > `<project>/node_modules/.bin/hyperframes`.

## What it adds

**Agent tools** (available in any session with the plugin enabled):

| Tool | Purpose |
| --- | --- |
| `hyperframes_lint` | Lint a composition, structured error/warning summary |
| `hyperframes_preview` | Start / status / stop the managed Hyperframes Studio preview |
| `hyperframes_preview_context` | Studio selection bridge — what the human has selected on the canvas |
| `hyperframes_timeline` | Read back the composition timeline (clips, spans, duration) |
| `hyperframes_snapshot` | Capture contact sheets / frames for visual verification |
| `hyperframes_render` | Render the composition to MP4 |

**Dashboard tab** (`/hyperframes`, after Skills): project switcher (scans your project roots), one-toolbar-row status (preview state, lint, duration), the Studio preview embedded live, and an agent chat. The tab consumes the dashboard's own theme variables, so it follows whatever theme is active.

## Agent chat: two lanes, native by default

The chat panel drives a **real Hermes gateway session** — the same conversation a terminal sees via `hermes chat --resume <session-id>`:

- **Native lane (default, zero config):** the plugin auto-derives the local gateway's `api_server` listener from `~/.hermes/gateway_state.json` and authenticates with `API_SERVER_KEY`. Each message is one synchronous gateway turn carrying the current project path + preview state as context, so the session can act on the composition with its `hyperframes_*` tools.
- **CLI fallback:** only when no gateway credential resolves at all — one local `hermes chat -q` per message (slow).

Resolution order for the credential: plugin `api_server_key` setting > `HERMES_API_SERVER_KEY` env > Hermes home `.env` `API_SERVER_KEY`. URL: plugin `api_server_url` setting > `HERMES_API_SERVER_URL` env > `gateway_state.json` listener_base (loopback `127.0.0.1:8642`).

## Heavy actions — disclosure

This plugin spawns and stops local processes and talks to the local gateway. Specifically:

- `hyperframes_preview` **starts and kills** the Hyperframes Studio preview process for the selected project.
- `hyperframes_render` **runs the pinned Hyperframes CLI** to encode MP4 output.
- The chat lane **creates sessions in your local Hermes gateway** (`POST /api/sessions`) and **may run `hermes chat -q` as a subprocess** in the fallback lane. Every message costs a real model call.
- The chat credential read (above) touches the Hermes home `.env` — `API_SERVER_KEY` only, parsed in-process, never logged or echoed.

Everything is loopback/local: the plugin talks to the local AO-free stack — your Hyperframes CLI, your gateway, your disk. No third-party network calls, no telemetry.

## Configuration (`config_schema`)

| Setting | Default | Purpose |
| --- | --- | --- |
| `projects_roots` | `["~/projects/personal", "~/projects/clients"]` | Dirs scanned (depth 1) for hyperframes projects — a dir qualifies when it has `index.html` with `data-composition-id` and `node_modules/.bin/hyperframes` |
| `api_server_url` | `""` | Override for the gateway api_server base URL; empty auto-derives |
| `api_server_key` | `""` | Override for the gateway key; empty auto-derives |

## Requires

- A local [Hyperframes](https://www.npmjs.com/package/hyperframes) install (the CLI comes from your project's `node_modules`).
- Hermes >= 0.21 with the dashboard; the agent chat's native lane additionally wants the gateway `api_server` platform enabled.

## Install

```sh
hermes plugins install wesleymatosdev/hermes-hyperframes-plugin --ref <full-40-hex-sha> --enable
```

## Screenshots

Before/after the v0.3.0 de-clutter (nebula theme removed, tab follows the dashboard theme, snapshot gallery moved to agent tools):

![Before — custom nebula theme](docs/screenshots/before-nebula.png)
![After — minimal, theme-consistent](docs/screenshots/after-minimal.png)
![After — chat driving a real gateway session](docs/screenshots/after-chat-native.png)

## Tests

```sh
HERMES_SRC=~/.hermes/hermes-agent ~/.hermes/hermes-agent/.venv/bin/python -m unittest discover -s tests
```

The suite runs against a fake loopback gateway (`http.server` on port 0), a stub `hyperframes` bin, a stub `hermes` CLI, and a temp `HERMES_HOME` per test — no real composition project is ever touched. The auto-derivation suite (`tests/test_native_autoderive.py`) was proven RED against the pre-fix behavior before the native lane landed.

## License

MIT — see [LICENSE](LICENSE).
