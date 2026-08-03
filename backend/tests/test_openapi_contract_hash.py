"""Tests for the reproducible OpenAPI contract-hash utility
(`backend/tools/openapi_contract_hash.py`,
`dec-isola-api-contract-hash-method-provenance-2026-08-02`).

These tests exercise the tool's pure canonicalization/closure logic directly
(no HTTP, no subprocess) against small synthetic OpenAPI-shaped documents,
plus one end-to-end run against the real app to prove `--source app`
actually works and is deterministic.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "openapi_contract_hash.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("openapi_contract_hash", _TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


def _doc(paths, components=None):
    return {
        "openapi": "3.1.0",
        "info": {"title": "x", "version": "1"},
        "paths": paths,
        "components": components or {},
    }


# ── Canonicalization determinism ─────────────────────────────────────────────


def test_canonical_bytes_is_deterministic_across_key_order_permutations():
    doc_a = {"b": 1, "a": 2, "c": {"z": 1, "y": 2}}
    doc_b = {"a": 2, "c": {"y": 2, "z": 1}, "b": 1}
    assert tool._canonical_bytes(doc_a) == tool._canonical_bytes(doc_b)


def test_canonical_bytes_is_compact_and_unicode_preserving():
    data = {"name": "café", "list": [1, 2, 3]}
    out = tool._canonical_bytes(data)
    # separators=(",", ":") -> no spaces after separators.
    assert b", " not in out
    assert b": " not in out
    # ensure_ascii=False -> non-ASCII bytes are UTF-8, not \uXXXX escapes.
    assert "café".encode() in out


def test_sha256_hex_matches_hashlib_directly():
    import hashlib

    data = b"some bytes"
    assert tool._sha256_hex(data) == hashlib.sha256(data).hexdigest()


# ── Path selection ───────────────────────────────────────────────────────────


def test_build_subset_selects_only_named_paths():
    doc = _doc(
        {
            "/a": {"get": {}},
            "/b": {"get": {}},
            "/c": {"get": {}},
        }
    )
    subset = tool.build_subset(doc, ["/a", "/c"])
    assert set(subset["paths"].keys()) == {"/a", "/c"}


def test_build_subset_raises_on_missing_path():
    doc = _doc({"/a": {"get": {}}})
    with pytest.raises(SystemExit):
        tool.build_subset(doc, ["/does-not-exist"])


def test_selected_path_change_changes_subset_hash():
    doc_v1 = _doc({"/a": {"get": {"summary": "v1"}}, "/b": {"get": {}}})
    doc_v2 = _doc({"/a": {"get": {"summary": "v2"}}, "/b": {"get": {}}})

    subset_v1 = tool.build_subset(doc_v1, ["/a"])
    subset_v2 = tool.build_subset(doc_v2, ["/a"])

    assert tool._sha256_hex(tool._canonical_bytes(subset_v1)) != tool._sha256_hex(
        tool._canonical_bytes(subset_v2)
    )


def test_unselected_unrelated_path_change_does_not_change_subset_hash():
    doc_v1 = _doc({"/a": {"get": {"summary": "stable"}}, "/b": {"get": {"summary": "v1"}}})
    doc_v2 = _doc({"/a": {"get": {"summary": "stable"}}, "/b": {"get": {"summary": "v2"}}})

    subset_v1 = tool.build_subset(doc_v1, ["/a"])
    subset_v2 = tool.build_subset(doc_v2, ["/a"])

    assert tool._sha256_hex(tool._canonical_bytes(subset_v1)) == tool._sha256_hex(
        tool._canonical_bytes(subset_v2)
    )


# ── Transitive $ref closure ──────────────────────────────────────────────────


def test_transitive_referenced_component_change_changes_subset_hash():
    components_v1 = {
        "schemas": {
            "Widget": {
                "type": "object",
                "properties": {"part": {"$ref": "#/components/schemas/Part"}},
            },
            "Part": {"type": "object", "properties": {"sku": {"type": "string"}}},
        }
    }
    components_v2 = copy.deepcopy(components_v1)
    # Change ONLY the transitively-referenced component (Part), not Widget
    # itself or the path that references Widget directly.
    components_v2["schemas"]["Part"]["properties"]["sku"]["maxLength"] = 40

    paths = {
        "/widgets": {
            "get": {
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Widget"}
                            }
                        }
                    }
                }
            }
        }
    }

    doc_v1 = _doc(paths, components_v1)
    doc_v2 = _doc(paths, components_v2)

    subset_v1 = tool.build_subset(doc_v1, ["/widgets"])
    subset_v2 = tool.build_subset(doc_v2, ["/widgets"])

    # The transitively-referenced Part component must actually be pulled in.
    assert "Part" in subset_v1["components"]["schemas"]
    assert "Part" in subset_v2["components"]["schemas"]
    assert tool._sha256_hex(tool._canonical_bytes(subset_v1)) != tool._sha256_hex(
        tool._canonical_bytes(subset_v2)
    )


def test_closure_reaches_a_fixed_point_across_multiple_hops():
    components = {
        "schemas": {
            "A": {"properties": {"b": {"$ref": "#/components/schemas/B"}}},
            "B": {"properties": {"c": {"$ref": "#/components/schemas/C"}}},
            "C": {"type": "string"},
            "Unrelated": {"type": "string"},
        }
    }
    paths = {
        "/a": {"get": {"responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/A"}}}}}}}
    }
    doc = _doc(paths, components)
    subset = tool.build_subset(doc, ["/a"])

    assert set(subset["components"]["schemas"].keys()) == {"A", "B", "C"}


def test_closure_covers_all_five_component_containers():
    components = {
        "schemas": {"S": {"$ref_holder": True}},
        "responses": {"R": {"description": "ok"}},
        "parameters": {"P": {"name": "x", "in": "query"}},
        "requestBodies": {"RB": {"description": "body"}},
        "securitySchemes": {"SS": {"type": "http", "scheme": "bearer"}},
    }
    paths = {
        "/x": {
            "get": {
                "parameters": [{"$ref": "#/components/parameters/P"}],
                "requestBody": {"$ref": "#/components/requestBodies/RB"},
                "responses": {"200": {"$ref": "#/components/responses/R"}},
                "security": [{"$ref": "#/components/securitySchemes/SS"}],
            }
        }
    }
    doc = _doc(paths, components)
    subset = tool.build_subset(doc, ["/x"])

    assert subset["components"]["parameters"]["P"] == components["parameters"]["P"]
    assert subset["components"]["requestBodies"]["RB"] == components["requestBodies"]["RB"]
    assert subset["components"]["responses"]["R"] == components["responses"]["R"]
    assert subset["components"]["securitySchemes"]["SS"] == components["securitySchemes"]["SS"]
    # "S" was never referenced by /x — must not be pulled in.
    assert "S" not in subset["components"].get("schemas", {})


def test_closure_ignores_dangling_and_external_refs_without_raising():
    paths = {
        "/x": {
            "get": {
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/DoesNotExist"}
                            }
                        }
                    }
                },
                "external": {"$ref": "https://example.com/other.json#/Foo"},
            }
        }
    }
    doc = _doc(paths, {"schemas": {}})
    subset = tool.build_subset(doc, ["/x"])
    assert subset["paths"] == paths
    assert subset["components"] == {}


# ── Legacy routes file ───────────────────────────────────────────────────────


def test_legacy_bridge_routes_file_names_exactly_the_three_ratified_paths():
    routes_path = _TOOL_PATH.with_name("legacy_bridge_routes.txt")
    paths = tool._load_paths_file(routes_path)
    assert paths == [
        "/api/isola/bridge/message",
        "/api/isola/bridge/v2/requests",
        "/api/isola/bridge/v2/requests/{bridge_request_id}",
    ]


# ── End-to-end: real app, method version, tool provenance ───────────────────


def test_end_to_end_run_against_the_real_app_emits_method_version_and_tool_sha(tmp_path):
    out_dir = tmp_path / "out"
    exit_code = tool.main(
        [
            "--source",
            "app",
            "--paths-file",
            str(_TOOL_PATH.with_name("legacy_bridge_routes.txt")),
            "--out",
            str(out_dir),
        ]
    )
    assert exit_code == 0

    provenance = json.loads((out_dir / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["method_version"] == tool.METHOD_VERSION == 1
    assert provenance["tool_file_sha256"] == tool._tool_sha256()
    assert provenance["source_type"] == "app"
    assert provenance["source"] == "app:app.main:app"
    assert len(provenance["full_document_sha256"]) == 64
    assert len(provenance["subset_sha256"]) == 64
    assert (out_dir / "openapi_full.canonical.json").exists()
    assert (out_dir / "openapi_subset.canonical.json").exists()


def test_end_to_end_run_is_byte_stable_across_repeated_invocations(tmp_path):
    """Determinism proof: running the tool twice against the same
    (unchanged) app must produce byte-identical canonical artifacts and
    identical hashes — no nondeterministic key ordering, timestamps or
    object-identity leakage into the hashed bytes."""
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    for out_dir in (out_a, out_b):
        exit_code = tool.main(
            [
                "--source",
                "app",
                "--paths-file",
                str(_TOOL_PATH.with_name("legacy_bridge_routes.txt")),
                "--out",
                str(out_dir),
            ]
        )
        assert exit_code == 0

    assert (out_a / "openapi_full.canonical.json").read_bytes() == (
        out_b / "openapi_full.canonical.json"
    ).read_bytes()
    assert (out_a / "openapi_subset.canonical.json").read_bytes() == (
        out_b / "openapi_subset.canonical.json"
    ).read_bytes()

    prov_a = json.loads((out_a / "provenance.json").read_text(encoding="utf-8"))
    prov_b = json.loads((out_b / "provenance.json").read_text(encoding="utf-8"))
    assert prov_a["full_document_sha256"] == prov_b["full_document_sha256"]
    assert prov_a["subset_sha256"] == prov_b["subset_sha256"]


def test_real_app_legacy_subset_contains_exactly_the_three_ratified_paths():
    document = tool._load_from_app()
    subset = tool.build_subset(document, tool._load_paths_file(_TOOL_PATH.with_name("legacy_bridge_routes.txt")))
    assert set(subset["paths"].keys()) == {
        "/api/isola/bridge/message",
        "/api/isola/bridge/v2/requests",
        "/api/isola/bridge/v2/requests/{bridge_request_id}",
    }


def test_real_app_full_document_contains_the_structured_route_but_subset_does_not():
    """The structured route exists in the full document (it's a real,
    deployed-dark route) but must never appear in the legacy subset — the
    subset is scoped to exactly the three pre-existing routes this
    migration's compatibility gate cares about."""
    document = tool._load_from_app()
    assert "/api/isola/bridge/structured/message" in document["paths"]

    subset = tool.build_subset(
        document, tool._load_paths_file(_TOOL_PATH.with_name("legacy_bridge_routes.txt"))
    )
    assert "/api/isola/bridge/structured/message" not in subset["paths"]
