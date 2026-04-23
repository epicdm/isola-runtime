"""WhatsApp Business Cloud API channel routes.

Phase B.1 scaffolding. CRUD for per-agent WhatsApp channel config, webhook
verification (Meta's GET challenge), and a webhook POST handler stub that
currently just acknowledges receipt. B.2 wires the POST handler to
channel_common._call_agent_llm and sends the reply back.

Webhook URL shape (matches slack.py's per-agent pattern):
    GET  /api/channel/whatsapp/{agent_id}/webhook  → Meta verification
    POST /api/channel/whatsapp/{agent_id}/webhook  → inbound events

Meta's single-URL-per-WABA constraint is fine: the agent_id in the URL path
disambiguates when one WABA powers multiple agents. For MVP we assume one
agent per phone_number_id.

WhatsApp-specific fields live in ChannelConfig.extra_config (JSON):
    phone_number_id: str    Meta phone_number_id (the "from" number)
    waba_id: str            WhatsApp Business Account ID
    access_token: str       Long-lived WABA access token (encrypted)
    verify_token: str       shared secret for GET webhook verification
    app_secret: str         Meta app secret for HMAC SHA256 signature check
"""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.database import get_db
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.schemas.schemas import ChannelConfigOut
from app.services.whatsapp_service import whatsapp_service

router = APIRouter(tags=["whatsapp"])

_REQUIRED_CONFIG_FIELDS = ("phone_number_id", "waba_id", "access_token", "verify_token", "app_secret")

# In-memory dedup for Meta webhook message_ids. Meta retries on non-200.
# Swap for Redis-backed dedup in Phase B.2 when we actually process messages.
_processed_wa_messages: set[str] = set()


# ─── Config CRUD ────────────────────────────────────────


@router.post(
    "/agents/{agent_id}/whatsapp-channel",
    response_model=ChannelConfigOut,
    status_code=201,
)
async def configure_whatsapp_channel(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Configure WhatsApp Business Cloud API for an agent.

    Required body fields: phone_number_id, waba_id, access_token,
    verify_token, app_secret. All live in ChannelConfig.extra_config.
    """
    agent, _ = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    missing = [k for k in _REQUIRED_CONFIG_FIELDS if not str(data.get(k, "")).strip()]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing required fields: {', '.join(missing)}",
        )

    extra_config = {k: str(data[k]).strip() for k in _REQUIRED_CONFIG_FIELDS}

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "whatsapp",
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.extra_config = extra_config
        existing.is_configured = True
        await db.commit()
        await db.refresh(existing)
        return existing

    config = ChannelConfig(
        agent_id=agent_id,
        channel_type="whatsapp",
        extra_config=extra_config,
        is_configured=True,
    )
    db.add(config)
    await db.commit()
    await db.refresh(config)
    logger.info(f"[WhatsApp] Configured channel for agent {agent_id}")
    return config


@router.get(
    "/agents/{agent_id}/whatsapp-channel",
    response_model=ChannelConfigOut,
)
async def get_whatsapp_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fetch an agent's WhatsApp channel config."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "whatsapp",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="WhatsApp channel not configured")
    return config


@router.get("/agents/{agent_id}/whatsapp-channel/webhook-url")
async def get_whatsapp_webhook_url(
    agent_id: uuid.UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the public webhook URL the operator gives to Meta in app settings."""
    await check_agent_access(db, current_user, agent_id)
    base = str(request.base_url).rstrip("/")
    return {
        "webhook_url": f"{base}/api/channel/whatsapp/{agent_id}/webhook",
        "note": (
            "Copy verify_token from your saved config when registering this URL in Meta's "
            "WhatsApp Business App > Configuration > Webhooks."
        ),
    }


@router.delete("/agents/{agent_id}/whatsapp-channel", status_code=204)
async def delete_whatsapp_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove an agent's WhatsApp channel config."""
    agent, _ = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "whatsapp",
        )
    )
    config = result.scalar_one_or_none()
    if config:
        await db.delete(config)
        await db.commit()
        logger.info(f"[WhatsApp] Removed channel for agent {agent_id}")


# ─── Webhook (Meta Cloud API) ────────────────────────────


@router.get("/channel/whatsapp/{agent_id}/webhook")
async def whatsapp_webhook_verify(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle Meta's GET webhook verification challenge.

    Meta calls this once when the operator saves the webhook URL.
    Expects query params: hub.mode=subscribe, hub.verify_token=<theirs>,
    hub.challenge=<random>. We match their verify_token against the one
    stored in ChannelConfig.extra_config and echo the challenge back.
    """
    params = dict(request.query_params)
    mode = params.get("hub.mode")
    their_token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")

    if mode != "subscribe" or not challenge:
        return Response(status_code=400)

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "whatsapp",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        return Response(status_code=404)

    our_token = (config.extra_config or {}).get("verify_token", "")
    if not our_token or their_token != our_token:
        logger.warning(f"[WhatsApp] Webhook verify_token mismatch for agent {agent_id}")
        return Response(status_code=403)

    logger.info(f"[WhatsApp] Webhook verified for agent {agent_id}")
    return Response(content=challenge, media_type="text/plain")


@router.post("/channel/whatsapp/{agent_id}/webhook")
async def whatsapp_webhook_event(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle Meta's POST webhook events.

    Phase B.1: verify signature, dedup by message_id, log envelope, ack 200.
    Phase B.2 wires this to channel_common._call_agent_llm and sends replies.
    """
    body_bytes = await request.body()

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "whatsapp",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        return Response(status_code=404)

    app_secret = (config.extra_config or {}).get("app_secret", "")
    signature_header = request.headers.get("x-hub-signature-256", "")
    if app_secret:
        if not whatsapp_service.verify_webhook_signature(
            body_bytes, signature_header, app_secret
        ):
            logger.warning(f"[WhatsApp] Webhook signature mismatch for agent {agent_id}")
            return Response(status_code=401)

    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError:
        return Response(status_code=400)

    # Dedup by message_id — Meta retries aggressively on non-200.
    entries = body.get("entry") or []
    for entry in entries:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            messages = value.get("messages") or []
            for msg in messages:
                msg_id = msg.get("id", "")
                if msg_id and msg_id in _processed_wa_messages:
                    logger.info(f"[WhatsApp] Skipping dedup'd message {msg_id}")
                    continue
                if msg_id:
                    _processed_wa_messages.add(msg_id)
                    if len(_processed_wa_messages) > 2000:
                        _processed_wa_messages.clear()
                logger.info(
                    f"[WhatsApp] agent={agent_id} from={msg.get('from')} "
                    f"type={msg.get('type')} id={msg_id} — B.2 will dispatch"
                )

    # Always ack 200 so Meta stops retrying. Errors logged above.
    return {"status": "ok"}
