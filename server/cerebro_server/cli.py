"""Cerebro master node CLI."""

import json
import os
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(no_args_is_help=False, add_completion=False, invoke_without_command=True)


def _load_config() -> dict:
    cfg_path = Path(os.environ.get("CEREBRO_HOME", str(Path.home() / ".cerebro"))) / "server.json"
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text())
        except Exception:
            pass
    return {}


@app.callback()
def _default(ctx: typer.Context):
    """Cerebro master node."""
    if ctx.invoked_subcommand is None:
        start()


@app.command()
def start(
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(None, "--port", "-p"),
    token: Optional[str] = typer.Option(None, "--token", envvar="CEREBRO_TOKEN"),
    redis_url: Optional[str] = typer.Option(None, "--redis-url", envvar="REDIS_URL"),
    log_level: str = typer.Option("info", "--log-level"),
):
    """Start the master server."""
    cfg = _load_config()
    resolved_port = port or int(os.environ.get("CEREBRO_PORT", cfg.get("port", 8000)))
    resolved_token = token or cfg.get("token")
    resolved_redis = redis_url or cfg.get("redis_url", "redis://localhost:6379")

    if not resolved_token:
        typer.echo(
            "error: token required. Set via --token, CEREBRO_TOKEN env, "
            "or ~/.cerebro/server.json: {\"token\": \"...\"}",
            err=True,
        )
        raise typer.Exit(2)

    os.environ["CEREBRO_TOKEN"] = resolved_token
    os.environ["REDIS_URL"] = resolved_redis
    os.environ["CEREBRO_LOG_LEVEL"] = log_level.upper()

    typer.echo(f"cerebro-server starting on {host}:{resolved_port}")

    import uvicorn

    uvicorn.run(
        "cerebro_server.main:app",
        host=host,
        port=resolved_port,
        ws_ping_interval=20,
        ws_ping_timeout=20,
        log_level=log_level.lower(),
    )


@app.command()
def config():
    """Print config file path and contents."""
    cfg_path = Path(os.environ.get("CEREBRO_HOME", str(Path.home() / ".cerebro"))) / "server.json"
    typer.echo(f"path: {cfg_path}")
    if cfg_path.exists():
        typer.echo(cfg_path.read_text())
    else:
        typer.echo('(missing) create with: {"token": "...", "port": 8000}')


def main():
    app()


if __name__ == "__main__":
    main()
