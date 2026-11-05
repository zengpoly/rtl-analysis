#!/usr/bin/env python3
"""Deterministic scope-aware Build1 Manual renderer for Knowledge SQLite candidates."""

from __future__ import annotations

from collections import defaultdict, deque
import hashlib
import json
import posixpath
from pathlib import Path, PurePosixPath
import shutil
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from rtl_knowledge_prototype import PrototypeStore, _load_publication_contract, MODULE_Q2_PUBLICATION_PATH, Q2_FIFO_V2_PUBLICATION_PATH, StoreError


TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates/manual-v6"
ASSET_ROOT = TEMPLATE_ROOT / "assets"
STRUCTURED_DETAIL_SCHEMA = "rtl-analysis-agent/manual-v6-structured-detail-v1"
PREVIEW_LIMITS = {
    "children": 12,
    "parameters": 16,
    "bindings": 20,
    "known_instances": 20,
    "facts": 12,
    "evidence": 12,
    "sources": 20,
    "direct_submodules": 20,
}
MODULE_DEFINITION_TEMPLATE_VERSION = "Module Definition V2"
PAGE_TEMPLATES = {
    "SCOPE_OVERVIEW": ("scope-overview.html", "scope-overview"),
    "ACTUAL_INSTANCE": ("instance.html", "instance"),
    "MODULE_DEFINITION": ("module-definition.html", "module-definition"),
    "SOURCE_FILE": ("source-file.html", "source-file"),
}


class SourceSafetyError(RuntimeError):
    """A published source path or content identity is not safe/current."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_source_path(source_root: Path, stored_path: str) -> Path:
    relative = PurePosixPath(stored_path)
    if relative.is_absolute() or not stored_path or "\\" in stored_path or ".." in relative.parts:
        raise SourceSafetyError(f"unsafe source path: {stored_path!r}")
    root = source_root.resolve(strict=True)
    candidate = (root / Path(*relative.parts)).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise SourceSafetyError(f"source path escapes frozen root: {stored_path!r}") from error
    return candidate


def _slug(value: str) -> str:
    value = value.rsplit(".", 1)[-1]
    clean = "".join(
        character.lower() if character.isalnum() or character in "._-" else "-"
        for character in value
    )
    return clean.strip("-.") or "object"


def _object_path(kind: str, qualified_name: str, entity_id: str) -> str:
    digest = entity_id.split(":", 1)[-1]
    directory = "instance" if kind.endswith(":instance") else "definition"
    return f"{directory}/{_slug(qualified_name)}--{digest}.html"


def _structured_path(kind: str, qualified_name: str, entity_id: str) -> str:
    digest = entity_id.split(":", 1)[-1]
    directory = "instance" if kind.endswith(":instance") else "definition"
    return f"structured/{directory}/{_slug(qualified_name)}--{digest}.json"


def _human_name(value: str) -> str:
    """Make a conservative display label without changing canonical identity."""
    text = str(value).strip()
    return text[:1].upper() + text[1:] if text else "Unnamed Project"


def _scope_presentation(
    scope: dict[str, Any], metadata: dict[str, Any], entities: dict[str, dict[str, Any]],
    *, chinese: bool = False,
) -> dict[str, str]:
    roots = scope["roots"]
    actual_roots = [
        entities[row["entity_id"]]
        for row in roots
        if row["entity_id"] in entities and row["kind"].endswith(":instance")
    ]
    project_identity = str(metadata.get("project", ""))
    if actual_roots and scope["scope_mode"] == "ACTUAL_INSTANCE_ROOT":
        project_identity = actual_roots[0]["qualified_name"].split(".", 1)[0]
    project = _human_name(project_identity)
    capability = scope.get("producer_capability", {})
    if capability.get("actual_hierarchy") == "FULL":
        label = f"{project} · " + ("整体设计" if chinese else "Whole Design")
    elif scope["scope_mode"] == "ACTUAL_INSTANCE_ROOT" and actual_roots:
        label = f"{project} / {actual_roots[0]['qualified_name'].rsplit('.', 1)[-1]}"
    elif scope["scope_mode"] == "MODULE_DEFINITION_ROOT" and roots:
        root = entities[roots[0]["entity_id"]]
        label = f"{project} / {root['qualified_name']} · " + ("模块定义" if chinese else "Module Definition")
    elif scope["scope_mode"] == "MULTI_ROOT":
        label = f"{project} · {len(roots)} " + ("个根节点" if chinese else "Roots")
    elif scope["scope_mode"] == "SOURCE_ONLY":
        label = f"{project} · " + ("源码 / 定义范围" if chinese else "Source / Definition Scope")
    else:
        label = f"{project} · {scope['scope_mode'].replace('_', ' ').title()}"
    return {
        "label": label,
        "scope_id": scope["scope_id"],
        "mode": scope["scope_mode"],
    }


def _entity_reference(
    identifier: str | None,
    entities: dict[str, dict[str, Any]],
    page_map: dict[str, str],
) -> dict[str, Any] | None:
    if identifier is None:
        return None
    entity = entities[identifier]
    return {
        "entity_id": identifier,
        "kind": entity["kind"],
        "qualified_name": entity["qualified_name"],
        "page_path": page_map.get(identifier),
    }


def _relative_url(current: str, target: str) -> str:
    current_parent = str(PurePosixPath(current).parent)
    return posixpath.relpath(target, start=current_parent if current_parent != "." else ".")


def _base_module_page_v2(
    module: dict[str, Any],
    known_instances: list[dict[str, Any]],
    actual_by_id: dict[str, dict[str, Any]],
    entities: dict[str, dict[str, Any]],
    page_map: dict[str, str],
) -> dict[str, Any]:
    """Create the Build1-safe, semantic-empty Module Definition V2 view."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for parent in known_instances:
        for child_id in parent["children"]:
            child = actual_by_id[child_id]
            definition_id = child["definition_id"]
            local_name = str(child["attrs"].get("name") or child["qualified_name"].rsplit(".", 1)[-1])
            key = (local_name, definition_id)
            row = grouped.setdefault(key, {
                "name": local_name,
                "definition": entities[definition_id]["qualified_name"],
                "definition_id": definition_id,
                "definition_path": page_map.get(definition_id),
                "example_actual": child["qualified_name"],
                "example_actual_path": child["output_path"],
                "occurrences": 0,
            })
            row["occurrences"] += 1
    direct_submodules = [grouped[key] for key in sorted(grouped)]
    return {
        "template_version": MODULE_DEFINITION_TEMPLATE_VERSION,
        "identity": _entity_reference(module["entity_id"], entities, page_map),
        "semantic_maturity": "NOT_BUILT",
        "overview": [],
        "responsibilities": [],
        "architecture": None,
        "direct_submodules": direct_submodules,
        "direct_submodules_preview": direct_submodules[:PREVIEW_LIMITS["direct_submodules"]],
        "key_interfaces": [],
        "control_summary": [],
        "important_flows": [],
        "key_rtl": [],
        "facts": module["facts"],
        "source_evidence": module["source_links"],
        "related_reading": [],
        "unknowns": [],
        "support_metadata": {
            "support_contract": ["Slang Fact + RTL Source", "RTL Source Evidence"],
            "maturity_contract": ["NOT_BUILT", "AI Draft / UNREVIEWED"],
            "unsupported_factual_semantic": "REJECTED",
        },
    }


def _semantic_supports(
    model: dict[str, Any], payload: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    catalog = payload.get("supports")
    if not isinstance(catalog, dict):
        raise ValueError("semantic validation payload requires a support catalog")
    sources = {item["qualified_name"]: item for item in model["sources"]}
    fact_ids = {item["fact_id"] for item in model["facts"]}
    actuals = {item["qualified_name"]: item for item in model["actuals"]}
    result: dict[str, dict[str, Any]] = {}
    for support_id, raw in catalog.items():
        if not isinstance(raw, dict):
            raise ValueError(f"support {support_id!r} must be an object")
        kind = raw.get("kind")
        if kind == "RTL_SOURCE":
            path = raw.get("path")
            fragment = raw.get("fragment")
            source = sources.get(path)
            if source is None or not source["valid"] or not str(fragment).startswith("L"):
                raise ValueError(f"support {support_id!r} has unavailable RTL source")
            result[support_id] = {
                "support_id": support_id,
                "kind": kind,
                "path": path,
                "fragment": fragment,
                "source_entity_id": source["entity_id"],
                "sha256": source["expected_sha256"],
            }
        elif kind == "COMPILER_FACT":
            fact_id = raw.get("fact_id")
            metric = raw.get("metric")
            if fact_id is not None and fact_id not in fact_ids:
                raise ValueError(f"support {support_id!r} references an unknown compiler fact")
            if fact_id is None:
                actual = actuals.get(raw.get("actual"))
                expected = raw.get("expected_count")
                if actual is None or not isinstance(expected, int):
                    raise ValueError(f"support {support_id!r} has no verifiable compiler fact identity")
                if metric == "effective_parameters":
                    observed = len(actual["attrs"].get("effective_parameters", []))
                    if actual["attrs"].get("parameters_truncated", False):
                        raise ValueError(f"support {support_id!r} uses truncated parameters")
                elif metric == "direct_bindings":
                    observed = len(actual["bindings"])
                else:
                    raise ValueError(f"support {support_id!r} has unsupported Build1 metric")
                if observed != expected:
                    raise ValueError(f"support {support_id!r} Build1 metric does not match candidate")
            result[support_id] = {
                "support_id": support_id,
                "kind": kind,
                "fact_id": fact_id,
                "predicate": raw.get("predicate"),
                "detail": raw.get("detail"),
                "source": raw.get("source"),
            }
        else:
            raise ValueError(f"support {support_id!r} has unsupported kind")
    return result


def _semantic_item(
    raw: dict[str, Any], support_catalog: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    if raw.get("status") != "DRAFT_UNREVIEWED":
        raise ValueError("semantic validation item maturity must be DRAFT_UNREVIEWED")
    identifiers = raw.get("support")
    if not isinstance(identifiers, list) or not identifiers:
        raise ValueError("factual semantic item requires support")
    try:
        supports = [support_catalog[item] for item in identifiers]
    except (KeyError, TypeError) as error:
        raise ValueError("factual semantic item references unknown support") from error
    support_type = raw.get("support_type")
    kinds = {item["kind"] for item in supports}
    if support_type == "SOURCE_ONLY":
        if "RTL_SOURCE" not in kinds:
            raise ValueError("RTL Source Evidence support is missing")
        label = "RTL Source Evidence"
    elif support_type == "COMPILER_FACT_AND_SOURCE":
        if not {"COMPILER_FACT", "RTL_SOURCE"}.issubset(kinds):
            raise ValueError("Slang Fact + RTL Source support is incomplete")
        label = "Slang Fact + RTL Source"
    else:
        raise ValueError("semantic validation item has unsupported support_type")
    item = json.loads(_canonical(raw))
    item["maturity_label"] = "AI Draft / UNREVIEWED"
    item["support_label"] = label
    item["support_details"] = supports
    return item


def apply_module_page_v2_validation(
    model: dict[str, Any],
    payload: dict[str, Any],
    *,
    architecture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach a strictly unreviewed, transient validation overlay to one Module PageView.

    This never writes the Knowledge Store or the canonical structured-detail document.
    """
    if payload.get("schema") != "rtl-analysis-agent/manual-v2-module-validation-v1":
        raise ValueError("unknown Module Page V2 validation schema")
    if payload.get("publication_status") != "DRAFT_UNREVIEWED":
        raise ValueError("validation overlay must remain DRAFT_UNREVIEWED")
    subject = payload.get("subject")
    definition = subject.get("definition") if isinstance(subject, dict) else None
    matches = [item for item in model["modules"] if item["qualified_name"] == definition]
    if len(matches) != 1:
        raise ValueError("validation overlay subject must match exactly one Module Definition")
    module = matches[0]
    view = module["module_page_v2"]
    supports = _semantic_supports(model, payload)
    section_names = (
        "overview", "responsibilities", "key_interfaces", "important_flows"
    )
    for name in section_names:
        rows = payload.get(name, [])
        if not isinstance(rows, list):
            raise ValueError(f"semantic section {name} must be a list")
        view[name] = [_semantic_item(item, supports) for item in rows]
    control_rows = payload.get("key_data_structures", [])
    if not isinstance(control_rows, list):
        raise ValueError("key_data_structures must be a list")
    view["control_summary"] = [_semantic_item(item, supports) for item in control_rows]
    control = payload.get("control")
    if control:
        view["control_summary"].append(_semantic_item(control, supports))
    key_rtl: list[dict[str, Any]] = []
    sources = {item["qualified_name"]: item for item in model["sources"]}
    for raw in payload.get("key_rtl", []):
        source = sources.get(raw.get("path"))
        if source is None or not source["valid"] or not raw.get("fragment") or not raw.get("why"):
            raise ValueError("Key RTL requires current source path, fragment, and rationale")
        key_rtl.append({
            **json.loads(_canonical(raw)),
            "maturity_label": "AI Draft / UNREVIEWED",
            "support_label": "RTL Source Evidence",
            "source_entity_id": source["entity_id"],
            "sha256": source["expected_sha256"],
        })
    view["key_rtl"] = key_rtl
    view["unknowns"] = [str(item) for item in payload.get("unknowns", [])]
    view["semantic_maturity"] = "AI Draft / UNREVIEWED"

    role_by_actual = {
        item.get("actual"): _semantic_item(item, supports)
        for item in payload.get("direct_submodules", [])
    }
    for item in view["direct_submodules"]:
        if item["name"] in role_by_actual:
            item["semantic"] = role_by_actual[item["name"]]
    view["direct_submodules_preview"] = view["direct_submodules"][:PREVIEW_LIMITS["direct_submodules"]]

    if architecture is not None:
        diagram = architecture.get("model")
        supplied = Path(architecture.get("svg_path", ""))
        if (
            not isinstance(diagram, dict)
            or diagram.get("schema") != "rtl-analysis-agent/diagram-view-model-prototype-v1"
            or diagram.get("subject") != definition
        ):
            raise ValueError("architecture subject must match Module Definition")
        if diagram.get("status") != "DRAFT_UNREVIEWED":
            raise ValueError("architecture maturity must be DRAFT_UNREVIEWED")
        for edge in diagram.get("edges", []):
            if not edge.get("support"):
                raise ValueError("architecture edge requires support")
            if edge.get("type") not in {"STRUCTURAL", "SEMANTIC_COLLABORATION"}:
                raise ValueError("architecture edge type is outside the V2 validation boundary")
            if edge.get("type") == "STRUCTURAL" and edge.get("status") != "COMPILER_FACT":
                raise ValueError("structural architecture edge must be a compiler fact")
            if edge.get("type") == "SEMANTIC_COLLABORATION" and edge.get("status") != "DRAFT_UNREVIEWED":
                raise ValueError("semantic architecture edge must remain unreviewed")
        if supplied.is_symlink() or not supplied.is_file():
            raise ValueError("architecture SVG must be a regular non-symlink file")
        expected = architecture.get("sha256")
        if not expected or _sha256(supplied) != expected:
            raise ValueError("architecture SVG identity mismatch")
        digest = module["entity_id"].split(":", 1)[-1]
        asset_path = f"assets/diagrams/{_slug(definition)}--{digest}.svg"
        model.setdefault("manual_assets", []).append({
            "source": supplied.resolve(), "output_path": asset_path, "sha256": expected,
        })
        view["architecture"] = {
            "title": diagram.get("title") or f"{definition} architecture",
            "asset_path": asset_path,
            "alt": architecture.get("alt") or f"{definition} architecture diagram",
            "node_count": len(diagram.get("nodes", [])),
            "edge_count": len(diagram.get("edges", [])),
            "legend": diagram.get("legend", {}),
            "maturity_label": "AI Draft / UNREVIEWED",
        }
        module_names = {item["qualified_name"]: item for item in model["modules"]}
        related = []
        for node in diagram.get("nodes", []):
            candidate = module_names.get(node.get("label"))
            if candidate is not None and candidate["entity_id"] != module["entity_id"]:
                related.append({
                    "name": candidate["qualified_name"],
                    "page_path": candidate["output_path"],
                    "reason": node.get("role") or "Architecture collaborator",
                })
        view["related_reading"] = sorted(related, key=lambda item: item["name"])

    model["page_types"][0]["reference_entity_id"] = module["entity_id"]
    model["page_types"][0]["reference_path"] = module["output_path"]
    model["page_types"][0]["reference_name"] = module["qualified_name"]
    return module


def _row_json(row: Any, field: str = "attrs_json") -> dict[str, Any]:
    value = json.loads(row[field])
    if not isinstance(value, dict):
        raise ValueError(f"stored {field} is not an object")
    return value


def load_backbone(
    database: Path,
    source_root: Path,
    *,
    strict_sources: bool = True,
    _store: PrototypeStore | None = None,
) -> dict[str, Any]:
    """Load and validate the Build1 PageView from one immutable candidate."""
    supplied_database = Path(database)
    if supplied_database.is_symlink():
        raise ValueError("candidate database must be a regular non-symlink file")
    database = supplied_database.resolve(strict=True)
    if not database.is_file():
        raise ValueError("candidate database must be a regular non-symlink file")
    source_root = Path(source_root).resolve(strict=True)
    owned_store = _store is None
    store = PrototypeStore.open(database, readonly=True) if owned_store else _store
    if store.database != database or store.connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise ValueError("backbone caller Store must be readonly for this candidate")
    if not owned_store and store.connection.in_transaction:
        # Establish the SQLite read snapshot before hashing the candidate file.
        # The durable DELETE-mode writer cannot commit while this snapshot lives.
        store.connection.execute("SELECT 1 FROM model_metadata LIMIT 1").fetchone()
    candidate_sha256 = _sha256(database)
    try:
        scope = store.analysis_scope()
        integrity = store.integrity()
        if integrity["integrity_check"] != "ok" or integrity["foreign_key_check"]:
            raise ValueError("candidate integrity or foreign keys failed")
        metadata = {
            row["key"]: json.loads(row["value_json"])
            for row in store.connection.execute(
                "SELECT key, value_json FROM model_metadata ORDER BY key"
            )
        }
        instance_kind = store.contract_id("entities", "INSTANCE")
        module_kind = store.contract_id("entities", "MODULE")
        source_kind = store.contract_id("entities", "SOURCE_FILE")
        port_kind = store.contract_id("entities", "PORT")
        contains = store.contract_id("facts", "CONTAINS")
        instance_of = store.contract_id("facts", "INSTANCE_OF")
        binds_to = store.contract_id("facts", "BINDS_TO")

        # PageView deliberately loads page identities, not the entire Store graph.
        entities = {
            row["entity_id"]: {
                "entity_id": row["entity_id"],
                "kind": row["kind"],
                "qualified_name": row["qualified_name"],
                "attrs": _row_json(row),
            }
            for row in store.connection.execute(
                "SELECT * FROM entity WHERE kind IN (?, ?, ?) ORDER BY entity_id",
                (instance_kind, module_kind, source_kind),
            )
        }
        facts = [
            {
                "fact_id": row["fact_id"],
                "subject_id": row["subject_id"],
                "predicate": row["predicate"],
                "object_id": row["object_id"],
                "producer": row["producer"],
                "attrs": _row_json(row),
            }
            for row in store.connection.execute(
                "SELECT f.* FROM fact f JOIN entity o ON o.entity_id=f.object_id "
                "WHERE f.predicate=? OR (f.predicate=? AND o.kind=?) ORDER BY f.fact_id",
                (instance_of, contains, instance_kind),
            )
        ]

        source_paths = scope["boundary_policy"]["knowledge_source_paths"]
        source_entities_by_path = {
            item["qualified_name"]: item
            for item in entities.values()
            if item["kind"] == source_kind
        }
        sources: list[dict[str, Any]] = []
        source_by_path: dict[str, dict[str, Any]] = {}
        for stored_path in source_paths:
            item = source_entities_by_path.get(stored_path)
            if item is None:
                raise SourceSafetyError(f"scope source is not a SOURCE_FILE: {stored_path}")
            resolved = _safe_source_path(source_root, stored_path)
            expected = item["attrs"].get("sha256")
            if not isinstance(expected, str) or len(expected) != 64:
                raise SourceSafetyError(f"SOURCE_FILE lacks authoritative SHA-256: {stored_path}")
            raw = resolved.read_bytes() if resolved.is_file() else None
            actual = hashlib.sha256(raw).hexdigest() if raw is not None else None
            valid = actual == expected
            source = {
                **item,
                "resolved_path": resolved,
                "expected_sha256": expected,
                "actual_sha256": actual,
                "valid": valid,
                "validated_bytes": raw if valid else None,
                "status": "AVAILABLE" if valid else ("STALE" if actual else "SOURCE_UNAVAILABLE"),
                "output_path": f"source/{stored_path}.html",
            }
            sources.append(source)
            source_by_path[stored_path] = source
        if strict_sources and any(not source["valid"] for source in sources):
            failed = [source["qualified_name"] for source in sources if not source["valid"]]
            raise SourceSafetyError(f"source hash/availability validation failed: {failed}")

        evidence_catalog = {
            row["evidence_id"]: {
                "evidence_id": row["evidence_id"],
                "kind": row["kind"],
                "locator": row["locator"],
                "start_line": row["start_line"],
                "end_line": row["end_line"],
                "attrs": _row_json(row),
            }
            for row in store.connection.execute(
                "SELECT * FROM evidence ORDER BY evidence_id"
            )
        }

        evidence_detail_cache: dict[str, dict[str, Any]] = {}

        def evidence_detail(item: dict[str, Any]) -> dict[str, Any]:
            cached = evidence_detail_cache.get(item["evidence_id"])
            if cached is not None:
                return cached
            detail: dict[str, Any] = {
                "evidence_id": item["evidence_id"],
                "kind": item["kind"],
                "path": item["locator"],
                "line_start": item["start_line"],
                "line_end": item["end_line"],
            }
            if item["kind"] != "source":
                detail["status"] = "AVAILABLE"
                evidence_detail_cache[item["evidence_id"]] = detail
                return detail
            source = source_by_path.get(item["locator"])
            if source is None:
                raise SourceSafetyError(
                    f"published source evidence is outside Knowledge Scope: {item['locator']}"
                )
            valid = (
                source["valid"]
                and item["attrs"].get("sha256") == source["expected_sha256"]
                and item["start_line"] is not None
                and item["end_line"] is not None
                and item["start_line"] >= 1
                and item["end_line"] >= item["start_line"]
            )
            if strict_sources and not valid:
                raise SourceSafetyError(f"stale source evidence: {item['evidence_id']}")
            detail.update({
                "sha256": source["expected_sha256"],
                "current_sha256": source["actual_sha256"],
                "status": "AVAILABLE" if valid else "STALE",
                "source_entity_id": source["entity_id"],
                "fragment": f"L{item['start_line']}-L{item['end_line']}",
            })
            evidence_detail_cache[item["evidence_id"]] = detail
            return detail

        children: dict[str, list[str]] = defaultdict(list)
        definition_by_instance: dict[str, str] = {}
        for fact in facts:
            if fact["predicate"] == contains and entities[fact["object_id"]]["kind"] == instance_kind:
                children[fact["subject_id"]].append(fact["object_id"])
            elif fact["predicate"] == instance_of:
                if fact["subject_id"] in definition_by_instance:
                    raise ValueError("instance has more than one module definition")
                if entities[fact["object_id"]]["kind"] != module_kind:
                    raise ValueError("INSTANCE_OF object is not a module definition")
                definition_by_instance[fact["subject_id"]] = fact["object_id"]
        for values in children.values():
            values.sort(key=lambda identifier: entities[identifier]["qualified_name"])

        roots = scope["roots"]
        actual_ids: set[str] = set()
        parent_by_actual: dict[str, str | None] = {}
        max_depth = scope["boundary_policy"]["max_depth"]
        queue: deque[tuple[str, int]] = deque()
        for root in roots:
            if root["kind"] == instance_kind:
                actual_ids.add(root["entity_id"])
                parent_by_actual[root["entity_id"]] = None
                queue.append((root["entity_id"], 0))
        while queue:
            parent_id, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for child_id in children.get(parent_id, []):
                if child_id in actual_ids and parent_by_actual.get(child_id) != parent_id:
                    raise ValueError("actual instance has ambiguous in-scope parentage")
                if child_id not in actual_ids:
                    actual_ids.add(child_id)
                    parent_by_actual[child_id] = parent_id
                    queue.append((child_id, depth + 1))

        module_ids = {
            root["entity_id"] for root in roots if root["kind"] == module_kind
        }
        for actual_id in actual_ids:
            definition_id = definition_by_instance.get(actual_id)
            if definition_id is None:
                raise ValueError(f"instance lacks INSTANCE_OF: {entities[actual_id]['qualified_name']}")
            module_ids.add(definition_id)
        if scope["scope_mode"] == "SOURCE_ONLY":
            module_ids.update(
                item["entity_id"]
                for item in entities.values()
                if item["kind"] == module_kind and item["attrs"].get("source") in source_by_path
            )

        page_map: dict[str, str] = {"scope": "index.html"}
        for entity_id in sorted(actual_ids | module_ids):
            entity = entities[entity_id]
            page_map[entity_id] = _object_path(
                entity["kind"], entity["qualified_name"], entity_id
            )
        for source in sources:
            page_map[source["entity_id"]] = source["output_path"]

        selected_endpoint_ids = actual_ids | module_ids
        facts_by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
        source_links_by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
        seen_links: dict[str, set[str]] = defaultdict(set)
        evidence_by_fact: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in store.connection.execute(
            "SELECT fe.fact_id, e.* FROM fact_evidence fe "
            "JOIN evidence e ON e.evidence_id=fe.evidence_id "
            "JOIN fact f ON f.fact_id=fe.fact_id "
            "JOIN entity o ON o.entity_id=f.object_id "
            "WHERE f.predicate=? OR (f.predicate=? AND o.kind=?) "
            "ORDER BY fe.fact_id, e.evidence_id",
            (instance_of, contains, instance_kind),
        ):
            evidence_by_fact[row["fact_id"]].append({
                "evidence_id": row["evidence_id"],
                "kind": row["kind"],
                "locator": row["locator"],
                "start_line": row["start_line"],
                "end_line": row["end_line"],
                "attrs": _row_json(row),
            })
        for fact in facts:
            selected_endpoints = {fact["subject_id"], fact["object_id"]} & selected_endpoint_ids
            fact_is_in_scope = (
                fact["subject_id"] in selected_endpoint_ids
                and fact["object_id"] in selected_endpoint_ids
            )
            evidence_endpoints = selected_endpoints if fact_is_in_scope else set()
            for endpoint in sorted(evidence_endpoints):
                if fact_is_in_scope:
                    facts_by_entity[endpoint].append(fact)
                for item in evidence_by_fact[fact["fact_id"]]:
                    if item["kind"] != "source":
                        continue
                    source = source_by_path.get(item["locator"])
                    if source is None:
                        raise SourceSafetyError(
                            f"published structural evidence is outside Knowledge Scope: {item['locator']}"
                        )
                    valid = source["valid"] and item["attrs"].get("sha256") == source["expected_sha256"]
                    if strict_sources and not valid:
                        raise SourceSafetyError(f"stale source evidence: {item['evidence_id']}")
                    fragment = (
                        f"L{item['start_line']}-L{item['end_line']}"
                        if item["start_line"] is not None else None
                    )
                    key = item["evidence_id"]
                    if key not in seen_links[endpoint]:
                        source_links_by_entity[endpoint].append({
                            "evidence_id": item["evidence_id"],
                            "fact_id": None,
                            "path": item["locator"],
                            "fragment": fragment,
                            "line_start": item["start_line"],
                            "line_end": item["end_line"],
                            "sha256": source["expected_sha256"],
                            "current_sha256": source["actual_sha256"],
                            "source_entity_id": source["entity_id"],
                            "predicate": fact["predicate"],
                            "status": "AVAILABLE" if valid else "STALE",
                        })
                        seen_links[endpoint].add(key)

        for module_id in module_ids:
            declared_source = entities[module_id]["attrs"].get("source")
            source = source_by_path.get(declared_source)
            if source is not None and not source_links_by_entity[module_id]:
                source_links_by_entity[module_id].append({
                    "evidence_id": None,
                    "fact_id": None,
                    "path": declared_source,
                    "fragment": None,
                    "line_start": None,
                    "line_end": None,
                    "sha256": source["expected_sha256"],
                    "current_sha256": source["actual_sha256"],
                    "source_entity_id": source["entity_id"],
                    "predicate": "DECLARED_IN",
                    "status": source["status"],
                })

        bindings_by_instance: dict[str, list[dict[str, Any]]] = defaultdict(list)
        seen_bindings: set[str] = set()
        binding_query = (
            "SELECT owner.subject_id AS instance_id, p.entity_id AS port_id, "
            "p.qualified_name AS port_name, "
            "p.attrs_json AS port_attrs_json, binding.fact_id AS binding_fact_id, "
            "binding.attrs_json AS binding_attrs_json, target.entity_id AS target_id, "
            "target.kind AS target_kind, target.qualified_name AS target_name, "
            "target.attrs_json AS target_attrs_json, "
            "e.evidence_id, e.kind AS evidence_kind, e.locator, e.start_line, e.end_line, "
            "e.attrs_json AS evidence_attrs_json "
            "FROM fact owner JOIN entity p ON p.entity_id=owner.object_id "
            "JOIN fact binding ON binding.subject_id=p.entity_id AND binding.predicate=? "
            "JOIN entity target ON target.entity_id=binding.object_id "
            "JOIN fact_evidence fe ON fe.fact_id=binding.fact_id "
            "JOIN evidence e ON e.evidence_id=fe.evidence_id AND e.kind='source' "
            "WHERE owner.predicate=? AND p.kind=? "
            "ORDER BY owner.subject_id, p.qualified_name, binding.fact_id, e.evidence_id"
        )
        for row in store.connection.execute(binding_query, (binds_to, contains, port_kind)):
            instance_id = row["instance_id"]
            if instance_id not in actual_ids or row["binding_fact_id"] in seen_bindings:
                continue
            seen_bindings.add(row["binding_fact_id"])
            port_attrs = json.loads(row["port_attrs_json"])
            binding_attrs = json.loads(row["binding_attrs_json"])
            target_attrs = json.loads(row["target_attrs_json"])
            target_expression = target_attrs.get("expression")
            target_display = (
                target_expression.get("text")
                if isinstance(target_expression, dict)
                and isinstance(target_expression.get("text"), str)
                else row["target_name"]
            )
            evidence_link = None
            if row["evidence_id"] is not None:
                source = source_by_path.get(row["locator"])
                evidence_attrs = json.loads(row["evidence_attrs_json"])
                if source is None:
                    raise SourceSafetyError(
                        f"published binding evidence is outside Knowledge Scope: {row['locator']}"
                    )
                valid = source["valid"] and evidence_attrs.get("sha256") == source["expected_sha256"]
                if strict_sources and not valid:
                    raise SourceSafetyError(f"stale binding evidence: {row['evidence_id']}")
                evidence_link = {
                    "evidence_id": row["evidence_id"],
                    "path": row["locator"],
                    "line_start": row["start_line"],
                    "line_end": row["end_line"],
                    "fragment": (
                        f"L{row['start_line']}-L{row['end_line']}"
                        if row["start_line"] is not None else None
                    ),
                    "source_entity_id": source["entity_id"],
                    "sha256": source["expected_sha256"],
                    "current_sha256": source["actual_sha256"],
                    "status": "AVAILABLE" if valid else "STALE",
                }
            bindings_by_instance[instance_id].append({
                "fact_id": row["binding_fact_id"],
                "port_entity_id": row["port_id"],
                "formal": row["port_name"].rsplit(".", 1)[-1],
                "direction": port_attrs.get("direction", "NOT_AVAILABLE"),
                "target": target_display,
                "target_entity_id": row["target_id"],
                "target_kind": row["target_kind"],
                "target_identity": row["target_name"],
                "attrs": binding_attrs,
                "evidence": evidence_link,
            })
        for rows in bindings_by_instance.values():
            rows.sort(key=lambda item: item["formal"])

        fact_detail_cache: dict[str, dict[str, Any]] = {}

        def fact_detail(fact: dict[str, Any]) -> dict[str, Any]:
            cached = fact_detail_cache.get(fact["fact_id"])
            if cached is not None:
                return cached
            detail = {
                "fact_id": fact["fact_id"],
                "predicate": fact["predicate"],
                "subject_id": fact["subject_id"],
                "object_id": fact["object_id"],
                "producer": fact["producer"],
                "attrs": fact["attrs"],
                "evidence_ids": [
                    item["evidence_id"]
                    for item in evidence_by_fact.get(fact["fact_id"], [])
                ],
            }
            fact_detail_cache[fact["fact_id"]] = detail
            return detail

        def preview_fact_detail(fact: dict[str, Any]) -> dict[str, Any]:
            return {
                **fact_detail(fact),
                "evidence": [
                    evidence_detail(item)
                    for item in evidence_by_fact.get(fact["fact_id"], [])
                    if item["kind"] == "source"
                ],
            }

        def referenced_parameter_evidence(
            parameters: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            identifiers: set[str] = set()
            for parameter in parameters:
                for identifier in (
                    parameter.get("declaration_evidence_id"),
                    (parameter.get("default") or {}).get("evidence_id"),
                    (parameter.get("override") or {}).get("evidence_id"),
                    parameter.get("elaboration_evidence_id"),
                ):
                    if identifier:
                        identifiers.add(identifier)
            missing = {item for item in identifiers if item not in evidence_catalog}
            if missing:
                raise SourceSafetyError(
                    f"parameter evidence is missing from candidate: {sorted(missing)}"
                )
            return [evidence_detail(evidence_catalog[item]) for item in sorted(identifiers)]

        actuals: list[dict[str, Any]] = []
        for entity_id in sorted(actual_ids, key=lambda item: entities[item]["qualified_name"]):
            entity = entities[entity_id]
            actuals.append({
                **entity,
                "output_path": page_map[entity_id],
                "definition_id": definition_by_instance[entity_id],
                "parent_id": parent_by_actual.get(entity_id),
                "children": [child for child in children.get(entity_id, []) if child in actual_ids],
                "facts": sorted(facts_by_entity[entity_id], key=lambda item: item["fact_id"]),
                "source_links": sorted(
                    source_links_by_entity[entity_id],
                    key=lambda item: (item["path"], item["fragment"] or "", item["predicate"]),
                ),
                "bindings": bindings_by_instance[entity_id],
                "structured_path": _structured_path(
                    entity["kind"], entity["qualified_name"], entity_id
                ),
            })
        actual_by_id = {item["entity_id"]: item for item in actuals}

        for actual in actuals:
            parameters = actual["attrs"].get("effective_parameters", [])
            child_references = [
                _entity_reference(item, entities, page_map) for item in actual["children"]
            ]
            fact_rows = [fact_detail(item) for item in actual["facts"]]
            source_evidence = actual["source_links"]
            actual["structured_detail"] = {
                "schema": STRUCTURED_DETAIL_SCHEMA,
                "page_kind": "ACTUAL_INSTANCE",
                "scope_id": scope["scope_id"],
                "object": _entity_reference(actual["entity_id"], entities, page_map),
                "definition": _entity_reference(
                    actual["definition_id"], entities, page_map
                ),
                "parent": _entity_reference(actual["parent_id"], entities, page_map),
                "children": child_references,
                "effective_parameters": parameters,
                "parameters_truncated": bool(
                    actual["attrs"].get("parameters_truncated", False)
                ),
                "parameter_evidence": referenced_parameter_evidence(parameters),
                "bindings": actual["bindings"],
                "deterministic_facts": fact_rows,
                "source_evidence": source_evidence,
                "structural_neighborhood": {
                    "contract": "Direct compiler facts only; no bit equivalence, transitive connectivity, or engineering semantics.",
                    "parent_count": 1 if actual["parent_id"] else 0,
                    "child_count": len(actual["children"]),
                    "instance_of_count": 1,
                    "direct_binding_count": len(actual["bindings"]),
                },
                "semantic": "NOT_BUILT",
            }
            actual["preview"] = {
                "children": child_references[:PREVIEW_LIMITS["children"]],
                "parameters": sorted(
                    parameters,
                    key=lambda item: (
                        not bool(item.get("overridden")), str(item.get("name", ""))
                    ),
                )[:PREVIEW_LIMITS["parameters"]],
                "bindings": actual["bindings"][:PREVIEW_LIMITS["bindings"]],
                "facts": [
                    preview_fact_detail(item)
                    for item in actual["facts"][:PREVIEW_LIMITS["facts"]]
                ],
                "evidence": source_evidence[:PREVIEW_LIMITS["evidence"]],
            }

        instances_by_definition: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for actual in actuals:
            instances_by_definition[actual["definition_id"]].append(actual)
        modules: list[dict[str, Any]] = []
        for entity_id in sorted(module_ids, key=lambda item: entities[item]["qualified_name"]):
            entity = entities[entity_id]
            known_instances = instances_by_definition[entity_id]
            module = {
                **entity,
                "output_path": page_map[entity_id],
                "known_instances": known_instances,
                "facts": sorted(facts_by_entity[entity_id], key=lambda item: item["fact_id"]),
                "source_links": sorted(
                    source_links_by_entity[entity_id],
                    key=lambda item: (item["path"], item["fragment"] or "", item["predicate"]),
                ),
                "reuse_limitation": (
                    "Definition-level facts are reusable only where evidence proves applicability; "
                    "actual configuration remains instance-specific."
                ),
                "structured_path": _structured_path(
                    entity["kind"], entity["qualified_name"], entity_id
                ),
            }
            instance_references = [
                _entity_reference(item["entity_id"], entities, page_map)
                for item in known_instances
            ]
            fact_rows = [fact_detail(item) for item in module["facts"]]
            module["structured_detail"] = {
                "schema": STRUCTURED_DETAIL_SCHEMA,
                "page_kind": "MODULE_DEFINITION",
                "scope_id": scope["scope_id"],
                "object": _entity_reference(entity_id, entities, page_map),
                "declared_source": entity["attrs"].get("source"),
                "known_instances": instance_references,
                "deterministic_facts": fact_rows,
                "source_evidence": module["source_links"],
                "reuse_limitation": module["reuse_limitation"],
                "semantic": "NOT_BUILT",
            }
            module["preview"] = {
                "known_instances": instance_references[
                    :PREVIEW_LIMITS["known_instances"]
                ],
                "facts": [
                    preview_fact_detail(item)
                    for item in module["facts"][:PREVIEW_LIMITS["facts"]]
                ],
                "evidence": module["source_links"][:PREVIEW_LIMITS["evidence"]],
            }
            module["module_page_v2"] = _base_module_page_v2(
                module, known_instances, actual_by_id, entities, page_map
            )
            modules.append(module)

        module_reference = modules[0] if modules else None
        page_types = [{
            "page_type": "MODULE_DEFINITION",
            "label": MODULE_DEFINITION_TEMPLATE_VERSION,
            "sections": [
                "Overview", "Responsibilities", "Architecture", "Direct Submodules",
                "Key Interfaces", "Control", "Important Flows", "Key RTL",
                "Compiler Facts", "RTL Source Evidence", "Related Reading",
            ],
            "support_rule": "Factual semantic requires Slang Fact + RTL Source or RTL Source Evidence.",
            "maturity_rule": "Support and semantic review maturity are independent.",
            "reference_entity_id": module_reference["entity_id"] if module_reference else None,
            "reference_path": module_reference["output_path"] if module_reference else None,
            "reference_name": module_reference["qualified_name"] if module_reference else None,
        }]

        nodes: list[dict[str, Any]] = []
        for actual in actuals:
            nodes.append({
                "id": actual["entity_id"], "kind": "ACTUAL_INSTANCE",
                "title": actual["qualified_name"], "path": actual["output_path"],
                "parent_id": actual["parent_id"], "children": actual["children"],
            })
        for module in modules:
            nodes.append({
                "id": module["entity_id"], "kind": "MODULE_DEFINITION",
                "title": module["qualified_name"], "path": module["output_path"],
                "parent_id": None, "children": [],
            })
        for source in sources:
            nodes.append({
                "id": source["entity_id"], "kind": "SOURCE_FILE",
                "title": source["qualified_name"], "path": source["output_path"],
                "parent_id": None, "children": [],
            })
        root_ids = [root["entity_id"] for root in roots]
        tree = {
            "root_ids": root_ids,
            "nodes": sorted(nodes, key=lambda item: (item["kind"], item["title"], item["id"])),
            "groups": {
                "Roots": root_ids,
                "Definitions": [item["entity_id"] for item in modules],
                "Sources": [item["entity_id"] for item in sources],
            },
        }
        structured_detail_map = {
            item["entity_id"]: item["structured_path"]
            for item in [*actuals, *modules]
        }
        return {
            "database": database,
            "candidate_sha256": candidate_sha256,
            "source_root": source_root,
            "metadata": metadata,
            "scope_presentation": _scope_presentation(scope, metadata, entities),
            "scope": scope,
            "entities": entities,
            "facts": facts,
            "actuals": actuals,
            "actual_by_id": actual_by_id,
            "modules": modules,
            "sources": sources,
            "tree": tree,
            "page_map": page_map,
            "structured_detail_map": structured_detail_map,
            "preview_limits": PREVIEW_LIMITS,
            "page_types": page_types,
            "manual_assets": [],
            "ai_count": store.connection.execute("SELECT count(*) FROM ai_knowledge").fetchone()[0],
            "behavior_count": 0,
        }
    finally:
        if owned_store:
            store.close()


def _formal_q2_sections(
    projection: dict[str, Any], actual_by_id: dict[str, dict[str, Any]],
    entity_names: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    """Derive the five established reading columns from one audited projection."""
    sections: dict[str, list[dict[str, Any]]] = {
        key: [] for key in ("overview", "responsibilities", "key_interfaces",
                          "control_summary", "important_flows")
    }
    if any(claim["structured_schema_id"].startswith("fifo.") for claim in projection["claims"]):
        sections["fifo"] = []
    coverage_by_claim: dict[str, list[dict[str, Any]]] = {}
    for coverage in projection["coverage"]:
        for ai_id in coverage["claim_ids"]:
            coverage_by_claim.setdefault(ai_id, []).append(coverage)
    shapes = {
        "module-role": "overview", "module-function": "responsibilities",
        "module-interface": "key_interfaces", "fsm-boundary": "control_summary",
        "flow-step": "important_flows",
    }
    for claim in projection["claims"]:
        shape = claim["structured_schema_id"].split(".")[-1].split("@")[0]
        column = "fifo" if claim["structured_schema_id"].startswith("fifo.") else shapes.get(shape)
        coverages = coverage_by_claim.get(claim["ai_id"], [])
        if column is None or not coverages:
            raise StoreError("formal Q2 claim has no readable column or coverage")
        fields = claim["structured_fields"]
        facts = claim["fact_support"]
        content = [link for link in facts if link["support_quality"] != "SCOPE_ONLY"]
        if not content:
            support_label = ("仅 SCOPE_ONLY 结构 Fact；无 content-level Fact；RTL Source 支持"
                             if facts else "无 content-level Fact；RTL Source 支持")
        else:
            support_label = "部分 content-level Fact + RTL Source" if any(
                link["support_quality"] == "PARTIAL" for link in content
            ) else "content-level Fact + RTL Source"
        statuses = {coverage["closure_status"] for coverage in coverages}
        if len(statuses) != 1:
            raise StoreError("formal Q2 claim has conflicting closure status")
        status = next(iter(statuses))
        item: dict[str, Any] = {
            "formal_q2": True, "ai_id": claim["ai_id"], "claim_key": claim["claim_key"],
            "text": claim["text"], "fields": fields,
            "inference_basis": fields["inference_basis"],
            "review": claim["review"], "applicability": claim["applicability"],
            "actual_names": {
                actual_id: actual_by_id[actual_id]["qualified_name"]
                for coverage in coverages
                for actual_id in coverage["applicability"].get("instance_id", [])
            },
            "coverage": coverages, "closure_status": status,
            "limitations": claim["limitations"], "fact_support": facts,
            "source_evidence": claim["source_evidence"],
            "support_label": support_label, "maturity_label": "PUBLISHED / " + status,
            "support_details": [
                {"kind": "RTL_SOURCE", "path": source["locator"],
                 "fragment": source["fragment"], "anchor": source["anchor"],
                 "source_entity_id": source["source_entity_id"]}
                for source in claim["source_evidence"]
            ] + [
                {"kind": "FACT", "fact_id": fact["fact_id"],
                 "detail": fact["support_quality"]} for fact in facts
            ],
        }
        if shape == "module-role":
            item.update(location=fields["location"], role=fields["role"])
        elif shape == "module-function":
            item.update(function=fields["function"], condition=fields["condition"],
                        effect=fields["effect"], exceptions=fields["exceptions"])
        elif shape == "module-interface":
            item.update(group=fields["group"], why_key=fields["why_key"],
                        signals=claim["signal_ports"])
        elif shape == "fsm-boundary":
            item.update(name="控制状态边界", description=fields["scope_limit"],
                        state_names=fields["state_names"], reset_path=fields["reset_path"],
                        scope_limit=fields["scope_limit"],
                        related_subjects=[entity_names[ref] for ref in fields["related_subject_ids"]])
        elif shape == "flow-step":
            item.update(name=claim["claim_key"], summary=fields["step"],
                        entry=fields["entry"], condition=fields["condition"],
                        effect=fields["effect"], boundary=fields["boundary"],
                        participants=[entity_names[ref] for ref in fields["participants"]])
        elif column == "fifo":
            item.update(name=shape, field_rows=[{"name": key, "value": value}
                                                 for key, value in fields.items()])
        sections[column].append(item)
    return sections


def load_formal_q2(database: Path, source_root: Path, *, combination: bool = False) -> dict[str, Any]:
    """Load the approved complete Q2 bundle and Build1 backbone in one snapshot."""
    path = Q2_FIFO_V2_PUBLICATION_PATH if combination else MODULE_Q2_PUBLICATION_PATH
    contract, _, purpose = _load_publication_contract(path)
    if purpose != ("REAL_Q2_FIFO" if combination else "REAL_MODULE_Q2"):
        raise StoreError("formal Mapping requires the pinned real Profile")
    store = PrototypeStore.open(database, readonly=True)
    try:
        if store.connection.in_transaction:
            raise StoreError("formal Mapping requires an idle readonly Store")
        store.connection.execute("BEGIN")
        try:
            model = load_backbone(database, source_root, _store=store)
            bundle = store.audit_real_publication_bundle(contract)
            sources = {source["qualified_name"]: source for source in model["sources"]}
            actual_by_id = model["actual_by_id"]
            entity_names = {
                row["entity_id"]: row["qualified_name"]
                for row in store.connection.execute("SELECT entity_id,qualified_name FROM entity")
            }
            modules_by_id = {module["entity_id"]: module for module in model["modules"]}
            all_claims: dict[str, dict[str, Any]] = {}
            all_coverage: dict[str, dict[str, Any]] = {}
            for actual in model["actuals"]:
                selected = store._read_published_semantic_snapshot(
                    {"instance_id": actual["entity_id"]}, contract,
                )
                for item in selected["claims"]:
                    all_claims[item["ai_id"]] = item
                for item in selected["coverage"]:
                    all_coverage[item["coverage_id"]] = item
            if (sorted(all_claims) != bundle["expected_claim_ids"]
                or sorted(all_coverage) != bundle["expected_coverage_ids"]):
                raise StoreError("formal Q2 applicability does not select the complete approved bundle")
            claims_by_module: dict[str, dict[str, dict[str, Any]]] = {
                key: {} for key in modules_by_id
            }
            coverage_by_module: dict[str, dict[str, dict[str, Any]]] = {
                key: {} for key in modules_by_id
            }
            for item in all_coverage.values():
                if item["target_kind"] == "SUBJECT":
                    destinations = {item["target_subject_id"]}
                elif item["owner_subject_id"] in modules_by_id:
                    destinations = {item["owner_subject_id"]}
                elif item["owner_subject_id"] in actual_by_id:
                    destinations = {actual_by_id[item["owner_subject_id"]]["definition_id"]}
                else:
                    # Explicit scope-owned Behavior appears on each module whose
                    # actual instance is in the reviewed applicability slice.
                    destinations = {
                        actual_by_id[actual_id]["definition_id"]
                        for actual_id in item["applicability"].get("instance_id", [])
                        if actual_id in actual_by_id
                    }
                if not destinations or not destinations <= set(modules_by_id):
                    raise StoreError("formal Q2 coverage has no in-scope Module page")
                for module_id in destinations:
                    coverage_by_module[module_id][item["coverage_id"]] = item
                    for ai_id in item["claim_ids"]:
                        claims_by_module[module_id][ai_id] = all_claims[ai_id]
            for claim in all_claims.values():
                for evidence in claim["source_evidence"]:
                    source = sources.get(evidence["locator"])
                    attrs = evidence["attrs"]
                    if (source is None or not source["valid"]
                        or attrs.get("hash_kind") != "utf8-line-span-v1"
                        or attrs.get("file_sha256") != source["expected_sha256"]
                        or evidence["start_line"] is None or evidence["end_line"] is None):
                        raise SourceSafetyError("formal Q2 Source Evidence is stale or untyped")
                    lines = source["validated_bytes"].decode("utf-8").splitlines()
                    first, last = evidence["start_line"], evidence["end_line"]
                    if (first < 1 or last < first or last > len(lines)
                        or hashlib.sha256("\n".join(lines[first-1:last]).encode()).hexdigest() != attrs.get("sha256")):
                        raise SourceSafetyError("formal Q2 Source span hash is stale")
                    evidence["source_entity_id"] = source["entity_id"]
                    evidence["fragment"] = f"L{first}-L{last}"
                    evidence["anchor"] = f"L{first}"
                review_row = store.connection.execute(
                    "SELECT attrs_json FROM evidence WHERE evidence_id=?",
                    (claim["review_evidence_id"],),
                ).fetchone()
                claim["review"] = {
                    key: json.loads(review_row[0])[key]
                    for key in ("status", "reviewer", "input_sha256", "artifact_sha256")
                }
            module_projection: dict[str, dict[str, Any]] = {}
            for module in model["modules"]:
                claims = claims_by_module[module["entity_id"]]
                coverage = coverage_by_module[module["entity_id"]]
                projection = {
                    "schema": ("rtl-analysis-agent/q2-fifo-page-projection-v1" if combination
                               else "rtl-analysis-agent/module-q2-page-projection-v1"),
                    "module_id": module["entity_id"],
                    "claims": [claims[key] for key in sorted(claims)],
                    "coverage": [coverage[key] for key in sorted(coverage)],
                }
                projection["sections"] = _formal_q2_sections(projection, actual_by_id, entity_names)
                module_projection[module["entity_id"]] = projection
                module["formal_q2"] = projection
                module["structured_detail"]["semantic"] = projection
                for section_name, items in projection["sections"].items():
                    module["module_page_v2"][section_name] = items
                statuses = {item["closure_status"] for item in projection["coverage"]}
                module["module_page_v2"]["semantic_maturity"] = (
                    "PUBLISHED_PARTIAL" if "PARTIAL" in statuses else
                    "PUBLISHED_GAP" if "GAP" in statuses else
                    "PUBLISHED_READY" if statuses else "NOT_BUILT"
                )
            model["formal_q2"] = {
                "schema": ("rtl-analysis-agent/q2-fifo-mapping-v1" if combination
                           else "rtl-analysis-agent/module-q2-mapping-v1"),
                "input_sha256": bundle["input_sha256"],
                "review_sha256": bundle["review_sha256"],
                "approval_sha256": bundle["approval_sha256"],
                "goal_dispositions": bundle["goal_dispositions"],
                "modules": module_projection,
            }
            model["ai_count"] = len(bundle["expected_claim_ids"])
            model["behavior_count"] = len(bundle["expected_behavior_ids"])
            return model
        finally:
            store.connection.rollback()
    finally:
        store.close()


def build_site(
    model: dict[str, Any],
    output: Path,
    *,
    source_viewer: str = "full",
    manual_brand: str = "RTL Build1 Manual",
) -> dict[str, Any]:
    """Build a clean deterministic static site; never overwrite an existing candidate."""
    if source_viewer not in {"locator", "full"}:
        raise ValueError("source_viewer must be locator or full")
    # Keep the exact verified bytes until rendering; a loader-time check alone
    # cannot protect a later full-source page from a changed source file.
    source_bytes: dict[str, bytes] = {}
    if source_viewer == "full" or "formal_q2" in model:
        source_root = Path(model["source_root"]).resolve(strict=True)
        for source in model["sources"]:
            if not source["valid"]:
                continue
            path = Path(source["resolved_path"])
            if (path.is_symlink() or not path.is_file()
                or not path.resolve(strict=True).is_relative_to(source_root)):
                raise ValueError("Source changed or is unsafe before rendering")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != source["expected_sha256"]:
                raise ValueError("Source changed after backbone validation")
            if source_viewer == "full":
                source_bytes[source["entity_id"]] = raw
    validated_assets: list[tuple[Path, PurePosixPath]] = []
    seen_asset_paths: set[PurePosixPath] = set()
    for asset in model.get("manual_assets", []):
        source = Path(asset["source"])
        target_relative = PurePosixPath(asset["output_path"])
        if source.is_symlink() or not source.is_file() or _sha256(source) != asset["sha256"]:
            raise ValueError("Manual static asset changed or is unsafe")
        if (
            target_relative.is_absolute()
            or ".." in target_relative.parts
            or target_relative.parts[:2] != ("assets", "diagrams")
            or target_relative in seen_asset_paths
        ):
            raise ValueError("Manual static asset output path is unsafe")
        seen_asset_paths.add(target_relative)
        validated_assets.append((source, target_relative))
    output = Path(output).resolve()
    if output.exists() or output.is_symlink():
        raise ValueError("Manual output already exists")
    output.mkdir(parents=True)
    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATE_ROOT)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    # Presentation-only labels: never write translations into Knowledge/JSON.
    display_text = {
        "RTL Build1 Manual": "RTL Build1 结构手册",
        "RTL Build2 Draft Manual": "RTL Build2 草稿手册（未审核）",
        "RTL Q2 Manual": "RTL Q2 知识手册",
        "RTL Q2+FIFO Manual": "RTL Q2+FIFO 知识手册",
        "ACTUAL_INSTANCE": "实际实例（ACTUAL_INSTANCE）",
        "MODULE_DEFINITION · V2": "模块定义（MODULE_DEFINITION · V2）",
        "SOURCE_FILE": "源码文件（SOURCE_FILE）",
        "Module Definition V2": "模块定义 V2",
        "RTL Source Evidence": "RTL 源码证据",
        "Slang Fact + RTL Source": "Slang 事实与 RTL 源码",
        "AI Draft / UNREVIEWED": "AI 草稿未审核（UNREVIEWED）",
        "PUBLISHED / READY": "已发布（PUBLISHED / READY）",
        "PUBLISHED / PARTIAL": "已发布、支持不完整（PUBLISHED / PARTIAL）",
        "Definition-level facts are reusable only where evidence proves applicability; "
        "actual configuration remains instance-specific.":
            "仅在证据证明适用时复用定义级事实；实际配置须以各实例为准。",
        "Binding expressions retain bounded Runtime structure and do not assert bit equivalence.":
            "绑定表达式保留有界 Runtime 结构，不证明位级等价。",
        "Build closure inputs without published structural Evidence are not Knowledge Scope sources.":
            "编译闭包中未提供结构证据的输入文件不属于知识范围内的源码。",
        "No reliable elaborated root was resolved.": "未能解析出可确认的已展开根节点。",
    }
    environment.filters["display_text"] = lambda value: display_text.get(value, value)
    scope_display_label = _scope_presentation(
        model["scope"], model["metadata"], model["entities"], chinese=True
    )["label"]
    page_map = dict(model["page_map"])
    if source_viewer == "locator":
        for source in model["sources"]:
            page_map.pop(source["entity_id"], None)

    def page_context(current_path: str) -> dict[str, Any]:
        def href(target: str, fragment: str | None = None) -> str:
            relative = _relative_url(current_path, target)
            return relative + (f"#{fragment}" if fragment else "")

        return {
            "model": model,
            "scope_display_label": scope_display_label,
            "scope": model["scope"],
            "metadata": model["metadata"],
            "page_map": page_map,
            "source_viewer": source_viewer,
            "manual_brand": manual_brand,
            "current_path": current_path,
            "href": href,
            "home_href": href("index.html"),
            "asset_href": lambda path: href(f"assets/{path}"),
        }

    def render(template_name: str, relative: str, **values: Any) -> None:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        text = environment.get_template(template_name).render(**page_context(relative), **values)
        target.write_text(text, encoding="utf-8", newline="\n")

    template, template_role = PAGE_TEMPLATES["SCOPE_OVERVIEW"]
    render(
        template, "index.html", page_kind="SCOPE_OVERVIEW",
        page_template=template_role,
    )
    template, template_role = PAGE_TEMPLATES["ACTUAL_INSTANCE"]
    for actual in model["actuals"]:
        render(
            template, actual["output_path"], page_kind="ACTUAL_INSTANCE",
            page_template=template_role, page=actual,
        )
        target = output / actual["structured_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            _canonical(actual["structured_detail"]) + "\n", encoding="utf-8"
        )
    template, template_role = PAGE_TEMPLATES["MODULE_DEFINITION"]
    for module in model["modules"]:
        render(
            template, module["output_path"], page_kind="MODULE_DEFINITION",
            page_template=template_role, page=module,
        )
        target = output / module["structured_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            _canonical(module["structured_detail"]) + "\n", encoding="utf-8"
        )
    if source_viewer == "full":
        template, template_role = PAGE_TEMPLATES["SOURCE_FILE"]
        for source in model["sources"]:
            text = source_bytes.get(source["entity_id"], b"").decode("utf-8", errors="replace")
            render(
                template, source["output_path"], page_kind="SOURCE_FILE",
                page_template=template_role, page=source, lines=text.splitlines(),
            )

    shutil.copytree(ASSET_ROOT, output / "assets", dirs_exist_ok=False)
    for source, target_relative in validated_assets:
        target = output / Path(*target_relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    search_index = [
        {"id": item["entity_id"], "kind": "ACTUAL_INSTANCE", "title": item["qualified_name"], "path": item["output_path"]}
        for item in model["actuals"]
    ] + [
        {"id": item["entity_id"], "kind": "MODULE_DEFINITION", "title": item["qualified_name"], "path": item["output_path"]}
        for item in model["modules"]
    ] + [
        {
            "id": item["entity_id"], "kind": "SOURCE_FILE",
            "title": item["qualified_name"],
            "path": item["output_path"] if source_viewer == "full" else None,
        }
        for item in model["sources"]
    ]
    tree = json.loads(_canonical(model["tree"]))
    if source_viewer == "locator":
        for node in tree["nodes"]:
            if node["kind"] == "SOURCE_FILE":
                node["path"] = None
    search_index = sorted(
        search_index, key=lambda item: (item["kind"], item["title"], item["id"])
    )
    tree_json = _canonical(tree)
    search_json = _canonical(search_index)
    (output / "assets/tree.json").write_text(tree_json + "\n", encoding="utf-8")
    (output / "assets/search-index.json").write_text(
        search_json + "\n",
        encoding="utf-8",
    )
    (output / "assets/tree-data.js").write_text(
        f"window.RTL_MANUAL_TREE={tree_json};\n", encoding="utf-8"
    )
    (output / "assets/search-data.js").write_text(
        f"window.RTL_MANUAL_SEARCH={search_json};\n", encoding="utf-8"
    )
    counts = {
        "scope": 1,
        "instance": len(model["actuals"]),
        "module_definition": len(model["modules"]),
        "source": len(model["sources"]) if source_viewer == "full" else 0,
        "container": 0,
    }
    manifest = {
        "schema": "rtl-analysis-agent/manual-v6-build1-manifest-v1",
        "scope_id": model["scope"]["scope_id"],
        "build_sha256": model["scope"]["build_identity"]["build_sha256"],
        "candidate_sha256": model["candidate_sha256"],
        "page_counts": counts,
        "resource_counts": {"source": len(model["sources"])},
        "structured_detail_schema": STRUCTURED_DETAIL_SCHEMA,
        "structured_detail_counts": {
            "instance": len(model["actuals"]),
            "module_definition": len(model["modules"]),
        },
        "structured_detail_map": {
            key: model["structured_detail_map"][key]
            for key in sorted(model["structured_detail_map"])
        },
        "source_serving": source_viewer,
        "page_map": {key: page_map[key] for key in sorted(page_map)},
        "ai_count": model["ai_count"],
        "behavior_count": model["behavior_count"],
    }
    if "formal_q2" in model:
        manifest["formal_q2"] = model["formal_q2"]
    (output / "manifest.json").write_text(_canonical(manifest) + "\n", encoding="utf-8")
    return manifest
