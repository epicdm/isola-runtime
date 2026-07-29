"""Focused tests for the additive Foundation<->Clawith structured contract (schema 1.0)."""
import os
import types
import uuid
import pytest

from app.api import isola_bridge as B

TA = uuid.uuid4()


def _row(tool_name, status="succeeded", result_summary="", sanitized_arguments=None, tool_call_id="tc1"):
    return types.SimpleNamespace(tool_name=tool_name, status=status, result_summary=result_summary,
                                 sanitized_arguments=sanitized_arguments, tool_call_id=tool_call_id)


def test_contract_gating_env(monkeypatch):
    aid = uuid.uuid4()
    monkeypatch.setenv(B._CONTRACT_AGENTS_ENV, "")
    assert B._contract_enabled_for(aid) is False
    monkeypatch.setenv(B._CONTRACT_AGENTS_ENV, f"{uuid.uuid4()}, {aid} ,{uuid.uuid4()}")
    assert B._contract_enabled_for(aid) is True
    assert B._contract_enabled_for(uuid.uuid4()) is False


def test_derive_escalation_and_reason():
    rows = [_row("finish"),
            _row("escalate_to_human", sanitized_arguments={"reason": "complaint_sensitive"})]
    esc, reason, ksu, outcomes = B._derive_contract_fields(rows)
    assert esc is True
    assert reason == "complaint_sensitive"
    assert len(outcomes) == 2


def test_derive_escalation_default_reason():
    esc, reason, ksu, outcomes = B._derive_contract_fields([_row("escalate_to_human")])
    assert esc is True and reason == "customer_requested_human"


def test_derive_knowledge_sources():
    rows = [_row("read_file", result_summary="opened enterprise_info/epic.md"),
            _row("read_file", result_summary="opened enterprise_info/epic.md"),
            _row("list_files", result_summary="no match")]
    esc, reason, ksu, outcomes = B._derive_contract_fields(rows)
    assert ksu == ["read_file"]  # deduped; list_files had no enterprise_info hit
    assert esc is False and reason is None


def test_assemble_contract_shape():
    derived = B._derive_contract_fields([_row("escalate_to_human", sanitized_arguments={"reason": "low_confidence"})])
    c = B._assemble_contract(agent_id=uuid.uuid4(), agent_name="EPIC Front Desk 6737", tenant_id=TA,
                             run_id=uuid.uuid4(), session_id=uuid.uuid4(), correlation_id="corr-1",
                             status="completed", reply="hello", derived=derived)
    expected = {"schema_version", "agent_id", "agent_name", "tenant_id", "run_id", "matched_session",
                "correlation_id", "status", "response_text", "customer_facing_next_step", "intent",
                "confidence", "missing_information", "proposed_tool_calls", "knowledge_sources_used",
                "completed_tool_outcomes", "escalation_requested", "escalation_reason", "follow_up_required"}
    assert set(c.keys()) == expected
    assert c["schema_version"] == "1.0"
    assert c["response_text"] == "hello"
    assert c["escalation_requested"] is True and c["escalation_reason"] == "low_confidence"
    assert c["follow_up_required"] is True
    assert c["proposed_tool_calls"] == [] and c["intent"] is None and c["confidence"] is None
    assert isinstance(c["completed_tool_outcomes"], list)
