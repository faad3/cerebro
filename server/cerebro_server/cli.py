"""Cerebro master node CLI."""

import json
import os
import secrets
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(no_args_is_help=False, add_completion=False, invoke_without_command=True)


def _data_dir() -> Path:
    """Where persistent server-side state lives (token, backups).

    Default `/data` matches the Docker volume; override with CEREBRO_DATA_DIR.
    Falls back to ~/.cerebro for non-Docker installs where /data isn't writable.
    """
    explicit = os.environ.get("CEREBRO_DATA_DIR")
    if explicit:
        return Path(explicit)
    if os.access("/data", os.W_OK) or (Path("/data").exists() and os.access("/data", os.W_OK)):
        return Path("/data")
    return Path.home() / ".cerebro"


def _token_path() -> Path:
    return _data_dir() / "token"


def _resolve_token(*, generate_if_missing: bool = True) -> Optional[str]:
    """Resolution order: env → server.json → token file → (optionally) auto-generate."""
    env = os.environ.get("CEREBRO_TOKEN")
    if env:
        return env
    cfg = _load_config()
    if cfg.get("token"):
        return cfg["token"]
    tp = _token_path()
    if tp.exists():
        val = tp.read_text().strip()
        if val:
            return val
    if not generate_if_missing:
        return None
    # Generate one, persist with restrictive perms.
    token = secrets.token_urlsafe(32)
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(token)
    try:
        tp.chmod(0o600)
    except Exception:
        pass
    return token


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
    token: Optional[str] = typer.Option(None, "--token"),
    redis_url: Optional[str] = typer.Option(None, "--redis-url", envvar="REDIS_URL"),
    log_level: str = typer.Option("info", "--log-level"),
):
    """Start the master server."""
    cfg = _load_config()
    resolved_port = port or int(os.environ.get("CEREBRO_PORT", cfg.get("port", 8000)))
    resolved_redis = redis_url or cfg.get("redis_url", "redis://localhost:6379")
    # Token: explicit flag wins, otherwise resolve (env → config → file → autogen).
    if token:
        resolved_token = token
        source = "--token flag"
    else:
        existed_before = _token_path().exists() or bool(os.environ.get("CEREBRO_TOKEN")) or bool(cfg.get("token"))
        resolved_token = _resolve_token(generate_if_missing=True)
        if os.environ.get("CEREBRO_TOKEN"):
            source = "CEREBRO_TOKEN env"
        elif cfg.get("token"):
            source = "server.json"
        elif existed_before:
            source = f"file {_token_path()}"
        else:
            source = f"auto-generated → {_token_path()}"

    os.environ["CEREBRO_TOKEN"] = resolved_token
    os.environ["REDIS_URL"] = resolved_redis
    os.environ["CEREBRO_LOG_LEVEL"] = log_level.upper()

    typer.echo(f"cerebro-server starting on {host}:{resolved_port}")
    typer.echo(f"token source: {source}")
    typer.echo(f"token: {resolved_token}")
    typer.echo("(retrieve any time via: cerebro-server token)")

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
def token(
    generate: bool = typer.Option(False, "--generate", help="Generate one if missing."),
    rotate: bool = typer.Option(False, "--rotate", help="Replace the existing token with a new one."),
):
    """Print the master's bearer token (auto-generates if missing and --generate)."""
    if rotate:
        # Wipe stored token, regenerate.
        tp = _token_path()
        if tp.exists():
            tp.unlink()
        os.environ.pop("CEREBRO_TOKEN", None)
        new = _resolve_token(generate_if_missing=True)
        typer.echo(new)
        typer.secho("rotated — restart the server and re-point all nodes", fg=typer.colors.YELLOW, err=True)
        return
    val = _resolve_token(generate_if_missing=generate)
    if not val:
        typer.secho("no token set (run with --generate to create one)", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.echo(val)


@app.command(name="devices")
def devices_cmd():
    """List registered passkey devices."""
    from . import passkeys
    if not passkeys.HAS_FIDO2:
        typer.secho("fido2 library not installed", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    rows = passkeys.list_devices()
    if not rows:
        typer.echo("(no passkeys registered — log in with the master token to add one)")
        return
    typer.echo(f"{'id':<14} {'label':<14} {'last_used':<20}")
    for d in rows:
        last = "—" if not d.get("last_used") else _ts(d["last_used"])
        typer.echo(f"{d['id'][:12]:<14} {(d.get('label') or '-'):<14} {last:<20}")


@app.command(name="revoke-device")
def revoke_device_cmd(cid_prefix: str = typer.Argument(..., help="Credential id prefix (first ~12 chars are enough)")):
    """Remove a passkey by id prefix."""
    from . import passkeys
    rows = passkeys.list_devices()
    matches = [d for d in rows if d["id"].startswith(cid_prefix)]
    if not matches:
        typer.secho(f"no passkey matches id prefix {cid_prefix!r}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if len(matches) > 1:
        typer.secho(f"ambiguous prefix — {len(matches)} matches; use more characters", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    passkeys.remove(matches[0]["id"])
    typer.echo(f"revoked: {matches[0]['id']} ({matches[0].get('label')})")


def _ts(epoch: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


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
