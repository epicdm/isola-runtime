"""Unit proofs for the tenant-isolation guards added to app/api/tools.py.

Covers def-clawith-tools-cross-tenant-write-2026-07-29. These exercise the guard
logic directly (no HTTP, no DB) so they also cover update_mcp_server, whose PUT
route is currently shadowed by PUT /{tool_id} and therefore not wire-reachable —
the guard still runs first if the shadow is ever removed.
"""
import uuid
import types
import pytest
from fastapi import HTTPException

from app.api import tools as T

TA = uuid.uuid4()  # tenant A
TB = uuid.uuid4()  # tenant B


def _user(role, tenant_id, is_platform_admin=False):
    ident = types.SimpleNamespace(is_platform_admin=is_platform_admin)
    return types.SimpleNamespace(role=role, tenant_id=tenant_id, identity=ident, id=uuid.uuid4())


def test_is_platform_admin_user():
    assert T._is_platform_admin_user(_user("platform_admin", TA)) is True
    assert T._is_platform_admin_user(_user("member", TA, is_platform_admin=True)) is True
    assert T._is_platform_admin_user(_user("org_admin", TA)) is False
    assert T._is_platform_admin_user(_user("member", TA)) is False


def test_resolve_target_tenant_defaults_to_own():
    assert T._resolve_target_tenant_id(_user("member", TA), None) == TA


def test_resolve_target_tenant_same_tenant_ok():
    assert T._resolve_target_tenant_id(_user("member", TA), str(TA)) == TA


def test_resolve_target_tenant_foreign_nonadmin_denied():
    with pytest.raises(HTTPException) as e:
        T._resolve_target_tenant_id(_user("org_admin", TB), str(TA))
    assert e.value.status_code == 403


def test_resolve_target_tenant_foreign_platform_admin_allowed():
    assert T._resolve_target_tenant_id(_user("platform_admin", TB), str(TA)) == TA


@pytest.mark.asyncio
async def test_update_mcp_server_rejects_nonadmin_role():
    data = types.SimpleNamespace(server_name="s", server_url="u", api_key="k", tenant_id=None)
    with pytest.raises(HTTPException) as e:
        await T.update_mcp_server(data=data, current_user=_user("member", TA), db=None)
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_update_mcp_server_rejects_org_admin_foreign_tenant():
    # org_admin passes the role gate but must not target another tenant (no DB touched)
    data = types.SimpleNamespace(server_name="s", server_url="u", api_key="k", tenant_id=str(TA))
    with pytest.raises(HTTPException) as e:
        await T.update_mcp_server(data=data, current_user=_user("org_admin", TB), db=None)
    assert e.value.status_code == 403
