"""WhatsApp Business Cloud API client service.

Phase B.1 scaffolding. Owns outbound calls to Meta's Graph API and
webhook signature verification. B.2 adds the actual LLM→reply round-trip.

Meta Graph API base: https://graph.facebook.com/v21.0
Auth: Bearer <access_token> (WABA-scoped, per ChannelConfig.extra_config).
Rate limits: 1,000 messages/sec per WABA (plenty for MVP).
Docs: https://developers.facebook.com/docs/whatsapp/cloud-api
"""

from __future__ import annotations

import hashlib
import hmac

import httpx
from loguru import logger


META_GRAPH_API_BASE = "https://graph.facebook.com/v21.0"


class WhatsAppService:
    """Client for Meta's WhatsApp Business Cloud API."""

    @staticmethod
    def verify_webhook_signature(body_bytes: bytes, signature_header: str, app_secret: str) -> bool:
        """Verify the x-hub-signature-256 header on a Meta webhook POST.

        Header format: 'sha256=<hex-digest>'. Meta computes HMAC-SHA256 of the
        raw request body using the WhatsApp app's app_secret. Returns True if
        the digest matches, False otherwise (including on malformed header).
        """
        if not signature_header or not signature_header.startswith("sha256="):
            return False
        expected = signature_header.split("=", 1)[1].strip()
        computed = hmac.new(
            app_secret.encode("utf-8"),
            body_bytes,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, computed)

    async def send_text_message(
        self,
        *,
        phone_number_id: str,
        access_token: str,
        to: str,
        text: str,
        preview_url: bool = False,
    ) -> dict:
        """Send a text message to a WhatsApp user.

        Args:
            phone_number_id: Meta's phone_number_id (the "from" number).
            access_token: WABA-scoped long-lived token.
            to: E.164 recipient number without leading '+' (Meta format).
            text: body (up to 4096 chars; caller should chunk).
            preview_url: include link preview for any URL in the text.

        Returns the raw Graph API JSON response on success. Raises httpx.HTTPError
        on transport failure; the caller should log and continue.
        """
        url = f"{META_GRAPH_API_BASE}/{phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": preview_url, "body": text},
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                logger.error(
                    f"[WhatsApp] send_text_message failed "
                    f"status={resp.status_code} body={resp.text[:500]}"
                )
            resp.raise_for_status()
            return resp.json()

    async def send_typing_indicator(
        self,
        *,
        phone_number_id: str,
        access_token: str,
        message_id: str,
    ) -> dict:
        """Send a typing indicator (reply to a specific inbound message).

        Only works within 24h of the user's last inbound. Meta shows a
        typing bubble for ~25s or until the next outbound, whichever first.
        """
        url = f"{META_GRAPH_API_BASE}/{phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
            "typing_indicator": {"type": "text"},
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                logger.debug(
                    f"[WhatsApp] typing indicator failed "
                    f"status={resp.status_code} — non-fatal"
                )
            # Typing failures are non-fatal — return the response regardless.
            return resp.json() if resp.status_code < 400 else {}


whatsapp_service = WhatsAppService()
