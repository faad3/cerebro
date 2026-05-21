"""Cerebro master node — FastAPI app entry point."""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

logging.basicConfig(
    level=os.environ.get("CEREBRO_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from .registry import Registry  # noqa: E402

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

registry = Registry(REDIS_URL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    from .persistence import backup_loop, restore_if_needed

    await registry.connect()

    # Restore agents from file backup if Redis lost them.
    restored = await restore_if_needed(registry)
    if restored:
        logging.getLogger("cerebro").info("restored %d agents from backup", restored)

    # Start periodic backup.
    backup_task = asyncio.create_task(backup_loop(registry))

    try:
        yield
    finally:
        backup_task.cancel()
        # Final backup before shutdown.
        from .persistence import save_backup
        try:
            await save_backup(registry)
        except Exception:
            pass
        await registry.close()


app = FastAPI(title="Cerebro", version="0.2.0", lifespan=lifespan)

# Routers loaded after registry is bound so they can import it lazily.
from .routers import agents, folders, nodes, plugins, ws  # noqa: E402

app.include_router(nodes.router)
app.include_router(agents.router)
app.include_router(plugins.router)
app.include_router(folders.router)
app.include_router(ws.router)


STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
