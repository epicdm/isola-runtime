"""Degradation footer — customer-facing notice when LCR degraded the model tier.

Pure function; no DB calls. Called after model dispatch in channel_common.

Footer rules (D6 grilling decision):
  - Show when lcr_result.degraded AND difficulty != 'low'
  - Silent on low-difficulty (greetings, small_talk) — avoid friction for trivial intents
  - Transparent when safety override fired (customer got the right model; no notice needed)
  - No footer on happy path

Substrate: parse_and_dispatch (reply_markers.py) strips only [marker: ...] syntax.
_italic_ markdown passes through to WhatsApp unchanged.

# FLOOR-13: trigger condition (lcr_result.degraded) may reshape when LiteLLM
#           virtual-key budgets replace bucket degradation logic
"""
from __future__ import annotations
from datetime import date, datetime
from typing import Optional

from app.services.lcr import LcrResult

_LOW_DIFFICULTY = "low"
_FOOTER_TEMPLATE = (
    "\n\n"
    "_Quick reply with limited detail — full answer available "
    "when your account refreshes {renewal}, "
    "or buy credits to continue at full quality._"
)


def apply_degradation_footer(
    base_reply: str,
    lcr_result: LcrResult,
    intent_difficulty: str,
    bucket_renews_at: Optional[date | datetime | str],
) -> str:
    """Return base_reply with optional degradation footer appended.

    No footer when: happy path, safety override only, or low-difficulty intent.
    Footer when: bucket exhaustion degraded the tier AND difficulty >= medium.
    """
    if not lcr_result.degraded or intent_difficulty == _LOW_DIFFICULTY:
        return base_reply

    if isinstance(bucket_renews_at, str):
        renewal = bucket_renews_at[:10]
    elif isinstance(bucket_renews_at, (date, datetime)):
        renewal = bucket_renews_at.strftime("%Y-%m-%d")
    else:
        renewal = "next month"

    return base_reply + _FOOTER_TEMPLATE.format(renewal=renewal)
