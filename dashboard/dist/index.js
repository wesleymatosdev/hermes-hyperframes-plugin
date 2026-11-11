/**
 * hermes-hyperframes — Hermes Dashboard Plugin (frontend)
 *
 * Minimal Hyperframes tab: one toolbar row (project switcher, preview state,
 * Open Studio, lint/duration), the big Studio preview embed, and the agent
 * chat. Styling consumes the dashboard theme's own CSS custom properties —
 * the same vocabulary the stock kanban plugin uses — so the tab reads as
 * part of the dashboard instead of its own themed page.
 *
 * Plain IIFE, no build step. Uses window.__HERMES_PLUGIN_SDK__ for React and
 * the authed fetch. Bail out silently when the SDK global is absent.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const { React } = SDK;
  const h = React.createElement;
  const { useState, useEffect, useCallback, useRef } = SDK.hooks || React;

  const API = "/api/plugins/hermes-hyperframes";
  const ASSET_BASE = "/dashboard-plugins/hermes-hyperframes";
  const STATUS_POLL_MS = 10000;

  function authedFetchJson(url, options) {
    return (SDK.authedFetch || fetch)(url, options).then(function (resp) {
      return resp.text().then(function (txt) {
        if (!resp.ok) {
          let detail = txt;
          try {
            const j = JSON.parse(txt);
            if (j && j.detail !== undefined && j.detail !== null) detail = JSON.stringify({ detail: j.detail });
          } catch (_e) { /* raw */ }
          throw new Error(resp.status + ": " + detail);
        }
        try { return JSON.parse(txt); } catch (_e) { return {}; }
      });
    });
  }

  function postJson(url, body) {
    return authedFetchJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  }

  // Typed plugin-API failure ({detail:{kind,message}}) → honest one-line message.
  function errorMessage(err) {
    const raw = (err && err.message) ? String(err.message) : String(err || "");
    const m = raw.match(/^(\d{3}):\s*(.*)$/s);
    const body = m ? m[2] : raw;
    try {
      const parsed = JSON.parse(body);
      const detail = parsed && parsed.detail;
      if (detail) {
        if (typeof detail === "object") return String(detail.message || detail.kind || "request failed");
        return String(detail);
      }
    } catch (_e) { /* not JSON */ }
    return body || raw || "request failed";
  }

  // Disk-served manifest version vs the backend's import-time plugin_version:
  // the dashboard imports plugin_api.py ONCE at startup, this file and the
  // manifest are served fresh from disk — a mismatch means this UI is newer
  // than the process serving it. Banner a reload instead of misbehaving.
  let manifestVersionPromise = null;
  function loadManifestVersion() {
    if (!manifestVersionPromise) {
      manifestVersionPromise = fetch(ASSET_BASE + "/manifest.json")
        .then(function (r) { return r.ok ? r.json() : {}; })
        .catch(function () { return {}; });
    }
    return manifestVersionPromise;
  }

  function useManifestVersion() {
    const [v, setV] = useState(null);
    useEffect(function () {
      let alive = true;
      loadManifestVersion().then(function (m) { if (alive) setV((m && m.version) || ""); });
      return function () { alive = false; };
    }, []);
    return v;
  }

  function formatDuration(seconds) {
    if (seconds == null || isNaN(Number(seconds))) return "";
    const s = Math.round(Number(seconds));
    if (s < 60) return s + "s";
    const m = Math.floor(s / 60), rem = s % 60;
    if (m < 60) return m + "m " + (rem ? rem + "s" : "");
    return Math.floor(m / 60) + "h " + (m % 60) + "m";
  }

  // --- Toolbar -----------------------------------------------------------------

  function Toolbar(props) {
    const projects = props.projects || [];
    const project = props.project;
    const st = props.status || {};
    return h("div", { className: "hf-toolbar" },
      h("label", { className: "hf-toolbar-label", htmlFor: "hf-project-select" }, "Project"),
      h("select", {
        id: "hf-project-select",
        className: "hf-project-select",
        value: props.current || "",
        disabled: props.busy,
        onChange: function (e) { props.onSelect(e.target.value); },
      },
        h("option", { value: "", disabled: true },
          projects.length ? "Select a project…" : "No hyperframes projects found"),
        projects.map(function (p) {
          const bits = [];
          if (p.duration != null) bits.push(formatDuration(p.duration));
          if (p.has_preview_running) bits.push("preview running");
          const suffix = bits.length ? " — " + bits.join(" · ") : "";
          return h("option", { key: p.path, value: p.path }, p.name + suffix);
        })),
      h("button", {
        className: "hf-btn hf-btn-ghost", onClick: props.onRefresh, disabled: props.busy,
        title: "Rescan the configured project folders",
      }, "Rescan"),
      h("span", { className: "hf-toolbar-sep" }),
      h("span", {
        className: "hf-chip " + (st.running ? "hf-chip-on" : ""),
        title: "Managed Studio preview state",
      }, st.running ? "Preview running" : "Preview stopped"),
      st.studioUrl
        ? h("a", {
            className: "hf-studio-link",
            href: st.studioUrl, target: "_blank", rel: "noreferrer",
            title: "Open the full frame-true Studio editor in a new tab",
          }, "Open Studio ↗")
        : null,
      h("span", { className: "hf-toolbar-facts" },
        (project && project.lint_summary ? "Lint: " + project.lint_summary : "Lint: —")
        + (project && project.duration != null
          ? "  ·  Duration: " + formatDuration(project.duration)
          : "")));
  }

  // --- Preview panel -------------------------------------------------------------

  function PreviewPanel(props) {
    const st = props.status || {};
    const wrapRef = useRef(null);
    const [isFullscreen, setIsFullscreen] = useState(false);

    // Fullscreen the whole preview wrapper (controls + iframe), not just the
    // iframe element — the Studio's own fullscreen button also works now that
    // the iframe delegates fullscreen permission (allow includes "fullscreen"
    // + allowFullscreen, verified: without them requestFullscreen() inside
    // the iframe was silently refused by the browser).
    useEffect(function () {
      function onChange() {
        setIsFullscreen(document.fullscreenElement === wrapRef.current);
      }
      document.addEventListener("fullscreenchange", onChange);
      return function () { document.removeEventListener("fullscreenchange", onChange); };
    }, []);

    const toggleFullscreen = useCallback(function () {
      if (document.fullscreenElement) {
        document.exitFullscreen().catch(function () { /* leaving anyway */ });
      } else if (wrapRef.current && wrapRef.current.requestFullscreen) {
        wrapRef.current.requestFullscreen().catch(function (_e) { /* refused */ });
      }
    }, []);

    return h("div", { className: "hf-preview" + (isFullscreen ? " hf-preview-fullscreen" : ""), ref: wrapRef },
      h("div", { className: "hf-preview-controls" },
        h("button", {
          className: "hf-btn hf-btn-primary",
          disabled: props.busy,
          onClick: function () { props.onAction("start"); },
        }, "Start preview"),
        h("button", {
          className: "hf-btn hf-btn-ghost",
          disabled: props.busy,
          onClick: function () { props.onAction("stop"); },
        }, "Stop"),
        h("span", { className: "hf-spacer" }),
        h("button", {
          className: "hf-btn hf-btn-ghost",
          onClick: toggleFullscreen,
          title: isFullscreen ? "Exit fullscreen (Esc)" : "Fullscreen the preview",
        }, isFullscreen ? "Exit fullscreen" : "Fullscreen")),
      st.running && st.studioUrl
        ? h("iframe", {
            className: "hf-frame",
            src: st.studioUrl,
            title: "Hyperframes Studio preview",
            allow: "clipboard-write; fullscreen",
            allowFullscreen: true,
          })
        : h("div", { className: "hf-frame hf-frame-empty" },
            h("div", { className: "hf-frame-empty-text" },
              st.running
                ? "Preview is running — the Studio URL appears once reported."
                : "No preview running. Start it to see the composition here, or open Studio in its own tab.")));
  }

  // --- Chat panel -----------------------------------------------------------------

  // A real agent turn takes 1-5 minutes; the POST only ACCEPTS the message
  // (202) and the server runs the turn. This panel polls /chat/status until
  // the turn settles — so progress is visible, a page reload resumes waiting
  // from server state, and nothing is ever stuck on "Sending…".
  const CHAT_POLL_MS = 2000;
  // Stop polling well past the server-side turn timeout (300s): the turn may
  // still complete server-side, but this tab stops spinning and says so.
  const CHAT_POLL_CAP_MS = 330000;

  function ChatPanel(props) {
    const [messages, setMessages] = useState(null);
    const [input, setInput] = useState("");
    const [err, setErr] = useState("");
    const [lane, setLane] = useState("");
    const [sessionId, setSessionId] = useState("");
    const [turn, setTurn] = useState(null); // {queuedAt, text} while a turn is in flight
    const [elapsed, setElapsed] = useState(0);
    const scrollRef = useRef(null);

    const load = useCallback(function () {
      return authedFetchJson(API + "/messages").then(function (d) {
        setMessages(d.messages || []);
        setLane(d.lane || "");
        setSessionId(d.sessionId || "");
      }).catch(function (e) { setErr(errorMessage(e)); });
    }, []);

    // Reload history + resume a pending turn on mount (a page reload mid-turn
    // lands here: the turn lives server-side, not in this fetch).
    useEffect(function () {
      load();
      authedFetchJson(API + "/chat/status").then(function (st) {
        if (st && st.pending) setTurn({ queuedAt: st.queuedAt || Date.now() / 1000, text: "" });
      }).catch(function (_e) { /* status is optional surface */ });
    }, [load]);

    // The server-side turn watcher: one interval while a turn is pending.
    useEffect(function () {
      if (!turn) return undefined;
      const started = Date.now();
      const tick = window.setInterval(function () {
        setElapsed(Math.round((Date.now() - started) / 1000));
      }, 1000);
      const poll = window.setInterval(function () {
        authedFetchJson(API + "/chat/status").then(function (st) {
          if (Date.now() - started > CHAT_POLL_CAP_MS) {
            window.clearInterval(poll); window.clearInterval(tick);
            setTurn(null);
            setErr("The agent is still working — this turn is taking unusually long. " +
                   "Reload in a moment; the reply will appear here once it lands.");
            return;
          }
          if (st && !st.pending) {
            window.clearInterval(poll); window.clearInterval(tick);
            setTurn(null);
            if (st.lastError && st.lastError.message) {
              setErr(String(st.lastError.message));
            }
            load();
          }
        }).catch(function (_e) { /* transient poll failures keep the wait alive */ });
      }, CHAT_POLL_MS);
      return function () { window.clearInterval(poll); window.clearInterval(tick); };
    }, [turn, load]);

    useEffect(function () {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    }, [messages, turn, elapsed]);

    const send = useCallback(function () {
      const text = input.trim();
      if (!text || turn) return;
      setErr("");
      setInput("");
      // Optimistic user bubble; the post-turn history reload replaces it with
      // the persisted rows (user + assistant).
      setMessages(function (prev) {
        const base = Array.isArray(prev) ? prev : [];
        return base.concat([{ role: "user", text: text, pending: true }]);
      });
      setTurn({ queuedAt: Date.now() / 1000, text: text });
      postJson(API + "/messages", { text: text })
        .catch(function (e) {
          // Not accepted: give the text back and stop the wait. A 409
          // turn_in_progress means the previous turn is still running.
          setTurn(null);
          setMessages(function (prev) {
            const base = Array.isArray(prev) ? prev : [];
            return base.filter(function (m) { return !(m.pending && m.text === text); });
          });
          setInput(text);
          setErr(errorMessage(e));
        });
    }, [input, turn]);

    const reset = useCallback(function () {
      if (turn) return;
      setErr("");
      postJson(API + "/messages/reset", {})
        .then(function () { return load(); })
        .catch(function (e) { setErr(errorMessage(e)); });
    }, [turn, load]);

    const working = !!turn;
    const turnErrorIsBusy = /turn_in_progress/.test(err);

    return h("div", { className: "hf-chat" },
      h("div", { className: "hf-chat-head" },
        h("span", { className: "hf-chat-title" }, "Agent chat"),
        h("span", {
          className: "hf-chip",
          title: lane === "native"
            ? "Drives a real Hermes session via the local gateway — one agent turn per message. The session id below is resumable from a terminal: hermes chat --resume <id>."
            : "Native gateway lane unavailable (needs the local gateway's api_server and its API_SERVER_KEY) — each message runs a local `hermes chat -q` model call (slow).",
        }, lane === "native" ? "native" : lane === "cli" ? "cli" : ""),
        sessionId ? h("span", { className: "hf-session-id", title: "Pinned Hermes session — resumable: hermes chat --resume " + sessionId }, sessionId) : null,
        h("span", { className: "hf-spacer" }),
        h("button", {
          className: "hf-btn hf-btn-ghost", onClick: reset, disabled: working,
          title: working
            ? "Wait for the agent to finish the current message first"
            : "Start a fresh Hermes session (clears this tab's chat history)",
        }, "New session")),
      err ? h("div", {
        className: "hf-banner hf-banner-error" + (turnErrorIsBusy ? " hf-banner-warn" : ""),
      }, turnErrorIsBusy ? "The agent is already working on your previous message — wait for it to finish, then send again." : err) : null,
      h("div", { className: "hf-chat-log", ref: scrollRef },
        messages === null
          ? h("div", { className: "hf-chat-empty" }, "Loading chat…")
          : messages.length === 0 && !working
            ? h("div", { className: "hf-chat-empty" },
                "Message the Hermes agent about this composition.",
                lane === "cli"
                  ? " The native gateway lane is unavailable — messages fall back to slow local CLI calls."
                  : null)
            : messages.map(function (m, i) {
                return h("div", {
                  key: i,
                  className: "hf-msg hf-msg-" + (m.role === "user" ? "user" : "agent") + (m.pending ? " hf-msg-pending" : ""),
                },
                  h("div", { className: "hf-msg-role" }, m.role === "user" ? "You" : "Agent"),
                  h("div", { className: "hf-msg-text" }, m.text));
              }).concat(working ? [
                // The live turn marker: visible interaction while the agent
                // works — the thing a dead "Sending…" button never gave.
                h("div", { key: "hf-turn-marker", className: "hf-msg hf-msg-agent hf-msg-working" },
                  h("div", { className: "hf-msg-role" }, "Agent"),
                  h("div", { className: "hf-msg-text hf-msg-working-text" },
                    "Working… ", h("span", { className: "hf-turn-elapsed" }, elapsed + "s"),
                    h("span", { className: "hf-turn-note" }, "  (a real agent turn; this can take a few minutes)"))),
              ] : [])),
      h("div", { className: "hf-chat-input-row" },
        h("textarea", {
          className: "hf-chat-input", rows: 2,
          placeholder: working
            ? "The agent is working on your previous message…"
            : (props.hasProject
              ? "Ask the agent about this composition…"
              : "Ask the agent… (select a project for composition context)"),
          value: input,
          disabled: working,
          onChange: function (e) { setInput(e.target.value); },
          onKeyDown: function (e) {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
          },
        }),
        h("button", {
          className: "hf-btn hf-btn-primary hf-chat-send",
          onClick: send, disabled: working || !input.trim(),
          title: working
            ? "The agent is still working on the previous message"
            : "Send (one agent turn in the pinned Hermes session)",
        }, working ? "Working…" : "Send")));
  }

  // --- Page ---------------------------------------------------------------------

  function HyperframesPage() {
    const manifestVersion = useManifestVersion();
    const [projects, setProjects] = useState(null);
    const [current, setCurrent] = useState("");
    const [status, setStatus] = useState(null);
    const [error, setError] = useState("");
    const [busy, setBusy] = useState(false);
    const [pluginVersion, setPluginVersion] = useState("");

    const currentProject = (projects || []).find(function (p) { return p.path === current; }) || null;

    const loadProjects = useCallback(function () {
      return authedFetchJson(API + "/projects").then(function (d) {
        setProjects(d.projects || []);
        setCurrent(d.current || "");
        setPluginVersion(d.pluginVersion || "");
        setError("");
      }).catch(function (e) { setError(errorMessage(e)); });
    }, []);

    const loadStatus = useCallback(function () {
      return authedFetchJson(API + "/preview/status").then(function (d) {
        setStatus(d);
      }).catch(function (_e) { /* polled — the banner shows the next hard failure */ });
    }, []);

    useEffect(function () {
      loadProjects();
      loadStatus();
      const t = window.setInterval(loadStatus, STATUS_POLL_MS);
      return function () { window.clearInterval(t); };
    }, [loadProjects, loadStatus]);

    const selectProject = useCallback(function (path) {
      if (!path) return;
      setBusy(true);
      postJson(API + "/projects/select", { path: path })
        .then(function (_d) { setCurrent(path); return loadStatus(); })
        .catch(function (e) { setError(errorMessage(e)); })
        .finally(function () { setBusy(false); });
    }, [loadStatus]);

    const previewAction = useCallback(function (action) {
      setBusy(true);
      postJson(API + "/preview", { action: action })
        .then(function (d) {
          if (d && d.studioUrl) setStatus(function (prev) {
            return Object.assign({}, prev || {}, { running: true, studioUrl: d.studioUrl, state: d.state });
          });
          return loadStatus();
        })
        .catch(function (e) { setError(errorMessage(e)); })
        .finally(function () { setBusy(false); });
    }, [loadStatus]);

    const staleBackend = manifestVersion && pluginVersion && manifestVersion !== pluginVersion;

    return h("div", { className: "hf-tab" },
      error ? h("div", { className: "hf-banner hf-banner-error" }, error) : null,
      staleBackend
        ? h("div", { className: "hf-banner-row" },
            h("span", { className: "hf-banner hf-banner-reload" },
              "Plugin updated — reload dashboard (backend v" + pluginVersion
              + ", on disk v" + manifestVersion + ")"),
            h("button", {
              className: "hf-btn hf-btn-ghost",
              title: "Reload the dashboard page to pick up the updated plugin",
              onClick: function () { window.location.reload(); },
            }, "Reload"))
        : null,
      projects === null
        ? h("div", { className: "hf-loading" }, "Loading hyperframes projects…")
        : h(Toolbar, {
            projects: projects, current: current, busy: busy,
            project: currentProject, status: status,
            onSelect: selectProject,
            onRefresh: function () { setError(""); loadProjects(); },
          }),
      h(PreviewPanel, {
        status: status, busy: busy,
        onAction: previewAction,
      }),
      h(ChatPanel, { hasProject: !!current }));
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("hermes-hyperframes", HyperframesPage);
  }
})();
