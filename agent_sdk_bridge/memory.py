"""Load Hermes file-memory (MEMORY.md / USER.md) as a system-prompt append.

Hermes' built-in memory is two markdown files under <hermes_home>/memories/. The
Claude Code engine has its own memory/CLAUDE.md mechanism, but to carry Hermes'
existing memory across the fork we inject these files into the system prompt each
turn (cheap, and reflects edits made between turns).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def load_memory_append(
    memories_dir: str | Path,
    memory_char_limit: int = 2200,
    user_char_limit: int = 1375,
) -> Optional[str]:
    """Return a system-prompt append string built from MEMORY.md + USER.md, or None."""
    base = Path(memories_dir)
    sections: list[str] = []

    mem = base / "MEMORY.md"
    if mem.exists():
        try:
            t = mem.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            t = ""
        if t:
            sections.append("# Project / environment memory\n" + t[:memory_char_limit])

    usr = base / "USER.md"
    if usr.exists():
        try:
            t = usr.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            t = ""
        if t:
            sections.append("# User profile\n" + t[:user_char_limit])

    if not sections:
        return None

    return (
        "Persistent memory about this user and environment — carry it forward "
        "across turns; don't repeat it back verbatim unless asked:\n\n"
        + "\n\n".join(sections)
    )
