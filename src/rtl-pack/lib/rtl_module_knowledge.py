#!/usr/bin/env python3
"""Thin full-build runtime normalization for one bounded module candidate."""

from __future__ import annotations

from collections import deque
import hashlib
import json
import os
import resource
import tempfile
import time
from pathlib import Path
from typing import Any

from rtl_knowledge_prototype import PrototypeStore


SCOPE_LIMIT = 100
TRANSPARENT_SCOPE_BUDGET = 1_000
TRANSPARENT_SCOPE_KINDS = frozenset({"GenerateBlockArray", "GenerateBlock"})
RELEVANT_SCOPE_KINDS = ("Instance", "GenerateBlockArray", "GenerateBlock")
DESIGN_INSTANCE_BUDGET = 100_000
DESIGN_TRANSPARENT_SCOPE_BUDGET = 100_000
DESIGN_PARAMETER_BUDGET = 50_000
DESIGN_CONNECTION_BUDGET = 50_000


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _link_candidate(temporary: Path, output: Path) -> None:
    """Create the final name exclusively; there is no overwrite fallback."""
    os.link(temporary, output, follow_symlinks=False)


def _publish_staged_candidate(temporary: Path, output: Path) -> None:
    """Publish only a closed, audited DB; the caller owns temporary.parent."""
    _fsync_file(temporary)
    _link_candidate(temporary, output)
    _fsync_directory(output.parent)
    temporary.unlink()
    _fsync_directory(temporary.parent)
    temporary.parent.rmdir()
    _fsync_directory(output.parent)


def _cleanup_owned_staging(temporary: Path, output_parent: Path) -> None:
    staging = temporary.parent
    if not staging.exists():
        return
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    _fsync_directory(staging)
    staging.rmdir()
    _fsync_directory(output_parent)


def _audit_stage1_candidate(
    store: PrototypeStore,
    *,
    identity: dict[str, Any],
    scope: dict[str, Any],
    normalized: dict[str, Any],
    design: bool,
) -> dict[str, Any]:
    """Check the persisted deterministic rows before a Stage1 name is published."""
    integrity = store.integrity()
    if integrity != {"integrity_check": "ok", "foreign_key_check": []}:
        raise RuntimeError(f"candidate integrity/FK audit failed: {integrity}")
    for key in ("build_sha256", "build_spec_sha256", "source_closure_sha256"):
        if store.metadata(key) != identity[key]:
            raise RuntimeError(f"candidate build identity audit failed: {key}")
    if store.analysis_scope() != scope:
        raise RuntimeError("candidate Analysis Scope audit failed")

    count = lambda table, where="", args=(): store.connection.execute(
        f"SELECT count(*) FROM {table} {where}", args
    ).fetchone()[0]
    instance_kind = store.contract_id("entities", "INSTANCE")
    module_kind = store.contract_id("entities", "MODULE")
    source_kind = store.contract_id("entities", "SOURCE_FILE")
    expected_instances = (
        normalized["actual_instance_count"] if design
        else 1 + len(normalized["direct_instance_ids"])
    )
    expected_modules = (
        normalized["module_definition_count"] if design
        else len(normalized["definitions"])
    )
    for kind, expected in (
        (instance_kind, expected_instances),
        (module_kind, expected_modules),
        (source_kind, len(normalized["source_paths"])),
    ):
        if count("entity", "WHERE kind=?", (kind,)) != expected:
            raise RuntimeError("candidate deterministic Entity audit failed")
    if not count("fact") or not count("evidence") or not count("fact_evidence"):
        raise RuntimeError("candidate deterministic Fact/Evidence audit failed")
    if store.schema_version == "v0.2":
        try:
            store.audit_stage1_rows()
        except Exception as error:
            raise RuntimeError(f"Stage1 Fact/Evidence audit failed: {error}") from error
    semantic_tables = (
        ("ai_knowledge", "obligation_coverage") if store.schema_version == "v0.2"
        else ("ai_knowledge", "knowledge_coverage")
    )
    if any(count(table) for table in semantic_tables):
        raise RuntimeError("Stage1 candidate unexpectedly contains semantic rows")
    return integrity


def stable_runtime_value(value: Any) -> Any:
    """Remove task-local handles while retaining bounded expression meaning."""
    if isinstance(value, dict):
        return {
            key: stable_runtime_value(item)
            for key, item in sorted(value.items())
            if key != "handle"
        }
    if isinstance(value, list):
        return [stable_runtime_value(item) for item in value]
    return value


def _native_kind(value: Any) -> str:
    return str(getattr(value, "kind", "")).rsplit(".", 1)[-1]


def _enum_name(value: Any) -> str:
    """Return a stable PySlang enum name without assuming a symbol object."""
    return str(value).rsplit(".", 1)[-1]


def _compact_type_record(value: Any) -> dict[str, Any] | None:
    """Keep the bounded deterministic type fields needed by Build1 serving."""
    if not isinstance(value, dict):
        return None
    keys = ("kind", "text", "bit_width", "signed", "four_state", "packed")
    return {key: stable_runtime_value(value[key]) for key in keys if key in value}


def _compact_object_identity(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    keys = ("kind", "path", "name")
    return {key: stable_runtime_value(value[key]) for key in keys if key in value}


def _compact_expression_record(value: Any) -> dict[str, Any] | None:
    """Normalize an expression without duplicating AST or source evidence JSON.

    Exact source grounding is stored once through fact_evidence.  The compact
    endpoint retains the useful expression identity, display text, type and
    referenced canonical paths; it intentionally is not a second AST store.
    """
    if not isinstance(value, dict):
        return None
    result = {
        key: stable_runtime_value(value[key])
        for key in ("kind", "text", "text_truncated", "structure_complete")
        if key in value
    }
    type_record = _compact_type_record(value.get("type"))
    if type_record:
        result["type"] = type_record
    references = sorted({
        (str(item.get("kind", "")), str(item["path"]))
        for item in value.get("references", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
        and item["path"]
    })
    if references:
        result["references"] = [
            {"kind": kind, "path": path} for kind, path in references
        ]
    return result


def iter_design_instances(root: Any):
    """Yield active actual instances with their real actual parent and depth.

    This producer-side walk uses the already elaborated PySlang object graph. It
    intentionally does not allocate Runtime query handles or apply interactive
    result limits. Generate containers are transparent and never become rows.
    """
    tops = [
        item for item in getattr(root, "topInstances", ())
        if _native_kind(item) == "Instance"
        and not bool(getattr(item, "isUninstantiated", False))
    ]
    tops.sort(key=lambda item: str(getattr(item, "hierarchicalPath", "")))
    queue = deque((item, None, 0) for item in tops)
    seen_actuals: set[str] = set()
    transparent_count = 0
    while queue:
        instance, parent_path, depth = queue.popleft()
        path = str(getattr(instance, "hierarchicalPath", ""))
        if not path or path in seen_actuals:
            raise RuntimeError(f"duplicate or missing actual instance path: {path!r}")
        seen_actuals.add(path)
        if len(seen_actuals) > DESIGN_INSTANCE_BUDGET:
            raise RuntimeError("whole-design instance budget exhausted")
        yield {
            "instance": instance,
            "path": path,
            "parent_path": parent_path,
            "depth": depth,
        }

        children: dict[str, Any] = {}
        scopes = deque([getattr(instance, "body", None)])
        seen_transparent_paths: set[tuple[str, str]] = set()
        while scopes:
            scope = scopes.popleft()
            if scope is None or not hasattr(scope, "__iter__"):
                continue
            for member in scope:
                if bool(getattr(member, "isUninstantiated", False)):
                    continue
                kind = _native_kind(member)
                member_path = str(getattr(member, "hierarchicalPath", ""))
                if kind == "Instance":
                    if not member_path or member_path in children:
                        raise RuntimeError(
                            f"duplicate or missing direct child instance path: {member_path!r}"
                        )
                    children[member_path] = member
                    continue
                if kind not in TRANSPARENT_SCOPE_KINDS:
                    continue
                transparent_count += 1
                if transparent_count > DESIGN_TRANSPARENT_SCOPE_BUDGET:
                    raise RuntimeError("whole-design transparent scope budget exhausted")
                transparent_key = (kind, member_path)
                if transparent_key in seen_transparent_paths:
                    raise RuntimeError(
                        f"duplicate transparent scope path: {member_path!r}"
                    )
                seen_transparent_paths.add(transparent_key)
                body = getattr(member, "body", None)
                if body is not None and hasattr(body, "__iter__"):
                    scopes.append(body)
                elif hasattr(member, "__iter__"):
                    scopes.append(member)
        for child_path in sorted(children):
            queue.append((children[child_path], path, depth + 1))


def _project_instance_record(session: Any, instance: Any) -> dict[str, Any]:
    record = session._symbol_record(instance, register_handle=False)
    parameters = list(getattr(instance.body, "parameters", ()))
    if len(parameters) > DESIGN_PARAMETER_BUDGET:
        raise RuntimeError(
            f"whole-design parameter budget exhausted: {instance.hierarchicalPath}"
        )
    overrides, overrides_truncated = session._parameter_override_map(instance)
    if overrides_truncated:
        raise RuntimeError(
            f"whole-design parameter override projection is truncated: "
            f"{instance.hierarchicalPath}"
        )
    record["parameters"] = [
        session._parameter_record(parameter, overrides.get(str(parameter.name)))
        for parameter in parameters
    ]
    record["parameters_truncated"] = False
    return stable_runtime_value(record)


def _project_instance_bindings(session: Any, instance: Any) -> list[dict[str, Any]]:
    connections = list(getattr(instance, "portConnections", ()))
    if len(connections) > DESIGN_CONNECTION_BUDGET:
        raise RuntimeError(
            f"whole-design connection budget exhausted: {instance.hierarchicalPath}"
        )
    syntax_map, syntax_truncated = session._connection_syntax_map(instance, connections)
    if syntax_truncated:
        raise RuntimeError(
            f"whole-design connection syntax is truncated: {instance.hierarchicalPath}"
        )
    connections.sort(key=lambda item: item.port.name)
    records: list[dict[str, Any]] = []
    for connection in connections:
        port = connection.port
        interface = bool(tuple(item for item in connection.ifaceConn if item is not None))
        expression = connection.expression
        syntax = syntax_map.get(port.name)
        status, origin = session._connection_classification(
            connection, syntax, interface
        )
        internal_expression = getattr(port, "internalExpr", None)
        actual = session._expression_record(
            expression, register_handle=False
        ) if expression is not None else None
        records.append({
            "relation": "port_binding",
            "instance": str(instance.hierarchicalPath),
            "formal": port.name,
            "direction": _enum_name(getattr(port, "direction", "unknown")),
            "formal_type": _compact_type_record(
                session._type_record(getattr(port, "type", None))
            ),
            "internal_endpoint": _compact_object_identity(
                session._object_identity(
                    getattr(port, "internalSymbol", None), register_handle=False
                )
            ),
            "internal_expression": _compact_expression_record(
                session._expression_record(
                    internal_expression, register_handle=False
                ) if internal_expression is not None else None
            ),
            "connection_status": "interface" if interface else status,
            "connection_origin": "interface" if interface else origin,
            "actual": _compact_expression_record(actual),
            "connection_evidence": session._evidence(
                getattr(syntax, "sourceRange", None)
            ) if syntax is not None else None,
            "default_evidence": session._evidence(
                getattr(expression, "sourceRange", None)
            ) if status == "defaulted" and expression is not None else None,
            "formal_evidence": session._evidence_for(port),
        })
    return stable_runtime_value(records)


def _resolve_exact_native_instance(session: Any, path: str) -> Any:
    """Resolve one active native Instance without handles or suffix fallback."""
    instance = session._lookup_exact(path)
    native_path = str(getattr(instance, "hierarchicalPath", ""))
    if (
        instance is None
        or _native_kind(instance) != "Instance"
        or bool(getattr(instance, "isUninstantiated", False))
        or native_path != path
    ):
        raise RuntimeError(f"exact native instance is incomplete: {path}")
    return instance


def collect_module_scope(session: Any, target_path: str) -> dict[str, Any]:
    """Collect one exact elaborated module and its immediate instance boundary."""
    target_instance = _resolve_exact_native_instance(session, target_path)
    target_record = _project_instance_record(session, target_instance)
    target_bindings = _project_instance_bindings(session, target_instance)

    queued = {target_path}
    queue = [target_path]
    visited: list[str] = []
    children_by_path: dict[str, dict[str, Any]] = {}
    while queue:
        if len(visited) >= TRANSPARENT_SCOPE_BUDGET:
            raise RuntimeError("transparent scope budget exhausted")
        scope_path = queue.pop(0)
        relevant_members: list[dict[str, Any]] = []
        for kind in RELEVANT_SCOPE_KINDS:
            scope = session.query({
                "id": f"scope-{len(visited)}-{kind}", "op": "scope",
                "path": scope_path, "kind": kind, "limit": SCOPE_LIMIT,
            })
            if (
                scope.get("status") != "ok"
                or scope.get("completeness") != "complete"
                or scope.get("truncated") is True
            ):
                raise RuntimeError(f"transparent scope is incomplete: {scope_path}")
            members = scope.get("records")
            if not isinstance(members, list) or any(
                not isinstance(member, dict) or member.get("kind") != kind
                for member in members
            ):
                raise RuntimeError(f"transparent scope kind filter is invalid: {scope_path}")
            relevant_members.extend(members)
        visited.append(scope_path)
        for member in relevant_members:
            if member.get("active") is False:
                continue
            kind = member.get("kind")
            if kind not in TRANSPARENT_SCOPE_KINDS and kind != "Instance":
                continue
            path = member.get("path")
            if not isinstance(path, str) or not path:
                raise RuntimeError(f"transparent member path is missing: {scope_path}")
            if kind == "Instance":
                children_by_path.setdefault(path, member)
            elif path not in queued:
                queued.add(path)
                queue.append(path)

    records: list[dict[str, Any]] = []
    for child_path in sorted(children_by_path):
        child = children_by_path[child_path]
        child_instance = _resolve_exact_native_instance(session, child_path)
        inspection = _project_instance_record(session, child_instance)
        bindings = _project_instance_bindings(session, child_instance)
        records.append({
            "scope": stable_runtime_value(child),
            "inspection": inspection,
            "bindings": bindings,
        })
    return {
        "target": target_record,
        "target_bindings": target_bindings,
        "children": records,
        "validation": {
            "target_status": "ok",
            "target_inspect_completeness": "native-complete",
            "target_parameters_complete": target_record.get("parameters_truncated") is False,
        },
        "traversal": {
            "status": "complete",
            "scope_paths": sorted(visited),
            "semantic_child_paths": sorted(children_by_path),
            "per_scope_limit": SCOPE_LIMIT,
            "scope_budget": TRANSPARENT_SCOPE_BUDGET,
        },
    }


def populate_module_scope(
    store: PrototypeStore,
    *,
    source_root: Path,
    project_name: str,
    compilation_identity: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Persist only one module boundary from authoritative Runtime records."""
    source_root = source_root.resolve()
    evidence_cache: dict[str, str] = {}
    source_paths: set[str] = set()

    def source_evidence(row: dict[str, Any] | None) -> str:
        if not isinstance(row, dict):
            raise RuntimeError("runtime record lacks source evidence")
        path = row.get("path")
        expected_hash = row.get("sha256")
        if not isinstance(path, str) or not isinstance(expected_hash, str):
            raise RuntimeError("runtime source evidence is incomplete")
        source = source_root / path
        if sha256_file(source) != expected_hash:
            raise RuntimeError(f"runtime source evidence hash mismatch: {path}")
        source_paths.add(path)
        key = _canonical(stable_runtime_value(row))
        if key not in evidence_cache:
            evidence_cache[key] = store.add_evidence(
                kind="source", locator=path,
                start_line=row.get("line_start"), end_line=row.get("line_end"),
                attrs={
                    "sha256": expected_hash,
                    "configuration_identity": row.get("configuration_identity"),
                },
            )
        return evidence_cache[key]

    entity_kind = lambda name: store.contract_id("entities", name)
    predicate = lambda name: store.contract_id("facts", name)
    provenance = store.add_evidence(
        kind="provenance", locator=f"runtime-query-build:{project_name}",
        attrs=stable_runtime_value(compilation_identity),
    )

    def normalize_parameters(rows: Any) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            declaration_record = row.get("declaration_evidence") or row.get("evidence")
            declaration_evidence_id = (
                source_evidence(declaration_record)
                if isinstance(declaration_record, dict)
                else None
            )
            default_record = row.get("default")
            default = None
            default_evidence_id = None
            if isinstance(default_record, dict):
                default_source = default_record.get("evidence")
                default_evidence_id = (
                    source_evidence(default_source)
                    if isinstance(default_source, dict)
                    else None
                )
                default = {
                    "text": default_record.get("text"),
                    "text_truncated": bool(default_record.get("text_truncated", False)),
                    "evidence_id": default_evidence_id,
                }
            override_record = row.get("override")
            override = None
            override_evidence_id = None
            if isinstance(override_record, dict):
                override_source = override_record.get("evidence")
                override_evidence_id = (
                    source_evidence(override_source)
                    if isinstance(override_source, dict)
                    else None
                )
                override = {
                    "binding_kind": override_record.get("binding_kind"),
                    "formal_name": override_record.get("formal_name"),
                    "expression_text": override_record.get("expression_text"),
                    "text_truncated": bool(override_record.get("text_truncated", False)),
                    "evidence_id": override_evidence_id,
                }
            overridden = bool(row.get("overridden", False))
            provenance_complete = bool(
                row.get("provenance_complete")
                and declaration_evidence_id
                and (default is None or default_evidence_id)
                and (not overridden or (override is not None and override_evidence_id))
            )
            item = {
                key: stable_runtime_value(row[key])
                for key in ("name", "kind", "value", "target_type", "type", "overridden")
                if key in row
            }
            item.update({
                "declaration_evidence_id": declaration_evidence_id,
                "default": default,
                "override": override,
                "elaboration_evidence_id": provenance,
                "provenance_complete": provenance_complete,
            })
            normalized.append(item)
        return normalized

    binding_count = 0
    expression_boundary_count = 0

    def publish_instance_bindings(
        instance_id: str, instance_path: str, bindings: Any
    ) -> None:
        nonlocal binding_count, expression_boundary_count
        for binding in bindings if isinstance(bindings, list) else []:
            binding_evidence_record = (
                binding.get("connection_evidence")
                or binding.get("default_evidence")
                or binding.get("formal_evidence")
            )
            binding_evidence = source_evidence(binding_evidence_record)
            port = store.add_entity(
                entity_kind("PORT"), f"{instance_path}.{binding['formal']}",
                attrs={
                    "direction": binding["direction"],
                    "type": binding["formal_type"],
                    "internal_endpoint": binding.get("internal_endpoint"),
                },
            )
            store.add_fact(
                instance_id, predicate("CONTAINS"), port,
                producer="slang", evidence_ids=[binding_evidence],
            )
            actual = stable_runtime_value(binding.get("actual"))
            references = actual.get("references", []) if isinstance(actual, dict) else []
            simple_named = (
                isinstance(actual, dict)
                and actual.get("kind") == "NamedValue"
                and not actual.get("text_truncated")
                and not actual.get("nodes_truncated")
                and len(references) == 1
            )
            if simple_named:
                endpoint = store.add_entity(entity_kind("SIGNAL"), references[0]["path"])
            else:
                expression_boundary_count += 1
                expression_identity = hashlib.sha256(
                    _canonical({
                        "instance": instance_path, "formal": binding["formal"],
                        "actual": actual, "status": binding["connection_status"],
                    }).encode("utf-8")
                ).hexdigest()
                endpoint = store.add_entity(
                    entity_kind("UNRESOLVED"),
                    f"{instance_path}.{binding['formal']}@expression:{expression_identity}",
                    attrs={
                        "actual": actual,
                        "boundary": (
                            "reference/source expression retained; bit equivalence not inferred"
                        ),
                    },
                )
            store.add_fact(
                port, predicate("BINDS_TO"), endpoint,
                producer="slang", evidence_ids=[binding_evidence],
                attrs={
                    "connection_status": binding["connection_status"],
                    "connection_origin": binding["connection_origin"],
                    "actual": actual,
                    "internal_expression": stable_runtime_value(
                        binding.get("internal_expression")
                    ),
                    "complete_connection_semantics": False,
                    "semantic_boundary": (
                        "bounded Runtime expression and references retained; "
                        "no expression or bit-equivalence inference"
                    ),
                },
            )
            binding_count += 1

    project = store.add_entity(entity_kind("PROJECT"), project_name)
    config = store.add_entity(
        entity_kind("CONFIGURATION"),
        f"{project_name}@{compilation_identity['build_sha256']}",
        attrs={"compilation_identity": stable_runtime_value(compilation_identity)},
    )
    store.add_fact(
        project, predicate("HAS_CONFIGURATION"), config,
        producer="build-spec", evidence_ids=[provenance],
    )

    target_record = snapshot["target"]
    target_evidence = source_evidence(target_record["evidence"])
    target_definition_evidence = source_evidence(target_record["definition_evidence"])
    target_module = store.add_entity(
        entity_kind("MODULE"), target_record["definition"],
        attrs={"source": target_record["definition_evidence"]["path"]},
    )
    target_instance = store.add_entity(
        entity_kind("INSTANCE"), target_record["path"],
        attrs={
            "name": target_record["name"],
            "definition": target_record["definition"],
            "effective_parameters": normalize_parameters(
                target_record.get("parameters", [])
            ),
            "parameters_truncated": target_record.get("parameters_truncated", False),
        },
    )
    store.add_fact(
        project, predicate("CONTAINS"), target_instance,
        producer="slang", evidence_ids=[target_evidence],
    )
    store.add_fact(
        target_instance, predicate("INSTANCE_OF"), target_module,
        producer="slang",
        evidence_ids=sorted({target_evidence, target_definition_evidence}),
    )
    publish_instance_bindings(
        target_instance, target_record["path"], snapshot.get("target_bindings", [])
    )

    definitions: dict[str, str] = {target_record["definition"]: target_module}
    instance_ids: list[str] = []
    source_paths.update({
        target_record["evidence"]["path"],
        target_record["definition_evidence"]["path"],
    })
    for child_record in snapshot["children"]:
        child = child_record["scope"]
        inspection = child_record["inspection"]
        child_evidence = source_evidence(child["evidence"])
        definition_evidence = source_evidence(child["definition_evidence"])
        source_paths.add(child["evidence"]["path"])
        source_paths.add(child["definition_evidence"]["path"])
        if child["definition"] not in definitions:
            definitions[child["definition"]] = store.add_entity(
                entity_kind("MODULE"), child["definition"],
                attrs={"source": child["definition_evidence"]["path"]},
            )
        instance = store.add_entity(
            entity_kind("INSTANCE"), child["path"],
            attrs={
                "name": child["name"], "definition": child["definition"],
                "effective_parameters": normalize_parameters(
                    inspection.get("parameters", [])
                ),
                "parameters_truncated": inspection.get("parameters_truncated", False),
            },
        )
        instance_ids.append(instance)
        store.add_fact(
            target_instance, predicate("CONTAINS"), instance,
            producer="slang", evidence_ids=[child_evidence],
        )
        store.add_fact(
            instance, predicate("INSTANCE_OF"), definitions[child["definition"]],
            producer="slang", evidence_ids=[definition_evidence],
        )
        publish_instance_bindings(instance, child["path"], child_record["bindings"])

    for relative in sorted(source_paths):
        source_file = store.add_entity(
            entity_kind("SOURCE_FILE"), relative,
            attrs={"sha256": sha256_file(source_root / relative)},
        )
        store.add_fact(
            config, predicate("USES_SOURCE"), source_file,
            producer="build-spec", evidence_ids=[provenance],
        )
    return {
        "target_instance_entity_id": target_instance,
        "target_module_entity_id": target_module,
        "direct_instance_ids": sorted(instance_ids),
        "direct_instance_paths": sorted(
            child["scope"]["path"] for child in snapshot["children"]
        ),
        "definitions": sorted(definitions),
        "binding_count": binding_count,
        "expression_boundary_count": expression_boundary_count,
        "source_paths": sorted(source_paths),
    }


def populate_design_scope(
    store: PrototypeStore,
    *,
    session: Any,
    source_root: Path,
    project_name: str,
    compilation_identity: dict[str, Any],
) -> dict[str, Any]:
    """Persist one complete elaborated design scope from the existing Compilation."""
    source_root = source_root.resolve()
    evidence_cache: dict[str, str] = {}
    source_paths: set[str] = set()

    def source_evidence(row: dict[str, Any] | None) -> str:
        if not isinstance(row, dict):
            raise RuntimeError("runtime record lacks source evidence")
        path = row.get("path")
        expected_hash = row.get("sha256")
        if (
            not isinstance(path, str) or not path or "\\" in path
            or Path(path).is_absolute() or ".." in Path(path).parts
            or not isinstance(expected_hash, str)
        ):
            raise RuntimeError("runtime source evidence is incomplete or unsafe")
        source = (source_root / path).resolve()
        try:
            source.relative_to(source_root)
        except ValueError as error:
            raise RuntimeError(f"runtime source evidence escapes source root: {path}") from error
        if not source.is_file() or sha256_file(source) != expected_hash:
            raise RuntimeError(f"runtime source evidence hash mismatch: {path}")
        source_paths.add(path)
        key = _canonical(stable_runtime_value(row))
        if key not in evidence_cache:
            evidence_cache[key] = store.add_evidence(
                kind="source", locator=path,
                start_line=row.get("line_start"), end_line=row.get("line_end"),
                attrs={
                    "sha256": expected_hash,
                    "configuration_identity": row.get("configuration_identity"),
                },
            )
        return evidence_cache[key]

    def normalize_parameters(rows: Any, provenance: str) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            declaration = row.get("declaration_evidence") or row.get("evidence")
            declaration_id = source_evidence(declaration) if isinstance(declaration, dict) else None
            default_record = row.get("default")
            default = None
            default_id = None
            if isinstance(default_record, dict):
                default_source = default_record.get("evidence")
                default_id = source_evidence(default_source) if isinstance(default_source, dict) else None
                default = {
                    "text": default_record.get("text"),
                    "text_truncated": bool(default_record.get("text_truncated", False)),
                    "evidence_id": default_id,
                }
            override_record = row.get("override")
            override = None
            override_id = None
            if isinstance(override_record, dict):
                override_source = override_record.get("evidence")
                override_id = source_evidence(override_source) if isinstance(override_source, dict) else None
                override = {
                    "binding_kind": override_record.get("binding_kind"),
                    "formal_name": override_record.get("formal_name"),
                    "expression_text": override_record.get("expression_text"),
                    "text_truncated": bool(override_record.get("text_truncated", False)),
                    "evidence_id": override_id,
                }
            overridden = bool(row.get("overridden", False))
            item = {
                key: stable_runtime_value(row[key])
                for key in ("name", "kind", "value", "target_type", "type", "overridden")
                if key in row
            }
            item.update({
                "declaration_evidence_id": declaration_id,
                "default": default,
                "override": override,
                "elaboration_evidence_id": provenance,
                "provenance_complete": bool(
                    row.get("provenance_complete") and declaration_id
                    and (default is None or default_id)
                    and (not overridden or (override is not None and override_id))
                ),
            })
            normalized.append(item)
        return normalized

    entity_kind = lambda name: store.contract_id("entities", name)
    predicate = lambda name: store.contract_id("facts", name)
    provenance = store.add_evidence(
        kind="provenance", locator=f"runtime-query-build:{project_name}",
        attrs=stable_runtime_value(compilation_identity),
    )
    project = store.add_entity(entity_kind("PROJECT"), project_name)
    config = store.add_entity(
        entity_kind("CONFIGURATION"),
        f"{project_name}@{compilation_identity['build_sha256']}",
        attrs={"compilation_identity": stable_runtime_value(compilation_identity)},
    )
    store.add_fact(
        project, predicate("HAS_CONFIGURATION"), config,
        producer="build-spec", evidence_ids=[provenance],
    )

    definitions: dict[str, str] = {}
    definition_sources: dict[str, str] = {}
    instances: dict[str, str] = {}
    roots: list[str] = []
    binding_count = 0
    expression_boundary_count = 0
    max_depth = 0
    max_containment_depth = 0
    for item in iter_design_instances(session.root):
        native = item["instance"]
        record = _project_instance_record(session, native)
        path = item["path"]
        parent_path = item["parent_path"]
        max_depth = max(max_depth, item["depth"])
        max_containment_depth = max(max_containment_depth, item["depth"])
        instance_evidence = source_evidence(record.get("evidence"))
        definition_evidence_record = record.get("definition_evidence")
        definition_evidence = source_evidence(definition_evidence_record)
        definition_name = record.get("definition")
        definition_source = definition_evidence_record.get("path")
        if not isinstance(definition_name, str) or not definition_name:
            raise RuntimeError(f"instance definition is missing: {path}")
        if definition_name in definition_sources and definition_sources[definition_name] != definition_source:
            raise RuntimeError(f"module definition source is ambiguous: {definition_name}")
        definition_sources[definition_name] = definition_source
        definition_id = definitions.get(definition_name)
        if definition_id is None:
            definition_id = store.add_entity(
                entity_kind("MODULE"), definition_name,
                attrs={"source": definition_source},
            )
            definitions[definition_name] = definition_id
        instance_id = store.add_entity(
            entity_kind("INSTANCE"), path,
            attrs={
                "name": record.get("name"),
                "definition": definition_name,
                "effective_parameters": normalize_parameters(
                    record.get("parameters", []), provenance
                ),
                "parameters_truncated": bool(record.get("parameters_truncated", False)),
            },
        )
        instances[path] = instance_id
        if parent_path is None:
            roots.append(instance_id)
            parent_id = project
        else:
            parent_id = instances.get(parent_path)
            if parent_id is None:
                raise RuntimeError(f"actual parent was not published first: {parent_path}")
        store.add_fact(
            parent_id, predicate("CONTAINS"), instance_id,
            producer="slang", evidence_ids=[instance_evidence],
        )
        store.add_fact(
            instance_id, predicate("INSTANCE_OF"), definition_id,
            producer="slang",
            evidence_ids=sorted({instance_evidence, definition_evidence}),
        )

        for binding in _project_instance_bindings(session, native):
            binding_record = (
                binding.get("connection_evidence")
                or binding.get("default_evidence")
                or binding.get("formal_evidence")
            )
            binding_evidence = source_evidence(binding_record)
            formal = binding.get("formal")
            if not isinstance(formal, str) or not formal:
                raise RuntimeError(f"binding formal is missing: {path}")
            port = store.add_entity(
                entity_kind("PORT"), f"{path}.{formal}",
                attrs={
                    "direction": binding.get("direction"),
                    "type": binding.get("formal_type"),
                    "internal_endpoint": binding.get("internal_endpoint"),
                },
            )
            store.add_fact(
                instance_id, predicate("CONTAINS"), port,
                producer="slang", evidence_ids=[binding_evidence],
            )
            max_containment_depth = max(max_containment_depth, item["depth"] + 1)
            actual = stable_runtime_value(binding.get("actual"))
            references = actual.get("references", []) if isinstance(actual, dict) else []
            simple_named = (
                isinstance(actual, dict)
                and actual.get("kind") == "NamedValue"
                and not actual.get("text_truncated")
                and not actual.get("nodes_truncated")
                and len(references) == 1
                and isinstance(references[0].get("path"), str)
                and bool(references[0]["path"])
            )
            if simple_named:
                endpoint = store.add_entity(entity_kind("SIGNAL"), references[0]["path"])
            else:
                expression_boundary_count += 1
                expression_identity = hashlib.sha256(_canonical({
                    "instance": path, "formal": formal,
                    "actual": actual, "status": binding.get("connection_status"),
                }).encode("utf-8")).hexdigest()
                endpoint = store.add_entity(
                    entity_kind("UNRESOLVED"),
                    f"{path}.{formal}@expression:{expression_identity}",
                    attrs={
                        "expression": actual,
                    },
                )
            store.add_fact(
                port, predicate("BINDS_TO"), endpoint,
                producer="slang", evidence_ids=[binding_evidence],
                attrs={
                    "connection_status": binding.get("connection_status"),
                    "connection_origin": binding.get("connection_origin"),
                    "complete_connection_semantics": False,
                },
            )
            binding_count += 1

    if not roots:
        raise RuntimeError("whole-design compilation has no active actual root")
    for relative in sorted(source_paths):
        source_file = store.add_entity(
            entity_kind("SOURCE_FILE"), relative,
            attrs={"sha256": sha256_file(source_root / relative)},
        )
        store.add_fact(
            config, predicate("USES_SOURCE"), source_file,
            producer="build-spec", evidence_ids=[provenance],
        )
    return {
        "root_instance_entity_ids": roots,
        "actual_instance_count": len(instances),
        "module_definition_count": len(definitions),
        "max_hierarchy_depth": max_depth,
        "max_containment_depth": max_containment_depth,
        "binding_count": binding_count,
        "expression_boundary_count": expression_boundary_count,
        "source_paths": sorted(source_paths),
    }


def _expected_source_evidence(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    locator = row.get("path")
    sha256 = row.get("sha256")
    if not isinstance(locator, str) or not isinstance(sha256, str):
        return None
    start_line = row.get("line_start")
    end_line = row.get("line_end")
    if end_line is None:
        end_line = start_line
    return {
        "kind": "source",
        "locator": locator,
        "start_line": start_line,
        "end_line": end_line,
        "attrs": {
            "sha256": sha256,
            "configuration_identity": row.get("configuration_identity"),
        },
    }


def _evidence_row(store: PrototypeStore, evidence_id: Any) -> dict[str, Any] | None:
    if not isinstance(evidence_id, str):
        return None
    row = store.connection.execute(
        "SELECT kind, locator, start_line, end_line, attrs_json "
        "FROM evidence WHERE evidence_id=?", (evidence_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "kind": row["kind"],
        "locator": row["locator"],
        "start_line": row["start_line"],
        "end_line": row["end_line"],
        "attrs": json.loads(row["attrs_json"]),
    }


def _source_evidence_matches(
    store: PrototypeStore, evidence_id: Any, runtime_evidence: Any
) -> bool:
    expected = _expected_source_evidence(runtime_evidence)
    return expected is not None and _evidence_row(store, evidence_id) == expected


def _fact_evidence_ids(store: PrototypeStore, fact_id: str | None) -> list[str]:
    if fact_id is None:
        return []
    return [
        row[0]
        for row in store.connection.execute(
            "SELECT evidence_id FROM fact_evidence WHERE fact_id=? ORDER BY evidence_id",
            (fact_id,),
        )
    ]


def _find_fact(
    store: PrototypeStore, subject_id: str | None, predicate: str, object_id: str | None
) -> str | None:
    if subject_id is None or object_id is None:
        return None
    row = store.connection.execute(
        "SELECT fact_id FROM fact WHERE subject_id=? AND predicate=? AND object_id=? "
        "ORDER BY fact_id LIMIT 1",
        (subject_id, predicate, object_id),
    ).fetchone()
    return str(row[0]) if row is not None else None


def _validated_snapshot_obligations(
    snapshot: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[str]]:
    target = snapshot.get("target")
    traversal = snapshot.get("traversal")
    validation = snapshot.get("validation")
    if (
        not isinstance(target, dict)
        or target.get("kind") != "Instance"
        or not isinstance(target.get("path"), str)
        or not isinstance(traversal, dict)
        or traversal.get("status") != "complete"
        or not isinstance(validation, dict)
        or validation.get("target_status") != "ok"
        or validation.get("target_parameters_complete") is not True
        or target.get("parameters_truncated") is not False
    ):
        raise RuntimeError("authoritative Runtime snapshot is not complete")
    children = snapshot.get("children")
    if not isinstance(children, list):
        raise RuntimeError("authoritative Runtime child snapshot is invalid")
    child_paths = [
        item.get("scope", {}).get("path")
        for item in children
        if isinstance(item, dict)
    ]
    if (
        any(not isinstance(path, str) or not path for path in child_paths)
        or len(set(child_paths)) != len(child_paths)
        or sorted(child_paths) != sorted(traversal.get("semantic_child_paths", []))
    ):
        raise RuntimeError("authoritative Runtime child obligations are not complete")
    parameters = target.get("parameters")
    if not isinstance(parameters, list):
        raise RuntimeError("authoritative Runtime parameters are invalid")
    parameter_names = [item.get("name") for item in parameters if isinstance(item, dict)]
    if (
        len(parameter_names) != len(parameters)
        or any(not isinstance(name, str) or not name for name in parameter_names)
        or len(set(parameter_names)) != len(parameter_names)
    ):
        raise RuntimeError("authoritative Runtime parameter obligations are ambiguous")
    return target, sorted(child_paths), sorted(parameter_names)


def evaluate_cycle1_conformance(
    store: PrototypeStore,
    *,
    snapshot: dict[str, Any],
    compilation_identity: dict[str, Any],
) -> dict[str, Any]:
    """Compare one validated Runtime snapshot with persisted Cycle-1 grounding."""
    target, expected_child_paths, required_parameter_names = (
        _validated_snapshot_obligations(snapshot)
    )
    entity_kind = lambda name: store.contract_id("entities", name)
    predicate = lambda name: store.contract_id("facts", name)

    target_row = store.connection.execute(
        "SELECT entity_id, attrs_json FROM entity WHERE kind=? AND qualified_name=?",
        (entity_kind("INSTANCE"), target["path"]),
    ).fetchone()
    target_id = str(target_row["entity_id"]) if target_row is not None else None
    target_attrs = json.loads(target_row["attrs_json"]) if target_row is not None else {}
    module_row = store.connection.execute(
        "SELECT entity_id FROM entity WHERE kind=? AND qualified_name=?",
        (entity_kind("MODULE"), target.get("definition")),
    ).fetchone()
    module_id = str(module_row[0]) if module_row is not None else None
    target_instance_present = bool(
        target_id
        and target_attrs.get("name") == target.get("name")
        and target_attrs.get("definition") == target.get("definition")
    )

    project_name = store.metadata("project")
    project_row = store.connection.execute(
        "SELECT entity_id FROM entity WHERE kind=? AND qualified_name=?",
        (entity_kind("PROJECT"), project_name),
    ).fetchone()
    project_id = str(project_row[0]) if project_row is not None else None
    contains_fact = _find_fact(
        store, project_id, predicate("CONTAINS"), target_id
    )
    contains_evidence_ids = _fact_evidence_ids(store, contains_fact)
    project_contains_target = bool(
        contains_fact
        and any(
            _source_evidence_matches(store, item, target.get("evidence"))
            for item in contains_evidence_ids
        )
    )

    instance_of_fact = _find_fact(
        store, target_id, predicate("INSTANCE_OF"), module_id
    )
    instance_of_evidence_ids = _fact_evidence_ids(store, instance_of_fact)
    instance_of_present = bool(
        instance_of_fact
        and any(
            _source_evidence_matches(store, item, target.get("evidence"))
            for item in instance_of_evidence_ids
        )
        and any(
            _source_evidence_matches(store, item, target.get("definition_evidence"))
            for item in instance_of_evidence_ids
        )
    )

    observed_child_rows = list(store.connection.execute(
        "SELECT e.entity_id, e.qualified_name FROM fact f "
        "JOIN entity e ON e.entity_id=f.object_id "
        "WHERE f.subject_id=? AND f.predicate=? AND e.kind=? "
        "ORDER BY e.qualified_name",
        (target_id, predicate("CONTAINS"), entity_kind("INSTANCE")),
    )) if target_id is not None else []
    observed_child_paths = [str(row["qualified_name"]) for row in observed_child_rows]
    semantic_children_exact = observed_child_paths == expected_child_paths

    required_parameter_rows = {
        item["name"]: item for item in target.get("parameters", [])
    }
    stored_parameters = target_attrs.get("effective_parameters", [])
    stored_parameter_rows = {
        item.get("name"): item
        for item in stored_parameters
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    parameter_name_set_complete = (
        sorted(stored_parameter_rows) == required_parameter_names
        and len(stored_parameter_rows) == len(stored_parameters)
    )
    parameter_fields_complete = parameter_name_set_complete
    parameter_evidence_correspondence = parameter_name_set_complete
    parameter_evidence_ids: set[str] = set()
    for name in required_parameter_names:
        runtime_parameter = required_parameter_rows[name]
        stored_parameter = stored_parameter_rows.get(name)
        if stored_parameter is None:
            parameter_fields_complete = False
            parameter_evidence_correspondence = False
            continue
        for key in ("name", "kind", "overridden"):
            if stored_parameter.get(key) != runtime_parameter.get(key):
                parameter_fields_complete = False
        effective_key = "target_type" if "target_type" in runtime_parameter else "value"
        if stable_runtime_value(stored_parameter.get(effective_key)) != stable_runtime_value(
            runtime_parameter.get(effective_key)
        ):
            parameter_fields_complete = False
        if stored_parameter.get("provenance_complete") is not True or runtime_parameter.get(
            "provenance_complete"
        ) is not True:
            parameter_fields_complete = False

        declaration_id = stored_parameter.get("declaration_evidence_id")
        if isinstance(declaration_id, str):
            parameter_evidence_ids.add(declaration_id)
        declaration_ok = _source_evidence_matches(
            store,
            declaration_id,
            runtime_parameter.get("declaration_evidence")
            or runtime_parameter.get("evidence"),
        )
        runtime_default = runtime_parameter.get("default")
        stored_default = stored_parameter.get("default")
        default_ok = runtime_default is None and stored_default is None
        if isinstance(runtime_default, dict) and isinstance(stored_default, dict):
            default_id = stored_default.get("evidence_id")
            if isinstance(default_id, str):
                parameter_evidence_ids.add(default_id)
            default_ok = bool(
                stored_default.get("text") == runtime_default.get("text")
                and stored_default.get("text_truncated")
                == bool(runtime_default.get("text_truncated", False))
                and _source_evidence_matches(
                    store, default_id, runtime_default.get("evidence")
                )
            )
        runtime_override = runtime_parameter.get("override")
        stored_override = stored_parameter.get("override")
        override_ok = runtime_override is None and stored_override is None
        if isinstance(runtime_override, dict) and isinstance(stored_override, dict):
            override_id = stored_override.get("evidence_id")
            if isinstance(override_id, str):
                parameter_evidence_ids.add(override_id)
            override_ok = bool(
                stored_override.get("binding_kind")
                == runtime_override.get("binding_kind")
                and stored_override.get("formal_name")
                == runtime_override.get("formal_name")
                and stored_override.get("formal_name") == name
                and stored_override.get("expression_text")
                == runtime_override.get("expression_text")
                and stored_override.get("text_truncated")
                == bool(runtime_override.get("text_truncated", False))
                and _source_evidence_matches(
                    store, override_id, runtime_override.get("evidence")
                )
            )
        elaboration_id = stored_parameter.get("elaboration_evidence_id")
        if isinstance(elaboration_id, str):
            parameter_evidence_ids.add(elaboration_id)
        elaboration_row = _evidence_row(store, elaboration_id)
        elaboration_ok = elaboration_row == {
            "kind": "provenance",
            "locator": f"runtime-query-build:{project_name}",
            "start_line": None,
            "end_line": None,
            "attrs": stable_runtime_value(compilation_identity),
        }
        if not (declaration_ok and default_ok and override_ok and elaboration_ok):
            parameter_evidence_correspondence = False

    parameter_grounding_complete = bool(
        parameter_fields_complete and parameter_evidence_correspondence
    )

    child_evidence_complete = semantic_children_exact
    child_by_path = {
        item["scope"]["path"]: item
        for item in snapshot.get("children", [])
    }
    for child_row in observed_child_rows:
        path = str(child_row["qualified_name"])
        expected_child = child_by_path.get(path, {}).get("scope", {})
        child_id = str(child_row["entity_id"])
        child_contains = _find_fact(
            store, target_id, predicate("CONTAINS"), child_id
        )
        child_contains_evidence = _fact_evidence_ids(store, child_contains)
        if not any(
            _source_evidence_matches(store, item, expected_child.get("evidence"))
            for item in child_contains_evidence
        ):
            child_evidence_complete = False
        definition_row = store.connection.execute(
            "SELECT entity_id FROM entity WHERE kind=? AND qualified_name=?",
            (entity_kind("MODULE"), expected_child.get("definition")),
        ).fetchone()
        definition_id = str(definition_row[0]) if definition_row is not None else None
        child_instance_of = _find_fact(
            store, child_id, predicate("INSTANCE_OF"), definition_id
        )
        child_definition_evidence = _fact_evidence_ids(store, child_instance_of)
        if not any(
            _source_evidence_matches(
                store, item, expected_child.get("definition_evidence")
            )
            for item in child_definition_evidence
        ):
            child_evidence_complete = False

    build_elaboration_provenance = all(
        store.metadata(key) == value
        for key, value in compilation_identity.items()
        if key in {"build_sha256", "build_spec_sha256", "source_closure_sha256"}
    ) and all(
        _evidence_row(store, item) is not None for item in parameter_evidence_ids
    )
    evidence_complete = bool(
        project_contains_target
        and instance_of_present
        and child_evidence_complete
        and parameter_evidence_correspondence
        and build_elaboration_provenance
    )
    checks = {
        "target_instance_present": target_instance_present,
        "project_contains_target": project_contains_target,
        "instance_of_present": instance_of_present,
        "semantic_children_exact": semantic_children_exact,
        "parameter_grounding_complete": parameter_grounding_complete,
        "evidence_complete": evidence_complete,
        "build_elaboration_provenance": build_elaboration_provenance,
    }
    reasons: list[str] = []
    if not target_instance_present:
        reasons.append("target-instance-missing")
    if not project_contains_target:
        reasons.append("project-containment-missing")
    if not instance_of_present:
        reasons.append("instance-of-grounding-missing")
    if not semantic_children_exact:
        reasons.append("semantic-child-set-mismatch")
    if not parameter_grounding_complete:
        reasons.append("parameter-grounding-incomplete")
    if not parameter_evidence_correspondence:
        reasons.append("parameter-evidence-correspondence-mismatch")
    if not evidence_complete:
        reasons.append("required-evidence-incomplete")
    if not build_elaboration_provenance:
        reasons.append("build-elaboration-provenance-mismatch")
    status = (
        "NOT COVERED"
        if target_id is None
        else "READY"
        if all(checks.values())
        else "PARTIAL"
    )
    result = {
        "schema": "rtl-analysis-agent/module-core-v1-cycle1-conformance-v1",
        "coverage_scope": [
            "root-instance-identity",
            "semantic-direct-child-set",
            "parameter-grounding",
            "required-evidence",
        ],
        "target_instance_entity_id": target_id,
        "target_module_entity_id": module_id,
        "target_path": target["path"],
        "status": status,
        "checks": checks,
        "reasons": reasons,
        "expected_child_paths": expected_child_paths,
        "observed_child_paths": observed_child_paths,
        "required_parameter_names": required_parameter_names,
        "evidence_ids": sorted(
            set(contains_evidence_ids)
            | set(instance_of_evidence_ids)
            | parameter_evidence_ids
        ),
    }
    store.add_metadata_once("module_core_v1_cycle1_conformance", result)
    return result


def build_runtime_module_candidate(
    *,
    source_root: Path,
    build_path: Path,
    pyslang_site: Path,
    project_name: str,
    source_revision: str,
    target_path: str,
    output: Path,
    schema_version: str = "v0.1",
    publication_contract: Path | None = None,
) -> dict[str, Any]:
    """Build one new candidate atomically from a full Runtime compilation."""
    from rtl_query_runtime import RuntimeQuerySession

    started = time.perf_counter()
    if output.is_symlink():
        raise RuntimeError("output candidate already exists")
    output = output.parent.resolve() / output.name
    if output.exists() or output.is_symlink():
        raise RuntimeError("output candidate already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(
        prefix=output.name + ".building-", dir=output.parent,
    )) / output.name
    store: PrototypeStore | None = None
    try:
        with RuntimeQuerySession.open(
            source_root=source_root, build_path=build_path,
            project_name=project_name, source_revision=source_revision,
            pyslang_site=pyslang_site,
        ) as session:
            ready = session.ready_result()
            identity = ready["compilation_identity"]
            snapshot = collect_module_scope(session, target_path)
            store = PrototypeStore.create(temporary, {
                "project": project_name,
                "source_revision": source_revision,
                "build_sha256": identity["build_sha256"],
                "build_spec_sha256": identity["build_spec_sha256"],
                "source_closure_sha256": identity["source_closure_sha256"],
                "effective_input_count": identity["effective_input_count"],
                "target_instance_path": target_path,
                "subtree_scope": "direct",
                **({"candidate_id": hashlib.sha256(
                    (project_name + "\0" + source_revision + "\0" + identity["build_sha256"]
                     + "\0" + target_path + "\0" + hashlib.sha256(publication_contract.read_bytes()).hexdigest()).encode()
                ).hexdigest()} if publication_contract else {}),
            }, schema_version=schema_version, publication_contract=publication_contract)
            if schema_version == "v0.2":
                store.begin_stage1_audit()
            with store.transaction():
                normalized = populate_module_scope(
                    store, source_root=source_root, project_name=project_name,
                    compilation_identity=identity, snapshot=snapshot,
                )
                cycle1_conformance = evaluate_cycle1_conformance(
                    store, snapshot=snapshot, compilation_identity=identity,
                )
                analysis_scope = store.register_analysis_scope(
                    scope_mode="ACTUAL_INSTANCE_ROOT",
                    roots=[{
                        "entity_id": normalized["target_instance_entity_id"],
                        "kind": store.contract_id("entities", "INSTANCE"),
                        "role": "PRIMARY",
                        "resolution_state": "RESOLVED",
                    }],
                    boundary_policy={
                        "kind": "ROOTED_CONTAINS_CLOSURE",
                        "max_depth": 1,
                        "knowledge_source_paths": normalized["source_paths"],
                    },
                    dependency_policy={
                        "kind": "REFERENCED_DEFINITIONS_AND_EVIDENCE",
                    },
                    applicability={
                        "schema": "rtl-analysis-agent/applicability-v1",
                        "constraints": {},
                    },
                    profile=(store.metadata("semantic_publication_contract")["profile"]
                             if publication_contract else {
                        "id": ("rtl-analysis-agent/g53-stage1-only"
                               if schema_version == "v0.2" else "rtl-analysis-agent/build1"),
                        "version": "1",
                    }),
                    vocabulary=(store.metadata("semantic_publication_contract")["vocabulary"]
                                if publication_contract else {
                        "id": "rtl-analysis-agent/knowledge-contract",
                        "version": store.metadata("contract_version"),
                    }),
                    producer_capability={
                        "actual_hierarchy": "DIRECT",
                        "module_definitions": "AVAILABLE",
                        "parameters": "EVIDENCE_GROUNDED",
                        "bindings": "EVIDENCE_GROUNDED",
                        "deterministic_facts": "AVAILABLE",
                        "source": "HASH_VERIFIED",
                        "semantic": "NOT_BUILT",
                    },
                    completeness={
                        "structure": "COMPLETE_WITHIN_BOUNDARY",
                        "source": "COMPLETE_FOR_PUBLISHED_FACTS",
                        "semantic": "NOT_BUILT",
                    },
                    limitations=[
                        "Actual hierarchy is bounded to the root and its direct semantic children.",
                        "Build closure inputs outside the published evidence set are not Knowledge Scope sources.",
                    ],
                )
            integrity = _audit_stage1_candidate(
                store, identity=identity, scope=analysis_scope,
                normalized=normalized, design=False,
            )
            coverage_table = (
                "obligation_coverage" if schema_version == "v0.2"
                else "knowledge_coverage"
            )
            counts = {
                table: store.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("entity", "fact", "evidence", "ai_knowledge", coverage_table)
            }
            store.close()
            store = None
        _publish_staged_candidate(temporary, output)
        return {
            "schema": "rtl-analysis-agent/runtime-module-candidate-build-v2",
            "target": target_path,
            "target_instance_entity_id": normalized["target_instance_entity_id"],
            "target_module_entity_id": normalized["target_module_entity_id"],
            "cycle1_conformance": cycle1_conformance,
            "analysis_scope": analysis_scope,
            "compilation_identity": identity,
            "diagnostic_summary": ready["diagnostic_summary"],
            "scope": normalized,
            "candidate": {
                "path": output.as_posix(), "size_bytes": output.stat().st_size,
                "sha256": sha256_file(output), "counts": counts, "integrity": integrity,
            },
            "execution_summary": {
                "object": target_path, "scope": "direct",
                "actual_path": "FULL_BUILD_TO_BOUNDED_CANDIDATE",
                "returned_items": counts["entity"] + counts["fact"] + counts["evidence"],
                "stop_or_fallback_reason": ["bounded-direct-scope-complete"],
                "wall_seconds": round(time.perf_counter() - started, 3),
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            },
        }
    except BaseException as error:
        if store is not None:
            try:
                store.close()
            except BaseException as close_error:
                error.add_note(f"candidate connection close failed: {close_error!r}")
        try:
            _cleanup_owned_staging(temporary, output.parent)
        except BaseException as cleanup_error:
            error.add_note(f"owned staging cleanup failed: {cleanup_error!r}")
        raise


def build_runtime_design_candidate(
    *,
    source_root: Path,
    build_path: Path,
    pyslang_site: Path,
    project_name: str,
    source_revision: str,
    output: Path,
    schema_version: str = "v0.1",
    publication_contract: Path | None = None,
) -> dict[str, Any]:
    """Build a complete actual-design candidate from one Runtime Compilation."""
    from rtl_query_runtime import RuntimeQuerySession

    started = time.perf_counter()
    if output.is_symlink():
        raise RuntimeError("output candidate already exists")
    output = output.parent.resolve() / output.name
    if output.exists() or output.is_symlink():
        raise RuntimeError("output candidate already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(
        prefix=output.name + ".building-", dir=output.parent,
    )) / output.name
    store: PrototypeStore | None = None
    try:
        with RuntimeQuerySession.open(
            source_root=source_root, build_path=build_path,
            project_name=project_name, source_revision=source_revision,
            pyslang_site=pyslang_site,
        ) as session:
            ready = session.ready_result()
            identity = ready["compilation_identity"]
            store = PrototypeStore.create(temporary, {
                "project": project_name,
                "source_revision": source_revision,
                "build_sha256": identity["build_sha256"],
                "build_spec_sha256": identity["build_spec_sha256"],
                "source_closure_sha256": identity["source_closure_sha256"],
                "effective_input_count": identity["effective_input_count"],
                "scope_selection": "whole-design",
                **({"candidate_id": hashlib.sha256(
                    (project_name + "\0" + source_revision + "\0" + identity["build_sha256"]
                     + "\0whole-design\0" + hashlib.sha256(publication_contract.read_bytes()).hexdigest()).encode()
                ).hexdigest()} if publication_contract else {}),
            }, schema_version=schema_version, publication_contract=publication_contract)
            if schema_version == "v0.2":
                store.begin_stage1_audit()
            with store.transaction():
                normalized = populate_design_scope(
                    store, session=session, source_root=source_root,
                    project_name=project_name, compilation_identity=identity,
                )
                root_ids = normalized["root_instance_entity_ids"]
                roots = [
                    {
                        "entity_id": entity_id,
                        "kind": store.contract_id("entities", "INSTANCE"),
                        "role": "PRIMARY" if index == 0 else "PEER",
                        "resolution_state": "RESOLVED",
                    }
                    for index, entity_id in enumerate(root_ids)
                ]
                analysis_scope = store.register_analysis_scope(
                    scope_mode=(
                        "ACTUAL_INSTANCE_ROOT" if len(roots) == 1 else "MULTI_ROOT"
                    ),
                    roots=roots,
                    boundary_policy={
                        "kind": "ROOTED_CONTAINS_CLOSURE",
                        "max_depth": normalized["max_containment_depth"],
                        "knowledge_source_paths": normalized["source_paths"],
                    },
                    dependency_policy={
                        "kind": "REFERENCED_DEFINITIONS_AND_EVIDENCE",
                    },
                    applicability={
                        "schema": "rtl-analysis-agent/applicability-v1",
                        "constraints": {},
                    },
                    profile=(store.metadata("semantic_publication_contract")["profile"]
                             if publication_contract else {
                        "id": ("rtl-analysis-agent/g53-stage1-only"
                               if schema_version == "v0.2" else "rtl-analysis-agent/build1"),
                        "version": "1",
                    }),
                    vocabulary=(store.metadata("semantic_publication_contract")["vocabulary"]
                                if publication_contract else {
                        "id": "rtl-analysis-agent/knowledge-contract",
                        "version": store.metadata("contract_version"),
                    }),
                    producer_capability={
                        "actual_hierarchy": "FULL",
                        "module_definitions": "AVAILABLE",
                        "parameters": "EVIDENCE_GROUNDED",
                        "bindings": "BOUNDED_EVIDENCE_GROUNDED",
                        "deterministic_facts": "AVAILABLE",
                        "source": "HASH_VERIFIED_LOCATORS",
                        "semantic": "NOT_BUILT",
                    },
                    completeness={
                        "structure": "COMPLETE",
                        "source": "COMPLETE_FOR_PUBLISHED_FACTS",
                        "semantic": "NOT_BUILT",
                    },
                    limitations=[
                        "Build closure inputs without published structural Evidence are not Knowledge Scope sources.",
                        "Binding expressions retain bounded Runtime structure and do not assert bit equivalence.",
                    ],
                )
            integrity = _audit_stage1_candidate(
                store, identity=identity, scope=analysis_scope,
                normalized=normalized, design=True,
            )
            coverage_table = (
                "obligation_coverage" if schema_version == "v0.2"
                else "knowledge_coverage"
            )
            counts = {
                table: store.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "entity", "fact", "evidence", "fact_evidence",
                    "ai_knowledge", coverage_table,
                )
            }
            store.close()
            store = None
        _publish_staged_candidate(temporary, output)
        return {
            "schema": "rtl-analysis-agent/runtime-design-candidate-build-v1",
            "analysis_scope": analysis_scope,
            "compilation_identity": identity,
            "diagnostic_summary": ready["diagnostic_summary"],
            "scope": normalized,
            "candidate": {
                "path": output.as_posix(),
                "size_bytes": output.stat().st_size,
                "sha256": sha256_file(output),
                "counts": counts,
                "integrity": integrity,
            },
            "execution_summary": {
                "scope": "whole-design",
                "actual_path": "FULL_BUILD_TO_COMPLETE_STRUCTURAL_CANDIDATE",
                "returned_items": counts["entity"] + counts["fact"] + counts["evidence"],
                "stop_or_fallback_reason": ["whole-design-structure-complete"],
                "wall_seconds": round(time.perf_counter() - started, 3),
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            },
        }
    except BaseException as error:
        if store is not None:
            try:
                store.close()
            except BaseException as close_error:
                error.add_note(f"candidate connection close failed: {close_error!r}")
        try:
            _cleanup_owned_staging(temporary, output.parent)
        except BaseException as cleanup_error:
            error.add_note(f"owned staging cleanup failed: {cleanup_error!r}")
        raise
