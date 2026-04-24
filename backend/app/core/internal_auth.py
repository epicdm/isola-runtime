"""Phase E.6c: X-Internal-Secret auth gate for /api/internal/* routes.

Used for service-to-service calls from apps/isola (the tenant-facing
Next.js app at isola.epic.dm) into isola-runtime. Shared secret lives
in ISOLA_INTERNAL_SECRET env on both sides.

We deliberately keep this as a SEPARATE auth path from user JWTs so
that internal endpoints never leak into the user-facing surface and
the service secret can be rotated independently.
"""
from fastapi import Header, HTTPException, status

from app.config import get_settings


async def require_internal_secret(
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> None:
    """FastAPI dependency — 401 if header missing or mismatched.

    Env-var gating: if ISOLA_INTERNAL_SECRET is blank (dev default), the
    endpoint is effectively disabled and always 401s. This prevents an
    accidentally-exposed deployment from accepting arbitrary internal
    calls.
    """
    settings = get_settings()
    expected = (settings.ISOLA_INTERNAL_SECRET or "").strip()
    if not expected:
        # Dev default: no secret configured -> endpoint is off.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Internal API disabled on this deployment",
        )
    if not x_internal_secret or x_internal_secret != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Internal-Secret header",
        )
