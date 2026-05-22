# Cerebro

**Run terminals on multiple machines from one browser tab.**

Cerebro is a browser app that lets you open, organize, and revisit terminal sessions — like browser tabs, but each tab is a live shell, a Claude Code session, an `htop`, or anything else you wire up. The tabs live on whichever machines you point at it (your laptop, a couple of servers, a GPU box), but you see them all in one Arc-style sidebar with folders, favorites, and drag-to-organize.

![architecture](https://img.shields.io/badge/python-3.11+-blue) ![docker](https://img.shields.io/badge/docker-ready-blue) ![license](https://img.shields.io/badge/license-MIT-green)

---

## What you get

- **One sidebar, many machines.** Add a server, get its shells in the same list as everything else.
- **Tabs that don't die.** Sessions outlive disconnects, master restarts, and reboots — Claude conversations resume, terminals keep their scrollback.
- **Folders and favorites.** Drag tabs around like in Arc Browser: pin to top, group into folders, switch between "all" and "by machine" views.
- **Plugins, not hardcoded apps.** Want a tab that starts `claude`? `bash`? `htop`? `nvtop`? Drop a small JSON file. No code.
- **Funny names by default.** New tabs come pre-named `crimson-lemur`, `frosty-pickle`, etc. (wandb-style). Double-click to rename.

---

## Quickstart

You need:
- One machine to be the **master** (the brain — runs the web UI and Redis).
- One or more machines to be **nodes** (where shells actually run). Master can be a node too.
- Docker (recommended) or Python 3.11+ on the master.

### 1. Start the master (Docker)

```bash
git clone https://github.com/faad3/cerebro.git
cd cerebro
docker compose up -d
```

The master auto-generates a bearer token on first boot. Grab it:

```bash
docker compose exec cerebro-server cerebro-server token
```

Web UI is at `http://localhost:8000`. Paste that token to log in.

### 2. Add a node

On each machine you want to spawn shells on (the master itself counts):

```bash
pip install --user git+https://github.com/faad3/cerebro.git#subdirectory=client
cerebro start --master http://<master-host>:8000 --token <token-from-step-1>
```

Refresh the UI — the new machine shows up under "nodes".

### 3. Open your first session

Click the green **+** at the bottom-right → pick a plugin (Claude / Bash / htop) → click **create**. A tab appears in the sidebar with a funny name. Click it. You're in a live terminal.

---

## Tips

- **Drag a tab onto another tab** → creates a folder with both inside.
- **Drag a tab into the top section** → pins it (it's now a favorite). Drag back down to unpin.
- **Double-click a tab name** → rename. Same for folders.
- **The star button** does the same as dragging to favorites — pick whichever feels natural.
- **`all` vs `by node`** (top of sidebar) — toggle to group your tabs by machine.
- **Click a Claude tab while it's working** elsewhere — your browser pings you when it goes idle.

---

## Public deployment

Run Cerebro behind a TLS-terminating reverse proxy. A working Caddy config is in [`deploy/Caddyfile.example`](deploy/Caddyfile.example) — point a hostname at your master, drop the file in `/etc/caddy/`, and you get HTTPS with auto-renewed certs.

Set this env var on the master so browser WS upgrades from random origins are refused:

```bash
CEREBRO_ALLOWED_ORIGINS=https://cerebro.example.com
```

What you get out of the box once behind TLS:
- **Touch ID / Windows Hello / passkey login.** After the first master-token login, the UI offers to register the device's biometric authenticator. Subsequent visits: one fingerprint tap, no token typing. Manage registered devices with `cerebro-server devices` / `revoke-device`.
- Token never appears in URLs — the browser logs in with `POST /api/login` (master token or passkey), session cookie is httpOnly `Secure SameSite=Strict`.
- Constant-time token comparison and 5-fail/minute brute-force throttle on both login paths.
- WebSocket upgrades validate `Origin` against the allowlist.
- Every session create / delete / resume / passkey event is written as a JSON line to `/data/audit.log`.

Threat-model caveat: there's still **one shared master token across all users** (passkeys are per-device but they all unlock the same "root" account). A leaked master token = RCE on every node. Don't share Cerebro with people you wouldn't share root with. See [DEVELOPMENT.md → Security](DEVELOPMENT.md#security) for the longer story.

## Roadmap

- One-command master migration: `cerebro-server export` / `cerebro-server import` to move the brain to a new server without losing any session.
- More built-in plugins (nvtop, btop, tmux attach, ssh-to-anywhere).
- Public deployment guide (TLS, multi-user).

---

## Going deeper

- **[DEVELOPMENT.md](DEVELOPMENT.md)** — architecture, data model, plugin manifest schema, master↔node WS protocol, contributing.

## License

MIT — do whatever you want, just don't blame us when your shells go feral.
