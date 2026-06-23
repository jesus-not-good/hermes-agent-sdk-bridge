"""Standalone smoke test — prove the bridge runs on the Claude subscription.

Run from the repo root with NO ANTHROPIC_API_KEY set (so the engine uses the
local `claude` login / subscription), in a venv that has `claude-agent-sdk`:

    env -u ANTHROPIC_API_KEY python examples/smoke_test.py

Expects a PONG-style reply and a session_id.
"""

import os
import sys
import tempfile
import anyio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent_sdk_bridge import AgentSDKBridge, BridgeConfig


async def main():
    cwd = tempfile.mkdtemp()
    bridge = AgentSDKBridge(BridgeConfig(cwd=cwd, tools=[], max_turns=1))
    r = await bridge.run_turn("smoke", "Reply with the single word PONG.")
    print("reply:     ", repr(r.text))
    print("session_id:", r.session_id)
    print("is_error:  ", r.is_error)
    ok = bool(r.text and not r.is_error and r.session_id)
    print("SMOKE_OK:  ", ok)
    sys.exit(0 if ok else 1)


anyio.run(main)
