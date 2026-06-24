"""Core bridge: per-chat, session-persistent turns on the Claude Agent SDK.

Design (validated in scratchpad/bridge_proto.py):
- Each chat (Hermes session_key) maps to one Claude Agent SDK session id.
- A turn calls `query(prompt=..., options=ClaudeAgentOptions(resume=<id>, cwd=<dir>))`.
  `resume=None` starts a fresh session; the new id is read off the ResultMessage and
  stored, so the next turn resumes with full context.
- Tool access is gated per-turn by availability (see permissions.py): read-only tools
  always; powerful tools only when the chat has /yolo. This avoids the SDK's
  `can_use_tool` callback, which would force streaming-input mode.

Two execution modes:
- Cold (default): `query()` + `resume` per turn. Spawns the Node engine each turn
  (~5-8s tax) but holds no persistent subprocess — scales to many idle chats.
- Warm (`BridgeConfig.warm`): a long-lived `ClaudeSDKClient` per chat. The engine is
  spawned once on connect and reused across turns, eliminating the per-turn spawn tax.
  Under asyncio the SDK's read loop is bound to the event loop (not the calling task —
  see _internal/_task_compat.spawn_detached), so a warm client can be driven across
  turns/tasks safely. We serialize a chat's turns with a per-key lock, reconnect when
  the system-prompt/tool signature changes (memory edit, /yolo) or on /branch, and bound
  live subprocesses with an LRU cap + lazy idle reaping.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Awaitable, Callable, Optional, Protocol

from claude_agent_sdk import (
    query,
    ClaudeSDKClient,
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
    # Warm mode: hold a long-lived ClaudeSDKClient per chat (reuse the engine).
    warm: bool = False
    warm_max: int = 6  # max concurrent live engine subprocesses (LRU-evicted)
    warm_idle_seconds: float = 900.0  # reap a warm client idle longer than this


@dataclass
class TurnResult:
    text: str
    session_id: Optional[str]
    is_error: bool
    subtype: Optional[str]
    total_cost_usd: Optional[float]
    num_turns: Optional[int]


@dataclass
class _WarmEntry:
    client: ClaudeSDKClient
    lock: asyncio.Lock
    signature: tuple
    last_used: float


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
        # Warm-mode state.
        self._warm: dict[str, _WarmEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._warm_lock = asyncio.Lock()  # guards _warm structure + serializes connects
        self._warm_dead: set[str] = set()  # keys forced to reconnect on next turn

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
        if self.config.warm:
            return await self._run_turn_warm(
                session_key, user_text,
                on_text=on_text, on_tool=on_tool, cwd=cwd,
                extra_system_append=extra_system_append,
            )
        return await self._run_turn_cold(
            session_key, user_text,
            on_text=on_text, on_tool=on_tool, cwd=cwd,
            extra_system_append=extra_system_append,
        )

    async def _run_turn_cold(
        self,
        session_key: str,
        user_text: str,
        *,
        on_text: Optional[OnText] = None,
        on_tool: Optional[OnTool] = None,
        cwd: Optional[str] = None,
        extra_system_append: Optional[str] = None,
    ) -> TurnResult:
        """Cold path: a fresh engine per turn via query()+resume."""
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
            return self._make_result(result, chunks, session_key)

        logger.error("agent_sdk_bridge.run_turn failed after retry (key=%s): %s", session_key, last_exc)
        raise last_exc  # type: ignore[misc]

    # --- warm path -------------------------------------------------------

    async def _run_turn_warm(
        self,
        session_key: str,
        user_text: str,
        *,
        on_text: Optional[OnText] = None,
        on_tool: Optional[OnTool] = None,
        cwd: Optional[str] = None,
        extra_system_append: Optional[str] = None,
    ) -> TurnResult:
        """Warm path: reuse a long-lived ClaudeSDKClient per chat."""
        lock = self._get_lock(session_key)
        async with lock:  # serialize this chat's turns
            last_exc: Optional[Exception] = None
            for attempt in range(2):
                try:
                    entry = await self._ensure_warm(
                        session_key, cwd, extra_system_append, force_new=(attempt == 1)
                    )
                except Exception as e:
                    last_exc = e
                    logger.warning("warm connect attempt %d/2 failed (key=%s): %s",
                                   attempt + 1, session_key, e)
                    continue

                chunks: list[str] = []
                result: Optional[ResultMessage] = None
                try:
                    await entry.client.query(user_text)
                    async for msg in entry.client.receive_response():
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
                    logger.warning("warm turn attempt %d/2 failed (key=%s): %s",
                                   attempt + 1, session_key, e)
                    await self._drop_warm(session_key)  # subprocess likely dead -> reconnect
                    continue

                entry.last_used = monotonic()
                self._pending_fork.discard(session_key)
                self._reap_idle_lazy()
                return self._make_result(result, chunks, session_key)

            logger.error("warm turn failed after retry (key=%s): %s", session_key, last_exc)
            raise last_exc  # type: ignore[misc]

    async def _ensure_warm(
        self,
        session_key: str,
        cwd: Optional[str],
        append: Optional[str],
        force_new: bool,
    ) -> _WarmEntry:
        """Return a connected warm client for the chat, (re)connecting when needed."""
        resume = self.sessions.get(session_key)
        do_fork = bool(resume) and session_key in self._pending_fork
        tools_eff = self.permissions.resolve_tools(session_key, self.config.tools)
        sig = (repr(tools_eff), append or "")

        async with self._warm_lock:
            entry = self._warm.get(session_key)
            reuse = (
                entry is not None
                and not force_new
                and not do_fork
                and entry.signature == sig
                and session_key not in self._warm_dead
            )
            if reuse:
                return entry  # type: ignore[return-value]

            # (Re)connect: tear down any stale client first.
            if entry is not None:
                await self._safe_disconnect(entry.client)
                self._warm.pop(session_key, None)
            self._warm_dead.discard(session_key)

            options = self._build_options(
                resume=resume, cwd=cwd, append=append, fork=do_fork, tools=tools_eff
            )
            client = ClaudeSDKClient(options=options)
            await client.connect()
            entry = _WarmEntry(
                client=client,
                lock=self._get_lock(session_key),
                signature=sig,
                last_used=monotonic(),
            )
            self._warm[session_key] = entry
            if do_fork:
                self._pending_fork.discard(session_key)
            await self._evict_lru_locked(protect=session_key)
            return entry

    def _get_lock(self, session_key: str) -> asyncio.Lock:
        # No await between get and set -> race-free under asyncio.
        lock = self._locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_key] = lock
        return lock

    async def _safe_disconnect(self, client: ClaudeSDKClient) -> None:
        try:
            await client.disconnect()
        except Exception:
            logger.debug("warm disconnect failed", exc_info=True)

    async def _drop_warm(self, session_key: str) -> None:
        """Disconnect + forget a chat's warm client (e.g. after a transport error)."""
        async with self._warm_lock:
            entry = self._warm.pop(session_key, None)
        if entry is not None:
            await self._safe_disconnect(entry.client)

    async def _evict_lru_locked(self, protect: str) -> None:
        """Evict least-recently-used idle warm clients over capacity. Caller holds _warm_lock."""
        if len(self._warm) <= self.config.warm_max:
            return
        cands = [
            (e.last_used, k, e)
            for k, e in self._warm.items()
            if k != protect and not e.lock.locked()
        ]
        cands.sort(key=lambda t: t[0])
        for _, k, e in cands:
            if len(self._warm) <= self.config.warm_max:
                break
            self._warm.pop(k, None)
            await self._safe_disconnect(e.client)

    def _reap_idle_lazy(self) -> None:
        """Best-effort: schedule a sweep of idle warm clients (never raises)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._reap_idle())

    async def _reap_idle(self) -> None:
        now = monotonic()
        async with self._warm_lock:
            victims = [
                (k, e)
                for k, e in self._warm.items()
                if not e.lock.locked() and now - e.last_used > self.config.warm_idle_seconds
            ]
            for k, _ in victims:
                self._warm.pop(k, None)
        for _, e in victims:
            await self._safe_disconnect(e.client)

    def _schedule_disconnect(self, client: ClaudeSDKClient) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._safe_disconnect(client))
        except RuntimeError:
            pass

    # --- shared ----------------------------------------------------------

    def _make_result(
        self,
        result: Optional[ResultMessage],
        chunks: list[str],
        session_key: str,
    ) -> TurnResult:
        text = (result.result if result and result.result else "".join(chunks)).strip()
        return TurnResult(
            text=text,
            session_id=result.session_id if result else self.sessions.get(session_key),
            is_error=bool(getattr(result, "is_error", False)) if result else True,
            subtype=getattr(result, "subtype", None) if result else None,
            total_cost_usd=getattr(result, "total_cost_usd", None) if result else None,
            num_turns=getattr(result, "num_turns", None) if result else None,
        )

    # --- session ops used by Phase 2 slash commands ---

    def start_new(self, session_key: str) -> None:
        """/new — forget the SDK session id so the next turn starts fresh."""
        self.sessions.clear(session_key)
        self._pending_fork.discard(session_key)
        if self.config.warm:
            # Force a fresh connect next turn; tear down the current client if idle.
            self._warm_dead.add(session_key)
            entry = self._warm.get(session_key)
            if entry is not None and not entry.lock.locked():
                self._warm.pop(session_key, None)
                self._schedule_disconnect(entry.client)

    def request_fork(self, session_key: str) -> bool:
        """/branch — next turn forks the SDK session (new id; original preserved on disk)."""
        if not self.sessions.get(session_key):
            return False
        self._pending_fork.add(session_key)
        return True

    def current_session_id(self, session_key: str) -> Optional[str]:
        return self.sessions.get(session_key)

    async def aclose(self) -> None:
        """Disconnect all warm clients (graceful shutdown)."""
        async with self._warm_lock:
            entries = list(self._warm.values())
            self._warm.clear()
        for e in entries:
            await self._safe_disconnect(e.client)
