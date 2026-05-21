# Cerebro

Distributed orchestrator for **Claude Code agents** — run multiple Claude sessions on multiple machines from one browser/TUI, with a built-in orchestrator meta-agent that can manage them for you.

```
              ┌──────────────────────┐
              │  cerebro-server      │
 browser ───▶ │  (master)            │ ◀─── cerebro-node (eva56)
   / TUI      │  + orchestrator      │ ◀─── cerebro-node (eva57)
              │  + Redis             │ ◀─── cerebro-node (...)
              └──────────────────────┘
```

## Install

Two machine roles: **master** (one) and **node** (one per host that should serve agents).

### Master

**Option A — Docker (recommended, bundles Redis + claude CLI + orchestrator):**
```bash
git clone <repo> cerebro && cd cerebro
cp .env.example .env   # edit CEREBRO_TOKEN
docker compose up -d
```

**Option B — pip (bring your own Redis + claude CLI):**
```bash
# Prereqs: python >=3.11, redis-server, claude CLI
pip install cerebro-server  # or: pip install ./server/

# Configure
mkdir -p ~/.cerebro
cat > ~/.cerebro/server.json <<EOF
{ "token": "your-shared-token", "port": 8000, "redis_url": "redis://localhost:6379" }
EOF

cerebro-server start
```

### Node

```bash
pip install --user cerebro      # or: pip install ./client/

# Configure
cat > ~/.cerebro/config.json <<EOF
{ "master": "http://master-host:8000", "token": "your-shared-token" }
EOF

cerebro start   # runs the node daemon
cerebro tui     # interactive terminal UI (switch between agents with Ctrl+])
```

## UI

Open `http://master-host:8000` in a browser → paste `CEREBRO_TOKEN`.

Three tabs:
- **nodes** — hosts running `cerebro` daemon
- **agents** — master-detail view: sidebar with all agents + live terminal. `+` (bottom-right) creates new agents, with optional custom path browser.
- **dashboard** — orchestrator Claude that can manage other agents via `cerebro-ctl`

## Concepts

- **Node** — a host running the `cerebro` daemon. Persists identity in `~/.cerebro/node_id`.
- **Agent** — one Claude Code session on a node. Optionally paired with a bash side-terminal (`+ shell`).
- **Terminal** — one PTY. Internal primitive.
- **Orchestrator** — special Claude agent living inside the master; uses `cerebro-ctl` to manage other agents.

## Features

- Seamless agent switching (terminals stay alive in memory)
- Multi-node support with live node picker
- Path browser for custom cwd at agent creation
- Agent name + node name rename (inline, click-to-edit)
- Session persistence via `--session-id` / `--resume` (agents survive node restarts)
- Auto-backup of agent metadata to `/data` (survives Redis flush)
- Browser notifications when an agent finishes working (active → idle)
- Live activity dots (green pulse = generating, yellow = idle, gray = dead)

## Layout

```
cerebro/
├── server/                      # master
│   ├── pyproject.toml           # pip-installable as cerebro-server
│   ├── Dockerfile               # docker image (bundles Redis via compose)
│   ├── entrypoint.sh            # UID mapping for mounted ~/.claude
│   └── cerebro_server/
│       ├── cli.py               # cerebro-server entry point
│       ├── main.py              # FastAPI app
│       ├── routers/             # /api/nodes, /api/agents, /ws
│       ├── registry.py          # Redis wrapper
│       ├── hub.py               # live WS connections
│       ├── persistence.py       # file backup
│       ├── orchestrator_manager.py
│       ├── static/              # web UI (vanilla JS + xterm.js)
│       └── orchestrator/        # orchestrator's CLAUDE.md
├── client/                      # node + TUI
│   └── cerebro_node/
│       ├── cli.py               # cerebro entry point
│       ├── node.py              # daemon
│       ├── tui.py               # terminal UI
│       └── pty_session.py
├── docker-compose.yml
└── .env.example
```

## Security notes

- Single shared bearer token (`CEREBRO_TOKEN`) — all hosts and the browser use it.
- Agents run with `--dangerously-skip-permissions` by default (opt-out per agent).
- No TLS — put nginx/Caddy in front for public deployments.
- Claude auth uses the host user's `~/.claude/` directory (mounted into Docker container as non-root).
