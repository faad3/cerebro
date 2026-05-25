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

## Multiple users on one host

Cerebro is designed for one person per instance. If several people want to run their **own** Cerebro on the same machine, that works fine — just give each user a unique port. Everything else is already isolated:

Set two things per user in `.env`:

```bash
# alice@host:~/cerebro-alice$ cat .env
COMPOSE_PROJECT_NAME=cerebro-alice
CEREBRO_PORT=8001

# bob@host:~/cerebro-bob$ cat .env
COMPOSE_PROJECT_NAME=cerebro-bob
CEREBRO_PORT=8002
```

(The simplest way: each user clones into a directory named differently, since Docker Compose uses the directory name as project name by default.)

Then each user runs `docker compose up -d` in their own checkout. Everything else is automatically isolated:

- **Docker volumes** are scoped by project name → Alice's `cerebro-alice_redis_data` ≠ Bob's `cerebro-bob_redis_data`.
- **Tokens** are auto-generated per master, never shared.
- **Audit log, passkeys, backups** all live in the per-instance Docker volume.
- **Rate-limit counters** are in-memory per process, so Alice's bad logins don't affect Bob.
- **Node daemons** read `~/.cerebro/node_id` from each user's own home directory; PTYs they fork run as that user.
- **Each instance gets its own port** — Docker refuses to start if a port is taken, so collisions are loud, not silent. Each user's chosen `CEREBRO_PORT` is independently exposed on `0.0.0.0` (set `CEREBRO_BIND=127.0.0.1` if you want any one of them localhost-only).

What's still on the user's responsibility:

- Each Cerebro instance must run under that user's OS account. If both `cerebro-node` daemons run as the same UID, the PTYs they fork share permissions (Alice's bash can read Bob's `/tmp`, see his `/proc/<pid>/environ`, etc.). That's standard UNIX behavior, not a Cerebro thing.
- Being in the `docker` group is equivalent to root — anyone in it can `docker exec` into another user's container. If you need stronger isolation, use rootless Docker.
- **WebAuthn passkeys** are scoped to the hostname (not host+port). If Alice runs on `host:8001` and Bob on `host:8002`, the browser sees them as the same WebAuthn realm. Each user's server only knows its own credentials, so cross-login simply fails — no security leak, but the UX wart is real. Use different hostnames (subdomains) per user if it bothers you.

What's **not** supported and won't work: multiple humans sharing **one** Cerebro instance with separate accounts/permissions. There are no users inside Cerebro — anyone who can log in owns everything. If that's what you need, open an issue.

## Roadmap

- One-command master migration: `cerebro-server export` / `cerebro-server import` to move the brain to a new server without losing any session.
- More built-in plugins (nvtop, btop, tmux attach, ssh-to-anywhere).
- Public deployment guide (TLS, multi-user).

---

## Going deeper

- **[DEVELOPMENT.md](DEVELOPMENT.md)** — architecture, data model, plugin manifest schema, master↔node WS protocol, contributing.

## License

MIT — do whatever you want, just don't blame us when your shells go feral.
