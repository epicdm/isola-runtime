"""Tool management API — CRUD for tools and per-agent assignments."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.tool import Tool, AgentTool
from app.models.user import User

router = APIRouter(prefix="/tools", tags=["tools"])


# Sensitive field keys that should be encrypted when stored.
# This is used as a FALLBACK for tools that don't have config_schema.
# When config_schema is available, fields with type='password' are used instead.
SENSITIVE_FIELD_KEYS = {"api_key", "private_key", "auth_code", "password", "secret"}


def _get_sensitive_keys(config_schema: dict | None = None) -> set[str]:
    """Determine which config keys are sensitive.

    If config_schema is provided, extract keys whose field type is 'password'.
    Always includes the hardcoded SENSITIVE_FIELD_KEYS as a fallback so that
    tools without config_schema still get encrypted/decrypted correctly.
    """
    keys = set(SENSITIVE_FIELD_KEYS)
    if config_schema:
        for field in config_schema.get("fields", []):
            if field.get("type") == "password":
                keys.add(field.get("key", ""))
    keys.discard("")  # remove empty string if any
    return keys


def _safe_config_keys(config_schema: dict | None, sensitive_keys: set[str]) -> set[str]:
    """Allowlist of config keys that may be returned to an API consumer.

    Derived from the category's declared ``config_schema``: every declared
    field that is not a password field and is not otherwise sensitive.

    Fail-closed by construction. A key the schema does not declare is never
    returned, so a credential written into the row under a new name cannot
    reach a response without a reviewed schema change.
    """
    if not config_schema:
        return set()
    keys: set[str] = set()
    for field in config_schema.get("fields", []):
        key = field.get("key", "")
        if key and key not in sensitive_keys and field.get("type") != "password":
            keys.add(key)
    return keys


def _declared_secret_keys(config_schema: dict | None) -> set[str]:
    """Password-type keys declared by the schema (for configured-state flags)."""
    if not config_schema:
        return set()
    keys = {f.get("key", "") for f in config_schema.get("fields", []) if f.get("type") == "password"}
    keys.discard("")
    return keys


# ─── Response-side credential containment ───────────────────
#
# defect-isola-runtime-tool-config-mask-writeback-and-partial-secret-disclosure-2026-08-06
#
# What follows governs what may leave this process in an HTTP response. It is
# deliberately SEPARATE from SENSITIVE_FIELD_KEYS / _get_sensitive_keys, which
# govern encryption at rest. Widening the response classifier must never widen
# the storage classifier: app/services/agent_tools.py keeps its own decryption
# key set, so a field this module started encrypting would become unreadable to
# the runtime and break tool execution.
#
# A response may never carry a credential in any form — decrypted, encrypted,
# masked, truncated or placeheld. A "****" + last-four mask is both a partial
# disclosure and a write-back hazard: the UI can submit it straight back over a
# valid stored credential. Consumers get booleans instead.

RESPONSE_SENSITIVE_NAME_PARTS = (
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "auth_code",
    "authorization",
    "client_secret",
    "credential",
    "hmac_secret",
    "passwd",
    "password",
    "private_key",
    "pwd",
    "secret",
    "token",
    "webhook_secret",
)


def _is_sensitive_name(key: str) -> bool:
    """True if a config key name is inherently credential-shaped.

    Substring matching on purpose, so ``smithery_api_key``, ``atlassian_api_key``,
    ``modelscope_api_token`` and ``bot_token`` are all caught without being
    declared anywhere. It fails closed: a harmless field whose name happens to
    contain one of these parts is dropped from value-bearing output rather than
    risked.
    """
    lowered = (key or "").strip().lower()
    return any(part in lowered for part in RESPONSE_SENSITIVE_NAME_PARTS)


def _response_secret_keys(config_schema: dict | None, raw: dict | None) -> set[str]:
    """Every key whose value must never appear in a response.

    Union of the schema's declared password fields, the storage-side fallback
    set, and any stored key with an inherently sensitive name. Neither source is
    trusted alone: a schema that mis-declares ``api_key`` as text is still
    caught by name, and a credential stored under an undeclared name is still
    caught even though the schema says nothing about it.
    """
    try:
        keys = set(_get_sensitive_keys(config_schema)) | _declared_secret_keys(config_schema)
    except (AttributeError, TypeError):
        # A malformed schema must not crash a response, and must not narrow the
        # secret set either. Fall back to the name-based classification alone.
        keys = set(SENSITIVE_FIELD_KEYS)
    keys |= {k for k in (raw or {}) if _is_sensitive_name(k)}
    keys.discard("")
    return keys


def _project_safe_config(raw: dict | None, config_schema: dict | None) -> dict:
    """Allowlisted, non-secret projection of a tool config dict.

    Fail-closed on every axis the containment contract requires:

    * only keys the schema declares as non-secret survive, so an absent,
      empty or malformed schema yields ``{}``;
    * a declared key whose name is inherently sensitive is dropped anyway, so a
      mis-declared schema cannot promote a credential into a response;
    * unknown stored keys are dropped;
    * a container value is dropped whole, because the schema declares no nested
      field and therefore allowlists no nested key.

    The input is the value as stored. It is never decrypted, so no plaintext
    credential is brought into memory on a response path at all.
    """
    if not isinstance(raw, dict) or not raw:
        return {}
    if not isinstance(config_schema, dict):
        return {}
    try:
        safe_keys = _safe_config_keys(config_schema, _get_sensitive_keys(config_schema))
    except (AttributeError, TypeError):
        return {}  # malformed schema fails closed
    out: dict = {}
    for key in sorted(safe_keys):
        if key not in raw or _is_sensitive_name(key):
            continue
        value = raw[key]
        if isinstance(value, (dict, list, tuple, set)):
            continue
        out[key] = value
    return out


def _configured_map(raw: dict | None, config_schema: dict | None) -> dict[str, bool]:
    """``{secret_key: bool}`` — whether a credential exists, never any part of it.

    Presence is derived from the stored value without decrypting it: ciphertext
    is present exactly when plaintext was. Every value is strictly ``bool``.
    """
    source = raw if isinstance(raw, dict) else {}
    keys = _response_secret_keys(config_schema, source)
    return {key: bool(str(source.get(key) or "").strip()) for key in sorted(keys)}


def _safe_config_schema(config_schema: dict | None) -> dict:
    """Field metadata with any stored-looking value stripped.

    Schema metadata describes fields; it must not become a second channel for a
    credential, so a secret field's ``default`` is removed before the schema is
    returned.
    """
    if not isinstance(config_schema, dict) or not config_schema:
        return {}
    fields = config_schema.get("fields")
    if not isinstance(fields, list):
        return {k: v for k, v in config_schema.items() if k != "fields"}
    safe_fields = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        cleaned = dict(field)
        if cleaned.get("type") == "password" or _is_sensitive_name(cleaned.get("key", "")):
            cleaned.pop("default", None)
        safe_fields.append(cleaned)
    out = dict(config_schema)
    out["fields"] = safe_fields
    return out


def _redact_values(payload, values) -> object:
    """Remove specific credential values from anything about to be returned.

    A provider's error string is outside this module's control and can quote the
    credential it rejected. Nothing that was resolved server-side for a
    connectivity test may travel back out, so those exact values are stripped
    from every string in the payload.
    """
    targets = [v for v in (values or []) if isinstance(v, str) and len(v) >= 4]
    if not targets:
        return payload
    if isinstance(payload, str):
        for value in targets:
            payload = payload.replace(value, "[redacted]")
        return payload
    if isinstance(payload, dict):
        return {k: _redact_values(v, targets) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_redact_values(v, targets) for v in payload]
    return payload


def _merge_preserving_secrets(
    stored: dict | None,
    incoming: dict | None,
    config_schema: dict | None,
) -> dict:
    """Apply an incoming config without destroying an omitted credential.

    Secrets are write-only now: the API no longer returns them, so a client
    cannot echo one back and an omitted secret key must mean "unchanged" rather
    than "delete". Explicit removal stays available — send the key with ``null``
    or an empty string — which is a deliberate act distinct from leaving an
    input blank, because a blank secret input is omitted from the payload
    entirely. Callers that genuinely want a wholesale replacement pass
    ``replace_config=true``.

    A preserved value is carried across exactly as stored (ciphertext stays
    ciphertext); ``_encrypt_sensitive_fields`` recognises it and leaves it
    alone.
    """
    stored = stored if isinstance(stored, dict) else {}
    incoming = incoming if isinstance(incoming, dict) else {}
    secret_keys = _response_secret_keys(config_schema, {**stored, **incoming})
    result = dict(incoming)
    for key in secret_keys:
        if key not in incoming:
            if key in stored:
                result[key] = stored[key]
            continue
        value = incoming[key]
        if value is None or (isinstance(value, str) and not value.strip()):
            result.pop(key, None)  # explicit clear
    return result


def _encrypt_sensitive_fields(config: dict, config_schema: dict | None = None) -> dict:
    """Encrypt sensitive fields in config dict.

    Args:
        config: Tool config dict
        config_schema: Optional config_schema to extract password-type field keys

    Returns:
        Config dict with sensitive fields encrypted
    """
    from app.core.security import encrypt_data, decrypt_data
    from app.config import get_settings

    if not config:
        return config

    settings = get_settings()
    result = dict(config)
    sensitive_keys = _get_sensitive_keys(config_schema)

    for key in sensitive_keys:
        if key in result and result[key]:
            value = result[key]
            if isinstance(value, str) and value:
                # Guard against double-encryption: if the value can be
                # successfully decrypted, it is already encrypted — skip it.
                # This happens when the frontend re-submits a config without
                # the user changing the password field (the field value comes
                # from a previous list_tools response which returns decrypted
                # values… EXCEPT when list_tools runs against a tool whose
                # config_schema is empty and therefore couldn't decrypt).
                try:
                    decrypt_data(value, settings.SECRET_KEY)
                    # Decryption succeeded → value is already encrypted, keep as-is
                    continue
                except Exception:
                    # Not encrypted yet → proceed to encrypt
                    pass

                try:
                    result[key] = encrypt_data(value, settings.SECRET_KEY)
                except Exception:
                    # If encryption fails, keep the value as-is
                    pass

    return result


# There is deliberately NO decrypt helper in this module.
#
# Every response path here is now non-decrypting, so a plaintext credential is
# never materialised on an API path at all. Decryption belongs to the runtime,
# which owns its own copy in app/services/agent_tools.py and reads the database
# directly. Re-adding one here would hand a future edit an easy way to reopen
# defect-isola-runtime-tool-config-mask-writeback-and-partial-secret-disclosure-2026-08-06;
# tests/test_tool_config_response_containment.py asserts it stays absent.


# ─── Schemas ────────────────────────────────────────────────
class ToolCreate(BaseModel):
    name: str
    display_name: str
    description: str = ""
    type: str = "mcp"
    category: str = "custom"
    icon: str = "🔧"
    parameters_schema: dict = {}
    mcp_server_url: str | None = None
    mcp_server_name: str | None = None
    mcp_tool_name: str | None = None
    is_default: bool = False
    # Optional: platform admins can specify target tenant (e.g. when managing
    # another company's tools via the Enterprise Settings page).
    tenant_id: str | None = None


class ToolUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    icon: str | None = None
    enabled: bool | None = None
    mcp_server_url: str | None = None
    mcp_server_name: str | None = None
    parameters_schema: dict | None = None
    is_default: bool | None = None
    config: dict | None = None
    # Opt-in wholesale replacement. Off by default so a save that omits a
    # write-only secret preserves the stored credential instead of wiping it.
    replace_config: bool = False


class AgentToolUpdate(BaseModel):
    tool_id: str
    enabled: bool


class CategoryConfigUpdate(BaseModel):
    config: dict


# ─── Global Tool CRUD ──────────────────────────────────────
@router.get("")
async def list_tools(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List platform tools scoped by tenant (builtin + tenant-specific)."""
    query = (
        select(Tool)
        .where(Tool.source.in_(["builtin", "admin"]))
        .order_by(Tool.category, Tool.name)
    )
    # Scope by tenant: show builtin (tenant_id is NULL) + tenant-specific tools
    tid = tenant_id or (str(current_user.tenant_id) if current_user.tenant_id else None)
    if tid:
        from sqlalchemy import or_ as _or
        query = query.where(_or(Tool.tenant_id == None, Tool.tenant_id == uuid.UUID(tid)))
    result = await db.execute(query)
    tools = result.scalars().all()
    return [
        {
            "id": str(t.id),
            "name": t.name,
            "display_name": t.display_name,
            "description": t.description,
            "type": t.type,
            "category": t.category,
            "icon": t.icon,
            "parameters_schema": t.parameters_schema,
            "mcp_server_url": t.mcp_server_url,
            "mcp_server_name": t.mcp_server_name,
            "mcp_tool_name": t.mcp_tool_name,
            "enabled": t.enabled,
            "is_default": t.is_default,
            # Non-secret configuration only, allowlisted from the tool's own
            # config_schema. Credentials are neither decrypted, encrypted nor
            # masked here — callers read the configured-state booleans instead.
            "config": _project_safe_config(t.config, t.config_schema),
            "config_configured": _configured_map(t.config, t.config_schema),
            "credentials_configured": any(_configured_map(t.config, t.config_schema).values()),
            "config_schema": _safe_config_schema(t.config_schema),
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in tools
    ]


@router.post("")
async def create_tool(
    data: ToolCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new tool (typically MCP).

    The tool is scoped to the target tenant, which defaults to the caller's
    own tenant but can be overridden via data.tenant_id. This allows platform
    admins to import MCP tools while viewing another company's settings page.
    """
    # Resolve target tenant: explicit payload value takes priority so that
    # platform admins importing tools for another company work correctly.
    target_tenant_id: uuid.UUID | None = None
    if data.tenant_id:
        try:
            target_tenant_id = uuid.UUID(data.tenant_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid tenant_id format")
    else:
        target_tenant_id = current_user.tenant_id

    # Unique name check is scoped per tenant to avoid cross-tenant collisions.
    existing = await db.execute(
        select(Tool).where(Tool.name == data.name, Tool.tenant_id == target_tenant_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"Tool '{data.name}' already exists")

    tool = Tool(
        name=data.name,
        display_name=data.display_name,
        description=data.description,
        type=data.type,
        category=data.category,
        icon=data.icon,
        parameters_schema=data.parameters_schema,
        mcp_server_url=data.mcp_server_url,
        mcp_server_name=data.mcp_server_name,
        mcp_tool_name=data.mcp_tool_name,
        is_default=data.is_default,
        tenant_id=target_tenant_id,
        source="admin",
    )
    db.add(tool)
    await db.commit()
    await db.refresh(tool)
    return {"id": str(tool.id), "name": tool.name}


# NOTE: Literal path routes (/bulk, /mcp-server) MUST be defined BEFORE
# parameterized routes (/{tool_id}) to avoid older FastAPI/Starlette versions
# matching "bulk" as a uuid.UUID path parameter and returning 422.

class BulkToolUpdateItem(BaseModel):
    tool_id: str
    enabled: bool

@router.put("/bulk")
async def update_tools_bulk(
    updates: list[BulkToolUpdateItem],
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bulk update the enabled status of multiple tools."""
    tool_ids = [uuid.UUID(u.tool_id) for u in updates]
    result = await db.execute(select(Tool).where(Tool.id.in_(tool_ids)))
    tools_map = {str(t.id): t for t in result.scalars().all()}
    
    for update in updates:
        if update.tool_id in tools_map:
            tools_map[update.tool_id].enabled = update.enabled
            
    await db.commit()
    return {"ok": True}


@router.put("/{tool_id}")
async def update_tool(
    tool_id: uuid.UUID,
    data: ToolUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a tool."""
    result = await db.execute(select(Tool).where(Tool.id == tool_id))
    tool = result.scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    update_data = data.model_dump(exclude_unset=True)
    replace_config = bool(update_data.pop("replace_config", False))
    # Merge, then encrypt. A secret the client omitted is preserved rather than
    # deleted: the read contract no longer returns credentials, so the UI cannot
    # echo one back and an omission means "unchanged". Sending the key with null
    # or an empty string clears it; replace_config=true replaces wholesale.
    if "config" in update_data:
        incoming = update_data["config"] or {}
        merged = (
            incoming
            if replace_config
            else _merge_preserving_secrets(tool.config, incoming, tool.config_schema)
        )
        update_data["config"] = _encrypt_sensitive_fields(merged, tool.config_schema)

    for field, value in update_data.items():
        setattr(tool, field, value)
    await db.commit()
    return {"ok": True}


@router.delete("/{tool_id}")
async def delete_tool(
    tool_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a tool (only non-builtin)."""
    result = await db.execute(select(Tool).where(Tool.id == tool_id))
    tool = result.scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    if tool.type == "builtin":
        raise HTTPException(status_code=400, detail="Cannot delete builtin tools")

    await db.execute(delete(AgentTool).where(AgentTool.tool_id == tool_id))
    await db.delete(tool)
    await db.commit()
    return {"ok": True}


# ─── Per-Agent Tool Assignment ─────────────────────────────
@router.get("/agents/{agent_id}")
async def get_agent_tools(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get tools for a specific agent with their enabled status."""
    # All available tools
    all_tools_r = await db.execute(select(Tool).where(Tool.enabled == True).order_by(Tool.category, Tool.name))
    all_tools = all_tools_r.scalars().all()

    # Agent-specific assignments
    agent_tools_r = await db.execute(select(AgentTool).where(AgentTool.agent_id == agent_id))
    assignments = {str(at.tool_id): at for at in agent_tools_r.scalars().all()}

    result = []
    for t in all_tools:
        tid = str(t.id)
        at = assignments.get(tid)
        # MCP tools installed by agents only show for that agent.
        # MCP admin tools show for all agents (default disabled).
        if t.source == "agent" and not at:
            continue
        # If no explicit assignment, use is_default
        enabled = at.enabled if at else t.is_default
        result.append({
            "id": tid,
            "name": t.name,
            "display_name": t.display_name,
            "description": t.description,
            "type": t.type,
            "category": t.category,
            "icon": t.icon,
            "enabled": enabled,
            "is_default": t.is_default,
            "mcp_server_name": t.mcp_server_name,
        })
    return result


@router.put("/agents/{agent_id}")
async def update_agent_tools(
    agent_id: uuid.UUID,
    updates: list[AgentToolUpdate],
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update tool assignments for an agent."""
    for u in updates:
        tool_id = uuid.UUID(u.tool_id)
        # Upsert
        result = await db.execute(
            select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool_id)
        )
        at = result.scalar_one_or_none()
        if at:
            at.enabled = u.enabled
        else:
            db.add(AgentTool(agent_id=agent_id, tool_id=tool_id, enabled=u.enabled))
    await db.commit()
    return {"ok": True}


# ─── MCP Server Testing ────────────────────────────────────
class MCPTestRequest(BaseModel):
    server_url: str
    # Optional standalone API Key. If provided, it is sent as
    # 'Authorization: Bearer {api_key}' and is NOT embedded in the URL.
    api_key: str | None = None


@router.post("/test-mcp")
async def test_mcp_connection(
    data: MCPTestRequest,
    current_user: User = Depends(get_current_user),
):
    """Test connection to an MCP server and list available tools.

    Supports two authentication modes:
    - URL-embedded key (e.g. ?tavilyApiKey=xxx) — include in server_url.
    - Bearer token — pass via api_key field; sent as Authorization header.
    """
    from app.services.mcp_client import MCPClient

    try:
        client = MCPClient(data.server_url, api_key=data.api_key or None)
        tools = await client.list_tools()
        return {"ok": True, "tools": tools}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


# ─── MCP Server-level Credential Management ────────────────
class MCPServerUpdate(BaseModel):
    server_name: str            # Identifies which server's tools to update
    server_url: str             # New MCP server URL (may contain embedded key)
    api_key: str | None = None  # Optional standalone Bearer key
    # Target tenant (platform admins may manage another company's tools)
    tenant_id: str | None = None


@router.put("/mcp-server")
async def update_mcp_server(
    data: MCPServerUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bulk-update the Server URL and API Key for all tools from an MCP server.

    All tools sharing the same mcp_server_name under the target tenant are
    updated atomically. The API Key is stored encrypted in tool.config so
    the agent runner can resolve it at execution time without re-configuring
    each tool individually.

    Authentication priority at runtime (handled by MCPClient):
    1. tool.config['api_key'] — sent as Authorization: Bearer header.
    2. URL query param (e.g. ?tavilyApiKey=xxx) — extracted from the URL
       and converted to Bearer by MCPClient automatically.
    """
    # Resolve target tenant
    target_tenant_id: uuid.UUID | None = None
    if data.tenant_id:
        try:
            target_tenant_id = uuid.UUID(data.tenant_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid tenant_id format")
    else:
        target_tenant_id = current_user.tenant_id

    # Load all tools from this server under the target tenant
    result = await db.execute(
        select(Tool).where(
            Tool.mcp_server_name == data.server_name,
            Tool.tenant_id == target_tenant_id,
        )
    )
    tools = result.scalars().all()
    if not tools:
        raise HTTPException(
            status_code=404,
            detail=f"No tools found for server '{data.server_name}'",
        )

    for tool in tools:
        tool.mcp_server_url = data.server_url
        if data.api_key is not None:
            # Merge api_key into existing config (other keys preserved) and encrypt
            current_config = dict(tool.config or {})
            current_config["api_key"] = data.api_key
            tool.config = _encrypt_sensitive_fields(current_config, tool.config_schema)
        # If api_key is None (not provided), preserve the existing encrypted key

    await db.commit()
    return {"ok": True, "updated": len(tools)}




# ─── Agent-installed Tools Management (admin) ───────────────

@router.get("/agent-installed")
async def list_agent_installed_tools(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin endpoint: list user-installed tools scoped by tenant."""
    from app.models.agent import Agent
    query = (
        select(AgentTool, Tool, Agent)
        .join(Tool, AgentTool.tool_id == Tool.id)
        .outerjoin(Agent, AgentTool.installed_by_agent_id == Agent.id)
        .where(AgentTool.source == "user_installed")
        .order_by(AgentTool.created_at.desc())
    )
    # Scope by tenant: only show tools installed by agents in this tenant
    tid = tenant_id or (str(current_user.tenant_id) if current_user.tenant_id else None)
    if tid:
        from app.models.agent import Agent as Ag
        tenant_agent_ids = select(Ag.id).where(Ag.tenant_id == tid)
        query = query.where(AgentTool.agent_id.in_(tenant_agent_ids))
    result = await db.execute(query)
    rows = result.all()
    return [
        {
            "agent_tool_id": str(at.id),
            "agent_id": str(at.agent_id),
            "tool_id": str(t.id),
            "tool_name": t.name,
            "tool_display_name": t.display_name,
            "mcp_server_name": t.mcp_server_name,
            "installed_by_agent_id": str(at.installed_by_agent_id) if at.installed_by_agent_id else None,
            "installed_by_agent_name": a.name if a else None,
            "enabled": at.enabled,
            "installed_at": at.created_at.isoformat() if at.created_at else None,
        }
        for at, t, a in rows
    ]


@router.delete("/agent-tool/{agent_tool_id}")
async def delete_agent_tool(
    agent_tool_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin: remove an agent-tool assignment. Also deletes the tool record if no other agents use it."""
    at_r = await db.execute(select(AgentTool).where(AgentTool.id == agent_tool_id))
    at = at_r.scalar_one_or_none()
    if not at:
        raise HTTPException(status_code=404, detail="Agent tool assignment not found")
    tool_id = at.tool_id
    await db.delete(at)
    await db.flush()
    # If no other agent uses this tool, delete the tool record too (for MCP tools)
    remaining_r = await db.execute(select(AgentTool).where(AgentTool.tool_id == tool_id).limit(1))
    if not remaining_r.scalar_one_or_none():
        tool_r = await db.execute(select(Tool).where(Tool.id == tool_id))
        tool = tool_r.scalar_one_or_none()
        if tool and tool.type == "mcp":
            await db.delete(tool)
    await db.commit()
    return {"ok": True}


# ─── Per-Agent Tool Config ───────────────────────────────────

class AgentToolConfigUpdate(BaseModel):
    config: dict
    # Opt-in wholesale replacement — used by "remove agent override", which must
    # genuinely clear the row. Off by default so an ordinary save that omits a
    # write-only secret preserves the stored credential.
    replace_config: bool = False


@router.get("/agents/{agent_id}/tool-config/{tool_id}")
async def get_agent_tool_config(
    agent_id: uuid.UUID,
    tool_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get non-secret tool config (global defaults + agent overrides) and config_schema.

    Credentials are never returned — not decrypted, not encrypted, not masked
    and not placeheld. Callers that need to know whether a credential exists
    read the ``*_configured`` boolean maps instead.

    Runtime credential resolution is unaffected: the agent runtime merges and
    decrypts ``Tool.config`` / ``AgentTool.config`` itself in
    ``app/services/agent_tools.py::_get_tool_config``, straight from the
    database. It has never consumed this response.
    """
    tool_r = await db.execute(select(Tool).where(Tool.id == tool_id))
    tool = tool_r.scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    at_r = await db.execute(
        select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool_id)
    )
    at = at_r.scalar_one_or_none()

    # Values as stored, deliberately NOT decrypted: nothing on this path needs a
    # plaintext credential, so none is ever brought into memory.
    schema = tool.config_schema
    raw_global = tool.config or {}
    raw_agent = (at.config if at else {}) or {}
    # Same precedence the runtime applies: agent override beats company default.
    raw_merged = {**raw_global, **raw_agent}
    merged_configured = _configured_map(raw_merged, schema)

    return {
        # Non-secret configuration only, allowlisted from the tool's schema.
        "global_config": _project_safe_config(raw_global, schema),
        "agent_config": _project_safe_config(raw_agent, schema),
        "merged_config": _project_safe_config(raw_merged, schema),
        # Configured-state booleans: a consumer learns that a credential exists
        # without receiving any part of it, and has nothing to write back.
        "global_config_configured": _configured_map(raw_global, schema),
        "agent_config_configured": _configured_map(raw_agent, schema),
        "merged_config_configured": merged_configured,
        "credentials_configured": any(merged_configured.values()),
        "config_schema": _safe_config_schema(schema),
    }


@router.put("/agents/{agent_id}/tool-config/{tool_id}")
async def update_agent_tool_config(
    agent_id: uuid.UUID,
    tool_id: uuid.UUID,
    data: AgentToolConfigUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save per-agent config override for a tool."""
    # Check permission: only platform_admin and org_admin can modify allow_network
    if "allow_network" in data.config:
        if current_user.role not in ("platform_admin", "org_admin"):
            raise HTTPException(
                status_code=403,
                detail="Only platform admin or organization admin can modify network access settings"
            )

    # Encrypt sensitive fields using the tool's config_schema for field type awareness
    tool_r2 = await db.execute(select(Tool).where(Tool.id == tool_id))
    tool_for_schema = tool_r2.scalar_one_or_none()
    schema = tool_for_schema.config_schema if tool_for_schema else None

    at_r = await db.execute(
        select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool_id)
    )
    at = at_r.scalar_one_or_none()

    # Merge before encrypting. Secret inputs are write-only, so a save that omits
    # a secret must preserve the stored credential rather than wipe it. Sending
    # the key with null or an empty string clears it; replace_config=true (used
    # by "remove agent override") replaces the row's config wholesale.
    incoming = data.config or {}
    merged = (
        incoming
        if data.replace_config
        else _merge_preserving_secrets(at.config if at else {}, incoming, schema)
    )
    encrypted_config = _encrypt_sensitive_fields(merged, schema)

    if at:
        at.config = encrypted_config
    else:
        # Create assignment if not exists
        db.add(AgentTool(agent_id=agent_id, tool_id=tool_id, enabled=True, config=encrypted_config))
    await db.commit()
    return {"ok": True}


@router.get("/agents/{agent_id}/with-config")
async def get_agent_tools_with_config(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get agent's enabled tools with per-agent config state and config_schema for settings UI.

    Credentials are never returned — not decrypted, not encrypted, not masked
    and not placeheld. Each tool carries allowlisted non-secret configuration
    plus ``*_configured`` boolean maps, so the UI can show that a key exists
    without receiving any part of it and without anything to write back.

    Special handling: some tools (Jina) store their API key in system_settings
    rather than Tool.config. Only the *existence* of that key is resolved, so
    the agent-level UI can still show the inherited-key hint.
    """
    all_tools_r = await db.execute(select(Tool).where(Tool.enabled == True).order_by(Tool.category, Tool.name))
    all_tools = all_tools_r.scalars().all()
    agent_tools_r = await db.execute(select(AgentTool).where(AgentTool.agent_id == agent_id))
    assignments = {str(at.tool_id): at for at in agent_tools_r.scalars().all()}

    # Pre-fetch system_settings keys that some tools use as an alternative
    # config storage (e.g. Jina stores its API key in system_settings.jina_api_key)
    # Booleans only — the system-setting value is tested for presence and
    # discarded, never retained and never returned.
    system_keys_cache: dict[str, bool] = {}
    SYSTEM_SETTINGS_TOOL_MAP = {
        # tool_name -> system_settings key + value path
        "jina_search": ("jina_api_key", "api_key"),
        "jina_read": ("jina_api_key", "api_key"),
    }

    result = []
    for t in all_tools:
        tid = str(t.id)
        at = assignments.get(tid)
        # MCP tools installed by agents only show for that agent.
        # MCP admin tools show for all agents (default disabled).
        if t.source == "agent" and not at:
            continue
        enabled = at.enabled if at else t.is_default

        # Values as stored, deliberately NOT decrypted — nothing here needs a
        # plaintext credential, so none is brought into memory.
        raw_global = t.config or {}
        raw_agent = (at.config if at else {}) or {}

        global_configured = _configured_map(raw_global, t.config_schema)

        # Fallback: some tools (e.g. Jina) keep their key in system_settings
        # rather than Tool.config. Resolve only whether one EXISTS.
        if t.name in SYSTEM_SETTINGS_TOOL_MAP and not global_configured.get("api_key"):
            ss_key, ss_field = SYSTEM_SETTINGS_TOOL_MAP[t.name]
            if ss_key not in system_keys_cache:
                try:
                    from app.models.system_settings import SystemSetting
                    ss_r = await db.execute(
                        select(SystemSetting).where(SystemSetting.key == ss_key)
                    )
                    ss = ss_r.scalar_one_or_none()
                    system_keys_cache[ss_key] = bool(
                        str((ss.value or {}).get(ss_field, "") if ss else "").strip()
                    )
                except Exception:
                    system_keys_cache[ss_key] = False
            if system_keys_cache[ss_key]:
                global_configured["api_key"] = True

        raw_merged = {**raw_global, **raw_agent}
        merged_configured = _configured_map(raw_merged, t.config_schema)
        if global_configured.get("api_key"):
            merged_configured["api_key"] = True

        result.append({
            "id": tid,
            "agent_tool_id": str(at.id) if at else None,
            "name": t.name,
            "display_name": t.display_name,
            "description": t.description,
            "type": t.type,
            "category": t.category,
            "icon": t.icon,
            "enabled": enabled,
            "is_default": t.is_default,
            "mcp_server_name": t.mcp_server_name,
            "config_schema": _safe_config_schema(t.config_schema),
            # Non-secret configuration only, allowlisted from the tool's schema.
            "global_config": _project_safe_config(raw_global, t.config_schema),
            "agent_config": _project_safe_config(raw_agent, t.config_schema),
            # Configured-state booleans, never a value or any fragment of one.
            "global_config_configured": global_configured,
            "agent_config_configured": _configured_map(raw_agent, t.config_schema),
            "merged_config_configured": merged_configured,
            "credentials_configured": any(merged_configured.values()),
            # Does this agent hold its own override at all? Previously the UI
            # inferred this from a populated agent_config, which no longer
            # carries secrets — so state it explicitly instead.
            "has_agent_override": bool(raw_agent),
            "source": t.source,
        })
    return result


# ─── Email Connection Testing ──────────────────────────────

class EmailTestRequest(BaseModel):
    config: dict
    # Optional context. Secret inputs are write-only, so the client can no
    # longer echo back a credential it was never given; with these the stored
    # credential is resolved server-side for the connection attempt instead.
    agent_id: uuid.UUID | None = None
    tool_id: uuid.UUID | None = None


@router.post("/test-email")
async def test_email_connection(
    data: EmailTestRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Test IMAP and SMTP email connections.

    When ``tool_id`` is supplied, the baseline is whatever the agent runtime
    would itself resolve for that tool — the same merge and the same decryption,
    through ``app/services/agent_tools._get_tool_config``. Values the operator
    actually typed are layered on top. Nothing resolved here is returned: any
    credential that would otherwise surface through a provider error string is
    redacted before the response leaves this function.
    """
    from app.services.email_service import test_connection

    typed = {k: v for k, v in (data.config or {}).items() if v not in ("", None)}
    config = dict(typed)
    resolved: list[str] = []

    if data.tool_id is not None:
        tool_r = await db.execute(select(Tool).where(Tool.id == data.tool_id))
        tool = tool_r.scalar_one_or_none()
        if tool:
            from app.services.agent_tools import _get_tool_config

            runtime_config = await _get_tool_config(data.agent_id, tool.name) or {}
            config = {**runtime_config, **typed}
            resolved = [
                value
                for key, value in runtime_config.items()
                if _is_sensitive_name(key) and isinstance(value, str) and len(value) >= 4
            ]

    try:
        result = await test_connection(config)
    except Exception as e:
        result = {"ok": False, "error": str(e)[:300]}
    return _redact_values(result, resolved)


@router.get("/email-providers")
async def get_email_providers(
    current_user: User = Depends(get_current_user),
):
    """Get list of supported email provider presets with help text."""
    from app.services.email_service import EMAIL_PROVIDERS

    return {
        key: {
            "label": p["label"],
            "help_url": p.get("help_url", ""),
            "help_text": p.get("help_text", ""),
        }
        for key, p in EMAIL_PROVIDERS.items()
    }
# ─── Tool Category Sharing Config (Generic ChannelConfig) ───

@router.get("/agents/{agent_id}/category-config/{category}")
async def get_category_config(
    agent_id: uuid.UUID,
    category: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get shared configuration for a tool category.

    Returns both global_config (company-level, from Tool.config) and
    agent_config (agent-level override, from ChannelConfig) separately.

    Credentials are never returned — not decrypted, not encrypted and not
    masked. Callers that need to know whether a secret exists read the
    *_configured boolean maps instead.
    Company-level values always take precedence at runtime.
    """
    from app.core.permissions import check_agent_access
    from app.models.channel_config import ChannelConfig

    await check_agent_access(db, current_user, agent_id)

    # ── 1. Load company-level (global) config from Tool.config ──────────────
    # Find a tool in this category that actually has config data.
    # We cannot just LIMIT 1 because most tools may have empty config.
    all_cat_tools = await db.execute(
        select(Tool).where(
            Tool.category == category,
            Tool.enabled == True,
        ).order_by(Tool.name)
    )
    raw_global: dict = {}
    cat_schema: dict | None = None
    for ct in all_cat_tools.scalars():
        if ct.config and ct.config != {}:
            cat_schema = ct.config_schema
            # Deliberately NOT decrypted: this endpoint returns no credential
            # value, so plaintext secrets are never brought into memory here.
            raw_global = dict(ct.config)
            break

    # Credentials are never returned by this endpoint — not decrypted, not
    # encrypted, and not masked. A masked placeholder is both a partial
    # disclosure and a write-back hazard: the UI can submit it straight back
    # and overwrite the stored credential. Consumers get booleans instead.
    sensitive_keys = _get_sensitive_keys(cat_schema)
    safe_keys = _safe_config_keys(cat_schema, sensitive_keys)
    secret_state_keys = _declared_secret_keys(cat_schema) | sensitive_keys

    def _project_safe(raw: dict) -> dict:
        """Allowlisted, non-secret subset of a config dict."""
        return {k: v for k, v in raw.items() if k in safe_keys}

    def _configured_map(raw: dict) -> dict:
        """{secret_key: bool} — whether a credential exists, never its value."""
        keys = secret_state_keys | (set(raw) & sensitive_keys)
        return {k: bool(str(raw.get(k) or "").strip()) for k in sorted(keys)}

    # ── 2. Load agent-level config from ChannelConfig ───────────────────────
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == category,
        )
    )
    config = result.scalar_one_or_none()

    config_id = None
    is_configured = bool(raw_global) or config is not None
    raw_agent: dict = {}

    if config:
        config_id = str(config.id)
        # Not decrypted — only presence is needed to derive configured-state.
        raw_agent = {
            "api_key": config.app_secret,
            **(config.extra_config or {}),
        }
        raw_agent = {k: v for k, v in raw_agent.items() if v is not None}

    # ── 3. Build effective config ───────────────────────────────────────────
    # Priority: Agent config > Company config > Default
    # Agent can override company values by setting their own.
    effective_config = {**raw_global, **raw_agent}
    effective_configured = _configured_map(effective_config)

    return {
        "id": config_id,
        "agent_id": str(agent_id),
        "category": category,
        "is_configured": is_configured,
        # Non-secret configuration only, allowlisted from the category schema.
        "config": _project_safe(effective_config),
        "global_config": _project_safe(raw_global),
        "agent_config": _project_safe(raw_agent),
        # Configured-state booleans: the UI learns that a credential exists
        # without receiving any part of it.
        "global_config_configured": _configured_map(raw_global),
        "agent_config_configured": _configured_map(raw_agent),
        "credentials_configured": any(effective_configured.values()),
    }


@router.post("/agents/{agent_id}/category-config/{category}")
async def update_category_config(
    agent_id: uuid.UUID,
    category: str,
    data: CategoryConfigUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update or create shared configuration for a tool category."""
    from app.core.permissions import check_agent_access, is_agent_creator
    from app.models.channel_config import ChannelConfig

    agent, _ = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure category")

    # Encrypt sensitive fields
    encrypted_config = _encrypt_sensitive_fields(data.config)
    app_secret = encrypted_config.get("api_key") or encrypted_config.get("api_secret") or encrypted_config.get("app_secret")
    extra = {k: v for k, v in encrypted_config.items() if k not in ("api_key", "api_secret", "app_secret")}

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == category,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        if app_secret:
            existing.app_secret = app_secret
        # Merge extra config (note: extra is already encrypted)
        existing.extra_config = {**(existing.extra_config or {}), **extra}
        existing.is_configured = True
    else:
        config = ChannelConfig(
            agent_id=agent_id,
            channel_type=category,
            app_id=category,
            app_secret=app_secret,
            extra_config=extra,
            is_configured=True,
        )
        db.add(config)

    await db.commit()

    return {"ok": True}


@router.delete("/agents/{agent_id}/category-config/{category}", status_code=204)
async def delete_category_config(
    agent_id: uuid.UUID,
    category: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove shared configuration for a tool category."""
    from app.core.permissions import check_agent_access, is_agent_creator
    from app.models.channel_config import ChannelConfig

    agent, _ = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove config")

    await db.execute(
        delete(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == category,
        )
    )
    await db.commit()


@router.post("/agents/{agent_id}/category-config/{category}/test")
async def test_category_config(
    agent_id: uuid.UUID,
    category: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Test connectivity for a tool category."""
    return {"ok": True, "message": f"Settings for {category} saved."}
