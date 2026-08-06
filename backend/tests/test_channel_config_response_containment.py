"""Channel configuration responses must never carry a credential.

Covers:
  * defect-isola-runtime-channelconfigout-serialises-app-secret-and-extra-config-2026-08-05
  * defect-isola-runtime-category-config-returns-decrypted-agent-credentials-2026-08-05

Every secret used here is generated at run time, so a leak is detectable by a
plain substring search and no real credential is ever committed to the repo.
"""

import inspect
import io
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from loguru import logger

from app.api import discord_bot as discord_api
from app.api import slack as slack_api
from app.api import teams as teams_api
from app.api import tools as tools_api
from app.api import whatsapp as whatsapp_api
from app.api.admin_crossstore import _channel_view
from app.models.channel_config import ChannelConfig
from app.schemas.schemas import ChannelConfigOut

# ─── Synthetic credential material ──────────────────────────────────────────

SECRET_FIELD_NAMES = (
    "access_token",
    "app_secret",
    "verify_token",
    "webhook_verify_token",
    "api_key",
    "refresh_token",
    "client_secret",
    "hmac_secret",
    "encrypt_key",
)

# Names that must never appear in a response carrying a real value. A boolean
# is allowed: "<name>_configured"-style state is the whole point of the fix,
# and the category-config maps are keyed by the secret's own name.
FORBIDDEN_VALUE_FIELDS = frozenset(SECRET_FIELD_NAMES) | {
    "extra_config",
    "verification_token",
    "signing_secret",
    "bot_token",
}

# The complete, reviewed field list of the safe channel contract. A new field
# cannot appear without a deliberate change here, which forces review.
EXPECTED_CHANNEL_OUT_FIELDS = {
    "id",
    "agent_id",
    "channel_type",
    "app_id",
    "is_configured",
    "is_connected",
    "last_tested_at",
    "created_at",
    "app_secret_configured",
    "encrypt_key_configured",
    "credentials_configured",
    "config_summary",
}

CHANNEL_FAMILIES = ("whatsapp", "slack", "discord", "microsoft_teams")


def synthetic_secrets() -> dict[str, str]:
    """Fresh, unique, obviously-fake credential values."""
    return {name: f"SYNTH-{name}-{uuid.uuid4().hex}" for name in SECRET_FIELD_NAMES}


def render(payload) -> str:
    """Exactly what FastAPI would put on the wire."""
    return json.dumps(jsonable_encoder(payload), default=str)


def walk(node, path="$"):
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key, value
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")


def assert_no_credentials(payload, secrets: dict[str, str], *streams: str) -> str:
    """Assert no secret value and no credential-bearing field survives.

    Checks the parsed JSON, the raw response text, and any extra streams
    (stdout, stderr, captured logs, exception text) handed in by the caller.
    """
    text = render(payload)
    haystacks = [("response", text)] + [(f"stream[{i}]", s) for i, s in enumerate(streams)]
    for label, haystack in haystacks:
        for name, value in secrets.items():
            assert value not in haystack, f"{name} leaked into {label}"
        assert "****" not in haystack, f"a mask leaked into {label} and could be written back"

    for path, key, value in walk(json.loads(text)):
        if key in FORBIDDEN_VALUE_FIELDS:
            assert isinstance(value, bool), (
                f"{path}.{key} carries a value ({type(value).__name__}); "
                "only configured-state booleans may use a credential's name"
            )
    return text


def make_config(channel_type: str, **overrides) -> ChannelConfig:
    values = {
        "id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "channel_type": channel_type,
        "app_id": "public-app-id-123",
        "app_secret": None,
        "encrypt_key": None,
        "verification_token": None,
        "is_configured": True,
        "is_connected": False,
        "last_tested_at": None,
        "extra_config": {},
        "created_at": datetime.now(UTC),
    }
    values.update(overrides)
    return ChannelConfig(**values)


def fully_loaded(channel_type: str, secrets: dict[str, str]) -> ChannelConfig:
    """A row whose every credential slot — column and nested — is populated."""
    extra = dict(secrets)
    extra.update(
        {
            "phone_number_id": "15550001111",
            "waba_id": "272252189309178",
            "connection_mode": "webhook",
            "tenant_id": "contoso.onmicrosoft.com",
            "use_managed_identity": False,
            "display_name": "Support line",
            # A credential added later under a name nobody allowlisted.
            "brand_new_credential": secrets["app_secret"],
        }
    )
    return make_config(
        channel_type,
        app_secret=secrets["app_secret"],
        encrypt_key=secrets["encrypt_key"],
        verification_token=secrets["verify_token"],
        extra_config=extra,
    )


# ─── Fakes ──────────────────────────────────────────────────────────────────


class DummyResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value if isinstance(self._value, list) else []

    def __iter__(self):
        return iter(self._value if isinstance(self._value, list) else [])


class FakeDB:
    def __init__(self, *results):
        self.results = list(results)
        self.added = []
        self.committed = False

    async def execute(self, _statement):
        return self.results.pop(0) if self.results else DummyResult()

    def add(self, obj):
        # Postgres assigns the primary key and created_at on INSERT (SQLAlchemy
        # fetches them via RETURNING at flush). Mirror that here.
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(UTC)
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def flush(self):
        return None

    async def refresh(self, _obj):
        return None

    async def delete(self, _obj):
        return None


@pytest.fixture
def captured_logs():
    """Capture loguru output so we can prove nothing secret was logged."""
    buffer = io.StringIO()
    sink_id = logger.add(buffer, level="DEBUG")
    try:
        yield buffer
    finally:
        logger.remove(sink_id)


@pytest.fixture
def allow_access(monkeypatch):
    """Grant creator-level access on every channel module under test."""
    agent = SimpleNamespace(id=uuid.uuid4(), name="agent")

    async def _check(_db, _user, _agent_id):
        return agent, None

    for module in (whatsapp_api, slack_api, teams_api, discord_api):
        monkeypatch.setattr(module, "check_agent_access", _check)
        monkeypatch.setattr(module, "is_agent_creator", lambda _u, _a: True)
    return agent


# ─── Schema-level guard ─────────────────────────────────────────────────────


def test_channel_config_out_field_list_is_allowlisted():
    """A new field cannot reach the wire without editing this expectation."""
    assert set(ChannelConfigOut.model_fields) == EXPECTED_CHANNEL_OUT_FIELDS


def test_channel_config_out_declares_no_credential_field():
    assert not (set(ChannelConfigOut.model_fields) & FORBIDDEN_VALUE_FIELDS)


def test_channel_config_out_refuses_to_serialise_an_orm_row():
    """from_attributes is off, so a new DB column can never auto-serialise."""
    assert ChannelConfigOut.model_config.get("from_attributes") is False
    with pytest.raises(Exception):
        ChannelConfigOut.model_validate(make_config("slack", app_secret="x"))


def test_no_api_module_validates_a_channel_orm_row():
    """Static guard: the unsafe construction must not come back."""
    for path in sorted(Path("app/api").glob("*.py")):
        assert "ChannelConfigOut.model_validate(" not in path.read_text(encoding="utf-8"), path


def test_category_config_neither_masks_nor_decrypts():
    """Static guard on the endpoint under change.

    Scoped to get_category_config: the sibling tool-config endpoints read a
    different table (Tool/AgentTool) and are outside this packet's boundary.
    """
    source = inspect.getsource(tools_api.get_category_config)
    assert "****" not in source, "a reusable mask was reintroduced"
    assert "masked_global" not in source
    assert "_decrypt_sensitive_fields" not in source, "endpoint must never decrypt"


# ─── Projection: every channel family ───────────────────────────────────────


@pytest.mark.parametrize("channel_type", CHANNEL_FAMILIES)
def test_projection_drops_every_credential(channel_type):
    secrets = synthetic_secrets()
    out = ChannelConfigOut.from_channel_config(fully_loaded(channel_type, secrets))
    assert_no_credentials(out, secrets)
    assert_no_credentials(repr(out), secrets)


@pytest.mark.parametrize("channel_type", CHANNEL_FAMILIES)
def test_projection_keeps_non_secret_fields(channel_type):
    secrets = synthetic_secrets()
    config = fully_loaded(channel_type, secrets)
    out = ChannelConfigOut.from_channel_config(config)

    assert out.id == config.id
    assert out.agent_id == config.agent_id
    assert out.channel_type == channel_type
    assert out.app_id == "public-app-id-123"
    assert out.is_configured is True
    assert out.config_summary.get("display_name") == "Support line"


@pytest.mark.parametrize(
    ("channel_type", "expected"),
    [
        ("whatsapp", {"phone_number_id", "waba_id", "display_name"}),
        ("slack", {"display_name"}),
        ("discord", {"connection_mode", "display_name"}),
        ("microsoft_teams", {"tenant_id", "use_managed_identity", "display_name"}),
    ],
)
def test_config_summary_is_a_strict_allowlist(channel_type, expected):
    secrets = synthetic_secrets()
    out = ChannelConfigOut.from_channel_config(fully_loaded(channel_type, secrets))
    assert set(out.config_summary) == expected


@pytest.mark.parametrize("channel_type", CHANNEL_FAMILIES)
def test_unknown_new_extra_config_key_is_dropped(channel_type):
    """A credential added later under an unreviewed name must not surface."""
    secrets = synthetic_secrets()
    out = ChannelConfigOut.from_channel_config(fully_loaded(channel_type, secrets))
    assert "brand_new_credential" not in out.config_summary


def test_configured_state_is_accurate_when_credentials_are_present():
    secrets = synthetic_secrets()

    slack = ChannelConfigOut.from_channel_config(
        make_config("slack", app_secret=secrets["app_secret"], encrypt_key=secrets["encrypt_key"])
    )
    assert slack.app_secret_configured is True
    assert slack.encrypt_key_configured is True
    assert slack.credentials_configured is True

    # WhatsApp keeps everything in extra_config, so column-based flags stay
    # false while the channel is genuinely configured.
    whatsapp = ChannelConfigOut.from_channel_config(
        make_config(
            "whatsapp",
            extra_config={
                "access_token": secrets["access_token"],
                "verify_token": secrets["verify_token"],
                "app_secret": secrets["app_secret"],
            },
        )
    )
    assert whatsapp.app_secret_configured is False
    assert whatsapp.credentials_configured is True


@pytest.mark.parametrize("channel_type", CHANNEL_FAMILIES)
def test_configured_state_is_accurate_when_credentials_are_absent(channel_type):
    out = ChannelConfigOut.from_channel_config(make_config(channel_type, is_configured=False))
    assert out.app_secret_configured is False
    assert out.encrypt_key_configured is False
    assert out.credentials_configured is False
    assert out.config_summary == {}


def test_blank_credentials_do_not_count_as_configured():
    out = ChannelConfigOut.from_channel_config(make_config("slack", app_secret="   ", encrypt_key=""))
    assert out.app_secret_configured is False
    assert out.encrypt_key_configured is False


# ─── Live endpoint responses: detail, create, update ────────────────────────


async def test_whatsapp_detail_response_is_clean(allow_access, captured_logs, capsys):
    secrets = synthetic_secrets()
    db = FakeDB(DummyResult(fully_loaded("whatsapp", secrets)))
    out = await whatsapp_api.get_whatsapp_channel(uuid.uuid4(), current_user=None, db=db)
    captured = capsys.readouterr()
    assert_no_credentials(out, secrets, captured.out, captured.err, captured_logs.getvalue())


async def test_whatsapp_create_response_does_not_echo_the_request(allow_access, captured_logs, capsys):
    """A secret the caller just supplied must not come back in the response."""
    secrets = synthetic_secrets()
    db = FakeDB(DummyResult(None))
    body = {
        "phone_number_id": "15550001111",
        "waba_id": "272252189309178",
        "access_token": secrets["access_token"],
        "verify_token": secrets["verify_token"],
        "app_secret": secrets["app_secret"],
    }
    out = await whatsapp_api.configure_whatsapp_channel(uuid.uuid4(), body, current_user=None, db=db)
    captured = capsys.readouterr()
    assert_no_credentials(out, secrets, captured.out, captured.err, captured_logs.getvalue())
    assert out.config_summary == {"phone_number_id": "15550001111", "waba_id": "272252189309178"}


async def test_whatsapp_update_response_is_clean(allow_access, captured_logs, capsys):
    """The update branch (existing row) must be as safe as the create branch."""
    secrets = synthetic_secrets()
    db = FakeDB(DummyResult(fully_loaded("whatsapp", secrets)))
    body = {
        "phone_number_id": "15550002222",
        "waba_id": "272252189309178",
        "access_token": secrets["access_token"],
        "verify_token": secrets["verify_token"],
        "app_secret": secrets["app_secret"],
    }
    out = await whatsapp_api.configure_whatsapp_channel(uuid.uuid4(), body, current_user=None, db=db)
    captured = capsys.readouterr()
    assert_no_credentials(out, secrets, captured.out, captured.err, captured_logs.getvalue())


async def test_slack_detail_and_create_are_clean(allow_access, captured_logs, capsys):
    secrets = synthetic_secrets()

    db = FakeDB(DummyResult(fully_loaded("slack", secrets)))
    detail = await slack_api.get_slack_channel(uuid.uuid4(), current_user=None, db=db)

    db = FakeDB(DummyResult(None))
    created = await slack_api.configure_slack_channel(
        uuid.uuid4(),
        {"bot_token": secrets["app_secret"], "signing_secret": secrets["hmac_secret"]},
        current_user=None,
        db=db,
    )
    captured = capsys.readouterr()
    for payload in (detail, created):
        assert_no_credentials(payload, secrets, captured.out, captured.err, captured_logs.getvalue())
    assert created.app_secret_configured is True


async def test_teams_detail_and_create_are_clean(allow_access, captured_logs, capsys):
    secrets = synthetic_secrets()

    db = FakeDB(DummyResult(fully_loaded("microsoft_teams", secrets)))
    detail = await teams_api.get_teams_channel(uuid.uuid4(), current_user=None, db=db)

    db = FakeDB(DummyResult(None))
    created = await teams_api.configure_teams_channel(
        uuid.uuid4(),
        {
            "app_id": "public-app-id-123",
            "app_secret": secrets["client_secret"],
            "tenant_id": "contoso.onmicrosoft.com",
        },
        current_user=None,
        db=db,
    )
    captured = capsys.readouterr()
    for payload in (detail, created):
        assert_no_credentials(payload, secrets, captured.out, captured.err, captured_logs.getvalue())
    # Non-secret Azure tenant identifier stays available to the UI.
    assert created.config_summary.get("tenant_id") == "contoso.onmicrosoft.com"


async def test_discord_detail_and_create_are_clean(allow_access, captured_logs, capsys, monkeypatch):
    secrets = synthetic_secrets()

    async def _no_registration(_app_id, _token):
        return {"status": "skipped"}

    monkeypatch.setattr(discord_api, "_register_slash_commands", _no_registration)

    db = FakeDB(DummyResult(fully_loaded("discord", secrets)))
    detail = await discord_api.get_discord_channel(uuid.uuid4(), current_user=None, db=db)

    db = FakeDB(DummyResult(None))
    created = await discord_api.configure_discord_channel(
        uuid.uuid4(),
        {
            "connection_mode": "webhook",
            "application_id": "public-app-id-123",
            "bot_token": secrets["app_secret"],
            "public_key": secrets["encrypt_key"],
        },
        current_user=None,
        db=db,
    )
    captured = capsys.readouterr()
    for payload in (detail, created):
        assert_no_credentials(payload, secrets, captured.out, captured.err, captured_logs.getvalue())


async def test_not_found_error_text_carries_no_credential(allow_access):
    secrets = synthetic_secrets()
    db = FakeDB(DummyResult(None))
    with pytest.raises(HTTPException) as excinfo:
        await slack_api.get_slack_channel(uuid.uuid4(), current_user=None, db=db)
    assert_no_credentials(str(excinfo.value), secrets)


# ─── List response (admin surface) ──────────────────────────────────────────


def test_admin_channel_list_projection_is_clean():
    """The list route was already safe; prove it stays that way."""
    secrets = synthetic_secrets()
    rows = [fully_loaded(family, secrets) for family in CHANNEL_FAMILIES]
    for row in rows:
        row.updated_at = datetime.now(UTC)
    assert_no_credentials({"channels": [_channel_view(row) for row in rows]}, secrets)


# ─── Tool category config (same channel_configs table) ──────────────────────


CATEGORY_SCHEMA = {
    "fields": [
        {"key": "api_key", "type": "password"},
        {"key": "os_type", "type": "select"},
        {"key": "base_url", "type": "text"},
    ]
}


def category_db(secrets, *, with_agent_row=True, global_config=None):
    tool = SimpleNamespace(
        config=global_config
        if global_config is not None
        else {"api_key": secrets["api_key"], "os_type": "linux"},
        config_schema=CATEGORY_SCHEMA,
    )
    agent_row = None
    if with_agent_row:
        agent_row = make_config(
            "browser",
            app_secret=secrets["app_secret"],
            extra_config={
                "base_url": "https://tools.example.com",
                "refresh_token": secrets["refresh_token"],
                "client_secret": secrets["client_secret"],
                "hmac_secret": secrets["hmac_secret"],
                "webhook_verify_token": secrets["webhook_verify_token"],
                "brand_new_credential": secrets["access_token"],
            },
        )
    return FakeDB(DummyResult([tool]), DummyResult(agent_row))


@pytest.fixture
def allow_category_access(monkeypatch):
    async def _check(_db, _user, _agent_id):
        return SimpleNamespace(id=uuid.uuid4()), None

    monkeypatch.setattr("app.core.permissions.check_agent_access", _check)
    monkeypatch.setattr("app.core.permissions.is_agent_creator", lambda _u, _a: True)


async def test_category_config_returns_no_credentials(allow_category_access, captured_logs, capsys):
    secrets = synthetic_secrets()
    out = await tools_api.get_category_config(
        uuid.uuid4(), "browser", current_user=None, db=category_db(secrets)
    )
    captured = capsys.readouterr()
    assert_no_credentials(out, secrets, captured.out, captured.err, captured_logs.getvalue())


async def test_category_config_keeps_non_secret_values(allow_category_access):
    secrets = synthetic_secrets()
    out = await tools_api.get_category_config(
        uuid.uuid4(), "browser", current_user=None, db=category_db(secrets)
    )
    assert out["config"] == {"os_type": "linux", "base_url": "https://tools.example.com"}
    assert out["global_config"] == {"os_type": "linux"}
    assert out["agent_config"] == {"base_url": "https://tools.example.com"}
    assert out["is_configured"] is True


async def test_category_config_configured_state_is_accurate(allow_category_access):
    secrets = synthetic_secrets()
    out = await tools_api.get_category_config(
        uuid.uuid4(), "browser", current_user=None, db=category_db(secrets)
    )
    assert out["global_config_configured"]["api_key"] is True
    assert out["agent_config_configured"]["api_key"] is True
    assert out["global_config_configured"]["password"] is False
    assert out["credentials_configured"] is True


async def test_category_config_reports_absent_credentials(allow_category_access):
    secrets = synthetic_secrets()
    db = category_db(secrets, with_agent_row=False, global_config={"os_type": "linux"})
    out = await tools_api.get_category_config(uuid.uuid4(), "browser", current_user=None, db=db)
    assert out["credentials_configured"] is False
    assert out["global_config_configured"]["api_key"] is False
    assert out["agent_config"] == {}


async def test_category_config_drops_nested_and_unknown_secrets(allow_category_access):
    """Nested extra_config credentials and unreviewed new keys never surface."""
    secrets = synthetic_secrets()
    out = await tools_api.get_category_config(
        uuid.uuid4(), "browser", current_user=None, db=category_db(secrets)
    )
    text = render(out)
    for key in ("refresh_token", "client_secret", "hmac_secret", "webhook_verify_token", "brand_new_credential"):
        assert key not in out["agent_config"]
        assert key not in out["config"]
    for value in secrets.values():
        assert value not in text


# ─── Write path: a blank secret must not clear the stored one ───────────────


async def test_saving_without_a_replacement_secret_preserves_the_credential(allow_category_access):
    """The UI omits an untouched secret input; storage must keep its value."""
    secrets = synthetic_secrets()
    existing = make_config(
        "browser",
        app_secret=secrets["app_secret"],
        extra_config={"base_url": "https://old.example.com", "refresh_token": secrets["refresh_token"]},
    )
    db = FakeDB(DummyResult(existing))

    await tools_api.update_category_config(
        uuid.uuid4(),
        "browser",
        tools_api.CategoryConfigUpdate(config={"os_type": "linux", "base_url": "https://new.example.com"}),
        current_user=None,
        db=db,
    )

    assert existing.app_secret == secrets["app_secret"], "an omitted secret was cleared"
    assert existing.extra_config["refresh_token"] == secrets["refresh_token"]
    assert existing.extra_config["base_url"] == "https://new.example.com"


async def test_entering_a_replacement_secret_writes_it(allow_category_access):
    secrets = synthetic_secrets()
    replacement = f"SYNTH-replacement-{uuid.uuid4().hex}"
    existing = make_config("browser", app_secret=secrets["app_secret"], extra_config={})
    db = FakeDB(DummyResult(existing))

    await tools_api.update_category_config(
        uuid.uuid4(),
        "browser",
        tools_api.CategoryConfigUpdate(config={"api_key": replacement}),
        current_user=None,
        db=db,
    )

    assert existing.app_secret != secrets["app_secret"], "replacement was not written"
    from app.config import get_settings
    from app.core.security import decrypt_data

    assert decrypt_data(existing.app_secret, get_settings().SECRET_KEY) == replacement


async def test_a_mask_is_never_accepted_as_a_credential(allow_category_access):
    """No response produces a mask, so a mask can never be written back."""
    secrets = synthetic_secrets()
    out = await tools_api.get_category_config(
        uuid.uuid4(), "browser", current_user=None, db=category_db(secrets)
    )
    assert "****" not in render(out)
