"""Tool-config responses must never carry a credential.

Covers defect-isola-runtime-tool-config-mask-writeback-and-partial-secret-disclosure-2026-08-06:

  * GET /tools                                        (list_tools)
  * GET /tools/agents/{agent_id}/tool-config/{tool_id} (get_agent_tool_config)
  * GET /tools/agents/{agent_id}/with-config           (get_agent_tools_with_config)

These read ``Tool.config`` / ``AgentTool.config`` — different tables from the
``channel_configs`` rows already covered by
tests/test_channel_config_response_containment.py.

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
from fastapi.encoders import jsonable_encoder
from loguru import logger

from app.api import tools as tools_api
from app.services import agent_tools as agent_tools_svc

# ─── Synthetic credential material ──────────────────────────────────────────

SECRET_FIELD_NAMES = (
    "api_key",
    "password",
    "secret",
    "private_key",
    "auth_code",
    "access_token",
    "refresh_token",
    "client_secret",
    "webhook_secret",
    "hmac_secret",
    "smithery_api_key",
    "atlassian_api_key",
    "bot_token",
)

# Names that may never carry a value in a response. A boolean is allowed: the
# configured-state maps are keyed by the secret's own name, which is the whole
# point of the fix.
FORBIDDEN_VALUE_FIELDS = frozenset(SECRET_FIELD_NAMES) | {
    "authorization",
    "credentials",
    "modelscope_api_token",
    "brand_new_credential",
}

# The tool schema used across these tests: one declared secret, two declared
# non-secret fields.
TOOL_SCHEMA = {
    "fields": [
        {"key": "api_key", "label": "API Key", "type": "password", "default": "SEEDED-DEFAULT-SECRET"},
        {"key": "os_type", "label": "OS", "type": "select", "default": "windows"},
        {"key": "base_url", "label": "Base URL", "type": "text"},
    ]
}


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
    """No secret value, no mask, no fragment, no credential-bearing field.

    Checks the parsed JSON, the raw response text, and any extra streams
    (stdout, stderr, captured logs, exception text) handed in by the caller.
    """
    text = render(payload)
    haystacks = [("response", text)] + [(f"stream[{i}]", s) for i, s in enumerate(streams)]
    for label, haystack in haystacks:
        for name, value in secrets.items():
            assert value not in haystack, f"{name} leaked into {label}"
            # A last-four fragment is a partial disclosure and a write-back hazard.
            assert value[-4:] not in haystack, f"a last-four fragment of {name} leaked into {label}"
        assert "****" not in haystack, f"a mask leaked into {label} and could be written back"

    for path, key, value in walk(json.loads(text)):
        if key in FORBIDDEN_VALUE_FIELDS:
            assert isinstance(value, bool), (
                f"{path}.{key} carries a value ({type(value).__name__}); "
                "only configured-state booleans may use a credential's name"
            )
    return text


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

    def first(self):
        return self._value

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


def make_tool(config=None, *, schema=TOOL_SCHEMA, name="agentbay_browser_navigate", source="builtin"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        display_name="Tool",
        description="",
        type="builtin",
        category="agentbay",
        icon="🔧",
        parameters_schema={},
        mcp_server_url=None,
        mcp_server_name=None,
        mcp_tool_name=None,
        enabled=True,
        is_default=True,
        source=source,
        tenant_id=None,
        config=config if config is not None else {},
        config_schema=schema,
        created_at=datetime.now(UTC),
    )


def make_agent_tool(tool, config=None, *, enabled=True):
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        tool_id=tool.id,
        enabled=enabled,
        config=config if config is not None else {},
        source="system",
        installed_by_agent_id=None,
        created_at=datetime.now(UTC),
    )


def loaded_global(secrets) -> dict:
    """A company-level config carrying every kind of credential we defend against."""
    return {
        "api_key": secrets["api_key"],
        "password": secrets["password"],
        "os_type": "linux",
        # Undeclared, credential-named — must be dropped without any schema help.
        "smithery_api_key": secrets["smithery_api_key"],
        "atlassian_api_key": secrets["atlassian_api_key"],
        "bot_token": secrets["bot_token"],
        # Undeclared and not credential-named — dropped because it is unknown.
        "undeclared_note": "harmless-but-undeclared",
        # Nested container: the schema declares no nested field, so nothing
        # inside it is allowlisted.
        "nested": {"client_secret": secrets["client_secret"], "keep": "no"},
    }


def loaded_agent(secrets) -> dict:
    return {
        "api_key": secrets["access_token"],
        "base_url": "https://tools.example.com",
        "refresh_token": secrets["refresh_token"],
        "webhook_secret": secrets["webhook_secret"],
        "hmac_secret": secrets["hmac_secret"],
        "brand_new_credential": secrets["secret"],
    }


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
def admin_user():
    return SimpleNamespace(id=uuid.uuid4(), role="platform_admin", tenant_id=None)


@pytest.fixture(autouse=True)
def _clear_runtime_cache():
    agent_tools_svc._tool_config_cache.clear()
    yield
    agent_tools_svc._tool_config_cache.clear()


# ═══════════════════════════════════════════════════════════════════════════
# A. RESPONSE SAFETY
# ═══════════════════════════════════════════════════════════════════════════


async def test_tool_config_detail_returns_no_credentials(admin_user, captured_logs, capsys):
    secrets = synthetic_secrets()
    tool = make_tool(loaded_global(secrets))
    at = make_agent_tool(tool, loaded_agent(secrets))

    out = await tools_api.get_agent_tool_config(
        at.agent_id, tool.id, current_user=admin_user,
        db=FakeDB(DummyResult(tool), DummyResult(at)),
    )

    captured = capsys.readouterr()
    assert_no_credentials(out, secrets, captured.out, captured.err, captured_logs.getvalue())


async def test_with_config_returns_no_credentials(admin_user, captured_logs, capsys):
    secrets = synthetic_secrets()
    tool = make_tool(loaded_global(secrets))
    at = make_agent_tool(tool, loaded_agent(secrets))

    out = await tools_api.get_agent_tools_with_config(
        at.agent_id, current_user=admin_user,
        db=FakeDB(DummyResult([tool]), DummyResult([at])),
    )

    captured = capsys.readouterr()
    assert_no_credentials(out, secrets, captured.out, captured.err, captured_logs.getvalue())


async def test_list_tools_returns_no_credentials(admin_user, captured_logs, capsys):
    """The broadest path: it previously returned every tool's config in clear."""
    secrets = synthetic_secrets()
    tool = make_tool(loaded_global(secrets))

    out = await tools_api.list_tools(
        tenant_id=None, current_user=admin_user, db=FakeDB(DummyResult([tool]))
    )

    captured = capsys.readouterr()
    assert_no_credentials(out, secrets, captured.out, captured.err, captured_logs.getvalue())


async def test_encrypted_values_are_absent_too(admin_user):
    """Not decrypted is not enough — the ciphertext must not ship either."""
    secrets = synthetic_secrets()
    stored = tools_api._encrypt_sensitive_fields(
        {"api_key": secrets["api_key"], "os_type": "linux"}, TOOL_SCHEMA
    )
    ciphertext = stored["api_key"]
    assert ciphertext != secrets["api_key"], "fixture did not actually encrypt"

    tool = make_tool(stored)
    out = await tools_api.get_agent_tool_config(
        uuid.uuid4(), tool.id, current_user=admin_user,
        db=FakeDB(DummyResult(tool), DummyResult(None)),
    )
    assert ciphertext not in render(out)


async def test_mask_shaped_stored_value_is_not_echoed(admin_user):
    """A legacy '****abcd' left in storage must not travel back out."""
    tool = make_tool({"api_key": "****abcd", "os_type": "linux"})
    out = await tools_api.get_agent_tool_config(
        uuid.uuid4(), tool.id, current_user=admin_user,
        db=FakeDB(DummyResult(tool), DummyResult(None)),
    )
    assert "****" not in render(out)
    assert out["global_config_configured"]["api_key"] is True


async def test_unknown_and_nested_keys_are_dropped(admin_user):
    secrets = synthetic_secrets()
    tool = make_tool(loaded_global(secrets))
    at = make_agent_tool(tool, loaded_agent(secrets))

    out = await tools_api.get_agent_tool_config(
        at.agent_id, tool.id, current_user=admin_user,
        db=FakeDB(DummyResult(tool), DummyResult(at)),
    )

    for container in ("global_config", "agent_config", "merged_config"):
        assert "undeclared_note" not in out[container], "an unknown key survived"
        assert "nested" not in out[container], "a nested container survived"
        assert "brand_new_credential" not in out[container]
    assert "keep" not in render(out), "a nested unknown key survived"


async def test_schema_default_for_a_secret_is_stripped(admin_user):
    """Schema metadata must not become a second channel for a value."""
    tool = make_tool({"os_type": "linux"})
    out = await tools_api.get_agent_tool_config(
        uuid.uuid4(), tool.id, current_user=admin_user,
        db=FakeDB(DummyResult(tool), DummyResult(None)),
    )
    fields = {f["key"]: f for f in out["config_schema"]["fields"]}
    assert "default" not in fields["api_key"], "a password field kept its default"
    assert fields["os_type"]["default"] == "windows", "a non-secret default was lost"


# ═══════════════════════════════════════════════════════════════════════════
# B. CONFIGURED STATE
# ═══════════════════════════════════════════════════════════════════════════


async def test_configured_state_is_accurate_and_strictly_boolean(admin_user):
    secrets = synthetic_secrets()
    tool = make_tool(loaded_global(secrets))
    at = make_agent_tool(tool, loaded_agent(secrets))

    out = await tools_api.get_agent_tool_config(
        at.agent_id, tool.id, current_user=admin_user,
        db=FakeDB(DummyResult(tool), DummyResult(at)),
    )

    assert out["global_config_configured"]["api_key"] is True
    assert out["global_config_configured"]["smithery_api_key"] is True
    assert out["agent_config_configured"]["refresh_token"] is True
    assert out["agent_config_configured"]["api_key"] is True
    assert out["merged_config_configured"]["api_key"] is True
    assert out["credentials_configured"] is True

    for container in ("global_config_configured", "agent_config_configured", "merged_config_configured"):
        for key, value in out[container].items():
            assert isinstance(value, bool), f"{container}.{key} is {type(value).__name__}, not bool"


async def test_absent_credentials_report_false(admin_user):
    tool = make_tool({"os_type": "linux"})
    out = await tools_api.get_agent_tool_config(
        uuid.uuid4(), tool.id, current_user=admin_user,
        db=FakeDB(DummyResult(tool), DummyResult(None)),
    )
    assert out["global_config_configured"]["api_key"] is False
    assert out["credentials_configured"] is False
    assert out["agent_config"] == {}
    assert out["agent_config_configured"]["api_key"] is False


async def test_blank_credential_does_not_count_as_configured(admin_user):
    tool = make_tool({"api_key": "   ", "password": "", "os_type": "linux"})
    out = await tools_api.get_agent_tool_config(
        uuid.uuid4(), tool.id, current_user=admin_user,
        db=FakeDB(DummyResult(tool), DummyResult(None)),
    )
    assert out["global_config_configured"]["api_key"] is False
    assert out["global_config_configured"]["password"] is False


async def test_configured_state_does_not_decrypt(admin_user, monkeypatch):
    """Deriving configured-state must never materialise a plaintext credential."""
    import app.core.security as security

    def _boom(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("a response path attempted to decrypt")

    monkeypatch.setattr(security, "decrypt_data", _boom)

    secrets = synthetic_secrets()
    tool = make_tool(loaded_global(secrets))
    at = make_agent_tool(tool, loaded_agent(secrets))
    db = FakeDB(DummyResult(tool), DummyResult(at))

    out = await tools_api.get_agent_tool_config(at.agent_id, tool.id, current_user=admin_user, db=db)
    assert out["credentials_configured"] is True


async def test_with_config_states_agent_override_explicitly(admin_user):
    """agent_config no longer carries secrets, so emptiness cannot imply 'none'."""
    secrets = synthetic_secrets()
    tool = make_tool(loaded_global(secrets))
    # An override consisting ONLY of a credential: the safe projection is empty.
    at = make_agent_tool(tool, {"api_key": secrets["api_key"]})

    out = await tools_api.get_agent_tools_with_config(
        at.agent_id, current_user=admin_user,
        db=FakeDB(DummyResult([tool]), DummyResult([at])),
    )
    assert out[0]["agent_config"] == {}
    assert out[0]["has_agent_override"] is True


# ═══════════════════════════════════════════════════════════════════════════
# C. ALLOWLIST — fail-closed
# ═══════════════════════════════════════════════════════════════════════════


def test_declared_non_secret_fields_survive():
    projected = tools_api._project_safe_config(
        {"os_type": "linux", "base_url": "https://x.example.com"}, TOOL_SCHEMA
    )
    assert projected == {"base_url": "https://x.example.com", "os_type": "linux"}


def test_secret_declared_fields_are_dropped():
    secrets = synthetic_secrets()
    projected = tools_api._project_safe_config(
        {"api_key": secrets["api_key"], "os_type": "linux"}, TOOL_SCHEMA
    )
    assert projected == {"os_type": "linux"}


def test_sensitive_name_wins_over_a_wrong_schema():
    """A schema that mis-declares a credential as text must not open the gate."""
    lying_schema = {"fields": [{"key": "api_key", "type": "text"}, {"key": "os_type", "type": "select"}]}
    secrets = synthetic_secrets()
    projected = tools_api._project_safe_config(
        {"api_key": secrets["api_key"], "os_type": "linux"}, lying_schema
    )
    assert "api_key" not in projected
    assert projected == {"os_type": "linux"}


@pytest.mark.parametrize(
    "schema",
    [None, {}, {"fields": None}, {"fields": "nonsense"}, {"fields": [{"no_key": 1}]}, "not-a-dict", 42],
)
def test_absent_or_malformed_schema_fails_closed(schema):
    secrets = synthetic_secrets()
    projected = tools_api._project_safe_config(
        {"api_key": secrets["api_key"], "os_type": "linux", "base_url": "x"}, schema
    )
    assert projected == {}, f"schema {schema!r} did not fail closed"


def test_undeclared_credential_named_keys_are_classified_as_secret():
    secrets = synthetic_secrets()
    raw = {name: secrets[name] for name in ("smithery_api_key", "atlassian_api_key", "bot_token")}
    configured = tools_api._configured_map(raw, None)
    for key in raw:
        assert configured[key] is True
    assert tools_api._project_safe_config(raw, TOOL_SCHEMA) == {}


@pytest.mark.parametrize(
    "name",
    [
        "api_key", "access_token", "refresh_token", "password", "secret",
        "client_secret", "private_key", "webhook_secret", "hmac_secret",
        "token", "authorization", "credentials",
    ],
)
def test_every_required_sensitive_classification_is_active(name):
    assert tools_api._is_sensitive_name(name) is True


# ═══════════════════════════════════════════════════════════════════════════
# D. WRITE PATH — blank preserves, explicit clears, mask never lands
# ═══════════════════════════════════════════════════════════════════════════


async def test_saving_non_secret_config_preserves_the_stored_credential(admin_user):
    """The UI omits an untouched secret input; storage must keep its value."""
    secrets = synthetic_secrets()
    stored = tools_api._encrypt_sensitive_fields(
        {"api_key": secrets["api_key"], "os_type": "windows"}, TOOL_SCHEMA
    )
    tool = make_tool(dict(stored))
    original_ciphertext = tool.config["api_key"]

    await tools_api.update_tool(
        tool.id,
        tools_api.ToolUpdate(config={"os_type": "linux"}),
        current_user=admin_user,
        db=FakeDB(DummyResult(tool)),
    )

    assert tool.config["api_key"] == original_ciphertext, "an omitted secret was wiped"
    assert tool.config["os_type"] == "linux"


async def test_agent_save_without_a_replacement_secret_preserves_the_credential(admin_user):
    secrets = synthetic_secrets()
    tool = make_tool({"os_type": "linux"})
    stored = tools_api._encrypt_sensitive_fields({"api_key": secrets["api_key"]}, TOOL_SCHEMA)
    at = make_agent_tool(tool, dict(stored))
    original_ciphertext = at.config["api_key"]

    await tools_api.update_agent_tool_config(
        at.agent_id, tool.id,
        tools_api.AgentToolConfigUpdate(config={"base_url": "https://new.example.com"}),
        current_user=admin_user,
        db=FakeDB(DummyResult(tool), DummyResult(at)),
    )

    assert at.config["api_key"] == original_ciphertext, "an omitted secret was wiped"
    assert at.config["base_url"] == "https://new.example.com"


async def test_deliberate_replacement_is_written(admin_user):
    from app.config import get_settings
    from app.core.security import decrypt_data

    secrets = synthetic_secrets()
    replacement = f"SYNTH-replacement-{uuid.uuid4().hex}"
    stored = tools_api._encrypt_sensitive_fields({"api_key": secrets["api_key"]}, TOOL_SCHEMA)
    tool = make_tool(dict(stored))

    await tools_api.update_tool(
        tool.id,
        tools_api.ToolUpdate(config={"api_key": replacement, "os_type": "linux"}),
        current_user=admin_user,
        db=FakeDB(DummyResult(tool)),
    )

    assert decrypt_data(tool.config["api_key"], get_settings().SECRET_KEY) == replacement


@pytest.mark.parametrize("clearing_value", [None, "", "   "])
async def test_explicit_clear_removes_the_credential(admin_user, clearing_value):
    """Distinct from leaving blank: the UI omits blanks, and only an explicit
    null/empty submission removes the stored value."""
    secrets = synthetic_secrets()
    stored = tools_api._encrypt_sensitive_fields({"api_key": secrets["api_key"]}, TOOL_SCHEMA)
    tool = make_tool(dict(stored))

    await tools_api.update_tool(
        tool.id,
        tools_api.ToolUpdate(config={"api_key": clearing_value, "os_type": "linux"}),
        current_user=admin_user,
        db=FakeDB(DummyResult(tool)),
    )

    assert "api_key" not in tool.config


async def test_replace_config_discards_the_whole_override(admin_user):
    """'Reset to Global' must genuinely clear the row, secrets included."""
    secrets = synthetic_secrets()
    tool = make_tool({"os_type": "linux"})
    at = make_agent_tool(tool, tools_api._encrypt_sensitive_fields({"api_key": secrets["api_key"]}, TOOL_SCHEMA))

    await tools_api.update_agent_tool_config(
        at.agent_id, tool.id,
        tools_api.AgentToolConfigUpdate(config={}, replace_config=True),
        current_user=admin_user,
        db=FakeDB(DummyResult(tool), DummyResult(at)),
    )

    assert at.config == {}


async def test_a_mask_can_no_longer_be_produced_or_written_back(admin_user):
    """End to end: read, resubmit verbatim, credential survives untouched."""
    secrets = synthetic_secrets()
    stored = tools_api._encrypt_sensitive_fields(
        {"api_key": secrets["api_key"], "os_type": "linux"}, TOOL_SCHEMA
    )
    tool = make_tool(dict(stored))
    original_ciphertext = tool.config["api_key"]

    out = await tools_api.get_agent_tool_config(
        uuid.uuid4(), tool.id, current_user=admin_user,
        db=FakeDB(DummyResult(tool), DummyResult(None)),
    )
    assert "****" not in render(out)

    # A naive client echoes the whole read contract straight back.
    await tools_api.update_tool(
        tool.id,
        tools_api.ToolUpdate(config=dict(out["global_config"])),
        current_user=admin_user,
        db=FakeDB(DummyResult(tool)),
    )

    assert tool.config["api_key"] == original_ciphertext, "an echoed response damaged the credential"


# ═══════════════════════════════════════════════════════════════════════════
# D2. WRITE PATH — storage does not inherit the response projection's blindness
# ═══════════════════════════════════════════════════════════════════════════
# defect-isola-runtime-tool-config-schema-empty-config-data-loss-2026-08-06
#
# _project_safe_config is fail-closed and legitimately returns {} when a
# tool's config_schema is absent, empty or malformed — true for 91/110 seeded
# tools and every MCP tool app/services/resource_discovery.py creates. That
# empty READ view must never be read as "the STORED config should become
# empty": a save must start from the stored config and change only the keys
# actually submitted, independent of whatever the response side could show.


@pytest.mark.parametrize("empty_schema", [None, {}, {"fields": []}])
async def test_empty_schema_tool_preserves_everything_on_an_ordinary_save(admin_user, empty_schema):
    """The exact bug: a tool with no usable config_schema loses its stored
    config on a save, because the read-side allowlist showed the UI nothing.

    Malformed-but-non-empty schema shapes (e.g. ``{"fields": "nonsense"}``) are
    deliberately excluded here: they exercise a separate, pre-existing gap in
    the untouched storage-side ``_encrypt_sensitive_fields``/``_get_sensitive_keys``
    (which lacks the response-side functions' fail-closed try/except), not the
    write-merge bug this test targets.
    """
    secrets = synthetic_secrets()
    stored = tools_api._encrypt_sensitive_fields(
        {"api_key": secrets["api_key"], "os_type": "linux", "region": "us-east"}, empty_schema
    )
    tool = make_tool(dict(stored), schema=empty_schema)
    original_ciphertext = tool.config["api_key"]

    # Prove the premise: the read side really does show nothing for this tool.
    read = await tools_api.get_agent_tool_config(
        uuid.uuid4(), tool.id, current_user=admin_user,
        db=FakeDB(DummyResult(tool), DummyResult(None)),
    )
    assert read["global_config"] == {}, "fixture must exercise the fail-closed empty projection"

    # The UI's natural no-op save echoes exactly what it was shown to have: nothing.
    await tools_api.update_tool(
        tool.id, tools_api.ToolUpdate(config={}),
        current_user=admin_user, db=FakeDB(DummyResult(tool)),
    )

    assert tool.config["api_key"] == original_ciphertext, "a credential was lost under an empty schema"
    assert tool.config["os_type"] == "linux", "non-secret config was lost under an empty schema"
    assert tool.config["region"] == "us-east", "an unknown-to-the-schema key was lost"


async def test_smithery_mcp_tool_config_survives_a_save():
    """Regression for the exact shape resource_discovery.py writes: an MCP tool
    with no config_schema holding non-secret Smithery routing fields alongside
    an encrypted api_key."""
    secrets = synthetic_secrets()
    stored = tools_api._encrypt_sensitive_fields(
        {
            "api_key": secrets["smithery_api_key"],
            "smithery_namespace": "epic-namespace",
            "smithery_connection_id": "conn-123",
        },
        None,
    )
    merged = tools_api._merge_preserving_secrets(
        stored, {"smithery_namespace": "epic-namespace-renamed"}, None
    )
    assert merged["smithery_connection_id"] == "conn-123", "smithery_connection_id was dropped"
    assert merged["smithery_namespace"] == "epic-namespace-renamed"
    assert merged["api_key"] == stored["api_key"], "the encrypted api_key was disturbed"


async def test_agentbay_os_type_survives_an_unrelated_field_update(admin_user):
    """22 of 23 agentbay tools declare no schema field for os_type at all — a
    save touching a different field must not silently revert it."""
    tool = make_tool({"os_type": "linux", "region": "us-west"}, schema=None, name="agentbay_tool_no_schema")

    await tools_api.update_tool(
        tool.id, tools_api.ToolUpdate(config={"region": "eu-central"}),
        current_user=admin_user, db=FakeDB(DummyResult(tool)),
    )

    assert tool.config["os_type"] == "linux", "os_type silently reverted"
    assert tool.config["region"] == "eu-central"


async def test_blank_secret_and_explicit_clear_still_behave_correctly_under_an_empty_schema(admin_user):
    """The secret write-only semantics (omit=preserve, null=clear, value=replace)
    must hold even when config_schema can't drive the read-side allowlist —
    _response_secret_keys' name-based fallback is what makes this possible."""
    secrets = synthetic_secrets()
    stored = tools_api._encrypt_sensitive_fields({"api_key": secrets["api_key"], "os_type": "linux"}, None)
    tool = make_tool(dict(stored), schema=None)
    original_ciphertext = tool.config["api_key"]

    # Omitted secret: preserved.
    await tools_api.update_tool(
        tool.id, tools_api.ToolUpdate(config={"os_type": "windows"}),
        current_user=admin_user, db=FakeDB(DummyResult(tool)),
    )
    assert tool.config["api_key"] == original_ciphertext
    assert tool.config["os_type"] == "windows"

    # Explicit clear: removes only the targeted secret, nothing else.
    await tools_api.update_tool(
        tool.id, tools_api.ToolUpdate(config={"api_key": None}),
        current_user=admin_user, db=FakeDB(DummyResult(tool)),
    )
    assert "api_key" not in tool.config
    assert tool.config["os_type"] == "windows", "an unrelated key was destroyed by an explicit clear"


def test_merge_starts_from_stored_not_from_incoming():
    """Direct unit test of the corrected merge semantics."""
    stored = {"api_key": "cipher", "keep_me": "value", "os_type": "linux"}
    merged = tools_api._merge_preserving_secrets(stored, {"os_type": "windows"}, None)
    assert merged == {"api_key": "cipher", "keep_me": "value", "os_type": "windows"}
    assert merged["keep_me"] == "value", "an omitted, undeclared, non-secret key must survive"


# ═══════════════════════════════════════════════════════════════════════════
# E. RUNTIME COMPATIBILITY
# ═══════════════════════════════════════════════════════════════════════════


class _FakeSessionCtx:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc):
        return False


async def test_runtime_still_receives_the_real_decrypted_merged_config(monkeypatch):
    """The containment is response-side only: execution keeps its credentials.

    The runtime reads Tool.config / AgentTool.config from the database through
    app/services/agent_tools._get_tool_config and decrypts them itself. It has
    never consumed the HTTP response these tests constrain.
    """
    secrets = synthetic_secrets()
    global_stored = tools_api._encrypt_sensitive_fields(
        {"api_key": secrets["api_key"], "os_type": "windows", "base_url": "https://global.example.com"},
        TOOL_SCHEMA,
    )
    agent_stored = tools_api._encrypt_sensitive_fields(
        {"api_key": secrets["access_token"], "os_type": "linux"}, TOOL_SCHEMA
    )
    row = (agent_stored, global_stored, TOOL_SCHEMA)
    db = FakeDB(DummyResult(row))
    monkeypatch.setattr(agent_tools_svc, "async_session", lambda: _FakeSessionCtx(db))

    resolved = await agent_tools_svc._get_tool_config(uuid.uuid4(), "agentbay_browser_navigate")

    # Agent override wins; company value is the fallback; both decrypt.
    assert resolved["api_key"] == secrets["access_token"], "agent override precedence broken"
    assert resolved["os_type"] == "linux"
    assert resolved["base_url"] == "https://global.example.com", "global fallback broken"


async def test_runtime_falls_back_to_company_config(monkeypatch):
    secrets = synthetic_secrets()
    stored = tools_api._encrypt_sensitive_fields(
        {"api_key": secrets["api_key"], "os_type": "windows"}, TOOL_SCHEMA
    )
    tool = make_tool(dict(stored))
    db = FakeDB(DummyResult(None), DummyResult(tool))
    monkeypatch.setattr(agent_tools_svc, "async_session", lambda: _FakeSessionCtx(db))

    resolved = await agent_tools_svc._get_tool_config(uuid.uuid4(), "agentbay_browser_navigate")

    assert resolved["api_key"] == secrets["api_key"], "global fallback stopped decrypting"


async def test_the_api_write_path_stays_readable_by_the_runtime(monkeypatch, admin_user):
    """A credential saved through the API must still decrypt for execution.

    This is the compatibility seam: app/api/tools.py and
    app/services/agent_tools.py keep independent sensitive-key sets, so a change
    to what the API encrypts would silently break tool execution.
    """
    replacement = f"SYNTH-runtime-{uuid.uuid4().hex}"
    tool = make_tool({"os_type": "linux"})

    await tools_api.update_tool(
        tool.id,
        tools_api.ToolUpdate(config={"api_key": replacement, "os_type": "linux"}),
        current_user=admin_user,
        db=FakeDB(DummyResult(tool)),
    )
    assert tool.config["api_key"] != replacement, "fixture did not encrypt"

    # agent_id=None skips the per-agent join and reads the company row directly.
    db = FakeDB(DummyResult(tool))
    monkeypatch.setattr(agent_tools_svc, "async_session", lambda: _FakeSessionCtx(db))
    resolved = await agent_tools_svc._get_tool_config(None, tool.name)

    assert resolved["api_key"] == replacement, "the runtime can no longer read an API-written secret"


async def test_reading_the_api_response_does_not_mutate_stored_config(admin_user):
    secrets = synthetic_secrets()
    tool = make_tool(loaded_global(secrets))
    at = make_agent_tool(tool, loaded_agent(secrets))
    before_tool = json.dumps(tool.config, sort_keys=True, default=str)
    before_agent = json.dumps(at.config, sort_keys=True, default=str)

    await tools_api.get_agent_tool_config(
        at.agent_id, tool.id, current_user=admin_user,
        db=FakeDB(DummyResult(tool), DummyResult(at)),
    )
    await tools_api.get_agent_tools_with_config(
        at.agent_id, current_user=admin_user,
        db=FakeDB(DummyResult([tool]), DummyResult([at])),
    )

    assert json.dumps(tool.config, sort_keys=True, default=str) == before_tool
    assert json.dumps(at.config, sort_keys=True, default=str) == before_agent


# ═══════════════════════════════════════════════════════════════════════════
# F. NON-LEAK CHANNELS — exceptions, validation errors, repr
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_missing_tool_raises_without_quoting_anything(admin_user):
    from fastapi import HTTPException

    secrets = synthetic_secrets()
    with pytest.raises(HTTPException) as excinfo:
        await tools_api.get_agent_tool_config(
            uuid.uuid4(), uuid.uuid4(), current_user=admin_user,
            db=FakeDB(DummyResult(None)),
        )
    for value in secrets.values():
        assert value not in str(excinfo.value)


async def test_repr_of_the_response_is_clean(admin_user):
    secrets = synthetic_secrets()
    tool = make_tool(loaded_global(secrets))
    at = make_agent_tool(tool, loaded_agent(secrets))
    out = await tools_api.get_agent_tool_config(
        at.agent_id, tool.id, current_user=admin_user,
        db=FakeDB(DummyResult(tool), DummyResult(at)),
    )
    assert_no_credentials(repr(out), secrets)


async def test_test_email_never_touches_the_database_or_tool_storage(admin_user, monkeypatch):
    """defect-isola-runtime-tool-config-test-email-credential-exfiltration-2026-08-06.

    An earlier revision resolved a stored, decrypted credential server-side and
    merged it with caller-supplied host/port fields, letting a caller redirect a
    real credential to a destination of their choosing. The endpoint must never
    resolve stored config at all: it takes no ``db`` dependency, no longer
    accepts ``agent_id``/``tool_id``, and never calls into runtime config
    resolution — proven here by making that call raise if it is ever reached.
    """
    import inspect

    sig = inspect.signature(tools_api.test_email_connection)
    assert "db" not in sig.parameters, "must not depend on the database at all"
    assert set(tools_api.EmailTestRequest.model_fields) == {"config"}, (
        "agent_id/tool_id must not exist on this request model"
    )

    async def _must_not_be_called(*_args, **_kwargs):
        raise AssertionError("test-email must never resolve stored tool config")

    monkeypatch.setattr(agent_tools_svc, "_get_tool_config", _must_not_be_called)

    async def _fake_test(config):
        return {"ok": True, "imap": "connected", "smtp": "connected", "_seen": config}

    monkeypatch.setattr("app.services.email_service.test_connection", _fake_test)

    result = await tools_api.test_email_connection(
        tools_api.EmailTestRequest(config={"imap_host": "caller.example.com"}),
        current_user=admin_user,
    )
    assert result["_seen"] == {"imap_host": "caller.example.com"}


async def test_test_email_config_is_exactly_what_the_caller_sent(admin_user, monkeypatch):
    """No server-side value is ever merged in — the caller-supplied dict is the
    entire connection config, byte for byte. This is what makes host/port/
    protocol manipulation harmless: there is nothing server-side to redirect."""
    secrets = synthetic_secrets()
    payload = {
        "imap_host": "attacker.example.com", "imap_port": 993,
        "smtp_host": "attacker.example.com", "smtp_port": 465,
        "password": secrets["password"],  # the caller's OWN value, not a stored one
    }
    seen = {}

    async def _fake_test(config):
        seen.update(config)
        return {"ok": True}

    monkeypatch.setattr("app.services.email_service.test_connection", _fake_test)

    await tools_api.test_email_connection(
        tools_api.EmailTestRequest(config=payload), current_user=admin_user,
    )
    assert seen == payload, "the endpoint augmented the caller's config with server-side data"


# A genuine negative control for this class (temporarily reintroducing the
# resolution behavior in app/api/tools.py itself and confirming the two tests
# above fail, then reverting) was run manually and is recorded in Port
# evidence, matching how the rest of this suite's negative controls are
# performed — not as a permanently committed test, since that would require
# either duplicating the vulnerable handler here (testing a fiction) or
# runtime-patching the real handler's source (fragile and unreadable).


# ═══════════════════════════════════════════════════════════════════════════
# STATIC GUARDS — the defect classes cannot be reintroduced quietly
# ═══════════════════════════════════════════════════════════════════════════

CONTAINED_HANDLERS = ("list_tools", "get_agent_tool_config", "get_agent_tools_with_config")


@pytest.mark.parametrize("handler_name", CONTAINED_HANDLERS)
def test_no_response_handler_decrypts_or_masks(handler_name):
    source = inspect.getsource(getattr(tools_api, handler_name))
    assert "_decrypt_sensitive_fields" not in source, f"{handler_name} must never decrypt"
    assert "_mask_sensitive_fields" not in source, f"{handler_name} must never mask"
    assert "****" not in source, f"{handler_name} reintroduced a reusable mask"
    assert "masked_global" not in source
    assert "[-4:]" not in source, f"{handler_name} reintroduced a last-four fragment"


@pytest.mark.parametrize("handler_name", CONTAINED_HANDLERS)
def test_every_contained_handler_projects_through_the_allowlist(handler_name):
    source = inspect.getsource(getattr(tools_api, handler_name))
    assert "_project_safe_config" in source, f"{handler_name} does not use the safe projection"
    assert "_configured_map" in source, f"{handler_name} does not report configured-state"


def test_the_api_layer_owns_no_decrypt_helper():
    """Decryption belongs to the runtime service, not to a response module."""
    assert not hasattr(tools_api, "_decrypt_sensitive_fields")
    assert not hasattr(tools_api, "_mask_sensitive_fields")


def test_no_api_module_decrypts_a_tool_config():
    for path in sorted(Path("app/api").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "_decrypt_sensitive_fields(" not in text, path
        assert "_mask_sensitive_fields(" not in text, path


def test_no_tool_response_returns_a_raw_config_blob():
    """Neither Tool.config nor AgentTool.config may be emitted unprojected."""
    source = inspect.getsource(tools_api)
    for forbidden in (
        '"config": t.config',
        '"config": tool.config',
        '"agent_config": raw_agent',
        '"global_config": raw_global',
        '"merged_config": merged',
        '"agent_config": at.config',
    ):
        assert forbidden not in source, f"a raw config blob is returned: {forbidden}"


def test_the_runtime_service_keeps_its_own_decryption():
    """The fix is response-side only. Removing this would break execution."""
    assert hasattr(agent_tools_svc, "_decrypt_sensitive_fields")
    source = inspect.getsource(agent_tools_svc._get_tool_config)
    assert "_decrypt_sensitive_fields" in source
    assert "{**(global_config or {}), **(agent_config or {})}" in source, "precedence changed"
