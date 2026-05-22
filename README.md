# Cerebro

A distributed **terminal-session manager** in your browser — Arc-Browser-style sidebar for tabs that happen to be live PTYs running across multiple machines. Sessions are spawned from a plugin manifest (claude, bash, htop, anything you add), grouped into folders, drag-dropped between favorites and default sections, and survive across master restarts.

```
              ┌──────────────────────┐
              │  cerebro-server      │
 browser  ──▶ │  (master)            │ ◀── cerebro-node (eva56)
              │  + Redis             │ ◀── cerebro-node (eva57)
              │  + orchestrator      │ ◀── cerebro-node (...)
              └──────────────────────┘
```

## Install

Two roles: **master** (one) and **node** (one per host you want to spawn sessions on).

### Master

**Option A — Docker (bundles Redis + claude CLI + orchestrator):**
```bash
git clone https://github.com/faad3/cerebro.git && cd cerebro
cp .env.example .env       # edit CEREBRO_TOKEN
docker compose up -d
```

**Option B — pip (bring your own Redis + plugin binaries):**
```bash
# Prereqs: python >=3.11, redis-server, and any plugin commands you want (claude, htop, …)
pip install ./server/
cerebro-server start
```

### Node

```bash
pip install ./client/
cerebro start --master http://master-host:8000 --token <CEREBRO_TOKEN>
cerebro tui     # optional interactive TUI (Ctrl+] to switch between sessions)
```

Node identity is persisted in `~/.cerebro/node_id`, so a restart reattaches all live PTYs to the same logical node.

## UI

Open `http://master-host:8000` and paste `CEREBRO_TOKEN`.

Three top-level routes:

- **nodes** — hosts running the daemon, live status + rename.
- **sessions** — Arc-style sidebar with two namespaces (favorites on top, default below). Folders inside each. Drag-drop to favorite, unfavorite, group, ungroup. `+` opens the plugin picker.
- **conductor** — orchestrator Claude living in the master container; it can manage other sessions via `cerebro-ctl`.

### Session sidebar — interactions

- `+` (bottom right) → plugin picker grid → options form (auto-built from manifest).
- Drag session A onto session B → creates a folder in B's section, A joins.
- Drag session onto the favorites/default section background → moves it into that namespace.
- Star button or section drag → favorite / unfavorite.
- Double-click name → inline rename.
- All-vs-by-node toggle at the top.
- New sessions get a wandb-style funny name (`crimson-lemur`, `frosty-pickle`, …) until you rename them.

## Plugins

Drop a JSON manifest into `server/cerebro_server/plugins/` (built-in) or `/data/plugins/` (user override). Schema:

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
    {"key": "cwd", "type": "path", "default": "~"},
    {"key": "skip_perms", "type": "bool", "default": true}
  ],
  "auto_fields": {"session_id": "uuid"},
  "behaviors": ["resumable", "claude_jsonl_stats"]
}
```

Option types: `string`, `number`, `bool`, `path` (path browser). `auto_fields` generates values at create-time (currently `uuid`). Conditional args: `{"if": "<option_key>", "then": "<arg>"}`. The picker renders one tile per plugin; the options form is generated from `options[]`.

Built-in: `claude.json`, `bash.json`, `htop.json`.

## Concepts

- **Node** — a host running the daemon. Persisted identity → restartable.
- **Session** (a.k.a. `agent_id` in the data model) — one interactive CLI session, instantiated from a plugin, attached to exactly one node.
- **Folder** — sidebar group. Lives in exactly one section (`default` or `favorite`); cannot mix.
- **Terminal** — low-level PTY. Internal; users don't see them directly.
- **Plugin** — JSON manifest defining how to spawn a session.

## Features

- Arc-style sidebar: two namespaces (favorites / default), folders, drag-drop, per-node toggle.
- Plugin system — extend with a JSON manifest, no code changes.
- Seamless session switching (xterm + WebSocket stay alive in memory across navigation).
- Live activity dots: green pulse = generating, yellow = idle, gray = dead.
- Browser notifications when a session goes idle (active → idle transition).
- Claude session persistence via `--session-id` (sessions survive node and master restarts).
- Auto-backup of session metadata to `/data` so Redis flush is non-fatal.
- Funny default names + double-click inline rename.
- Auto-cleanup of empty folders.

## Layout

```
cerebro/
├── server/                              # master
│   ├── pyproject.toml                   # pip-installable as cerebro-server
│   ├── Dockerfile                       # bundles redis + claude CLI
│   ├── entrypoint.sh                    # UID mapping for mounted ~/.claude
│   └── cerebro_server/
│       ├── cli.py                       # `cerebro-server` entry point
│       ├── main.py                      # FastAPI app + lifespan
│       ├── routers/                     # /api/nodes /api/agents /api/folders /api/plugins /ws
│       ├── registry.py                  # Redis wrapper
│       ├── hub.py                       # live WS connections
│       ├── persistence.py               # file backup
│       ├── orchestrator_manager.py
│       ├── funny_names.py               # wandb-style name generator
│       ├── plugins_loader.py            # plugin manifest loader
│       ├── plugins/                     # built-in manifests
│       ├── static/                      # web UI (vanilla JS + xterm.js)
│       └── orchestrator/                # orchestrator's CLAUDE.md
├── client/                              # node + TUI
│   └── cerebro_node/
│       ├── cli.py                       # `cerebro` entry point
│       ├── node.py                      # daemon
│       ├── tui.py                       # terminal UI
│       └── pty_session.py               # PTY child (PATH-augmented exec)
├── docker-compose.yml
└── .env.example
```

## Security notes

- Single shared bearer token (`CEREBRO_TOKEN`) — all hosts and the browser use the same one.
- No TLS — front it with nginx/Caddy for public deployments.
- Claude auth uses the host user's `~/.claude/` directory (mounted into the Docker container with UID mapping).
- Skip-permissions defaults are per-plugin (the claude manifest enables it by default; flip in the picker options).
