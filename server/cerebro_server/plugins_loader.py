"""Plugin loader: scans built-in (shipped) and user-defined (/data/plugins/) JSON manifests.

Plugin manifest schema:
{
  "id": "claude",
  "label": "Claude Code",
  "icon": "🧠",
  "color": "#00ff88",
  "command": "claude",
  "args": [
    "--session-id={session_id}",                          # placeholder substitution
    {"if": "skip_perms", "then": "--flag"}                # conditional arg
  ],
  "options": [
    {"key": "name", "type": "string", "optional": true},
    {"key": "cwd", "type": "path", "default": "~"},
    {"key": "skip_perms", "type": "bool", "default": true}
  ],
  "auto_fields": {"session_id": "uuid"},                  # auto-filled when creating
  "behaviors": ["resumable", "claude_jsonl_stats"]
}
"""

import json
import logging
import uuid
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger("cerebro.plugins")

BUILTIN_DIR = Path(__file__).parent / "plugins"
USER_DIR = Path("/data/plugins")


def list_plugins() -> List[Dict[str, Any]]:
    """Load all plugin manifests. User-defined override built-in by id."""
    plugins: Dict[str, Dict[str, Any]] = {}
    for src in (BUILTIN_DIR, USER_DIR):
        if not src.exists():
            continue
        for f in sorted(src.glob("*.json")):
            try:
                manifest = json.loads(f.read_text())
                pid = manifest.get("id")
                if not pid:
                    logger.warning("plugin %s missing id, skipping", f.name)
                    continue
                manifest["_source"] = "user" if src == USER_DIR else "builtin"
                plugins[pid] = manifest
            except Exception:
                logger.exception("failed to load plugin %s", f.name)
    return list(plugins.values())


def get_plugin(plugin_id: str) -> Optional[Dict[str, Any]]:
    for p in list_plugins():
        if p["id"] == plugin_id:
            return p
    return None


def fill_auto_fields(plugin: Dict[str, Any], options: Dict[str, Any]) -> Dict[str, Any]:
    """Generate auto fields (uuid, etc.) into options if not already set."""
    out = dict(options or {})
    for key, kind in (plugin.get("auto_fields") or {}).items():
        if key in out:
            continue
        if kind == "uuid":
            out[key] = str(uuid.uuid4())
    return out


def render_args(plugin: Dict[str, Any], options: Dict[str, Any]) -> List[str]:
    """Render plugin args: substitute {key} placeholders, evaluate conditionals."""
    rendered: List[str] = []
    for raw in plugin.get("args", []):
        if isinstance(raw, str):
            rendered.append(_interpolate(raw, options))
        elif isinstance(raw, dict):
            cond = raw.get("if")
            if cond and options.get(cond):
                then = raw.get("then")
                if isinstance(then, str):
                    rendered.append(_interpolate(then, options))
                elif isinstance(then, list):
                    rendered.extend(_interpolate(x, options) for x in then if isinstance(x, str))
    return rendered


def _interpolate(s: str, options: Dict[str, Any]) -> str:
    """Replace {key} placeholders with option values."""
    out = s
    for k, v in options.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def build_command(plugin: Dict[str, Any], options: Dict[str, Any]) -> str:
    """Build the full shell command string for a plugin invocation."""
    cmd = plugin.get("command", "")
    args = render_args(plugin, options)
    parts = [cmd] + args
    # Note: PTYSession uses shlex.split; if any arg has spaces user must quote.
    return " ".join(parts)
