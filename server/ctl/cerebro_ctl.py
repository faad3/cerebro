"""cerebro-ctl — CLI for the Cerebro orchestrator to manage agents.

Runs inside the master container, talks to localhost:8000.
The orchestrator's claude calls these commands via Bash.
"""

import json
import os
import sys
from typing import Optional

import httpx
import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)
agents_app = typer.Typer(no_args_is_help=True, help="Manage agents.")
app.add_typer(agents_app, name="agents")

BASE = os.environ.get("CEREBRO_API_URL", "http://localhost:8000")
TOKEN = os.environ.get("CEREBRO_TOKEN", "")


def _headers():
    return {"Authorization": f"Bearer {TOKEN}"}


def _get(path: str):
    r = httpx.get(f"{BASE}{path}", headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict = None):
    r = httpx.post(f"{BASE}{path}", headers=_headers(), json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def _delete(path: str):
    r = httpx.delete(f"{BASE}{path}", headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def _patch(path: str, body: dict):
    r = httpx.patch(f"{BASE}{path}", headers=_headers(), json=body, timeout=15)
    r.raise_for_status()
    return r.json()


# ---- agents commands -------------------------------------------------------


@agents_app.command("list")
def agents_list():
    """List all agents across all nodes."""
    agents = _get("/api/agents")
    nodes = {n["node_id"]: n for n in _get("/api/nodes")}
    if not agents:
        typer.echo("No agents.")
        return

    for a in agents:
        node = nodes.get(a["node_id"], {})
        hostname = node.get("name") or node.get("hostname", a["node_id"][:8])
        name = a.get("name") or a["agent_id"][:12]
        status = a.get("status", "?")
        last = a.get("last_activity_at", "")
        dsp = " [skip-perms]" if a.get("dangerously_skip_permissions") else ""
        typer.echo(f"  {name:20s}  {status:10s}  {hostname:20s}{dsp}  {a['agent_id']}")


@agents_app.command("read")
def agents_read(agent_id: str, last: int = typer.Option(20, "--last", "-n")):
    """Read the last N conversation messages of an agent."""
    messages = _get(f"/api/agents/{agent_id}/messages?last={last}")
    if not messages:
        typer.echo("No messages (agent may not have started a conversation yet).")
        return
    for m in messages:
        mtype = m.get("type", "?")
        ts = m.get("timestamp", "")
        text = m.get("text", "")
        if mtype == "user":
            typer.echo(f"\n  [{ts}] USER:")
            typer.echo(f"    {text}")
        elif mtype == "assistant":
            model = m.get("model", "")
            stop = m.get("stop_reason", "")
            tok = m.get("tokens", {})
            tok_str = f"in={tok.get('input',0)} out={tok.get('output',0)} cache_r={tok.get('cache_read',0)}"
            typer.echo(f"\n  [{ts}] ASSISTANT ({model}, {stop}, {tok_str}):")
            for line in text.split("\n"):
                typer.echo(f"    {line}")
        elif mtype == "system":
            sub = m.get("subtype", "")
            dur = m.get("durationMs", "")
            typer.echo(f"\n  [{ts}] SYSTEM: {sub} {dur}ms")


@agents_app.command("send")
def agents_send(agent_id: str, text: str):
    """Send a text message to an agent's claude terminal (types it + Enter)."""
    msg = text + "\n"
    _post(f"/api/agents/{agent_id}/input", {"text": msg})
    typer.echo(f"Sent to {agent_id[:12]}: {text}")


@agents_app.command("create")
def agents_create(
    node_id: str,
    name: str = typer.Option("", "--name", "-n"),
    task: str = typer.Option("", "--task", "-t"),
    skip_perms: bool = typer.Option(False, "--skip-perms"),
):
    """Create a new agent on a node, optionally with an initial task."""
    body = {
        "node_id": node_id,
        "name": name or None,
        "cols": 120,
        "rows": 40,
        "dangerously_skip_permissions": skip_perms,
    }
    a = _post("/api/agents", body)
    typer.echo(f"Created agent: {a['agent_id']}")
    typer.echo(f"  name={a.get('name')}, status={a['status']}")

    if task:
        # Wait a moment for claude to boot, then send the task.
        import time

        time.sleep(3)
        _post(f"/api/agents/{a['agent_id']}/input", {"text": task + "\n"})
        typer.echo(f"  Sent task: {task}")


@agents_app.command("kill")
def agents_kill(agent_id: str):
    """Kill an agent and all its terminals."""
    _delete(f"/api/agents/{agent_id}")
    typer.echo(f"Killed agent {agent_id[:12]}")


@agents_app.command("rename")
def agents_rename(agent_id: str, name: str):
    """Rename an agent."""
    _patch(f"/api/agents/{agent_id}", {"name": name})
    typer.echo(f"Renamed {agent_id[:12]} → {name}")


# ---- nodes -----------------------------------------------------------------


@app.command("nodes")
def nodes_list():
    """List all connected nodes."""
    nodes = _get("/api/nodes")
    if not nodes:
        typer.echo("No nodes.")
        return
    for n in nodes:
        display = n.get("name") or n["hostname"]
        extra = f"  ({n['hostname']})" if n.get("name") else ""
        typer.echo(f"  {display:25s}  {n['status']:8s}{extra}  {n['node_id']}")


# ---- stats -----------------------------------------------------------------


@app.command("stats")
def stats():
    """Show aggregate token and cost stats across all agents."""
    agents = _get("/api/agents")
    nodes = _get("/api/nodes")
    typer.echo(f"Nodes: {len(nodes)} connected")
    typer.echo(f"Agents: {len(agents)} total")

    running = [a for a in agents if a["status"] == "running"]
    idle_count = sum(1 for a in running if not a.get("last_activity_at"))
    typer.echo(f"  running: {len(running)} ({idle_count} idle)")
    typer.echo(f"  dead/orphaned: {len(agents) - len(running)}")

    # Token stats require reading each agent's session — expensive.
    # For now, just report counts.
    typer.echo("\n(Per-agent token/cost stats: use `cerebro-ctl agents read <id>` for individual breakdowns)")


if __name__ == "__main__":
    app()
