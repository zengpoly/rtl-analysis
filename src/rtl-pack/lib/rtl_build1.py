"""Thin, resumable orchestration for the compiler-grounded Build1 product path."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import sqlite3
import stat
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Any

import rtl_manual_v6 as manual_v6
import rtl_module_knowledge as module_knowledge
import rtl_query_runtime as query_runtime
from rtl_knowledge_prototype import PrototypeStore


STATUS_SCHEMA = "rtl-analysis-agent/build1-status-v1"
SUPPORTED_SCOPES = {"whole-design", "actual-root"}
REQUIRED_MANUAL_FILES = (
    "index.html",
    "manifest.json",
    "assets/manual.css",
    "assets/manual.js",
    "assets/tree.json",
    "assets/search-index.json",
    "assets/tree-data.js",
    "assets/search-data.js",
)


class Build1Blocked(RuntimeError):
    def __init__(self, code: str, message: str, next_action: str):
        super().__init__(message)
        self.code = code
        self.next_action = next_action


def _canonical_json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Build1Blocked(
            "MANUAL_INVALID",
            f"Manual manifest is missing or invalid: {error}",
            "Select a new Manual output path or remove the invalid derived Manual after review.",
        ) from error
    if not isinstance(value, dict):
        raise Build1Blocked(
            "MANUAL_INVALID",
            "Manual manifest must contain a JSON object.",
            "Select a new Manual output path or remove the invalid derived Manual after review.",
        )
    return value


def _counts(model: dict[str, Any]) -> dict[str, int]:
    actuals = model["actuals"]
    return {
        "actual_instances": len(actuals),
        "definitions": len(model["modules"]),
        "parameters": sum(
            len(item["structured_detail"]["effective_parameters"])
            for item in actuals
        ),
        "bindings": sum(
            len(item["structured_detail"]["bindings"])
            for item in actuals
        ),
        "knowledge_sources": len(model["sources"]),
    }


def _scope_summary(
    model: dict[str, Any], requested_scope: str, target_path: str | None
) -> dict[str, Any]:
    entities = model["entities"]
    roots = [
        {
            "entity_id": root["entity_id"],
            "kind": root["kind"],
            "qualified_name": entities[root["entity_id"]]["qualified_name"],
        }
        for root in model["scope"]["roots"]
    ]
    return {
        "requested_mode": requested_scope,
        "requested_target_path": target_path,
        "resolved_mode": model["scope"]["scope_mode"],
        "roots": roots,
    }


def _validate_candidate_full(
    *,
    candidate: Path,
    source_root: Path,
    build_path: Path,
    source_revision: str,
    requested_scope: str,
    target_path: str | None,
) -> tuple[dict[str, Any], dict[str, int]]:
    try:
        model = manual_v6.load_backbone(candidate, source_root)
    except manual_v6.SourceSafetyError as error:
        raise Build1Blocked(
            "SOURCE_STALE",
            str(error),
            "Restore the frozen source identity or select the matching candidate; do not patch SQLite.",
        ) from error
    except (KeyError, OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as error:
        raise Build1Blocked(
            "CANDIDATE_INTEGRITY_FAILED",
            str(error),
            "Keep the artifact for diagnosis and build a new candidate at a new path.",
        ) from error

    scope = model["scope"]
    try:
        spec = query_runtime._load_build(source_root.resolve(), build_path.resolve())
        identity_spec = {
            key: value for key, value in spec.items() if not key.startswith("_")
        }
        expected_build_spec = {
            "build_spec_sha256": query_runtime._canonical_sha256(identity_spec)
        }
    except query_runtime.QueryRuntimeError as error:
        raise Build1Blocked(
            "BUILD_IDENTITY_INVALID",
            str(error),
            "Restore the exact Project-relative Runtime build identity and rerun.",
        ) from error
    build_identity = scope["build_identity"]
    if (
        build_identity.get("source_revision") != source_revision
        or build_identity.get("build_spec_sha256")
        != expected_build_spec["build_spec_sha256"]
    ):
        raise Build1Blocked(
            "BUILD_IDENTITY_MISMATCH",
            "Candidate source revision or build specification does not match the selected Project.",
            "Select the matching immutable candidate or build a new candidate at a new path.",
        )

    metadata = model["metadata"]
    roots = scope["roots"]
    if requested_scope == "whole-design":
        if (
            metadata.get("scope_selection") != "whole-design"
            or scope["producer_capability"].get("actual_hierarchy") != "FULL"
            or scope["completeness"].get("structure") != "COMPLETE"
        ):
            raise Build1Blocked(
                "SCOPE_IDENTITY_MISMATCH",
                "Existing candidate is not a complete Whole Design Build1 candidate.",
                "Select the matching candidate or a new candidate output path.",
            )
    else:
        root_names = [
            model["entities"][root["entity_id"]]["qualified_name"] for root in roots
        ]
        conformance = metadata.get("module_core_v1_cycle1_conformance")
        if (
            scope["scope_mode"] != "ACTUAL_INSTANCE_ROOT"
            or root_names != [target_path]
            or metadata.get("target_instance_path") != target_path
            or not isinstance(conformance, dict)
            or conformance.get("status") != "READY"
        ):
            raise Build1Blocked(
                "SCOPE_IDENTITY_MISMATCH",
                "Existing candidate does not match the exact actual-root scope or is not READY.",
                "Use an exact actual path and a new candidate output path.",
            )

    if any(
        item["structured_detail"].get("parameters_truncated") is not False
        for item in model["actuals"]
    ):
        raise Build1Blocked(
            "MANDATORY_SKELETON_TRUNCATED",
            "Candidate contains truncated effective parameters.",
            "Stop and repair the deterministic producer; do not publish this candidate.",
        )
    if model.get("ai_count") != 0 or model.get("behavior_count") != 0:
        raise Build1Blocked(
            "BUILD1_BOUNDARY_VIOLATION",
            "Candidate contains AI or Behavior content outside the Build1 contract.",
            "Select a compiler-only Build1 candidate.",
        )
    return model, _counts(model)


def _candidate_integrity(candidate: Path) -> dict[str, Any]:
    store = PrototypeStore.open(candidate, readonly=True)
    try:
        return store.integrity()
    finally:
        store.close()


def _validate_candidate_lightweight(
    *,
    candidate: Path,
    source_root: Path,
    build_path: Path,
    source_revision: str,
    requested_scope: str,
    target_path: str | None,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Validate an immutable Build1 Store without constructing Manual PageViews."""
    supplied_candidate = Path(candidate)
    if supplied_candidate.is_symlink():
        raise Build1Blocked(
            "CANDIDATE_INTEGRITY_FAILED",
            "Candidate database must be a regular non-symlink file.",
            "Keep the artifact for diagnosis and build a new candidate at a new path.",
        )
    try:
        candidate = supplied_candidate.resolve(strict=True)
        source_root = Path(source_root).resolve(strict=True)
    except OSError as error:
        raise Build1Blocked(
            "CANDIDATE_INTEGRITY_FAILED",
            str(error),
            "Keep the artifact for diagnosis and build a new candidate at a new path.",
        ) from error
    if not candidate.is_file():
        raise Build1Blocked(
            "CANDIDATE_INTEGRITY_FAILED",
            "Candidate database must be a regular non-symlink file.",
            "Keep the artifact for diagnosis and build a new candidate at a new path.",
        )

    store: PrototypeStore | None = None
    executor: ThreadPoolExecutor | None = None
    try:
        store = PrototypeStore.open(candidate, readonly=True)
        scope = store.analysis_scope()
        metadata = {
            row["key"]: json.loads(row["value_json"])
            for row in store.connection.execute(
                "SELECT key, value_json FROM model_metadata ORDER BY key"
            )
        }

        try:
            spec = query_runtime._load_build(source_root, build_path.resolve())
            identity_spec = {
                key: value for key, value in spec.items() if not key.startswith("_")
            }
            expected_build_spec_sha256 = query_runtime._canonical_sha256(identity_spec)
        except query_runtime.QueryRuntimeError as error:
            raise Build1Blocked(
                "BUILD_IDENTITY_INVALID",
                str(error),
                "Restore the exact Project-relative Runtime build identity and rerun.",
            ) from error
        build_identity = scope["build_identity"]
        if (
            build_identity.get("source_revision") != source_revision
            or build_identity.get("build_spec_sha256")
            != expected_build_spec_sha256
        ):
            raise Build1Blocked(
                "BUILD_IDENTITY_MISMATCH",
                "Candidate source revision or build specification does not match the selected Project.",
                "Select the matching immutable candidate or build a new candidate at a new path.",
            )

        instance_kind = store.contract_id("entities", "INSTANCE")
        module_kind = store.contract_id("entities", "MODULE")
        source_kind = store.contract_id("entities", "SOURCE_FILE")
        port_kind = store.contract_id("entities", "PORT")
        contains = store.contract_id("facts", "CONTAINS")
        instance_of = store.contract_id("facts", "INSTANCE_OF")
        binds_to = store.contract_id("facts", "BINDS_TO")
        roots = scope["roots"]
        root_ids = [root["entity_id"] for root in roots if root["kind"] == instance_kind]
        all_instance_rows = {
            str(row["entity_id"]): row
            for row in store.connection.execute(
                "SELECT entity_id, qualified_name, attrs_json FROM entity "
                "WHERE kind=? ORDER BY entity_id",
                (instance_kind,),
            )
        }
        root_rows = {
            root["entity_id"]: all_instance_rows[root["entity_id"]]["qualified_name"]
            for root in roots
        }
        root_names = [root_rows[root["entity_id"]] for root in roots]
        if requested_scope == "whole-design":
            if (
                metadata.get("scope_selection") != "whole-design"
                or scope["producer_capability"].get("actual_hierarchy") != "FULL"
                or scope["completeness"].get("structure") != "COMPLETE"
            ):
                raise Build1Blocked(
                    "SCOPE_IDENTITY_MISMATCH",
                    "Existing candidate is not a complete Whole Design Build1 candidate.",
                    "Select the matching candidate or a new candidate output path.",
                )
        else:
            conformance = metadata.get("module_core_v1_cycle1_conformance")
            if (
                scope["scope_mode"] != "ACTUAL_INSTANCE_ROOT"
                or root_names != [target_path]
                or metadata.get("target_instance_path") != target_path
                or not isinstance(conformance, dict)
                or conformance.get("status") != "READY"
            ):
                raise Build1Blocked(
                    "SCOPE_IDENTITY_MISMATCH",
                    "Existing candidate does not match the exact actual-root scope or is not READY.",
                    "Use an exact actual path and a new candidate output path.",
                )

        executor = ThreadPoolExecutor(max_workers=2)
        candidate_digest = executor.submit(manual_v6._sha256, candidate)
        integrity_check = executor.submit(_candidate_integrity, candidate)

        actual_ids = set(root_ids)
        parent_by_actual: dict[str, str | None] = {
            identifier: None for identifier in root_ids
        }
        children_by_actual: dict[str, list[str]] = {}
        for row in store.connection.execute(
            "SELECT f.subject_id, f.object_id FROM fact f "
            "JOIN entity child ON child.entity_id=f.object_id "
            "WHERE f.predicate=? AND child.kind=? "
            "ORDER BY f.subject_id, f.object_id",
            (contains, instance_kind),
        ):
            children_by_actual.setdefault(str(row["subject_id"]), []).append(
                str(row["object_id"])
            )
        frontier = sorted(root_ids)
        for _depth in range(scope["boundary_policy"]["max_depth"]):
            if not frontier:
                break
            next_frontier: list[str] = []
            for parent_id in frontier:
                for child_id in children_by_actual.get(parent_id, []):
                    known_parent = parent_by_actual.get(child_id)
                    if child_id in actual_ids and known_parent != parent_id:
                        raise ValueError("actual instance has ambiguous in-scope parentage")
                    if child_id not in actual_ids:
                        actual_ids.add(child_id)
                        parent_by_actual[child_id] = parent_id
                        next_frontier.append(child_id)
            frontier = sorted(next_frontier)
        sorted_actual_ids = sorted(actual_ids)

        if actual_ids != set(all_instance_rows):
            raise ValueError("Analysis Scope actual instance is missing")
        parameter_count = 0
        for identifier in sorted_actual_ids:
            row = all_instance_rows[identifier]
            attrs = json.loads(row["attrs_json"])
            parameters = attrs.get("effective_parameters")
            if (
                not isinstance(parameters, list)
                or attrs.get("parameters_truncated") is not False
            ):
                raise Build1Blocked(
                    "MANDATORY_SKELETON_TRUNCATED",
                    "Candidate has missing or truncated effective parameters.",
                    "Stop and repair the deterministic producer; do not publish this candidate.",
                )
            parameter_count += len(parameters)

        definition_rows = store.connection.execute(
            "SELECT f.subject_id, f.object_id, definition.kind FROM fact f "
            "JOIN entity definition ON definition.entity_id=f.object_id "
            "WHERE f.predicate=? "
            "ORDER BY f.subject_id, f.object_id",
            (instance_of,),
        )
        definitions_by_actual: dict[str, list[str]] = {
            identifier: [] for identifier in sorted_actual_ids
        }
        for row in definition_rows:
            subject_id = str(row["subject_id"])
            if subject_id not in actual_ids:
                continue
            if row["kind"] != module_kind:
                raise ValueError("INSTANCE_OF object is not a module definition")
            definitions_by_actual[subject_id].append(str(row["object_id"]))
        if any(len(values) != 1 for values in definitions_by_actual.values()):
            raise ValueError("instance lacks exactly one module definition")
        definition_ids = {
            values[0] for values in definitions_by_actual.values()
        }

        binding_count = store.connection.execute(
            "SELECT count(*) FROM fact WHERE predicate=?", (binds_to,)
        ).fetchone()[0]
        typed_binding_count = store.connection.execute(
            "SELECT count(*) FROM fact binding "
            "JOIN entity port ON port.entity_id=binding.subject_id "
            "WHERE binding.predicate=? AND port.kind=?",
            (binds_to, port_kind),
        ).fetchone()[0]
        if binding_count != typed_binding_count:
            raise ValueError("Build1 binding subject is not a Contract PORT")

        source_paths = scope["boundary_policy"]["knowledge_source_paths"]
        source_entities = {
            row["qualified_name"]: json.loads(row["attrs_json"])
            for row in store.connection.execute(
                "SELECT qualified_name, attrs_json FROM entity WHERE kind=?",
                (source_kind,),
            )
        }
        for stored_path in source_paths:
            attrs = source_entities.get(stored_path)
            if attrs is None:
                raise manual_v6.SourceSafetyError(
                    f"scope source is not a SOURCE_FILE: {stored_path}"
                )
            expected = attrs.get("sha256")
            if not isinstance(expected, str) or len(expected) != 64:
                raise manual_v6.SourceSafetyError(
                    f"SOURCE_FILE lacks authoritative SHA-256: {stored_path}"
                )
            resolved = manual_v6._safe_source_path(source_root, stored_path)
            actual = manual_v6._sha256(resolved) if resolved.is_file() else None
            if actual != expected:
                raise manual_v6.SourceSafetyError(
                    f"source hash/availability validation failed: {stored_path}"
                )

        ai_count = store.connection.execute(
            "SELECT count(*) FROM ai_knowledge"
        ).fetchone()[0]
        behavior_tables = [
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND lower(name) LIKE '%behavior%'"
            )
        ]
        if ai_count != 0 or behavior_tables:
            raise Build1Blocked(
                "BUILD1_BOUNDARY_VIOLATION",
                "Candidate contains AI or Behavior content outside the Build1 contract.",
                "Select a compiler-only Build1 candidate.",
            )

        integrity = integrity_check.result()
        candidate_sha256 = candidate_digest.result()
        if integrity["integrity_check"] != "ok" or integrity["foreign_key_check"]:
            raise ValueError("candidate integrity or foreign keys failed")

        model = {
            "candidate_sha256": candidate_sha256,
            "scope": scope,
            "metadata": metadata,
            "entities": {
                root["entity_id"]: {
                    "qualified_name": root_rows[root["entity_id"]],
                }
                for root in roots
            },
        }
        counts = {
            "actual_instances": len(sorted_actual_ids),
            "definitions": len(definition_ids),
            "parameters": parameter_count,
            "bindings": binding_count,
            "knowledge_sources": len(source_paths),
        }
        return model, counts
    except Build1Blocked:
        raise
    except manual_v6.SourceSafetyError as error:
        raise Build1Blocked(
            "SOURCE_STALE",
            str(error),
            "Restore the frozen source identity or select the matching candidate; do not patch SQLite.",
        ) from error
    except (KeyError, OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as error:
        raise Build1Blocked(
            "CANDIDATE_INTEGRITY_FAILED",
            str(error),
            "Keep the artifact for diagnosis and build a new candidate at a new path.",
        ) from error
    finally:
        if store is not None:
            store.close()
        if executor is not None:
            executor.shutdown(wait=True)


def _validate_manual(
    *,
    manual: Path,
    model: dict[str, Any],
    counts: dict[str, int],
    source_viewer: str,
) -> dict[str, Any]:
    if manual.is_symlink() or not manual.is_dir():
        raise Build1Blocked(
            "MANUAL_INVALID",
            "Existing Manual output is not a safe directory.",
            "Select a new Manual output path.",
        )
    manual_root = manual.resolve(strict=True)
    manifest_path = manual / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise Build1Blocked(
            "MANUAL_INCOMPLETE",
            "Existing Manual is missing a safe regular manifest.json.",
            "Do not overwrite the derived Manual. Select a new Manual output path, or after review delete or move the damaged Manual and rerun the same command to render it.",
        )
    manifest = _canonical_json_file(manifest_path)
    expected = {
        "schema": "rtl-analysis-agent/manual-v6-build1-manifest-v1",
        "scope_id": model["scope"]["scope_id"],
        "build_sha256": model["scope"]["build_identity"]["build_sha256"],
        "candidate_sha256": model["candidate_sha256"],
        "source_serving": source_viewer,
    }
    mismatched = [key for key, value in expected.items() if manifest.get(key) != value]
    page_counts = manifest.get("page_counts", {})
    structured_counts = manifest.get("structured_detail_counts", {})
    if (
        page_counts.get("instance") != counts["actual_instances"]
        or page_counts.get("module_definition") != counts["definitions"]
        or structured_counts.get("instance") != counts["actual_instances"]
        or structured_counts.get("module_definition") != counts["definitions"]
    ):
        mismatched.append("counts")
    if mismatched:
        raise Build1Blocked(
            "MANUAL_IDENTITY_MISMATCH",
            f"Existing Manual does not match the candidate: {sorted(set(mismatched))}",
            "Select a new Manual output path or remove the mismatched derived Manual after review.",
        )

    declared_paths: list[str] = []
    declared_maps: dict[str, dict[str, str]] = {}
    for field in ("page_map", "structured_detail_map"):
        mapping = manifest.get(field)
        if not isinstance(mapping, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in mapping.items()
        ):
            raise Build1Blocked(
                "MANUAL_INCOMPLETE",
                f"Existing Manual {field} is missing or invalid.",
                "Do not overwrite the derived Manual. Select a new Manual output path, or after review delete or move the damaged Manual and rerun the same command to render it.",
            )
        declared_maps[field] = mapping
        declared_paths.extend(mapping.values())
    expected_page_map_count = sum(
        value for value in page_counts.values() if isinstance(value, int)
    )
    expected_structured_map_count = sum(
        value for value in structured_counts.values() if isinstance(value, int)
    )
    if (
        len(declared_maps["page_map"]) != expected_page_map_count
        or len(set(declared_maps["page_map"].values())) != expected_page_map_count
        or len(declared_maps["structured_detail_map"])
        != expected_structured_map_count
        or len(set(declared_maps["structured_detail_map"].values()))
        != expected_structured_map_count
    ):
        raise Build1Blocked(
            "MANUAL_INCOMPLETE",
            "Existing Manual path maps do not match their declared page counts.",
            "Do not overwrite the derived Manual. Select a new Manual output path, or after review delete or move the damaged Manual and rerun the same command to render it.",
        )
    required_paths = list(REQUIRED_MANUAL_FILES)
    if source_viewer == "full":
        required_paths.extend(("assets/source.css", "assets/source-span.js"))

    invalid_paths: list[str] = []
    safe_directories: set[Path] = {manual_root}
    for stored_path in [*required_paths, *declared_paths]:
        relative = PurePosixPath(stored_path)
        if (
            not stored_path
            or "\\" in stored_path
            or relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or relative.as_posix() != stored_path
        ):
            invalid_paths.append(stored_path)
            continue
        current = manual_root
        path_is_safe = True
        for part in relative.parts[:-1]:
            current = current / part
            if current in safe_directories:
                continue
            try:
                mode = os.lstat(current).st_mode
            except OSError:
                path_is_safe = False
                break
            if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                path_is_safe = False
                break
            safe_directories.add(current)
        if not path_is_safe:
            invalid_paths.append(stored_path)
            continue
        artifact = current / relative.parts[-1]
        try:
            mode = os.lstat(artifact).st_mode
        except OSError:
            invalid_paths.append(stored_path)
            continue
        if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            invalid_paths.append(stored_path)
    if invalid_paths:
        sample = sorted(set(invalid_paths))[:5]
        raise Build1Blocked(
            "MANUAL_INCOMPLETE",
            f"Existing Manual has missing or unsafe serving artifacts: {sample}",
            "Do not overwrite the derived Manual. Select a new Manual output path, or after review delete or move the damaged Manual and rerun the same command to render it.",
        )
    return manifest


def _base_result(
    *, candidate_relative: str, manual_relative: str, source_viewer: str
) -> dict[str, Any]:
    return {
        "schema": STATUS_SCHEMA,
        "status": "BLOCKED",
        "analysis_scope": None,
        "scope_id": None,
        "build_identity": None,
        "candidate": {
            "status": "NOT_READY",
            "path": candidate_relative,
            "sha256": None,
            "reused": False,
        },
        "counts": {
            "actual_instances": 0,
            "definitions": 0,
            "parameters": 0,
            "bindings": 0,
            "knowledge_sources": 0,
        },
        "manual": {
            "status": "NOT_READY",
            "path": manual_relative,
            "source_viewer": source_viewer,
            "reused": False,
        },
        "limitations": [],
        "timing": {
            "candidate_validation_wall_seconds": 0.0,
            "manual_validation_wall_seconds": 0.0,
            "build_wall_seconds": 0.0,
            "render_wall_seconds": 0.0,
            "total_wall_seconds": 0.0,
        },
        "heavy_actions": {"compile_build_count": 0, "render_count": 0},
        "reason_code": None,
        "human_message": "",
        "safe_next_action": "",
    }


def blocked_result(
    *,
    candidate_relative: str,
    manual_relative: str,
    source_viewer: str,
    reason_code: str,
    message: str,
    next_action: str,
) -> dict[str, Any]:
    """Return the public Build1 shape when CLI input validation fails early."""
    result = _base_result(
        candidate_relative=candidate_relative,
        manual_relative=manual_relative,
        source_viewer=source_viewer,
    )
    result.update(
        {
            "status": "BLOCKED",
            "reason_code": reason_code,
            "human_message": message,
            "safe_next_action": next_action,
        }
    )
    return result


def run_build1(
    *,
    project_name: str,
    source_revision: str,
    source_root: Path,
    build_path: Path,
    pyslang_site: Path | None,
    requested_scope: str,
    target_path: str | None,
    candidate: Path,
    candidate_relative: str,
    manual: Path,
    manual_relative: str,
    source_viewer: str,
) -> tuple[int, dict[str, Any]]:
    """Build, validate, and render once; resume only the missing safe phase."""
    started = time.perf_counter()
    build_wall = 0.0
    candidate_validation_wall = 0.0
    manual_validation_wall = 0.0
    render_wall = 0.0
    result = _base_result(
        candidate_relative=candidate_relative,
        manual_relative=manual_relative,
        source_viewer=source_viewer,
    )

    def finish(
        exit_code: int,
        status: str,
        reason_code: str | None,
        message: str,
        next_action: str,
    ) -> tuple[int, dict[str, Any]]:
        total = time.perf_counter() - started
        result["status"] = status
        result["reason_code"] = reason_code
        result["human_message"] = message
        result["safe_next_action"] = next_action
        result["timing"] = {
            "candidate_validation_wall_seconds": round(
                candidate_validation_wall, 3
            ),
            "manual_validation_wall_seconds": round(manual_validation_wall, 3),
            "build_wall_seconds": round(build_wall, 3),
            "render_wall_seconds": round(render_wall, 3),
            "total_wall_seconds": round(total, 3),
        }
        return exit_code, result

    if requested_scope not in SUPPORTED_SCOPES:
        result["limitations"] = [
            "No real product producer exists for this Analysis Scope mode."
        ]
        return finish(
            2,
            "BLOCKED",
            "SCOPE_MODE_UNSUPPORTED",
            f"Analysis Scope mode is not supported: {requested_scope}",
            "Use whole-design or actual-root, or implement and validate a real producer first.",
        )
    if requested_scope == "actual-root" and (
        not isinstance(target_path, str)
        or not target_path.strip()
        or target_path != target_path.strip()
        or "\n" in target_path
    ):
        return finish(
            2,
            "BLOCKED",
            "ACTUAL_ROOT_REQUIRED",
            "actual-root requires one exact elaborated target path.",
            "Provide --target-path with one exact actual identity; do not guess.",
        )
    if requested_scope == "whole-design" and target_path is not None:
        return finish(
            2,
            "BLOCKED",
            "SCOPE_ARGUMENT_CONFLICT",
            "whole-design does not accept --target-path.",
            "Remove --target-path or select actual-root.",
        )

    candidate_existed = candidate.exists() or candidate.is_symlink()
    if candidate_existed and (candidate.is_symlink() or not candidate.is_file()):
        return finish(
            2,
            "BLOCKED",
            "CANDIDATE_INVALID",
            "Existing candidate path is not a safe regular file.",
            "Select a new candidate output path.",
        )
    if not candidate_existed:
        if pyslang_site is None:
            return finish(
                2,
                "BLOCKED",
                "TOOL_UNAVAILABLE",
                "Locked PySlang is required to create a new candidate.",
                "Install the locked tool or reuse a matching READY candidate.",
            )
        result["heavy_actions"]["compile_build_count"] = 1
        phase_started = time.perf_counter()
        try:
            if requested_scope == "whole-design":
                module_knowledge.build_runtime_design_candidate(
                    source_root=source_root,
                    build_path=build_path,
                    pyslang_site=pyslang_site,
                    project_name=project_name,
                    source_revision=source_revision,
                    output=candidate,
                )
            else:
                module_knowledge.build_runtime_module_candidate(
                    source_root=source_root,
                    build_path=build_path,
                    pyslang_site=pyslang_site,
                    project_name=project_name,
                    source_revision=source_revision,
                    target_path=str(target_path),
                    output=candidate,
                )
        except Exception as error:
            build_wall = time.perf_counter() - phase_started
            reason = (
                "PRODUCER_BUDGET_EXHAUSTED"
                if "budget exhausted" in str(error) or "truncated" in str(error)
                else "CANDIDATE_BUILD_FAILED"
            )
            return finish(
                2,
                "BLOCKED",
                reason,
                str(error),
                "Fix the deterministic input/tool failure and rerun with the same unused candidate path.",
            )
        build_wall = time.perf_counter() - phase_started

    manual_existed = manual.exists() or manual.is_symlink()
    phase_started = time.perf_counter()
    try:
        validator = (
            _validate_candidate_lightweight
            if candidate_existed and manual_existed
            else _validate_candidate_full
        )
        model, counts = validator(
            candidate=candidate,
            source_root=source_root,
            build_path=build_path,
            source_revision=source_revision,
            requested_scope=requested_scope,
            target_path=target_path,
        )
    except Build1Blocked as error:
        candidate_validation_wall = time.perf_counter() - phase_started
        return finish(2, "BLOCKED", error.code, str(error), error.next_action)
    candidate_validation_wall = time.perf_counter() - phase_started

    result["analysis_scope"] = _scope_summary(model, requested_scope, target_path)
    result["scope_id"] = model["scope"]["scope_id"]
    result["build_identity"] = model["scope"]["build_identity"]
    result["counts"] = counts
    result["candidate"] = {
        "status": "READY",
        "path": candidate_relative,
        "sha256": model["candidate_sha256"],
        "reused": candidate_existed,
        "size_bytes": candidate.stat().st_size,
    }
    result["limitations"] = list(model["scope"].get("limitations", []))
    if source_revision == "unversioned":
        result["limitations"].append(
            "Project source revision is unversioned; Knowledge Source hashes are current, but repository revision immutability is not proven."
        )

    if manual_existed:
        phase_started = time.perf_counter()
        try:
            manifest = _validate_manual(
                manual=manual,
                model=model,
                counts=counts,
                source_viewer=source_viewer,
            )
        except Build1Blocked as error:
            manual_validation_wall = time.perf_counter() - phase_started
            return finish(3, "PARTIAL", error.code, str(error), error.next_action)
        manual_validation_wall = time.perf_counter() - phase_started
        result["manual"].update(
            {
                "status": "READY",
                "reused": True,
                "page_counts": manifest["page_counts"],
                "structured_detail_counts": manifest["structured_detail_counts"],
            }
        )
        return finish(0, "READY", None, "Build1 candidate and Manual are READY.", "NONE")

    staging = manual.with_name(manual.name + ".building")
    if staging.exists() or staging.is_symlink():
        return finish(
            3,
            "PARTIAL",
            "MANUAL_STAGING_EXISTS",
            "A stale Manual staging path exists; the READY candidate was not rebuilt.",
            "Inspect and remove only the reported derived staging path, then rerun the same command.",
        )
    result["heavy_actions"]["render_count"] = 1
    phase_started = time.perf_counter()
    try:
        manifest = manual_v6.build_site(
            model, staging, source_viewer=source_viewer
        )
        staging.replace(manual)
    except Exception as error:
        render_wall = time.perf_counter() - phase_started
        if staging.is_dir() and not staging.is_symlink():
            shutil.rmtree(staging)
        return finish(
            3,
            "PARTIAL",
            "MANUAL_RENDER_FAILED",
            str(error),
            "Rerun the same command; the READY candidate will be reused and only Manual render will run.",
        )
    render_wall = time.perf_counter() - phase_started
    result["manual"].update(
        {
            "status": "READY",
            "reused": False,
            "page_counts": manifest["page_counts"],
            "structured_detail_counts": manifest["structured_detail_counts"],
        }
    )
    return finish(0, "READY", None, "Build1 candidate and Manual are READY.", "NONE")
