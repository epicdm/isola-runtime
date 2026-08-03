#!/usr/bin/env python3
"""Reproducible OpenAPI contract-hash utility.

Implements `dec-isola-api-contract-hash-method-provenance-2026-08-02`: a
SHA-256 for an OpenAPI document is comparable across sessions only when the
exact extraction and canonicalization method is preserved alongside it. This
file IS that preserved method. Every hash it prints is accompanied by
`method_version`, this file's own SHA-256, the exact source, paths file and
invocation used to produce it — so a later session can tell whether a
mismatch means "the API changed" or "a different method produced this
number", and never has to guess.

METHOD_VERSION 1
-----------------
* Full-document hash: the ENTIRE parsed OpenAPI document, canonicalized as
  ``json.dumps(document, sort_keys=True, separators=(",", ":"),
  ensure_ascii=False)`` and SHA-256'd.
* Subset hash: a ``{"paths": ..., "components": ...}`` artifact containing
  only the paths named in ``--paths-file`` (default:
  ``legacy_bridge_routes.txt``, next to this file) plus the transitive
  closure of every ``#/components/{schemas,responses,parameters,
  requestBodies,securitySchemes}/...`` ``$ref`` those paths (and the
  components they in turn reference) point at, iterated to a fixed point.
  Canonicalized with the same JSON settings as the full document.

Any future change to this canonicalization must bump METHOD_VERSION — do
not silently change the bytes an existing method_version claims to produce.

USAGE
-----
    python tools/openapi_contract_hash.py --source app
    python tools/openapi_contract_hash.py --source url --url https://host/openapi.json
    python tools/openapi_contract_hash.py --source app --paths-file tools/legacy_bridge_routes.txt --out /tmp/evidence

``--source app`` imports the FastAPI application in-process (``app.main
.app``) and calls ``.openapi()`` — no server needs to be running. ``--source
url`` performs a plain HTTP GET and needs no backend dependencies installed,
so it can run against a live or containerized deployment from a bare
Python 3 interpreter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

METHOD_VERSION = 1

_COMPONENT_CONTAINERS = (
    "schemas",
    "responses",
    "parameters",
    "requestBodies",
    "securitySchemes",
)


def _canonical_bytes(document: dict) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_from_app() -> dict:
    # Imported lazily: `--source url` must not require the backend's full
    # dependency graph (database drivers, LLM SDKs, ...) to be installed.
    from app.main import app

    openapi = app.openapi()
    # FastAPI caches `app.openapi_schema` and returns that same object on
    # every call; round-trip through JSON so callers can freely inspect or
    # mutate the result without ever touching the live app's cache.
    return json.loads(json.dumps(openapi))


def _load_from_url(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as resp:  # operator-supplied URL, not user input
        raw = resp.read()
    return json.loads(raw)


def _load_paths_file(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def _ref_target(ref: str) -> tuple[str, str] | None:
    """Parse a local ``#/components/<container>/<name>`` ref into
    (container, name). Returns None for anything else — external refs and
    non-component local refs are not expected in this repository's schema
    and are simply not resolved further (not an error: hashing a document
    that happens to contain one should not crash)."""
    prefix = "#/components/"
    if not ref.startswith(prefix):
        return None
    rest = ref[len(prefix) :]
    parts = rest.split("/", 1)
    if len(parts) != 2:
        return None
    container, name = parts
    if container not in _COMPONENT_CONTAINERS:
        return None
    return container, name


def _iter_refs(node: Any):
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            yield ref
        for value in node.values():
            yield from _iter_refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_refs(item)


def _resolve_component_closure(document: dict, seed_nodes: list[Any]) -> dict:
    """Transitive $ref closure over the five component containers, iterated
    to a FIXED POINT: components referenced by a selected path, PLUS
    components referenced by those components, and so on, until one pass
    discovers nothing new."""
    all_components: dict = document.get("components", {})
    selected: dict[str, dict[str, Any]] = {c: {} for c in _COMPONENT_CONTAINERS}

    frontier: list[Any] = list(seed_nodes)
    seen_refs: set[tuple[str, str]] = set()

    while frontier:
        node = frontier.pop()
        for ref in _iter_refs(node):
            target = _ref_target(ref)
            if target is None:
                continue
            if target in seen_refs:
                continue
            seen_refs.add(target)
            container, name = target
            component_value = all_components.get(container, {}).get(name)
            if component_value is None:
                # Referenced but absent from components: nothing to add,
                # nothing further to walk from a name that doesn't exist.
                continue
            selected[container][name] = component_value
            frontier.append(component_value)

    # Drop empty containers so the subset artifact does not carry spurious
    # empty dicts that would still perturb its canonical bytes.
    return {container: names for container, names in selected.items() if names}


def build_subset(document: dict, selected_paths: list[str]) -> dict:
    doc_paths = document.get("paths", {})
    missing = [p for p in selected_paths if p not in doc_paths]
    if missing:
        raise SystemExit(
            f"paths file names route(s) not present in the OpenAPI document: {missing}"
        )
    subset_paths = {p: doc_paths[p] for p in selected_paths}
    components = _resolve_component_closure(document, [subset_paths])
    return {"paths": subset_paths, "components": components}


def _tool_sha256() -> str:
    return _sha256_hex(Path(__file__).read_bytes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", choices=("app", "url"), required=True)
    parser.add_argument("--url", help="OpenAPI JSON URL (required when --source url)")
    parser.add_argument(
        "--paths-file",
        type=Path,
        default=Path(__file__).with_name("legacy_bridge_routes.txt"),
        help="Newline-delimited list of OpenAPI path keys to select for the subset artifact",
    )
    parser.add_argument(
        "--out", type=Path, help="Directory to write canonical artifacts + provenance JSON into"
    )
    args = parser.parse_args(argv)

    if args.source == "url" and not args.url:
        parser.error("--source url requires --url")

    raw_argv = argv if argv is not None else sys.argv[1:]
    invocation = " ".join(["openapi_contract_hash.py", *raw_argv])

    if args.source == "app":
        document = _load_from_app()
        source_desc = "app:app.main:app"
    else:
        document = _load_from_url(args.url)
        source_desc = f"url:{args.url}"

    selected_paths = _load_paths_file(args.paths_file)
    subset = build_subset(document, selected_paths)

    full_bytes = _canonical_bytes(document)
    subset_bytes = _canonical_bytes(subset)
    full_sha = _sha256_hex(full_bytes)
    subset_sha = _sha256_hex(subset_bytes)
    tool_sha = _tool_sha256()

    provenance = {
        "method_version": METHOD_VERSION,
        "tool_file_sha256": tool_sha,
        "source_type": args.source,
        "source": source_desc,
        "paths_file": str(args.paths_file),
        "selected_paths": selected_paths,
        "invocation": invocation,
        "full_document_sha256": full_sha,
        "subset_sha256": subset_sha,
        "generated_at": datetime.now(UTC).isoformat(),
    }

    print(json.dumps(provenance, indent=2, sort_keys=True))

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "openapi_full.canonical.json").write_bytes(full_bytes)
        (args.out / "openapi_subset.canonical.json").write_bytes(subset_bytes)
        (args.out / "provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
