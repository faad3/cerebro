from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Node — a host running the cerebro-node daemon.
# ---------------------------------------------------------------------------


class NodeInfo(BaseModel):
    node_id: str
    hostname: str
    name: Optional[str] = None  # user-friendly alias; hostname is auto-detected
    registered_at: datetime
    last_heartbeat: datetime
    status: Literal["online", "offline"]


class RegisterNodeRequest(BaseModel):
    node_id: str
    hostname: str


class RenameRequest(BaseModel):
    name: str


# ---------------------------------------------------------------------------
# Agent — a logical Claude Code session living on a node, optionally with a
# paired bash side-terminal for ad-hoc inspection.
# ---------------------------------------------------------------------------


AgentStatus = Literal["running", "dead", "orphaned"]


class AgentInfo(BaseModel):
    agent_id: str
    node_id: str
    name: Optional[str] = None
    template: str = "empty"  # legacy field; use plugin_id going forward
    plugin_id: str = "claude"
    plugin_options: dict = {}  # all plugin-specific user config (cwd, skip_perms, session_id, ...)
    folder_id: Optional[str] = None
    is_favorite: bool = False
    position: int = 0
    created_at: datetime
    status: AgentStatus = "running"
    claude_terminal_id: Optional[str] = None
    bash_terminal_id: Optional[str] = None
    # Legacy convenience fields — derived from plugin_options for "claude" plugin:
    dangerously_skip_permissions: bool = False
    claude_session_id: Optional[str] = None
    cwd: Optional[str] = None
    last_activity_at: Optional[datetime] = None  # enriched from hub


class CreateAgentRequest(BaseModel):
    node_id: str
    name: Optional[str] = None
    plugin_id: str = "claude"
    plugin_options: dict = {}
    cols: int = 80
    rows: int = 24
    folder_id: Optional[str] = None
    # Legacy / convenience top-level fields (mapped into plugin_options for "claude"):
    template: Optional[str] = None
    dangerously_skip_permissions: Optional[bool] = None
    cwd: Optional[str] = None


FolderSection = Literal["default", "favorite"]


class FolderInfo(BaseModel):
    folder_id: str
    name: str
    node_id: Optional[str] = None  # set in per-node view; null = global
    section: FolderSection = "default"  # which sidebar section the folder lives in
    position: int = 0
    expanded: bool = True


class CreateFolderRequest(BaseModel):
    name: str
    node_id: Optional[str] = None
    section: FolderSection = "default"


class UpdateAgentRequest(BaseModel):
    name: Optional[str] = None
    folder_id: Optional[str] = None
    is_favorite: Optional[bool] = None
    position: Optional[int] = None


# ---------------------------------------------------------------------------
# Terminal — a single PTY (low-level primitive). UI usually doesn't talk to
# these directly except over WS for I/O.
# ---------------------------------------------------------------------------


TerminalKind = Literal["claude", "bash"]
TerminalStatus = Literal["running", "dead", "orphaned"]


class TerminalInfo(BaseModel):
    terminal_id: str
    agent_id: str
    node_id: str
    kind: TerminalKind
    created_at: datetime
    status: TerminalStatus = "running"
    pid: Optional[int] = None
