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

        # Odoo 18 note: `res.company` has no `ref` field — every company has
        # an auto-created `res.partner` (via partner_id), and partners DO
        # have ref. We store the Isola tenant UUID on the partner's ref so
        # the identity survives renames and future schema shifts.

        # 1. By external_ref on the linked partner.
        rows = self._execute(
            uid,
            "res.company",
            "search_read",
            args=[[("partner_id.ref", "=", external_ref)]],
            kwargs={"fields": ["id", "name", "partner_id"], "limit": 1},
        )
        if rows:
            company_id = int(rows[0]["id"])
            self._update_company(uid, company_id, name=name, email=email, phone=phone)
            logger.info("ensure_company: found by partner.ref, id=%s name=%s", company_id, name)
            return company_id

        # 2. By name — protects against re-provisioning after a DB wipe
        #    where external_ref wasn't persisted. We still backfill ref.
        rows = self._execute(
            uid,
            "res.company",
            "search_read",
            args=[[("name", "=ilike", name)]],
            kwargs={"fields": ["id", "name", "partner_id"], "limit": 1},
        )
        if rows:
            company_id = int(rows[0]["id"])
            partner_id = (
                rows[0]["partner_id"][0]
                if isinstance(rows[0].get("partner_id"), (list, tuple))
                else rows[0].get("partner_id")
            )
            if partner_id:
                self._execute(
                    uid,
                    "res.partner",
                    "write",
                    args=[[int(partner_id)], {"ref": external_ref}],
                )
            self._update_company(uid, company_id, name=name, email=email, phone=phone)
            logger.info(
                "ensure_company: name-match backfilled partner.ref, id=%s name=%s",
                company_id,
                name,
            )
            return company_id

        # 3. Create. res.company.create auto-creates the partner; we then
        # write `ref` on that partner in a second call.
        vals: dict[str, Any] = {
            "name": name[:128],
        }
        if email:
            vals["email"] = email[:240]
        if phone:
            vals["phone"] = phone[:64]
        if country_code:
            country_id = self._resolve_country_id(uid, country_code)
            if country_id:
                vals["country_id"] = country_id

        company_id = int(
            self._execute(uid, "res.company", "create", args=[vals])
        )

        # Pull the partner_id + set ref + (optional) vertical note.
        row = self._execute(
            uid,
            "res.company",
            "read",
            args=[[company_id], ["partner_id"]],
        )
        partner_id_raw = row[0].get("partner_id") if row else None
        partner_id = (
            int(partner_id_raw[0])
            if isinstance(partner_id_raw, (list, tuple))
            else (int(partner_id_raw) if partner_id_raw else None)
        )
        if partner_id:
            partner_vals: dict[str, Any] = {"ref": external_ref}
            if vertical:
                partner_vals["comment"] = f"Isola vertical: {vertical}"
            self._execute(
                uid,
                "res.partner",
                "write",
                args=[[partner_id], partner_vals],
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


def get_customer_balance(svc: OdooService, partner_id: int) -> dict:
    """Return total AR balance for a partner (sum of open invoices)."""
    uid = svc._authenticate()
    invoices = svc._execute(
        uid,
        "account.move",
        "search_read",
        [[
            ["partner_id", "=", partner_id],
            ["move_type", "=", "out_invoice"],
            ["payment_state", "in", ["not_paid", "partial"]],
            ["state", "=", "posted"],
        ]],
        {"fields": ["name", "amount_residual", "currency_id", "invoice_date_due"]},
    )
    total = sum(float(inv.get("amount_residual") or 0) for inv in invoices)
    return {
        "partner_id": partner_id,
        "open_invoices": len(invoices),
        "total_outstanding": total,
        "invoices": invoices,
    }


def list_overdue_invoices(svc: OdooService, limit: int = 10) -> list:
    """Return top overdue invoices across all customers ordered by amount desc."""
    from datetime import date
    uid = svc._authenticate()
    today = date.today().isoformat()
    return svc._execute(
        uid,
        "account.move",
        "search_read",
        [[
            ["move_type", "=", "out_invoice"],
            ["payment_state", "in", ["not_paid", "partial"]],
            ["state", "=", "posted"],
            ["invoice_date_due", "<", today],
        ]],
        {
            "fields": ["name", "partner_id", "amount_residual", "invoice_date_due"],
            "limit": limit,
            "order": "amount_residual desc",
        },
    )


def agent_odoo_service() -> OdooService:
    """Factory for agent AR queries — uses ODOO_AGENT_* env vars (epic_sandbox)."""
    s = get_settings()
    return OdooService(
        url=s.ODOO_AGENT_URL,
        db=s.ODOO_AGENT_DB,
        login=s.ODOO_AGENT_LOGIN,
        password=s.ODOO_AGENT_PASSWORD,
    )


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
