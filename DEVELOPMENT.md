# Cerebro — Developer Guide

Architecture, data model, internals, and how to extend.

## Architecture

```
                        ┌───────────────────────────────────────┐
                        │  master  (one per deployment)         │
                        │                                       │
   browser  ──HTTP/WS──▶│  FastAPI  ─┬─▶ Redis (state)          │
   (xterm)              │            └─▶ /data (file backup)    │
                        │                                       │
                        │  orchestrator (claude in-container)   │
                        └────────────────▲──────────────────────┘
                                         │ WebSocket
                                         │ (binary PTY data +
                                         │  JSON control)
            ┌────────────────────────────┼────────────────────────────┐
            ▼                            ▼                            ▼
       cerebro-node                 cerebro-node                 cerebro-node
       (forks PTYs)                 (forks PTYs)                 (forks PTYs)
        host A                       host B                       host C
```

**Roles**
- **master** — single FastAPI process. Owns Redis state, runs the orchestrator meta-agent, serves the browser UI, multiplexes WebSocket data between browsers and nodes.
- **node** — daemon on each host. Forks PTY processes on demand from the master, pipes their I/O over WS. Persists its `node_id` so a restart reattaches to the same logical node.
- **browser** — vanilla JS + xterm.js. One xterm per session, one WS per terminal.

**Why this split**
- Sessions are PTYs on the node side; they survive master restarts. The node code intentionally never kills a PTY on WS disconnect — only when explicitly told.
- The master is mostly stateless beyond Redis; everything important is in Redis + a file backup. Moving the master is therefore tractable (see [Migration plan](#migration-plan)).

## Data model (Redis)

```
nodes:{node_id}                → JSON NodeInfo, TTL 30s (refreshed by heartbeat)
nodes:all                      → SET<node_id>
nodes:names                    → HASH<node_id, name>  (TTL-less, survives expiry)

agents:{agent_id}              → JSON AgentInfo  (a "session" in UI parlance)
agents:by_node:{node_id}       → SET<agent_id>
agents:all                     → SET<agent_id>

folders:{folder_id}            → JSON FolderInfo  (has section: "default"|"favorite")
folders:all                    → SET<folder_id>

terminals:{terminal_id}        → JSON TerminalInfo  (PTY primitive)
terminals:by_agent:{agent_id}  → SET<terminal_id>
terminals:by_node:{node_id}    → SET<terminal_id>

replay_buffer:{terminal_id}    → LIST<bytes>  (rolling ~1 MB, last 256 chunks)
```

`AgentInfo` carries `plugin_id`, `plugin_options`, `folder_id`, `is_favorite`, `position`, plus legacy convenience fields (`cwd`, `claude_session_id`, `dangerously_skip_permissions`). The legacy fields are computed from `plugin_options` for the `claude` plugin and persist for backwards compatibility.

Every minute the entire state is also snapshotted to `/data/cerebro_backup.json` so that a Redis flush is recoverable (`persistence.py`).

## Terminology

| UI         | Code        | Notes                                                              |
|------------|-------------|--------------------------------------------------------------------|
| session    | `agent_id`  | The data model still says "agent" for compatibility with the API.  |
| conductor  | dashboard   | Orchestrator meta-agent route (`#/dashboard` still works).         |
| folder     | folder      | Sidebar grouping. Has `section: "default"|"favorite"`.             |
| section    | favorite/   | Two-namespace sidebar (favorites on top, default below).           |
|            | default     | Folders cannot mix items across sections.                          |
| plugin     | plugin      | JSON manifest defining what command spawns the session.            |

## Plugin manifest

Manifests live in `server/cerebro_server/plugins/` (shipped) or `/data/plugins/` (user override — user wins by id). The picker grid lists everything from both, the options form is generated from the manifest's `options[]`, and `command + args` are sent to the node verbatim at spawn time.

```json
{
  "id": "claude",
  "label": "Claude Code",
  "icon": "🧠",
  "color": "#00ff88",
  "command": "claude",
  "args": [
    "--session-id={session_id}",
    {"if": "skip_perms", "then": "--dangerously-skip-permissions"}
  ],
  "options": [
    {"key": "name",       "type": "string", "label": "name", "optional": true},
    {"key": "cwd",        "type": "path",   "label": "working directory", "default": "~"},
    {"key": "skip_perms", "type": "bool",   "label": "skip permissions",  "default": true}
  ],
  "auto_fields": {"session_id": "uuid"},
  "behaviors": ["resumable", "claude_jsonl_stats", "dashboard_visible"]
}
```

**Fields**
- `id` — unique key. User manifest with same id overrides built-in.
- `command` — shell command name (resolved via `PATH` on the node).
- `args` — list, each entry either:
  - `"string with {option_key} placeholders"` — interpolated from options.
  - `{"if": "<option_key>", "then": "<arg>"}` — included only if option truthy. `then` may be a string or a list of strings.
- `options[]` — UI form fields. Types: `string`, `number`, `bool`, `path` (renders the path browser, calls `/api/nodes/{id}/ls`).
- `auto_fields` — generated by master at spawn time. Currently supported: `uuid`. Goes into `plugin_options`, persisted with the agent, available for placeholder interpolation.
- `behaviors[]` — opt-in feature flags read by the UI/server. Recognised:
  - `resumable` — sidebar shows the resume affordance for dead sessions.
  - `claude_jsonl_stats` — server reads message counts from `~/.claude/projects/.../<session>.jsonl`.
  - `dashboard_visible` — visible to the orchestrator's `cerebro-ctl list`.

**Adding a plugin** — drop a `.json` next to the others, restart master (or wait for next plugin list refresh). No code changes.

## Master ↔ node WS protocol

Path: `/ws/node/{node_id}?token=<CEREBRO_TOKEN>`. Single long-lived WS per node.

**Binary frames** — PTY data, prefixed with a 16-byte terminal_id UUID. See `protocol.py` (`pack` / `unpack`).

**JSON frames** — control messages, both directions:

| direction        | type              | payload                                           |
|------------------|-------------------|---------------------------------------------------|
| master → node    | `create_terminal` | `terminal_id, kind, cols, rows, options{command,cwd,…}` |
| master → node    | `kill_terminal`   | `terminal_id`                                     |
| master → node    | `resize`          | `terminal_id, cols, rows`                         |
| master → node    | `read_session`    | `request_id, session_id, last` (claude JSONL)     |
| node → master    | `terminal_started`| `terminal_id, pid`                                |
| node → master    | `terminal_dead`   | `terminal_id, exit_code, signal`                  |
| node → master    | `session_read`    | `request_id, messages[]`                          |

Master tracks pending creates and reads via `asyncio.Future`s keyed by id; node responds in-order. If the WS drops mid-spawn the future times out and the registry entry is rolled back.

## Code layout

```
server/cerebro_server/
├── main.py                  FastAPI app + lifespan (registry connect/close, persistence backup)
├── auth.py                  Bearer token middleware
├── models.py                Pydantic — NodeInfo, AgentInfo, FolderInfo, TerminalInfo, requests
├── registry.py              Redis wrapper (all schema lives here)
├── hub.py                   In-memory live WS connections (node hub + browser hub)
├── persistence.py           File backup loop (/data/cerebro_backup.json)
├── plugins_loader.py        Manifest discovery, rendering, auto_fields, build_command()
├── funny_names.py           wandb-style name generator
├── orchestrator_manager.py  Lifecycle of the orchestrator meta-agent
├── protocol.py              WS binary frame pack/unpack
├── routers/
│   ├── nodes.py             /api/nodes, /api/nodes/{id}/{heartbeat,ls,…}
│   ├── agents.py            /api/agents — create/update/delete/resume + section enforcement
│   ├── folders.py           /api/folders — section-scoped CRUD
│   ├── plugins.py           /api/plugins — list manifests
│   └── ws.py                /ws/node/{id}, /ws/terminal/{id}, /ws/orchestrator
├── static/
│   ├── index.html           shell with templates for each route
│   ├── app.js               sidebar logic, drag-drop, picker, options form, xterm mount
│   └── style.css
├── plugins/                 built-in plugin manifests (claude, bash, htop)
└── orchestrator/
    └── CLAUDE.md            system prompt for the orchestrator agent

client/cerebro_node/
├── cli.py                   `cerebro` / `cerebro-node` entry point
├── node.py                  daemon — heartbeat loop + WS loop + terminal lifecycle
├── pty_session.py           pty.fork() + PATH augmentation + exec
├── tui.py                   in-terminal client (Ctrl+] to cycle sessions)
└── protocol.py              binary frame helpers (matches server-side)
```

## Local development

### Master (no Docker, faster iteration)

```bash
cd server
python -m venv .venv && . .venv/bin/activate
pip install -e .
# Need a Redis somewhere; the easiest is the docker-compose redis service:
docker compose up -d redis
REDIS_URL=redis://localhost:6379 CEREBRO_TOKEN=dev cerebro-server
```

Edit `cerebro_server/static/*` and refresh the browser — assets are served by FastAPI's `StaticFiles`. For Python changes, restart.

### Node

```bash
cd client
pip install -e .
cerebro start --master http://localhost:8000 --token dev
```

`pip install -e .` is editable — edit `cerebro_node/*` and restart the daemon. The PTY fork-then-exec means the **new** code only takes effect on **new** sessions.

### Docker rebuild (when only static assets changed)

```bash
docker compose build cerebro-server && docker compose up -d cerebro-server
```

The static folder is baked into the image via `package_data` in `pyproject.toml`. Bumping `?v=N` in `index.html` busts the browser cache.

## Tests

Playwright-driven UI tests covering picker, drag-drop, folders, sections, rename, kill, navigation. Run against a live deployment.

```bash
pip install --user playwright
playwright install chromium
python tests/test_arc_deep.py    # 12 scenarios, ~30s
```

Tests are intentionally end-to-end (no mocking) so they catch real WS/Redis/PTY regressions. Tests clean up their own sessions/folders.

> **TODO**: tests currently live in `/tmp/test_arc_*.py` for the author. Move under `tests/` and document fixtures.

## Migration plan

The end goal is one-shot master migration without losing sessions:

```bash
# old master
cerebro-server export migrate.tar     # Redis snapshot + /data backup + .env config
scp migrate.tar new-host:

# new master
cerebro-server import migrate.tar     # restores Redis, /data, registers itself
# then re-point each node:
cerebro reconfigure --master http://new-host:8000   # rewrites ~/.cerebro/config.json, HUPs daemon
```

Why this works: sessions are PTYs on the node side; they only need a fresh WS to the new master. The new master finds existing terminal records in Redis and resumes multiplexing.

Manual procedure (until commands ship) is documented in the chat log; in short: tar-copy the `redis_data` + `cerebro_data` Docker volumes, `.env`, point nodes at the new host.

## Security

**Threat model.** Single shared bearer token. One trust level: anyone with the token can spawn any session on any node, which is full RCE on every node. There is no concept of users or scoped permissions.

**What's in the box.**
- `POST /api/login {token}` → httpOnly `Secure SameSite=Strict` cookie. Browser auth uses the cookie, not `localStorage`; the token never appears in WS URLs.
- `Authorization: Bearer` is still accepted everywhere — nodes, `cerebro-ctl`, CI clients work unchanged.
- Constant-time token comparison (`hmac.compare_digest`).
- Per-IP login rate limit: 5 fails / 60s → 429. In-memory; resets on master restart.
- Optional `CEREBRO_ALLOWED_ORIGINS` env (comma-separated). When set, WS upgrades from other Origins are dropped. Leave unset on a trusted LAN.
- Append-only audit log at `/data/audit.log` (JSON lines): every agent create / delete / resume with `ts, action, ip, ua, agent_id, plugin_id, node_id, name`.

**What's NOT in the box (and needs to be).**
- TLS — terminate at a reverse proxy. See [`deploy/Caddyfile.example`](deploy/Caddyfile.example).
- Per-user identity, per-plugin scopes — out of scope for the current model.
- Plugin manifest signing — `/data/plugins/*.json` is trusted; treat write access to that directory as equivalent to root.
- Sandboxing of spawned processes — they inherit the node user's full FS + network.
- Audit log rotation — `/data/audit.log` grows forever. Wrap with `logrotate` or symlink to a syslog source.

**Operational recommendations for public exposure.**
- Always front with TLS. The cookie is set `Secure` based on `X-Forwarded-Proto`; if that header isn't `https`, the cookie won't survive a same-site reload on iOS Safari.
- Set `CEREBRO_ALLOWED_ORIGINS` to your one canonical hostname.
- Rotate the token (`cerebro-server token --rotate`) after any incident. Then restart master and re-point nodes (`cerebro start --token <new>` on each).
- Don't share the URL with people you wouldn't share `sudo` with.

## Conventions

- **Naming**: UI says "session", code says `agent_id`. Don't rename the API field — too much churn for too little win.
- **Folders are section-scoped**: when an agent's `is_favorite` changes, its `folder_id` is cleared server-side; when assigning a `folder_id`, the agent's `is_favorite` is forced to match the folder's `section`.
- **Funny names**: any unnamed agent at create-time gets one. The legacy `--session-id <uuid>` form was replaced everywhere with `--session-id=<uuid>` because the plugin manifest renders that form; both work with claude.
- **PATH augmentation in PTY child**: `~/.local/bin`, `~/.npm-global/bin`, `~/.cargo/bin`, `/opt/homebrew/bin`, `/usr/local/bin` are prepended. The daemon often runs under systemd/setsid with a stripped `PATH` that doesn't include user-installed tools.
- **No TLS**: front with nginx/Caddy in production. Single shared bearer token across all hosts and the browser.

## Contributing

PRs welcome. Useful things to add:

- More plugin manifests (the cheapest way to make Cerebro more useful).
- A real test runner under `tests/` (the existing scripts are a great starting point).
- The `cerebro-server export/import` commands (see [Migration plan](#migration-plan)).
- Keyboard navigation in the sidebar (Arc-style cmd-up/down to jump tabs).
- TLS-aware deployment guide.

Open an issue first for anything big — the architecture has opinions and I'd rather align before you write code.
