"""File-based backup for agents AND nodes — safety net beyond Redis.

Periodically dumps all agent + node records to a JSON file. On startup, if
Redis is empty, restores from it. Preserves user-set node names across restarts.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("cerebro.persistence")

BACKUP_PATH = Path("/data/cerebro-backup.json")
BACKUP_INTERVAL = 30  # seconds


async def backup_loop(registry) -> None:
    while True:
        await asyncio.sleep(BACKUP_INTERVAL)
        try:
            await save_backup(registry)
        except Exception:
            logger.exception("backup failed")


async def save_backup(registry) -> None:
    agents = await registry.list_agents()
    nodes = await registry.list_nodes()

    # Safety: never overwrite non-empty backup with empty data.
    if not agents and not nodes and BACKUP_PATH.exists():
        try:
            existing = json.loads(BACKUP_PATH.read_text())
            if existing.get("agents") or existing.get("nodes"):
                logger.debug("skipping backup: empty state but existing backup has data")
                return
        except Exception:
            pass

    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agents": [a.model_dump(mode="json") for a in agents],
        "nodes": [n.model_dump(mode="json") for n in nodes],
    }
    BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = BACKUP_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(BACKUP_PATH)
    logger.debug("backed up %d agents + %d nodes", len(agents), len(nodes))


async def restore_if_needed(registry) -> int:
    """If Redis has no agents, restore from backup. Also restore node names."""

    if not BACKUP_PATH.exists():
        return 0

    try:
        data = json.loads(BACKUP_PATH.read_text())
    except Exception:
        logger.exception("failed to read backup")
        return 0

    count = 0

    # Restore agents if Redis lost them.
    existing_agents = await registry.list_agents()
    if not existing_agents:
        from .models import AgentInfo

        for raw in data.get("agents", []):
            try:
                info = AgentInfo.model_validate(raw)
                info.status = "orphaned"
                info.claude_terminal_id = None
                info.bash_terminal_id = None
                await registry.r.set(f"agents:{info.agent_id}", info.model_dump_json())
                await registry.r.sadd(f"agents:by_node:{info.node_id}", info.agent_id)
                await registry.r.sadd("agents:all", info.agent_id)
                count += 1
            except Exception:
                logger.warning("skipped restoring agent %s", raw.get("agent_id", "?"))

        if count:
            logger.info("restored %d agents from backup", count)

    # Restore node names into the persistent hash (TTL-less).
    # register_node() reads from this hash on every node registration, so
    # names survive both Redis flush and the periodic 30s TTL expiry.
    for n in data.get("nodes", []):
        nid = n.get("node_id")
        nm = n.get("name")
        if nid and nm:
            await registry.r.hset("nodes:names", nid, nm)
            logger.info("restored node name: %s → %s", nid[:8], nm)

    return count
