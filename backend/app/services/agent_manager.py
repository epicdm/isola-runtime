"""Agent lifecycle manager — Docker container management for Edge (OpenClaw) Gateway instances + workspace file setup."""

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

import docker
from docker.errors import DockerException, NotFound
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.services.llm import get_model_api_key

settings = get_settings()


class AgentManager:
    """Manage Edge (OpenClaw) Gateway Docker containers + agent workspace files."""

    def __init__(self):
        try:
            self.docker_client = docker.from_env()
        except DockerException:
            logger.warning("Docker not available — Edge agent containers will not be managed")
            self.docker_client = None

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

        # Phase F.1.c — role-specific skill pack. After the base template is
        # in place, overlay a role pack (agent_template_packs/<role>/) if
        # the agent's template has a resolvable role. Safe to re-run: shutil
        # copytree with dirs_exist_ok merges without clobbering same-named
        # files. Rex gets 13 skills; Mara/Joey/Cash/Brief/Tech to follow.
        try:
            await self._apply_role_skill_pack(db, agent, agent_dir)
        except Exception as e:  # noqa: BLE001
            # Missing role pack isn't fatal — agent still works, just without
            # role-specific skills. Log and continue.
            logger.warning(f"Role pack overlay failed for agent {agent.id}: {e}")

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

    async def _apply_role_skill_pack(
        self,
        db: AsyncSession,
        agent: Agent,
        agent_dir: Path,
    ) -> None:
        """Overlay role-specific skill bundles on top of the base template.

        Pack layout: `backend/agent_template_packs/<role>/skills/<skill>/SKILL.md`
        Target:      `<agent_dir>/skills/<skill>/SKILL.md`

        Discovery:
          1. agent.template → AgentTemplate.role (e.g. "rex")
          2. If role is None (no template), silently skip.
          3. Pack dir = <this_file>/../../agent_template_packs/<role>
             (container path: /app/agent_template_packs/<role>)
          4. If pack doesn't exist, skip (unknown role = no pack).
        """
        if agent.template_id is None:
            return

        from app.models.agent import AgentTemplate

        r = await db.execute(
            select(AgentTemplate).where(AgentTemplate.id == agent.template_id)
        )
        tpl = r.scalar_one_or_none()
        if tpl is None or not tpl.role:
            return

        # Locate the pack directory. Two candidate paths: Docker (/app) and
        # source layout (backend/).
        candidates = [
            Path("/app/agent_template_packs") / tpl.role,
            Path(__file__).resolve().parent.parent.parent
            / "agent_template_packs"
            / tpl.role,
        ]
        pack_dir = next((p for p in candidates if p.exists()), None)
        if pack_dir is None:
            logger.debug(f"No role pack for '{tpl.role}' — skipping overlay")
            return

        # Merge the pack's skills/ tree into agent_dir/skills/.
        src_skills = pack_dir / "skills"
        if not src_skills.exists():
            return
        dst_skills = agent_dir / "skills"
        dst_skills.mkdir(parents=True, exist_ok=True)

        shutil.copytree(str(src_skills), str(dst_skills), dirs_exist_ok=True)
        logger.info(
            f"Applied '{tpl.role}' skill pack to agent {agent.id} "
            f"({len(list(src_skills.iterdir()))} skills)"
        )

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


    def _generate_openclaw_config(self, agent: Agent, model: "LLMModel | None") -> dict:
        """Generate openclaw.json config for the agent container."""
        config = {
            "agent": {
                "model": f"{model.provider}/{model.model}" if model else "anthropic/claude-sonnet-4-5",
            },
            "agents": {
                "defaults": {
                    "workspace": "/home/node/.openclaw/workspace",
                },
            },
        }

        if model:
            config["env"] = {
                f"{model.provider.upper()}_API_KEY": get_model_api_key(model),
            }

        return config

    async def start_container(self, db: AsyncSession, agent: Agent) -> str | None:
        """Start an Edge (OpenClaw) Gateway Docker container for the agent.

        Returns container_id or None if Docker not available OR agent_type != openclaw.
        Native agents pass through this call as a no-op (keeps create_agent call-site simple).
        """
        if getattr(agent, "agent_type", "native") != "openclaw":
            return None

        if not self.docker_client:
            logger.info("Docker not available, skipping container start")
            agent.status = "idle"
            agent.last_active_at = datetime.now(timezone.utc)
            return None

        agent_dir = self._agent_dir(agent.id)

        # Get model config
        model = None
        if agent.primary_model_id:
            result = await db.execute(select(LLMModel).where(LLMModel.id == agent.primary_model_id))
            model = result.scalar_one_or_none()

        # Generate OpenClaw config
        config = self._generate_openclaw_config(agent, model)
        config_dir = agent_dir / ".openclaw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "openclaw.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

        # Create workspace symlink
        workspace_dir = config_dir / "workspace"
        if not workspace_dir.exists():
            workspace_dir.symlink_to(agent_dir / "workspace")

        # Assign a unique port
        container_port = 18789 + hash(str(agent.id)) % 10000

        try:
            container = self.docker_client.containers.run(
                settings.OPENCLAW_IMAGE,
                detach=True,
                name=f"edge-agent-{str(agent.id)[:8]}",
                network=settings.DOCKER_NETWORK,
                ports={f"{settings.OPENCLAW_GATEWAY_PORT}/tcp": container_port},
                volumes={
                    str(agent_dir): {"bind": "/home/node/.openclaw", "mode": "rw"},
                },
                environment={
                    "OPENCLAW_GATEWAY_TOKEN": str(uuid.uuid4()),
                },
                restart_policy={"Name": "unless-stopped"},
                labels={
                    "isola.agent_id": str(agent.id),
                    "isola.agent_name": agent.name,
                    "isola.agent_type": "edge",
                },
            )

            agent.container_id = container.id
            agent.container_port = container_port
            agent.status = "running"
            agent.last_active_at = datetime.now(timezone.utc)

            logger.info(f"Started Edge container {container.id[:12]} for agent {agent.name} on port {container_port}")
            return container.id

        except DockerException as e:
            logger.error(f"Failed to start Edge container for agent {agent.name}: {e}")
            agent.status = "error"
            return None

    async def stop_container(self, agent: Agent) -> bool:
        """Stop the agent's Docker container (Edge agents only)."""
        if not self.docker_client or not agent.container_id:
            agent.status = "stopped"
            return True

        try:
            container = self.docker_client.containers.get(agent.container_id)
            container.stop(timeout=10)
            agent.status = "stopped"
            logger.info(f"Stopped container {agent.container_id[:12]} for agent {agent.name}")
            return True
        except NotFound:
            agent.status = "stopped"
            agent.container_id = None
            return True
        except DockerException as e:
            logger.error(f"Failed to stop container: {e}")
            return False

    async def remove_container(self, agent: Agent) -> bool:
        """Stop and remove the agent's Docker container (Edge agents only)."""
        if not self.docker_client or not agent.container_id:
            return True

        try:
            container = self.docker_client.containers.get(agent.container_id)
            container.stop(timeout=10)
            container.remove()
            agent.container_id = None
            agent.container_port = None
            logger.info(f"Removed container for agent {agent.name}")
            return True
        except NotFound:
            agent.container_id = None
            return True
        except DockerException as e:
            logger.error(f"Failed to remove container: {e}")
            return False

    def get_container_status(self, agent: Agent) -> dict:
        """Get real-time container status (Edge agents only)."""
        if not self.docker_client or not agent.container_id:
            return {"running": False, "status": agent.status}

        try:
            container = self.docker_client.containers.get(agent.container_id)
            return {
                "running": container.status == "running",
                "status": container.status,
                "ports": container.ports,
                "created": container.attrs.get("Created", ""),
            }
        except NotFound:
            return {"running": False, "status": "not_found"}
        except DockerException:
            return {"running": False, "status": "error"}


agent_manager = AgentManager()
