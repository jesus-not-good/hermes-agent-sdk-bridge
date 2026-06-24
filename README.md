# hermes-agent-sdk-bridge

Run [Hermes Agent](https://github.com/NousResearch/hermes-agent)'s messaging gateway
(Telegram, Discord, …) on the **first-party Claude Code engine** via the official
[Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview) — so the brain runs
on your **Claude Pro/Max subscription** instead of a paid API key.

## Why

Hermes can target Anthropic by borrowing the Claude Code OAuth token and calling
`api.anthropic.com` directly. Anthropic now blocks that path for third-party apps:

```
HTTP 400 — "Third-party apps now draw from extra usage, not plan limits."
```

The only legitimate way onto the subscription is the **first-party Claude Code engine**,
which the **Claude Agent SDK** (`claude-agent-sdk`) exposes programmatically. This add-on
replaces Hermes' inference path (`AIAgent` + provider layer) with a thin bridge that drives
the Agent SDK. The Agent SDK wraps the local `claude` CLI and inherits its login — so when
`claude` is logged in with your subscription, the bot runs on your subscription.

Everything else in Hermes — Telegram/adapters, commands, sessions, file-memory, skills —
stays as-is. Only the brain changes.

> ⚠️ **Honest caveat.** This runs an automated agent on a personal subscription. Keep it to
> **your own** use (lock the platform allowlist to your own ID), not a shared/public bot, and
> don't hammer it with autonomous loops. Subscription automated use is a gray area in
> Anthropic's terms; the unambiguous alternative is a normal API key. This project contains
> **no** circumvention — it uses the official SDK and the real first-party engine.

## What's inside

```
agent_sdk_bridge/                  # the add-on package (drop into the Hermes repo root)
  bridge.py        AgentSDKBridge — per-chat, session-persistent turns via query(resume=…)
  permissions.py   PermissionManager — per-/yolo tool-availability gating
  memory.py        load_memory_append — inject Hermes MEMORY.md/USER.md into the system prompt
patches/
  hermes-gateway-agent-sdk.patch   # edits to gateway/run.py + gateway/slash_commands.py
examples/
  smoke_test.py                    # standalone bridge test (no Telegram)
```

## How it works

- `gateway/run.py` `_run_agent_inner()` gets a flag-gated branch: when `HERMES_USE_AGENT_SDK`
  is truthy, the turn is routed to `_run_agent_via_sdk_bridge()` instead of the legacy
  `AIAgent`. It returns the same `{"final_response", "messages", "api_calls", "completed"}`
  dict the gateway already expects, so the rest of the pipeline is unchanged.
- The bridge maps each Hermes `session_key` → a Claude Agent SDK `session_id` (persisted to
  `<hermes_home>/agent_sdk_sessions.json`) and calls `query(prompt, ClaudeAgentOptions(
  resume=<id>, cwd=…))` per turn. `resume` gives multi-turn continuity; a fresh turn starts a
  new SDK session.
- Hermes `MEMORY.md`/`USER.md` are injected each turn via the system-prompt `append`.
- Tool access is gated by **availability**: read-only tools (Read/Glob/Grep/WebSearch/
  WebFetch/…) are always offered; powerful tools (Bash/Write/Edit/MCP) are only offered when
  the chat has `/yolo` enabled.

## Requirements

- A working [Hermes Agent](https://github.com/NousResearch/hermes-agent) install (the gateway).
- Python 3.11+ (Hermes' venv).
- The `claude` CLI logged in with your Claude Pro/Max subscription (`claude` → `/login`).
  Verify: `claude -p "Reply PONG"` returns `PONG` with **no** `ANTHROPIC_API_KEY` set.
- `claude-agent-sdk` (installed below).

## Install

From your Hermes repo root (e.g. `~/.hermes/hermes-agent`):

```bash
# 1. Install the Agent SDK into Hermes' venv (bundles its own Node engine)
venv/bin/python -m pip install -U claude-agent-sdk

# 2. Drop the add-on package into the repo root
cp -R /path/to/hermes-agent-sdk-bridge/agent_sdk_bridge ./agent_sdk_bridge

# 3. Apply the gateway patch
git apply /path/to/hermes-agent-sdk-bridge/patches/hermes-gateway-agent-sdk.patch
#   (if your Hermes is a different version and the patch doesn't apply cleanly,
#    apply the two hunks by hand — they add _agent_sdk_bridge() /
#    _run_agent_via_sdk_bridge() to GatewayRunner, a flag-gated branch in
#    _run_agent_inner, and /new + /yolo hooks in slash_commands.py)
```

## Run

Make sure **no** `ANTHROPIC_API_KEY` is set (so the engine uses the subscription), then start
the gateway with the flag:

```bash
env -u ANTHROPIC_API_KEY \
  HERMES_USE_AGENT_SDK=1 \
  HERMES_SDK_TOOLS=full \
  venv/bin/python -m hermes_cli.main gateway run
```

### Environment variables

| Var | Values | Default | Meaning |
|---|---|---|---|
| `HERMES_USE_AGENT_SDK` | `1`/`true`/… | off | Route turns through the Agent SDK bridge. Off = stock Hermes. |
| `HERMES_SDK_TOOLS` | `full` \| `chat` | `full` | `full` = Claude Code toolset (gated by `/yolo`); `chat` = no tools, pure chat. |
| `HERMES_SDK_PERMISSION_MODE` | SDK permission mode | `bypassPermissions` | Applied to whatever tools are *available* (availability is gated separately). |
| `HERMES_SDK_WARM` | `1`/`true`/… | off | Hold a long-lived `ClaudeSDKClient` per chat (reuse the engine across turns). Cuts ~5–8s/turn (the spawn tax; ~14s with the full toolset). Off = cold `query()`+`resume` per turn. |
| `HERMES_SDK_WARM_MAX` | int | `6` | Max concurrent live engine subprocesses (LRU-evicted) when warm. |
| `HERMES_SDK_WARM_IDLE` | seconds | `900` | Reap a warm client idle longer than this. |
| `HERMES_SDK_SHOW_SKILLS` | `1`/`0` | on | Append a line listing the skills the engine invoked this turn (`ToolUseBlock(name="Skill")`) to the reply. |

The flag is **off by default**, so installing the add-on does not change stock behavior until
you opt in.

## Using it

- Plain questions, reading files, web search → work immediately.
- Running commands / writing or editing files → send **`/yolo`** in the chat first, then ask.
  Without it, those tools aren't offered and the agent will say so.
- `/new` → starts a fresh SDK session for the chat.

## Verify (no Telegram needed)

```bash
env -u ANTHROPIC_API_KEY venv/bin/python /path/to/hermes-agent-sdk-bridge/examples/smoke_test.py
```

Expected: a `PONG`-style reply and a printed `session_id`, proving the bridge runs on the
subscription.

## Status

Working & verified: subscription auth (CLI + SDK), session-persistent multi-turn, memory
injection, fork at the bridge level (`/branch`), `/new`, `/yolo` tool gating, full multi-step
tool turns over Telegram, on-disk skills via `setting_sources`, and the warm-client latency
optimization (`HERMES_SDK_WARM` — engine reused across turns; verified continuity, reconnect
on memory/yolo change, fork, reset, and idle reap).

Work in progress: a true `/approve` `/deny` approval round-trip (current gating denies
powerful tools until `/yolo`).

## Always-on (macOS, launchd)

To keep the bot running after you close the terminal and across reboots, install it as a
launchd user agent. Template: `deploy/macos-launchd.plist.template` — replace the
`__PLACEHOLDERS__`, copy to `~/Library/LaunchAgents/ai.hermes.sdktest.plist`, then:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.hermes.sdktest.plist
launchctl print gui/$(id -u)/ai.hermes.sdktest | grep -i 'state ='   # -> running
```

- Redeploy after editing code: `launchctl kickstart -k gui/$(id -u)/ai.hermes.sdktest`
- Stop / remove: `launchctl bootout gui/$(id -u)/ai.hermes.sdktest`

The service deliberately omits `ANTHROPIC_API_KEY` so it uses your subscription login.
It is an always-on agent that can run tools after `/yolo` — keep your platform allowlist
locked to your own id, and treat it as you would any standing remote-shell exposure.

## Security

- Never commit `.env` files or bot tokens. The platform token lives in your Hermes home
  (`<hermes_home>/.env`), outside this repo.
- Lock your platform allowlist (e.g. `TELEGRAM_ALLOWED_USERS`) to your own ID.

## Credits / license

Built on [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) and
Anthropic's [claude-agent-sdk](https://github.com/anthropics/claude-agent-sdk-python).
MIT licensed — see `LICENSE`.
