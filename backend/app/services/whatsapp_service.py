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

    async def send_location(
        self,
        *,
        phone_number_id: str,
        access_token: str,
        to: str,
        latitude: float,
        longitude: float,
        name: str = "",
        address: str = "",
    ) -> dict:
        """Send a native WhatsApp location message — a tappable pin that
        opens the user's maps app. Much richer than a maps.google.com URL.

        Args:
            latitude / longitude: decimal degrees (WGS84).
            name: optional place name shown in the pin bubble.
            address: optional street address shown below the name.
        """
        url = f"{META_GRAPH_API_BASE}/{phone_number_id}/messages"
        location_body: dict = {
            "latitude": str(latitude),
            "longitude": str(longitude),
        }
        if name:
            location_body["name"] = name[:100]
        if address:
            location_body["address"] = address[:180]
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "location",
            "location": location_body,
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                logger.error(
                    f"[WhatsApp] send_location failed "
                    f"status={resp.status_code} body={resp.text[:500]}"
                )
            resp.raise_for_status()
            return resp.json()

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

    async def send_interactive_buttons(
        self,
        *,
        phone_number_id: str,
        access_token: str,
        to: str,
        body_text: str,
        buttons: list[dict],
        header_text: str = "",
        footer_text: str = "",
    ) -> dict:
        """Send a reply-buttons interactive message (max 3 buttons).

        Each button dict needs {"id": str (max 256), "title": str (max 20)}.
        The id flows back in the customer's button_reply event so the bot can
        route the tap without parsing the visible title.

        Meta Cloud API reference:
          https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages#interactive-object
        """
        if not buttons or len(buttons) > 3:
            raise ValueError(
                f"send_interactive_buttons needs 1-3 buttons, got {len(buttons)}"
            )
        interactive = {
            "type": "button",
            "body": {"text": body_text[:1024]},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": str(b["id"])[:256],
                            "title": str(b["title"])[:20],
                        },
                    }
                    for b in buttons
                ]
            },
        }
        if header_text:
            interactive["header"] = {"type": "text", "text": header_text[:60]}
        if footer_text:
            interactive["footer"] = {"text": footer_text[:60]}

        url = f"{META_GRAPH_API_BASE}/{phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": interactive,
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                logger.error(
                    f"[WhatsApp] send_interactive_buttons failed "
                    f"status={resp.status_code} body={resp.text[:500]}"
                )
            resp.raise_for_status()
            return resp.json()

    async def send_template(
        self,
        *,
        phone_number_id: str,
        access_token: str,
        to: str,
        template_name: str,
        language: str = "en_US",
        body_params: list[str] | None = None,
        components: list[dict] | None = None,
    ) -> dict:
        """Send a pre-approved template message.

        Meta requires templates for any business-initiated send outside the
        24h customer-service window. Templates are registered + approved in
        Meta Business Manager; the send names the template and fills its
        {{1}}, {{2}}, ... variable slots.

        Simple path — pass body_params as a list of strings, one per body
        variable in order:

            await send_template(
                ..., template_name="reservation_reminder",
                language="en_US",
                body_params=["John", "7:00 PM"],
            )

        Rich path — pass pre-built components (header media / quick-reply
        buttons / URL buttons) directly as the components list, per Meta's
        reference:
          https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages#template-object

        body_params and components are mutually exclusive; if components is
        non-None it's used as-is.
        """
        if components is None:
            components = []
            if body_params:
                components.append({
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": str(p)} for p in body_params
                    ],
                })

        url = f"{META_GRAPH_API_BASE}/{phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
                "components": components,
            },
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                logger.error(
                    f"[WhatsApp] send_template failed "
                    f"status={resp.status_code} body={resp.text[:500]}"
                )
            resp.raise_for_status()
            return resp.json()

    async def send_interactive_list(
        self,
        *,
        phone_number_id: str,
        access_token: str,
        to: str,
        body_text: str,
        button_text: str,
        sections: list[dict],
        header_text: str = "",
        footer_text: str = "",
    ) -> dict:
        """Send a list-picker interactive message (up to 10 rows total).

        sections is a list of {"title": str, "rows": [...]} dicts. Each row
        needs {"id": str, "title": str (max 24), "description": str (max 72)}.

        button_text is the single label that opens the menu drawer
        (max 20 chars per Meta).
        """
        total_rows = sum(len(s.get("rows") or []) for s in sections)
        if total_rows == 0 or total_rows > 10:
            raise ValueError(
                f"send_interactive_list needs 1-10 total rows, got {total_rows}"
            )

        normalized_sections = []
        for s in sections:
            rows = [
                {
                    "id": str(r["id"])[:200],
                    "title": str(r["title"])[:24],
                    "description": str(r.get("description", ""))[:72],
                }
                for r in (s.get("rows") or [])
            ]
            normalized_sections.append({
                "title": str(s.get("title", ""))[:24],
                "rows": rows,
            })

        interactive = {
            "type": "list",
            "body": {"text": body_text[:1024]},
            "action": {
                "button": button_text[:20],
                "sections": normalized_sections,
            },
        }
        if header_text:
            interactive["header"] = {"type": "text", "text": header_text[:60]}
        if footer_text:
            interactive["footer"] = {"text": footer_text[:60]}

        url = f"{META_GRAPH_API_BASE}/{phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": interactive,
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                logger.error(
                    f"[WhatsApp] send_interactive_list failed "
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
