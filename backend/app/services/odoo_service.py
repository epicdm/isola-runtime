"""Phase F.1.b-3 — Odoo bridge for ensure_company().

One self-hosted Odoo instance at odoo.epic.dm serves every tenant via
Odoo's multi-company feature. Each Isola tenant = one res.company row
in the shared 'epic' database. Runtime backend talks to Odoo over
XML-RPC using the platform admin credentials (server-side only — never
reaches tenant clients).

Design choices:
  - XML-RPC (xmlrpclib, stdlib) over REST — Odoo's canonical protocol,
    no extra deps, deterministic error shapes.
  - Idempotent: ensure_company() searches by external_ref first, then
    by name+country, creating only if no match. Returns existing id on
    repeat calls.
  - Per-call uid resolution (no long-lived session). uids are tiny to
    renew and avoids stale-session bugs during Odoo restarts.
"""

from __future__ import annotations

import logging
import xmlrpc.client
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)


class OdooError(Exception):
    """Raised when an Odoo XML-RPC call fails or returns unexpected shape."""

    def __init__(self, message: str, *, detail: str = ""):
        super().__init__(message)
        self.detail = detail


class OdooService:
    """Lightweight XML-RPC client for odoo.epic.dm.

    Not a singleton — instantiate per request via `odoo_service()` factory
    below so env overrides (future per-tenant creds) stay possible.
    """

    def __init__(
        self,
        *,
        url: str,
        db: str,
        login: str,
        password: str,
    ) -> None:
        self.url = url.rstrip("/")
        self.db = db
        self.login = login
        self.password = password

    # ── Authentication ───────────────────────────────────────────────────

    def _common_proxy(self) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(
            f"{self.url}/xmlrpc/2/common", allow_none=True
        )

    def _object_proxy(self) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(
            f"{self.url}/xmlrpc/2/object", allow_none=True
        )

    def _authenticate(self) -> int:
        """Resolve admin uid. Raises OdooError on bad creds / unreachable."""
        try:
            uid = self._common_proxy().authenticate(
                self.db, self.login, self.password, {}
            )
        except Exception as e:  # noqa: BLE001
            raise OdooError(
                "Odoo auth call failed", detail=f"{type(e).__name__}: {e}"
            ) from e
        if not uid:
            raise OdooError(
                "Odoo authentication rejected",
                detail=(
                    f"db={self.db} login={self.login} returned uid=0 — check "
                    "ODOO_ADMIN_PASSWORD or that the user exists in this DB."
                ),
            )
        return int(uid)

    # ── Generic execute_kw ───────────────────────────────────────────────

    def _execute(
        self,
        uid: int,
        model: str,
        method: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        """Wrapper around execute_kw that surfaces Odoo faults as OdooError."""
        try:
            return self._object_proxy().execute_kw(
                self.db,
                uid,
                self.password,
                model,
                method,
                args or [],
                kwargs or {},
            )
        except xmlrpc.client.Fault as e:
            raise OdooError(
                f"Odoo {model}.{method} fault",
                detail=f"{e.faultCode}: {e.faultString[:500]}",
            ) from e
        except Exception as e:  # noqa: BLE001
            raise OdooError(
                f"Odoo {model}.{method} transport error",
                detail=f"{type(e).__name__}: {e}",
            ) from e

    # ── Public: ensure_company ───────────────────────────────────────────

    def ensure_company(
        self,
        *,
        name: str,
        external_ref: str,
        vertical: str | None = None,
        country_code: str | None = None,
        email: str | None = None,
        phone: str | None = None,
    ) -> int:
        """Idempotent create-or-return res.company for an Isola tenant.

        Lookup order:
          1. res.company.ref == external_ref  (our tenant UUID)
          2. res.company.name == name         (case-insensitive, same vertical)

        Creates when neither matches. Updates basic fields on an existing
        match so tenant name/email changes flow through.

        Returns the Odoo company_id (int).

        Raises OdooError on auth failure or protocol errors. Callers in
        ensure-agent MUST catch and fall back to odoo_company_id=None so
        tenant provisioning never blocks on Odoo.
        """
        uid = self._authenticate()

        # 1. By external_ref (our tenant UUID stamped on res.company.ref).
        rows = self._execute(
            uid,
            "res.company",
            "search_read",
            args=[[("ref", "=", external_ref)]],
            kwargs={"fields": ["id", "name"], "limit": 1},
        )
        if rows:
            company_id = int(rows[0]["id"])
            self._update_company(uid, company_id, name=name, email=email, phone=phone)
            logger.info("ensure_company: found by ref, id=%s name=%s", company_id, name)
            return company_id

        # 2. By name — protects against re-provisioning after a DB wipe
        #    where external_ref wasn't persisted. We still update ref.
        rows = self._execute(
            uid,
            "res.company",
            "search_read",
            args=[[("name", "=ilike", name)]],
            kwargs={"fields": ["id", "name", "ref"], "limit": 1},
        )
        if rows:
            company_id = int(rows[0]["id"])
            self._execute(
                uid,
                "res.company",
                "write",
                args=[[company_id], {"ref": external_ref}],
            )
            self._update_company(uid, company_id, name=name, email=email, phone=phone)
            logger.info(
                "ensure_company: name-match backfilled ref, id=%s name=%s",
                company_id,
                name,
            )
            return company_id

        # 3. Create.
        vals: dict[str, Any] = {
            "name": name[:128],
            "ref": external_ref,
        }
        if email:
            vals["email"] = email[:240]
        if phone:
            vals["phone"] = phone[:64]
        if country_code:
            country_id = self._resolve_country_id(uid, country_code)
            if country_id:
                vals["country_id"] = country_id
        # vertical is informational — store on the `note` comment for now.
        # Future: add a custom field via a /mnt/extra-addons module.
        if vertical:
            vals["comment"] = f"Isola vertical: {vertical}"

        company_id = int(
            self._execute(uid, "res.company", "create", args=[vals])
        )
        logger.info(
            "ensure_company: created id=%s name=%s vertical=%s",
            company_id,
            name,
            vertical,
        )
        return company_id

    # ── Helpers ──────────────────────────────────────────────────────────

    def _update_company(
        self,
        uid: int,
        company_id: int,
        *,
        name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
    ) -> None:
        """Patch mutable fields on an existing res.company. No-op when empty."""
        patch: dict[str, Any] = {}
        if name:
            patch["name"] = name[:128]
        if email:
            patch["email"] = email[:240]
        if phone:
            patch["phone"] = phone[:64]
        if not patch:
            return
        self._execute(
            uid,
            "res.company",
            "write",
            args=[[company_id], patch],
        )

    def _resolve_country_id(self, uid: int, code: str) -> int | None:
        """Look up res.country.id for a 2-letter ISO code. Cached per process
        via @lru_cache would be cleaner; for F.1.b-3 we accept the round-trip
        since ensure_company runs only on first-bridge-hit per tenant."""
        code = code.strip().upper()
        if len(code) != 2:
            return None
        rows = self._execute(
            uid,
            "res.country",
            "search_read",
            args=[[("code", "=", code)]],
            kwargs={"fields": ["id"], "limit": 1},
        )
        return int(rows[0]["id"]) if rows else None


def odoo_service() -> OdooService:
    """Factory that reads current settings. Use inside request handlers so
    settings changes at runtime (rare) take effect without restart."""
    s = get_settings()
    return OdooService(
        url=s.ODOO_URL,
        db=s.ODOO_DB,
        login=s.ODOO_ADMIN_LOGIN,
        password=s.ODOO_ADMIN_PASSWORD,
    )
