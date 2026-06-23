"""Core bridge: per-chat, session-persistent turns on the Claude Agent SDK.

Design (validated in scratchpad/bridge_proto.py):
- Each chat (Hermes session_key) maps to one Claude Agent SDK session id.
- A turn calls `query(prompt=..., options=ClaudeAgentOptions(resume=<id>, cwd=<dir>))`.
  `resume=None` starts a fresh session; the new id is read off the ResultMessage and
  stored, so the next turn resumes with full context.
- Tool access is gated per-turn by availability (see permissions.py): read-only tools
  always; powerful tools only when the chat has /yolo. This avoids the SDK's
  `can_use_tool` callback, which would force streaming-input mode.

Why query()+resume per turn (not a long-lived ClaudeSDKClient): it scales to many idle
chats with no persistent Node engine per chat. A warm-client cache is a later optimization.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional, Protocol

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    ResultMessage,
)

from .permissions import PermissionManager

logger = logging.getLogger("hermes.agent_sdk_bridge")

OnText = Callable[[str], None]
OnTool = Callable[[str, dict], None]  # (tool_name, tool_input)


class SessionMap(Protocol):
    """Maps a Hermes session_key -> Claude Agent SDK session id."""

    def get(self, session_key: str) -> Optional[str]: ...
    def set(self, session_key: str, session_id: str) -> None: ...
    def clear(self, session_key: str) -> None: ...


class InMemorySessionMap:
    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def get(self, session_key: str) -> Optional[str]:
        return self._d.get(session_key)

    def set(self, session_key: str, session_id: str) -> None:
        self._d[session_key] = session_id

    def clear(self, session_key: str) -> None:
        self._d.pop(session_key, None)


class FileSessionMap:
    """Persistent session_key -> sdk_session_id map (atomic JSON file)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._d: dict[str, str] = {}
        if self._path.exists():
            try:
                self._d = json.loads(self._path.read_text())
            except Exception:
                logger.warning("FileSessionMap: could not parse %s, starting empty", self._path)
                self._d = {}

    def get(self, session_key: str) -> Optional[str]:
        return self._d.get(session_key)

    def set(self, session_key: str, session_id: str) -> None:
        with self._lock:
            self._d[session_key] = session_id
            self._flush()

    def clear(self, session_key: str) -> None:
        with self._lock:
            self._d.pop(session_key, None)
            self._flush()

    def _flush(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._d, indent=2))
            tmp.replace(self._path)
        except Exception:
            logger.exception("FileSessionMap: flush failed for %s", self._path)


@dataclass
class BridgeConfig:
    """Per-deployment defaults. The gateway can override per turn."""

    cwd: str = "."
    model: Optional[str] = None  # None -> Claude Code default
    # Auto-approve whatever tools ARE available; availability itself is gated per-turn
    # by the PermissionManager (read-only vs /yolo). So bypassPermissions is safe here.
    permission_mode: str = "bypassPermissions"
    # None -> full Claude Code toolset, [] -> chat (no tools), list -> explicit.
    tools: Optional[list[str]] = None
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    setting_sources: Optional[list[str]] = None
    append_system_prompt: Optional[str] = None
    max_turns: Optional[int] = None


@dataclass
class TurnResult:
    text: str
    session_id: Optional[str]
    is_error: bool
    subtype: Optional[str]
    total_cost_usd: Optional[float]
    num_turns: Optional[int]


class AgentSDKBridge:
    def __init__(
        self,
        config: Optional[BridgeConfig] = None,
        session_map: Optional[SessionMap] = None,
        can_use_tool: Optional[Callable[..., Awaitable]] = None,
    ) -> None:
        self.config = config or BridgeConfig()
        self.sessions: SessionMap = session_map or InMemorySessionMap()
        self.can_use_tool = can_use_tool  # reserved for v2 streaming approval
        self._pending_fork: set[str] = set()
        self.permissions = PermissionManager()

    def _build_options(
        self,
        resume: Optional[str],
        cwd: Optional[str],
        append: Optional[str] = None,
        fork: bool = False,
        tools: Optional[list] = None,
    ) -> ClaudeAgentOptions:
        c = self.config
        kwargs: dict = dict(
            resume=resume,
            cwd=cwd or c.cwd,
            permission_mode=c.permission_mode,
            allowed_tools=c.allowed_tools,
            disallowed_tools=c.disallowed_tools,
        )
        if fork:
            kwargs["fork_session"] = True
        # tools=None -> omit (full Claude Code preset); [] or list -> set explicitly.
        if tools is not None:
            kwargs["tools"] = tools
        if c.model is not None:
            kwargs["model"] = c.model
        if c.setting_sources is not None:
            kwargs["setting_sources"] = c.setting_sources
        if c.max_turns is not None:
            kwargs["max_turns"] = c.max_turns
        if append:
            kwargs["system_prompt"] = {
                "type": "preset",
                "preset": "claude_code",
                "append": append,
            }
        return ClaudeAgentOptions(**kwargs)

    async def run_turn(
        self,
        session_key: str,
        user_text: str,
        *,
        on_text: Optional[OnText] = None,
        on_tool: Optional[OnTool] = None,
        cwd: Optional[str] = None,
        extra_system_append: Optional[str] = None,
    ) -> TurnResult:
        """Run one user turn; resume the chat's SDK session if we have one."""
        last_exc: Optional[Exception] = None
        for attempt in range(2):  # one retry on transient SDK/transport errors
            resume = self.sessions.get(session_key)
            do_fork = bool(resume) and session_key in self._pending_fork
            tools_eff = self.permissions.resolve_tools(session_key, self.config.tools)
            options = self._build_options(
                resume=resume, cwd=cwd, append=extra_system_append, fork=do_fork, tools=tools_eff
            )

            chunks: list[str] = []
            result: Optional[ResultMessage] = None
            try:
                async for msg in query(prompt=user_text, options=options):
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                chunks.append(block.text)
                                if on_text:
                                    on_text(block.text)
                            elif isinstance(block, ToolUseBlock) and on_tool:
                                on_tool(block.name, dict(block.input or {}))
                    elif isinstance(msg, ResultMessage):
                        result = msg
                        if msg.session_id:
                            self.sessions.set(session_key, msg.session_id)
            except Exception as e:
                last_exc = e
                logger.warning(
                    "agent_sdk_bridge.run_turn attempt %d/2 failed (key=%s): %s",
                    attempt + 1, session_key, e,
                )
                continue  # transient (SDK error message / transport blip) — retry once

            self._pending_fork.discard(session_key)
            text = (result.result if result and result.result else "".join(chunks)).strip()
            return TurnResult(
                text=text,
                session_id=result.session_id if result else None,
                is_error=bool(getattr(result, "is_error", False)) if result else True,
                subtype=getattr(result, "subtype", None) if result else None,
                total_cost_usd=getattr(result, "total_cost_usd", None) if result else None,
                num_turns=getattr(result, "num_turns", None) if result else None,
            )

        logger.error("agent_sdk_bridge.run_turn failed after retry (key=%s): %s", session_key, last_exc)
        raise last_exc  # type: ignore[misc]

    # --- session ops used by Phase 2 slash commands ---

    def start_new(self, session_key: str) -> None:
        """/new — forget the SDK session id so the next turn starts fresh."""
        self.sessions.clear(session_key)
        self._pending_fork.discard(session_key)

    def request_fork(self, session_key: str) -> bool:
        """/branch — next turn forks the SDK session (new id; original preserved on disk)."""
        if not self.sessions.get(session_key):
            return False
        self._pending_fork.add(session_key)
        return True

    def current_session_id(self, session_key: str) -> Optional[str]:
        return self.sessions.get(session_key)
