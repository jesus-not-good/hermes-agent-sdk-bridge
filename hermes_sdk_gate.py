"""Honest gating for slash commands under the Agent SDK fork.

Light module (no heavy imports) shared by the gateway dispatch (gateway/run.py)
and the Telegram menu builder (hermes_cli/commands.py).

When HERMES_USE_AGENT_SDK is on, the legacy "brain" is bypassed, so commands
still wired to the old AIAgent / approval-queue / transcript would run but have
no real effect. Rather than pretend, we answer honestly and (optionally) hide
them from the menu until each is rewired to the SDK.

Two buckets:
- PENDING: rewiring is planned (plan R1-R4). Remove entries here as each lands.
- UNAVAILABLE: a hard limit of the Claude-first-party subscription (plan R5);
  these will never be 1:1, so we explain why.
"""

from __future__ import annotations

import os
from typing import Optional

# Not yet re-pointed at the SDK brain (will be fixed — remove as each is wired).
SDK_PENDING_COMMANDS = frozenset({
    # R1 — control plane (warm-client streaming). /stop wired (R1a).
    "approve", "deny", "steer",
    # R2 — generation knobs
    "model", "reasoning", "fast", "personality",
    # R3 — history & checkpoints
    "retry", "undo", "compress",
    # R4 — concurrency
    "background",
})

# Hard limits of the Claude-first-party subscription — cannot be 1:1.
SDK_UNAVAILABLE_COMMANDS = frozenset({"credits", "billing", "codex-runtime"})

# Per-command honest messages (Russian). Generic fallbacks below.
_PENDING_OVERRIDES = {
    "model": "⏳ /model ещё не подключён к движку Agent SDK. Сейчас модель зафиксирована движком; переключение моделей Claude добавляю (этап R2).",
    "reasoning": "⏳ /reasoning ещё не подключён к движку Agent SDK (этап R2 — глубина рассуждений через effort/thinking).",
    "fast": "⏳ /fast ещё не подключён к движку Agent SDK (этап R2).",
    "personality": "⏳ /personality ещё не подключён к движку Agent SDK (этап R2 — личность пойдёт в системный промпт).",
    "approve": "⏳ /approve ещё не подключён к движку Agent SDK (этап R1 — потульное подтверждение). Пока используй /yolo для разрешения инструментов.",
    "deny": "⏳ /deny ещё не подключён к движку Agent SDK (этап R1). Пока инструменты закрыты по умолчанию без /yolo.",
    "steer": "⏳ /steer ещё не подключён к движку Agent SDK (этап R1).",
    "retry": "⏳ /retry ещё не подключён к движку Agent SDK (этап R3). Пока просто повтори запрос сообщением.",
    "undo": "⏳ /undo ещё не подключён к движку Agent SDK (этап R3).",
    "compress": "⏳ /compress ещё не подключён к движку Agent SDK (этап R3). Движок сам сжимает контекст автоматически.",
    "background": "⏳ /background ещё не подключён к движку Agent SDK (этап R4 — фоновые сабагенты).",
}

_UNAVAILABLE_OVERRIDES = {
    "credits": "⛔ /credits относится к биллингу Nous-кредитов. Бот работает на твоей подписке Claude — отдельных кредитов здесь нет. Расход смотри в /usage.",
    "billing": "⛔ /billing относится к биллингу Nous-кредитов. Бот работает на подписке Claude — управления кредитами здесь нет.",
    "codex-runtime": "⛔ /codex-runtime управляет рантаймом моделей OpenAI/Codex. Движок — Claude Code (подписка), так что команда неприменима.",
}

_GENERIC_PENDING = "⏳ /{cmd} ещё не подключена к движку Agent SDK (в работе). На текущем движке она ничего не изменит."
_GENERIC_UNAVAILABLE = "⛔ /{cmd} недоступна на этом движке (подписка Claude). Это ограничение, не баг."


def is_sdk_mode() -> bool:
    return (os.environ.get("HERMES_USE_AGENT_SDK") or "").strip().lower() in {"1", "true", "yes", "on"}


def sdk_command_gate_message(canonical: Optional[str]) -> Optional[str]:
    """Return an honest reply if this command is gated under SDK mode, else None."""
    if not canonical or not is_sdk_mode():
        return None
    if canonical in SDK_UNAVAILABLE_COMMANDS:
        return _UNAVAILABLE_OVERRIDES.get(canonical) or _GENERIC_UNAVAILABLE.format(cmd=canonical)
    if canonical in SDK_PENDING_COMMANDS:
        return _PENDING_OVERRIDES.get(canonical) or _GENERIC_PENDING.format(cmd=canonical)
    return None


def sdk_hidden_commands() -> frozenset:
    """Commands to hide from the menu under SDK mode (empty in stock mode)."""
    if not is_sdk_mode():
        return frozenset()
    return SDK_PENDING_COMMANDS | SDK_UNAVAILABLE_COMMANDS
