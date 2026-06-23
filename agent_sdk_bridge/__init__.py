"""agent_sdk_bridge — run Hermes turns on the Claude Agent SDK (Max subscription).

This package replaces Hermes' provider/AIAgent inference path with the first-party
Claude Code engine via `claude-agent-sdk`. The gateway hands a user turn + the chat's
stored SDK session id to `AgentSDKBridge.run_turn()`, which drives `query(resume=...)`
on the subscription and streams the response back.

See plan: ~/.claude/plans/wobbly-launching-rainbow.md
"""

from .bridge import (
    AgentSDKBridge,
    BridgeConfig,
    TurnResult,
    SessionMap,
    InMemorySessionMap,
    FileSessionMap,
)
from .memory import load_memory_append

__all__ = [
    "AgentSDKBridge",
    "BridgeConfig",
    "TurnResult",
    "SessionMap",
    "InMemorySessionMap",
    "FileSessionMap",
    "load_memory_append",
]
