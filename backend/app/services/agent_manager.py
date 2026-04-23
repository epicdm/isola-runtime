"""Agent lifecycle manager — workspace file setup for digital employees.

OD-49 A.2-follow: OpenClaw Docker container lifecycle (start_container,
stop_container, remove_container, get_container_status, _generate_openclaw_config)
was stripped here. Native Clawith-style agents run inside the backend process,
not in per-agent Docker containers, so this module now only owns:
  - the agent workspace directory (soul.md / memory / skills / HEARTBEAT.md)
  - archive-on-delete

If we ever re-add per-tenant container isolation for heavier agents (Phase E?),
the scaffolding (docker SDK, network, volume mounts) can come back here.
"""

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.agent import Agent

settings = get_settings()


class AgentManager:
    """Manage agent workspace files (soul.md, skills, memory, HEARTBEAT)."""

    def _agent_dir(self, agent_id: uuid.UUID) -> Path:
        return Path(settings.AGENT_DATA_DIR) / str(agent_id)

    def _template_dir(self) -> Path:
        return Path(settings.AGENT_TEMPLATE_DIR)

    async def initialize_agent_files(self, db: AsyncSession, agent: Agent,
                                      personality: str = "", boundaries: str = "") -> None:
        """Copy template files and customize for this agent."""
        agent_dir = self._agent_dir(agent.id)
        template_dir = self._template_dir()

        if agent_dir.exists():
            logger.warning(f"Agent dir already exists: {agent_dir}")
            return

        if template_dir.exists():
            shutil.copytree(str(template_dir), str(agent_dir))
        else:
            # No template dir (local dev) — create minimal workspace structure
            logger.info(f"Template dir not found ({template_dir}), creating minimal workspace")
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / "workspace").mkdir(exist_ok=True)
            (agent_dir / "workspace" / "knowledge_base").mkdir(exist_ok=True)
            (agent_dir / "memory").mkdir(exist_ok=True)
            (agent_dir / "skills").mkdir(exist_ok=True)
            (agent_dir / "tasks.json").write_text("[]", encoding="utf-8")

        # Customize soul.md
        soul_path = agent_dir / "soul.md"
        from app.models.user import User
        result = await db.execute(select(User).where(User.id == agent.creator_id))
        creator = result.scalar_one_or_none()
        creator_name = creator.display_name if creator else "Unknown"

        soul_content = f"# Personality\n\nI'm {agent.name}, {agent.role_description or 'a digital assistant'}.\n"
        if soul_path.exists():
            template_content = soul_path.read_text()
            soul_content = template_content.replace("{{agent_name}}", agent.name)
            soul_content = soul_content.replace("{{role_description}}", agent.role_description or "general assistant")
            soul_content = soul_content.replace("{{creator_name}}", creator_name)
            soul_content = soul_content.replace("{{created_at}}", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

        def replace_or_append_section(content: str, section_name: str, section_content: str) -> str:
            """Replace existing ## SectionName or append if not found."""
            if not section_content:
                return content

            import re
            pattern = rf"^##\s+{re.escape(section_name)}\s*$"
            lines = content.split('\n')

            for i, line in enumerate(lines):
                if re.match(pattern, line.strip(), re.IGNORECASE):
                    section_start = i
                    section_end = len(lines)
                    for j in range(i + 1, len(lines)):
                        if lines[j].strip().startswith('## '):
                            section_end = j
                            break

                    new_section = f"## {section_name}\n{section_content}\n"
                    lines = lines[:section_start] + [new_section] + lines[section_end:]
                    return '\n'.join(lines)

            return content + f"\n## {section_name}\n{section_content}\n"

        soul_content = replace_or_append_section(soul_content, "Personality", personality)
        soul_content = replace_or_append_section(soul_content, "Boundaries", boundaries)

        soul_path.write_text(soul_content, encoding="utf-8")

        # Ensure memory.md exists
        mem_path = agent_dir / "memory" / "memory.md"
        if not mem_path.exists():
            mem_path.write_text("# Memory\n\n_Record important information and knowledge here._\n", encoding="utf-8")

        # Ensure reflections.md exists — copy from central template
        refl_path = agent_dir / "memory" / "reflections.md"
        if not refl_path.exists():
            refl_template = Path(__file__).parent.parent / "templates" / "reflections.md"
            refl_content = refl_template.read_text(encoding="utf-8") if refl_template.exists() else "# Reflections Journal\n"
            refl_path.write_text(refl_content, encoding="utf-8")

        # Ensure HEARTBEAT.md exists — copy from central template
        hb_path = agent_dir / "HEARTBEAT.md"
        if not hb_path.exists():
            hb_template = Path(__file__).parent.parent / "templates" / "HEARTBEAT.md"
            hb_content = hb_template.read_text(encoding="utf-8") if hb_template.exists() else "# Heartbeat Instructions\n"
            hb_path.write_text(hb_content, encoding="utf-8")

        # Customize state.json
        state_path = agent_dir / "state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
            state["agent_id"] = str(agent.id)
            state["name"] = agent.name
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

        logger.info(f"Initialized agent files at {agent_dir}")

    async def archive_agent_files(self, agent_id: uuid.UUID) -> Path:
        """Archive agent files to a backup location and return the archive directory."""
        agent_dir = self._agent_dir(agent_id)
        archive_dir = Path(settings.AGENT_DATA_DIR) / "_archived"
        archive_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dest = archive_dir / f"{agent_id}_{timestamp}"
        if agent_dir.exists():
            shutil.move(str(agent_dir), str(dest))
            logger.info(f"Archived agent files to {dest}")
        else:
            dest.mkdir(parents=True, exist_ok=True)
        return dest


agent_manager = AgentManager()
