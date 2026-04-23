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


import os
META_GRAPH_API_BASE = os.environ.get("WHATSAPP_GRAPH_BASE_URL") or "https://graph.facebook.com/v21.0"


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

    async def download_media(
        self,
        *,
        media_id: str,
        access_token: str,
    ) -> tuple[bytes, str, str]:
        """Fetch inbound media bytes from Meta in the standard 2-step flow.

        Step 1: GET /{media_id} → JSON with a short-lived signed `url` field.
        Step 2: GET that signed url (authenticated) → raw bytes.

        Returns (bytes, mime_type, sha256). Raises httpx.HTTPError on failure;
        the caller should log + return a graceful reply to the customer.
        """
        headers = {"Authorization": f"Bearer {access_token}"}
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            meta_resp = await client.get(
                f"{META_GRAPH_API_BASE}/{media_id}", headers=headers
            )
            meta_resp.raise_for_status()
            meta = meta_resp.json()
            signed_url = meta.get("url")
            mime_type = meta.get("mime_type", "application/octet-stream")
            sha256 = meta.get("sha256", "")
            if not signed_url:
                raise ValueError(f"No download url in Meta response: {meta}")

            file_resp = await client.get(signed_url, headers=headers)
            file_resp.raise_for_status()
            return file_resp.content, mime_type, sha256

    async def send_image_by_url(
        self,
        *,
        phone_number_id: str,
        access_token: str,
        to: str,
        image_url: str,
        caption: str = "",
    ) -> dict:
        """Send an image to a WhatsApp user by public URL.

        Meta requires the URL to be publicly accessible (HTTPS, no auth,
        TLS 1.2+, valid cert). For private images use upload_media first
        and send via media_id (send_media_by_id, to be added when needed).
        """
        url = f"{META_GRAPH_API_BASE}/{phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "image",
            "image": {"link": image_url},
        }
        if caption:
            payload["image"]["caption"] = caption[:1024]  # Meta caps caption at 1024.
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                logger.error(
                    f"[WhatsApp] send_image_by_url failed "
                    f"status={resp.status_code} body={resp.text[:500]}"
                )
            resp.raise_for_status()
            return resp.json()


# Extension map: MIME prefix -> default file extension when Meta doesn't
# give us a usable filename. Covers the common Cloud API media types.
_DEFAULT_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/amr": ".amr",
    "video/mp4": ".mp4",
    "video/3gpp": ".3gp",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/plain": ".txt",
    "text/csv": ".csv",
}


def guess_extension(mime_type: str) -> str:
    """Map a MIME type to a file extension (best-effort, defaults to .bin)."""
    return _DEFAULT_EXT.get(mime_type.split(";", 1)[0].strip().lower(), ".bin")


whatsapp_service = WhatsAppService()
