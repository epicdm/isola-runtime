"""Tier 1 / Tier 1.5a — knowledge.md write helper.

Extracted from `api/internal.py teach_agent` so two callers can share it:
  - HTTP route POST /internal/agents/{id}/teach (owner UI)
  - WA webhook owner-reply handler (live owner-ask loop, #109)

Single source of truth for the on-disk knowledge.md format. Idempotent:
re-teaching the same topic replaces the existing section in place.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone as _tz
from pathlib import Path

from app.config import get_settings


@dataclass
class TeachResult:
    topic: str
    knowledge_md_path: str
    gaps_marked_taught: int


def _agent_workspace(agent_id: uuid.UUID) -> Path:
    return Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "workspace"


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _normalize_topic(topic: str | None, question: str) -> str:
    candidate = (topic or question[:60]).strip().rstrip("?.!").strip()
    if candidate:
        candidate = candidate[0].upper() + candidate[1:]
    return candidate


def record_teach(
    *,
    agent_id: uuid.UUID,
    question: str,
    answer: str,
    topic: str | None = None,
    teacher_name: str | None = None,
) -> TeachResult:
    """Append/replace a Q&A in workspace/knowledge.md and mark matching
    gap rows as taught. Synchronous file I/O — wrap in a thread pool if
    you call from a hot async path; the WA webhook background task is
    already async-detached so direct calls are fine."""
    ws = _agent_workspace(agent_id)
    ws.mkdir(parents=True, exist_ok=True)

    topic_clean = _normalize_topic(topic, question)
    today = datetime.now(_tz.utc).strftime("%Y-%m-%d")
    teacher_line = f"*Source: {teacher_name or 'owner'} - taught {today}*"

    knowledge_path = ws / "knowledge.md"
    existing = _read_text(knowledge_path)
    if not existing:
        existing = (
            "# Knowledge\n\n"
            "_Q&A taught by the owner. Rex consults this when answering FAQs._\n"
        )

    section_pattern = re.compile(
        rf"^## {re.escape(topic_clean)}\s*$.*?(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    new_section = f"## {topic_clean}\n{answer.strip()}\n{teacher_line}\n\n"

    if section_pattern.search(existing):
        new_content = section_pattern.sub(new_section, existing)
    else:
        new_content = existing.rstrip() + "\n\n" + new_section
    knowledge_path.write_text(new_content, encoding="utf-8")

    gaps_path = ws / "knowledge-gaps.md"
    gaps_marked = 0
    taught_marker_re = re.compile(r"(?:·|-)\s*taught\s+\d{4}-\d{2}-\d{2}")
    if gaps_path.exists():
        norm_q = re.sub(r"[?!.,\s]+$", "", question.lower().strip())
        new_lines: list[str] = []
        for line in gaps_path.read_text(encoding="utf-8").splitlines():
            m = re.search(r'asked:\s*"([^"]+)"', line)
            if m and not taught_marker_re.search(line):
                line_q = re.sub(r"[?!.,\s]+$", "", m.group(1).lower().strip())
                if line_q == norm_q:
                    line = f"{line.rstrip()} - taught {today}"
                    gaps_marked += 1
            new_lines.append(line)
        gaps_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    return TeachResult(
        topic=topic_clean,
        knowledge_md_path=str(knowledge_path.relative_to(ws.parent)),
        gaps_marked_taught=gaps_marked,
    )
