"""Permission gating for the Agent SDK path (Phase 3).

The SDK's `can_use_tool` callback requires streaming-input mode (prompt as an
AsyncIterable), which is a larger change. v1 instead gates by **tool availability**:

  - Read-only / low-risk tools are always available.
  - Powerful tools (Bash, Write, Edit, MCP/custom) are only in the model's context
    when the chat has /yolo enabled. When gated, those tools simply aren't offered,
    so the model can't call them and will tell the user to send /yolo.

This needs no callback and no streaming, and is per-session via /yolo.

  v2 (later): a real approval round-trip via can_use_tool + ClaudeSDKClient streaming
  (message the user on a gated call, await /approve | /deny).
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("hermes.agent_sdk_bridge.permissions")

# Read-only / low-risk Claude Code tools available without /yolo.
# Note: "Skill" only loads skill instructions; any powerful tools a skill then wants
# to call are still gated by this same list, so it's safe to allow ungated.
SAFE_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "WebSearch",
    "WebFetch",
    "TodoWrite",
    "NotebookRead",
    "BashOutput",
    "Task",  # subagents (read-oriented orchestration)
    "Skill",  # invoke a loaded skill (instructions only; its tool use stays gated)
]


class PermissionManager:
    """Per-session tool-availability policy for the SDK bridge.

    default_yolo: when True, every session gets the full toolset (Bash/Write/Edit/…)
    by default — equivalent to /yolo always on. A per-session `/yolo off` still wins
    (explicit OFF overrides the default), and `/yolo on` is a no-op since it's already on.
    """

    def __init__(self, default_yolo: bool = False) -> None:
        self._yolo: set[str] = set()       # explicit per-session ON
        self._yolo_off: set[str] = set()   # explicit per-session OFF (overrides default)
        self.default_yolo = default_yolo

    def set_yolo(self, session_key: str, on: bool) -> None:
        if on:
            self._yolo.add(session_key)
            self._yolo_off.discard(session_key)
        else:
            self._yolo_off.add(session_key)
            self._yolo.discard(session_key)

    def is_yolo(self, session_key: str) -> bool:
        if session_key in self._yolo_off:
            return False
        return session_key in self._yolo or self.default_yolo

    def resolve_tools(self, session_key: str, base_tools: Optional[list]):
        """Return the effective `tools` value for this turn.

        base_tools: None = full Claude Code preset, [] = chat (no tools), list = explicit.
        Gated (no /yolo) + full deployment -> restrict to SAFE_TOOLS.
        """
        if base_tools == []:
            return []  # chat mode: nothing to gate
        if self.is_yolo(session_key):
            return base_tools  # full power (None = full preset, or explicit list)
        if base_tools is None:
            return list(SAFE_TOOLS)  # gated: read-only subset only
        return base_tools  # explicit custom list: respect as configured
