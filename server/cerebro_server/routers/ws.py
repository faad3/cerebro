"""WebSocket endpoints: internal node channel, per-terminal browser proxy,
and orchestrator terminal."""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from ..auth import check_token
from ..hub import hub
from ..orchestrator_manager import orchestrator
from ..protocol import pack, unpack
from ..registry import Registry

logger = logging.getLogger("cerebro.ws")

router = APIRouter(tags=["ws"])


def get_registry() -> Registry:
    from .. import main; registry = main.registry

    return registry


# ----------------------------------------------------------------------------
# /ws/node/{node_id}  — internal channel (master ↔ node daemon)
# ----------------------------------------------------------------------------


@router.websocket("/ws/node/{node_id}")
async def node_socket(
    ws: WebSocket, node_id: str, token: Optional[str] = Query(default=None)
):
    if not check_token(token):
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    reg = get_registry()
    if await reg.get_node(node_id) is None:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await ws.accept()
    conn = await hub.attach_node(node_id, ws)
    logger.info("node %s connected", node_id)

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break

            if "bytes" in msg and msg["bytes"] is not None:
                # PTY output → route to viewer + replay buffer.
                try:
                    terminal_id, data = unpack(msg["bytes"])
                except ValueError:
                    logger.warning("node %s sent malformed binary frame", node_id)
                    continue

                await reg.append_replay(terminal_id, data)
                hub.touch_activity(terminal_id)
                viewer = hub.get_viewer(terminal_id)
                if viewer is not None:
                    try:
                        await viewer.send_bytes(data)
                    except Exception:
                        pass

            elif "text" in msg and msg["text"] is not None:
                try:
                    payload = json.loads(msg["text"])
                except json.JSONDecodeError:
                    logger.warning("node %s sent invalid JSON", node_id)
                    continue

                mtype = payload.get("type")
                tid = payload.get("terminal_id")

                if mtype == "terminal_created" and tid:
                    fut = conn.pending_creates.get(tid)
                    if fut is not None and not fut.done():
                        fut.set_result(payload)

                elif mtype == "terminal_dead" and tid:
                    await _handle_terminal_dead(reg, tid, payload)

                elif mtype == "list_dir_response":
                    rid = payload.get("request_id")
                    if rid:
                        fut = conn.pending_reads.get(rid)
                        if fut is not None and not fut.done():
                            fut.set_result(payload)

                elif mtype == "session_response":
                    rid = payload.get("request_id")
                    if rid:
                        fut = conn.pending_reads.get(rid)
                        if fut is not None and not fut.done():
                            fut.set_result(payload)

                elif mtype == "claude_session_id" and tid:
                    # Node discovered the claude session UUID from
                    # ~/.claude/sessions/<pid>.json — store it on the agent.
                    csid = payload.get("session_id")
                    term = await reg.get_terminal(tid)
                    if term and csid:
                        agent = await reg.get_agent(term.agent_id)
                        if agent is not None:
                            agent.claude_session_id = csid
                            await reg.update_agent(agent)
                            logger.info(
                                "captured claude session %s for agent %s",
                                csid[:12], term.agent_id[:12],
                            )

                else:
                    logger.debug("node %s sent unknown control: %s", node_id, payload)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("node socket error for %s", node_id)
    finally:
        removed = await hub.detach_node(node_id, conn)
        if removed:
            # Only orphan if this was the ACTIVE connection — not if it was
            # replaced by a newer one (i.e., the node reconnected).
            for tid in await reg.terminals_for_node(node_id):
                await reg.mark_terminal_status(tid, "orphaned")
            for aid in await reg.agents_for_node(node_id):
                agent = await reg.get_agent(aid)
                if agent is not None and agent.status != "dead":
                    agent.status = "orphaned"
                    await reg.update_agent(agent)
            logger.info("node %s disconnected (orphaned)", node_id)
        else:
            logger.info("node %s old connection closed (replaced by new)", node_id)


async def _handle_terminal_dead(reg: Registry, terminal_id: str, payload: dict) -> None:
    info = await reg.mark_terminal_status(terminal_id, "dead")
    if info is None:
        return

    # Reflect into the parent agent.
    agent = await reg.get_agent(info.agent_id)
    if agent is not None:
        if info.kind == "claude":
            agent.status = "dead"
        elif info.kind == "bash" and agent.bash_terminal_id == terminal_id:
            agent.bash_terminal_id = None
        await reg.update_agent(agent)

    # Boot any browser viewer.
    viewer = hub.get_viewer(terminal_id)
    if viewer is not None:
        try:
            await viewer.send_text(
                json.dumps(
                    {
                        "type": "terminal_dead",
                        "exit_code": payload.get("exit_code"),
                    }
                )
            )
            await viewer.close()
        except Exception:
            pass

    logger.info(
        "terminal %s (%s) ended exit=%s", terminal_id, info.kind, payload.get("exit_code")
    )


# ----------------------------------------------------------------------------
# /ws/terminal/{terminal_id}  — browser ↔ PTY proxy
# ----------------------------------------------------------------------------


@router.websocket("/ws/terminal/{terminal_id}")
async def terminal_socket(
    ws: WebSocket, terminal_id: str, token: Optional[str] = Query(default=None)
):
    if not check_token(token):
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    reg = get_registry()
    info = await reg.get_terminal(terminal_id)
    if info is None:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    node_conn = hub.get_node(info.node_id)
    if node_conn is None:
        await ws.close(code=status.WS_1011_INTERNAL_ERROR)
        return

    await ws.accept()
    await hub.attach_viewer(terminal_id, ws)
    logger.info("viewer attached to terminal %s (%s)", terminal_id, info.kind)

    # Replay buffered output for reconnect.
    try:
        for chunk in await reg.read_replay(terminal_id):
            await ws.send_bytes(chunk)
    except Exception:
        logger.exception("replay failed for %s", terminal_id)

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break

            current_node = hub.get_node(info.node_id)
            if current_node is None:
                break

            if "bytes" in msg and msg["bytes"] is not None:
                try:
                    await current_node.send_bytes(pack(terminal_id, msg["bytes"]))
                except Exception:
                    logger.exception("forward input failed for %s", terminal_id)
                    break

            elif "text" in msg and msg["text"] is not None:
                try:
                    payload = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                if payload.get("type") == "resize":
                    try:
                        await current_node.send_json(
                            {
                                "type": "resize",
                                "terminal_id": terminal_id,
                                "cols": int(payload.get("cols", 80)),
                                "rows": int(payload.get("rows", 24)),
                            }
                        )
                    except Exception:
                        logger.exception("forward resize failed for %s", terminal_id)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("viewer socket error for %s", terminal_id)
    finally:
        await hub.detach_viewer(terminal_id, ws)
        logger.info("viewer detached from terminal %s", terminal_id)


# ----------------------------------------------------------------------------
# /ws/orchestrator  — browser ↔ orchestrator PTY (runs in master container)
# ----------------------------------------------------------------------------


@router.websocket("/ws/orchestrator")
async def orchestrator_socket(
    ws: WebSocket, token: Optional[str] = Query(default=None)
):
    if not check_token(token):
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await ws.accept()

    # Auto-start the orchestrator if it's not running.
    if not orchestrator.alive:
        orchestrator.start(cols=120, rows=40)

    await orchestrator.attach_viewer(ws)
    logger.info("orchestrator viewer attached")

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break

            if not orchestrator.alive:
                break

            if "bytes" in msg and msg["bytes"] is not None:
                orchestrator.write(msg["bytes"])
            elif "text" in msg and msg["text"] is not None:
                try:
                    payload = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                if payload.get("type") == "resize":
                    orchestrator.resize(
                        int(payload.get("cols", 120)),
                        int(payload.get("rows", 40)),
                    )
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("orchestrator socket error")
    finally:
        orchestrator.detach_viewer(ws)
        logger.info("orchestrator viewer detached")
