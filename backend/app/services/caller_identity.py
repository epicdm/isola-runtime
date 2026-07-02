"""DEF-006: resolve who is talking to the agent, from the channel sender.

Role is decided HERE (deterministic), never by the model:
  owner    - sender matches agent.owner_phone
  customer - sender matches exactly one Odoo res.partner phone
  public   - no match
"""
import logging
logger = logging.getLogger(__name__)

def _norm(p: str | None) -> str:
    return ''.join(ch for ch in (p or '') if ch.isdigit())[-10:]

async def resolve_caller(agent, sender_phone: str) -> dict:
    sp = _norm(sender_phone)
    if sp and _norm(getattr(agent, 'owner_phone', None)) == sp:
        return {"phone": sender_phone, "role": "owner", "partner_id": None}
    try:
        from app.services.odoo_service import agent_odoo_service
        svc = agent_odoo_service()
        uid = svc._authenticate()
        parts = svc._execute(uid, 'res.partner', 'search_read',
                             [[['customer_rank', '>', 0]]],
                             {'fields': ['id', 'phone'], 'limit': 500})
        matches = [p['id'] for p in parts if _norm(p.get('phone')) == sp and sp]
        if len(matches) == 1:
            return {"phone": sender_phone, "role": "customer", "partner_id": matches[0]}
    except Exception as e:
        logger.warning(f"caller_identity: odoo lookup failed: {e}")
    return {"phone": sender_phone, "role": "public", "partner_id": None}
