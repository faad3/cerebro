"""Cerebro node CLI."""

import asyncio
import logging
import os
import signal
from pathlib import Path

import typer

from .node import Node, load_or_generate_node_id

app = typer.Typer(no_args_is_help=False, add_completion=False, invoke_without_command=True)


@app.callback()
def main_callback(ctx: typer.Context):
    """Cerebro — distributed Claude Code agent orchestrator."""
    if ctx.invoked_subcommand is None:
        _launch_tui()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.command()
def start(
    master: str = typer.Option(
        ..., "--master", help="Master URL, e.g. http://host:8000"
    ),
    token: str = typer.Option(
        None, "--token", envvar="CEREBRO_TOKEN", help="Shared bearer token"
    ),
    claude_cmd: str = typer.Option(
        "claude", "--claude-cmd", help="Command (or path) to launch a Claude Code agent"
    ),
    bash_cmd: str = typer.Option(
        "bash", "--bash-cmd", help="Command (or path) to launch a bash side-terminal"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Register this host with a Cerebro master and serve PTY-backed agents."""
    _setup_logging(verbose)
    if not token:
        typer.echo("error: --token (or CEREBRO_TOKEN env) required", err=True)
        raise typer.Exit(2)

    node = Node(
        master_url=master,
        token=token,
        claude_cmd=claude_cmd,
        bash_cmd=bash_cmd,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown(*_: object) -> None:
        node.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            signal.signal(sig, _shutdown)

    try:
        loop.run_until_complete(node.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


@app.command()
def status():
    """Print this node's identity (persistent node_id and hostname)."""
    import socket as _socket

    nid = load_or_generate_node_id()
    typer.echo(f"node_id:  {nid}")
    typer.echo(f"hostname: {_socket.gethostname()}")
    typer.echo(
        f"id_file:  {Path(os.environ.get('CEREBRO_HOME', str(Path.home() / '.cerebro'))) / 'node_id'}"
    )


def _launch_tui(master: str = None, token: str = None):
    """Resolve config and launch TUI."""
    import json as _json

    config_path = Path(os.environ.get("CEREBRO_HOME", str(Path.home() / ".cerebro"))) / "config.json"
    config = {}
    if config_path.exists():
        try:
            config = _json.loads(config_path.read_text())
        except Exception:
            pass

    master = master or os.environ.get("CEREBRO_MASTER") or config.get("master")
    token = token or os.environ.get("CEREBRO_TOKEN") or config.get("token")

    if not master or not token:
        typer.echo("error: master URL and token required", err=True)
        typer.echo(f"set via: --master/--token flags, CEREBRO_MASTER/CEREBRO_TOKEN env,", err=True)
        typer.echo(f"or save to {config_path}:", err=True)
        typer.echo(f'  {{"master": "http://host:8000", "token": "your-token"}}', err=True)
        raise typer.Exit(2)

    from .tui import CerebroTUI
    CerebroTUI(master, token).run()


@app.command("tui")
def tui_cmd(
    master: str = typer.Option(None, "--master", envvar="CEREBRO_MASTER", help="Master URL"),
    token: str = typer.Option(None, "--token", envvar="CEREBRO_TOKEN"),
):
    """Interactive terminal UI — switch between agents with Ctrl+]."""
    _launch_tui(master, token)


def main():  # entry point
    app()


if __name__ == "__main__":
    main()
