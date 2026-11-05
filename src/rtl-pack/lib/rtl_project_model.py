#!/usr/bin/env python3
"""Portable SQLite property graph for bounded RTL project knowledge."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "rtlpm-v2"
JSONL_FORMAT = "rtl-analysis-agent/project-model-jsonl-v2"
KNOWLEDGE_REGISTRY_PATH = (
    Path(__file__).resolve().parents[1] / "config/rtl-knowledge-registry.json"
)
TABLE_ORDER = (
    "model_build",
    "producer_run",
    "entity",
    "external_ref",
    "evidence",
    "assertion",
    "assertion_evidence",
    "coverage",
    "capability_coverage",
)
PRIMARY_KEYS = {
    "model_build": ("model_id",),
    "producer_run": ("run_id",),
    "entity": ("entity_id",),
    "external_ref": ("run_id", "external_kind", "external_id"),
    "evidence": ("evidence_id",),
    "assertion": ("assertion_id",),
    "assertion_evidence": ("assertion_id", "evidence_id", "role"),
    "coverage": ("model_id", "scope", "dimension"),
    "capability_coverage": ("model_id", "scope", "dimension", "capability"),
}
OPERATIONAL_COLUMNS = {"created_at", "checked_at"}
PATH_PREDICATES = (
    "rtl.connectivity/1:binds-to",
    "rtl.connectivity/1:drives",
    "rtl.connectivity/1:loads",
    "rtl.behavior/1:data-depends-on",
    "rtl.behavior/1:control-depends-on",
    "rtl.temporal/1:next-cycle-from",
)

PREDICATE_PATTERN = re.compile(
    r"^[a-z][a-z0-9_.-]*/[1-9][0-9]*:[a-z][a-z0-9_.-]*$"
)
CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*\.v[1-9][0-9]*$")


class ModelError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _load_knowledge_registry() -> dict[str, Any]:
    try:
        registry = json.loads(KNOWLEDGE_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ModelError(
            "knowledge-registry", "RTL knowledge registry is missing or invalid"
        ) from error
    if (
        not isinstance(registry, dict)
        or registry.get("schema") != "rtl-analysis-agent/knowledge-registry-v1"
        or set(registry) != {"schema", "version", "dimensions", "predicates", "capabilities"}
    ):
        raise ModelError("knowledge-registry", "RTL knowledge registry shape is invalid")
    dimensions = registry.get("dimensions")
    if not isinstance(dimensions, list) or len(dimensions) != 7:
        raise ModelError("knowledge-registry", "RTL knowledge registry needs seven dimensions")
    dimension_ids = {entry.get("id") for entry in dimensions if isinstance(entry, dict)}
    if len(dimension_ids) != 7 or None in dimension_ids:
        raise ModelError("knowledge-registry", "RTL dimensions must have unique IDs")
    for group, pattern in (
        ("predicates", PREDICATE_PATTERN),
        ("capabilities", CAPABILITY_PATTERN),
    ):
        entries = registry.get(group)
        if not isinstance(entries, list):
            raise ModelError("knowledge-registry", f"RTL {group} registry is invalid")
        seen: set[str] = set()
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"id", "dimension", "description", "version"}
                or not pattern.fullmatch(str(entry.get("id", "")))
                or entry.get("dimension") not in dimension_ids
                or not str(entry.get("description", "")).strip()
                or not isinstance(entry.get("version"), int)
                or entry["version"] < 1
                or entry["id"] in seen
            ):
                raise ModelError("knowledge-registry", f"RTL {group} declaration is invalid")
            seen.add(entry["id"])
    return registry


KNOWLEDGE_REGISTRY = _load_knowledge_registry()
DIMENSIONS = tuple(entry["id"] for entry in KNOWLEDGE_REGISTRY["dimensions"])
DIMENSION_SET = frozenset(DIMENSIONS)
CORE_PREDICATES = {
    entry["id"]: entry for entry in KNOWLEDGE_REGISTRY["predicates"]
}
CORE_CAPABILITIES = {
    entry["id"]: entry for entry in KNOWLEDGE_REGISTRY["capabilities"]
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(*parts: Any) -> str:
    payload = "\0".join(
        part if isinstance(part, str) else canonical_json(part) for part in parts
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_identity(
    *,
    project_name: str,
    source_revision: str,
    config: dict[str, Any],
    toolchain: dict[str, Any],
    spec: dict[str, Any],
    schema_version: str = SCHEMA_VERSION,
) -> dict[str, str]:
    config_hash = digest(config)
    toolchain_hash = digest(toolchain)
    spec_hash = digest(spec)
    model_id = "m:" + digest(
        "rtlpm/model/v1",
        project_name,
        source_revision,
        config_hash,
        toolchain_hash,
        schema_version,
    )
    return {
        "model_id": model_id,
        "schema_version": schema_version,
        "project_name": project_name,
        "source_revision": source_revision,
        "config_hash": config_hash,
        "toolchain_hash": toolchain_hash,
        "spec_hash": spec_hash,
    }


def parse_kiss2(text: str) -> dict[str, Any]:
    """Parse the small, line-oriented KISS2 subset emitted by Yosys fsm_export."""
    headers: dict[str, int | str] = {}
    transitions: list[dict[str, str]] = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("."):
            parts = line.split(maxsplit=1)
            if len(parts) != 2 or parts[0] not in {".i", ".o", ".p", ".s", ".r"}:
                raise ModelError(
                    "kiss2-format", f"unsupported KISS2 header on line {line_number}"
                )
            key = parts[0][1:]
            headers[key] = parts[1] if key == "r" else int(parts[1])
            continue
        parts = line.split()
        if len(parts) != 4:
            raise ModelError(
                "kiss2-format", f"invalid KISS2 transition on line {line_number}"
            )
        transitions.append(
            {
                "input_pattern": parts[0],
                "current_state": parts[1],
                "next_state": parts[2],
                "output_pattern": parts[3],
            }
        )
    required = {"i", "o", "p", "s", "r"}
    if set(headers) != required or len(transitions) != headers["p"]:
        raise ModelError(
            "kiss2-format", "KISS2 header or transition count is inconsistent"
        )
    return {
        "input_count": headers["i"],
        "output_count": headers["o"],
        "transition_count": headers["p"],
        "state_count": headers["s"],
        "reset_encoding": headers["r"],
        "transitions": transitions,
    }


EMPTY_CONTRACT = {
    "required_capabilities": {},
    "project_profile": {"predicates": [], "capabilities": []},
}


def _validate_dimension(dimension: Any) -> str:
    value = str(dimension)
    if value not in DIMENSION_SET:
        raise ModelError("knowledge-dimension", f"unknown RTL dimension: {value}")
    return value


def _validate_contract(contract: dict[str, Any] | None) -> dict[str, Any]:
    value = json.loads(
        canonical_json(EMPTY_CONTRACT if contract is None else contract)
    )
    if set(value) != {"required_capabilities", "project_profile"}:
        raise ModelError("model-contract", "model contract shape is invalid")
    required = value["required_capabilities"]
    profile = value["project_profile"]
    if not isinstance(required, dict) or not isinstance(profile, dict):
        raise ModelError("model-contract", "model contract shape is invalid")
    if set(profile) != {"predicates", "capabilities"}:
        raise ModelError("model-contract", "Project profile shape is invalid")

    declared: dict[str, dict[str, dict[str, Any]]] = {
        "predicates": {},
        "capabilities": {},
    }
    patterns = {"predicates": PREDICATE_PATTERN, "capabilities": CAPABILITY_PATTERN}
    for group in ("predicates", "capabilities"):
        entries = profile[group]
        if not isinstance(entries, list):
            raise ModelError("model-contract", f"Project profile {group} must be a list")
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"id", "dimension", "description", "requires"}
                or not patterns[group].fullmatch(str(entry.get("id", "")))
                or not str(entry.get("id", "")).startswith("project.")
                or not str(entry.get("description", "")).strip()
                or not isinstance(entry.get("requires"), list)
                or len(set(entry["requires"])) != len(entry["requires"])
            ):
                raise ModelError("model-contract", f"Project profile {group} entry is invalid")
            _validate_dimension(entry["dimension"])
            if entry["id"] in declared[group]:
                raise ModelError("model-contract", f"duplicate Project profile ID: {entry['id']}")
            declared[group][entry["id"]] = entry

    known_capabilities = set(CORE_CAPABILITIES) | set(declared["capabilities"])
    for group in ("predicates", "capabilities"):
        for entry in declared[group].values():
            if any(
                not isinstance(item, str) or item not in known_capabilities
                for item in entry["requires"]
            ):
                raise ModelError(
                    "model-contract",
                    f"Project profile {entry['id']} requires an unknown capability",
                )
    for scope, capabilities in required.items():
        if (
            not isinstance(scope, str)
            or not scope.strip()
            or not isinstance(capabilities, list)
            or len(set(capabilities)) != len(capabilities)
            or any(
                not isinstance(capability, str) or capability not in known_capabilities
                for capability in capabilities
            )
        ):
            raise ModelError("model-contract", "required capabilities are invalid")
    return value


_DIMENSION_SQL = ",".join(f"'{value}'" for value in DIMENSIONS)


SCHEMA_SQL = f"""
PRAGMA foreign_keys = ON;
CREATE TABLE model_build (
    model_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    project_name TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    toolchain_hash TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    contract_json TEXT NOT NULL,
    content_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('building','complete','failed','stale')),
    created_at TEXT NOT NULL
);
CREATE TABLE producer_run (
    run_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL REFERENCES model_build(model_id) ON DELETE CASCADE,
    producer TEXT NOT NULL,
    version TEXT NOT NULL,
    command_json TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('building','complete','failed','stale')),
    diagnostics_json TEXT NOT NULL DEFAULT '[]',
    artifact_hash TEXT,
    UNIQUE(model_id, producer, version, command_json, scope_json, input_hash)
);
CREATE TABLE entity (
    entity_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL REFERENCES model_build(model_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    attrs_json TEXT NOT NULL DEFAULT '{{}}',
    UNIQUE(model_id, kind, qualified_name)
);
CREATE INDEX entity_name_idx ON entity(model_id, qualified_name);
CREATE INDEX entity_kind_idx ON entity(model_id, kind);
CREATE TABLE external_ref (
    run_id TEXT NOT NULL REFERENCES producer_run(run_id) ON DELETE CASCADE,
    external_kind TEXT NOT NULL,
    external_id TEXT NOT NULL,
    entity_id TEXT NOT NULL REFERENCES entity(entity_id) ON DELETE CASCADE,
    mapping_status TEXT NOT NULL DEFAULT 'verified',
    attrs_json TEXT NOT NULL DEFAULT '{{}}',
    PRIMARY KEY(run_id, external_kind, external_id)
);
CREATE TABLE evidence (
    evidence_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL REFERENCES model_build(model_id) ON DELETE CASCADE,
    producer_run_id TEXT REFERENCES producer_run(run_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    locator TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    content_hash TEXT,
    attrs_json TEXT NOT NULL DEFAULT '{{}}',
    CHECK (start_line IS NULL OR start_line > 0),
    CHECK (end_line IS NULL OR end_line >= start_line)
);
CREATE TABLE assertion (
    assertion_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL REFERENCES model_build(model_id) ON DELETE CASCADE,
    subject_id TEXT NOT NULL REFERENCES entity(entity_id) ON DELETE CASCADE,
    predicate TEXT NOT NULL,
    object_id TEXT REFERENCES entity(entity_id) ON DELETE CASCADE,
    literal_json TEXT,
    dimension TEXT NOT NULL CHECK (dimension IN ({_DIMENSION_SQL})),
    derivation TEXT NOT NULL CHECK (derivation IN ('tool','human','llm')),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    status TEXT NOT NULL CHECK (status IN ('verified','partial','conflicted','review-required','stale')),
    producer_run_id TEXT NOT NULL REFERENCES producer_run(run_id) ON DELETE CASCADE,
    attrs_json TEXT NOT NULL DEFAULT '{{}}',
    CHECK ((object_id IS NULL) != (literal_json IS NULL))
);
CREATE INDEX assertion_subject_idx ON assertion(model_id, subject_id, predicate);
CREATE INDEX assertion_object_idx ON assertion(model_id, object_id, predicate);
CREATE INDEX assertion_dimension_idx ON assertion(model_id, dimension, status);
CREATE TABLE assertion_evidence (
    assertion_id TEXT NOT NULL REFERENCES assertion(assertion_id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'support',
    PRIMARY KEY(assertion_id, evidence_id, role)
);
CREATE TABLE coverage (
    model_id TEXT NOT NULL REFERENCES model_build(model_id) ON DELETE CASCADE,
    scope TEXT NOT NULL,
    dimension TEXT NOT NULL CHECK (dimension IN ({_DIMENSION_SQL})),
    status TEXT NOT NULL CHECK (status IN ('verified','partial','unsupported','failed','stale')),
    reason TEXT NOT NULL,
    producer_run_id TEXT REFERENCES producer_run(run_id) ON DELETE SET NULL,
    checked_at TEXT NOT NULL,
    PRIMARY KEY(model_id, scope, dimension)
);
CREATE TABLE capability_coverage (
    model_id TEXT NOT NULL REFERENCES model_build(model_id) ON DELETE CASCADE,
    scope TEXT NOT NULL,
    dimension TEXT NOT NULL CHECK (dimension IN ({_DIMENSION_SQL})),
    capability TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('verified','partial','unsupported','failed','stale')),
    reason TEXT NOT NULL,
    producer_run_id TEXT REFERENCES producer_run(run_id) ON DELETE SET NULL,
    checked_at TEXT NOT NULL,
    PRIMARY KEY(model_id, scope, dimension, capability)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _json_value(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _yosys_polarity(value: Any, *, positive: str, negative: str) -> str | None:
    """Turn a Yosys binary parameter into a readable polarity label."""
    if value is None:
        return None
    bits = str(value).strip().lower()
    if not bits or any(bit not in "01" for bit in bits):
        return None
    return positive if int(bits, 2) else negative


def _decoded(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    for key in tuple(value):
        if key.endswith("_json"):
            value[key[:-5]] = _json_value(value.pop(key))
    return value


class ModelStore:
    def __init__(
        self,
        path: Path,
        connection: sqlite3.Connection,
        model_id: str,
        contract: dict[str, Any],
    ):
        self.path = path
        self.connection = connection
        self.model_id = model_id
        self.contract = contract
        profile = contract["project_profile"]
        self.predicate_declarations = {
            **CORE_PREDICATES,
            **{entry["id"]: entry for entry in profile["predicates"]},
        }
        self.capability_declarations = {
            **CORE_CAPABILITIES,
            **{entry["id"]: entry for entry in profile["capabilities"]},
        }
        self.extension_predicate_dimensions: dict[str, str] = {}

    @classmethod
    def create(
        cls,
        path: Path,
        identity: dict[str, str],
        *,
        contract: dict[str, Any] | None = None,
    ) -> "ModelStore":
        path = Path(path)
        if identity.get("schema_version") != SCHEMA_VERSION:
            raise ModelError(
                "model-identity", f"model identity must use {SCHEMA_VERSION}"
            )
        normalized_contract = _validate_contract(contract)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise ModelError("model-exists", "candidate model already exists")
        connection = _connect(path)
        try:
            connection.executescript(SCHEMA_SQL)
            connection.execute(
                """INSERT INTO model_build(
                       model_id,schema_version,project_name,source_revision,config_hash,
                       toolchain_hash,spec_hash,contract_json,status,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,'building',?)""",
                (
                    identity["model_id"],
                    identity["schema_version"],
                    identity["project_name"],
                    identity["source_revision"],
                    identity["config_hash"],
                    identity["toolchain_hash"],
                    identity["spec_hash"],
                    canonical_json(normalized_contract),
                    _now(),
                ),
            )
            connection.commit()
        except Exception:
            connection.close()
            path.unlink(missing_ok=True)
            raise
        return cls(path, connection, identity["model_id"], normalized_contract)

    @classmethod
    def open(cls, path: Path, *, readonly: bool = False) -> "ModelStore":
        path = Path(path)
        if not path.is_file():
            raise ModelError("model-missing", "model database does not exist")
        connection = _connect(path, readonly=readonly)
        try:
            rows = connection.execute(
                "SELECT model_id, schema_version, status FROM model_build ORDER BY model_id"
            ).fetchall()
        except sqlite3.DatabaseError as error:
            connection.close()
            raise ModelError(
                "model-invalid", "model database schema is invalid"
            ) from error
        if (
            len(rows) != 1
            or rows[0]["schema_version"] != SCHEMA_VERSION
            or rows[0]["status"] != "complete"
        ):
            connection.close()
            raise ModelError(
                "model-invalid", "model database is not one complete supported build"
            )
        try:
            contract_row = connection.execute(
                "SELECT contract_json FROM model_build WHERE model_id=?",
                (rows[0]["model_id"],),
            ).fetchone()
            contract = _validate_contract(json.loads(contract_row["contract_json"]))
        except (ModelError, ValueError, TypeError, sqlite3.DatabaseError) as error:
            connection.close()
            raise ModelError("model-invalid", "model contract is invalid") from error
        store = cls(path, connection, str(rows[0]["model_id"]), contract)
        try:
            store._validate_knowledge_rows()
        except Exception:
            connection.close()
            raise
        return store

    def close(self) -> None:
        self.connection.close()

    def _model(self) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM model_build WHERE model_id=?", (self.model_id,)
        ).fetchone()
        if row is None:
            raise ModelError("model-invalid", "model identity is missing")
        return row

    def add_producer_run(
        self,
        *,
        producer: str,
        version: str,
        command: list[str],
        scope: dict[str, Any],
        input_hash: str,
        status: str,
        diagnostics: list[Any] | None = None,
        artifact_hash: str | None = None,
    ) -> str:
        command_json = canonical_json(command)
        scope_json = canonical_json(scope)
        run_id = "r:" + digest(
            "rtlpm/run/v1",
            self.model_id,
            producer,
            version,
            command_json,
            scope_json,
            input_hash,
        )
        self.connection.execute(
            """INSERT OR IGNORE INTO producer_run(
                   run_id,model_id,producer,version,command_json,scope_json,input_hash,
                   status,diagnostics_json,artifact_hash
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                self.model_id,
                producer,
                version,
                command_json,
                scope_json,
                input_hash,
                status,
                canonical_json(diagnostics or []),
                artifact_hash,
            ),
        )
        return run_id

    def add_entity(
        self,
        kind: str,
        qualified_name: str,
        *,
        display_name: str | None = None,
        attrs: dict[str, Any] | None = None,
    ) -> str:
        model = self._model()
        entity_id = "e:" + digest(
            "rtlpm/entity/v1",
            model["project_name"],
            model["source_revision"],
            model["config_hash"],
            kind,
            qualified_name,
        )
        values = (
            entity_id,
            self.model_id,
            kind,
            qualified_name,
            display_name or qualified_name.rsplit(".", 1)[-1],
            canonical_json(attrs or {}),
        )
        self.connection.execute(
            """INSERT INTO entity(entity_id,model_id,kind,qualified_name,display_name,attrs_json)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(model_id,kind,qualified_name) DO UPDATE SET
                   display_name=excluded.display_name, attrs_json=excluded.attrs_json""",
            values,
        )
        return entity_id

    def add_external_ref(
        self,
        run_id: str,
        external_kind: str,
        external_id: str,
        entity_id: str,
        *,
        mapping_status: str = "verified",
        attrs: dict[str, Any] | None = None,
    ) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO external_ref(
                   run_id,external_kind,external_id,entity_id,mapping_status,attrs_json
               ) VALUES(?,?,?,?,?,?)""",
            (
                run_id,
                external_kind,
                external_id,
                entity_id,
                mapping_status,
                canonical_json(attrs or {}),
            ),
        )

    def add_evidence(
        self,
        producer_run_id: str | None,
        *,
        kind: str,
        locator: str,
        start_line: int | None = None,
        end_line: int | None = None,
        content_hash: str | None = None,
        attrs: dict[str, Any] | None = None,
    ) -> str:
        evidence_id = "v:" + digest(
            "rtlpm/evidence/v1",
            self.model_id,
            producer_run_id or "",
            kind,
            locator,
            start_line,
            end_line,
            content_hash or "",
            attrs or {},
        )
        self.connection.execute(
            """INSERT OR IGNORE INTO evidence(
                   evidence_id,model_id,producer_run_id,kind,locator,start_line,end_line,
                   content_hash,attrs_json
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                evidence_id,
                self.model_id,
                producer_run_id,
                kind,
                locator,
                start_line,
                end_line,
                content_hash,
                canonical_json(attrs or {}),
            ),
        )
        return evidence_id

    def _validate_predicate(self, predicate: Any, dimension: str) -> str:
        value = str(predicate)
        if not PREDICATE_PATTERN.fullmatch(value):
            raise ModelError("knowledge-predicate", f"invalid versioned predicate: {value}")
        declaration = self.predicate_declarations.get(value)
        if declaration is not None:
            canonical = declaration["dimension"]
        elif value.startswith("project."):
            raise ModelError(
                "knowledge-predicate",
                f"Project predicate is not declared in the Project profile: {value}",
            )
        else:
            canonical = self.extension_predicate_dimensions.setdefault(value, dimension)
        if canonical != dimension:
            raise ModelError(
                "knowledge-predicate",
                f"predicate {value} has primary dimension {canonical}, not {dimension}",
            )
        return value

    def _private_predicate(self, predicate: str) -> dict[str, Any] | None:
        declaration = self.predicate_declarations.get(predicate)
        if declaration is None or not predicate.startswith("project."):
            return None
        return declaration

    def add_assertion(
        self,
        *,
        subject_id: str,
        predicate: str,
        object_id: str | None = None,
        literal: Any | None = None,
        dimension: str,
        derivation: str,
        confidence: float,
        status: str,
        producer_run_id: str,
        evidence_ids: Iterable[str],
        attrs: dict[str, Any] | None = None,
    ) -> str:
        dimension = _validate_dimension(dimension)
        predicate = self._validate_predicate(predicate, dimension)
        if (object_id is None) == (literal is None):
            raise ModelError(
                "assertion-shape", "an assertion needs exactly one object or literal"
            )
        evidence_ids = tuple(evidence_ids)
        if not evidence_ids:
            raise ModelError("assertion-evidence", "every assertion must have evidence")
        assertion_attrs = dict(attrs or {})
        private = self._private_predicate(predicate)
        if private is not None:
            assertion_attrs.setdefault("description", private["description"])
            assertion_attrs.setdefault("requires", private["requires"])
        literal_json = None if literal is None else canonical_json(literal)
        assertion_id = "a:" + digest(
            "rtlpm/assertion/v1",
            self.model_id,
            subject_id,
            predicate,
            object_id or "",
            literal_json or "",
            dimension,
            derivation,
            producer_run_id,
        )
        self.connection.execute(
            """INSERT INTO assertion(
                   assertion_id,model_id,subject_id,predicate,object_id,literal_json,dimension,
                   derivation,confidence,status,producer_run_id,attrs_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(assertion_id) DO UPDATE SET confidence=excluded.confidence,
                   status=excluded.status,attrs_json=excluded.attrs_json""",
            (
                assertion_id,
                self.model_id,
                subject_id,
                predicate,
                object_id,
                literal_json,
                dimension,
                derivation,
                confidence,
                status,
                producer_run_id,
                canonical_json(assertion_attrs),
            ),
        )
        for evidence_id in evidence_ids:
            self.connection.execute(
                "INSERT OR IGNORE INTO assertion_evidence(assertion_id,evidence_id,role) VALUES(?,?,'support')",
                (assertion_id, evidence_id),
            )
        return assertion_id

    def set_coverage(
        self,
        *,
        scope: str,
        dimension: str,
        status: str,
        reason: str,
        producer_run_id: str | None,
    ) -> None:
        dimension = _validate_dimension(dimension)
        self.connection.execute(
            """INSERT OR REPLACE INTO coverage(
                   model_id,scope,dimension,status,reason,producer_run_id,checked_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (self.model_id, scope, dimension, status, reason, producer_run_id, _now()),
        )

    def set_capability_coverage(
        self,
        *,
        scope: str,
        dimension: str,
        capability: str,
        status: str,
        reason: str,
        producer_run_id: str | None,
    ) -> None:
        dimension = _validate_dimension(dimension)
        declaration = self.capability_declarations.get(capability)
        if declaration is None or not CAPABILITY_PATTERN.fullmatch(str(capability)):
            raise ModelError(
                "knowledge-capability", f"capability is not declared: {capability}"
            )
        if declaration["dimension"] != dimension:
            raise ModelError(
                "knowledge-capability",
                f"capability {capability} has primary dimension {declaration['dimension']}, not {dimension}",
            )
        required = self.contract["required_capabilities"].get(scope, [])
        if capability not in required:
            raise ModelError(
                "knowledge-capability",
                f"capability {capability} is not required for scope {scope}",
            )
        self.connection.execute(
            """INSERT OR REPLACE INTO capability_coverage(
                   model_id,scope,dimension,capability,status,reason,producer_run_id,checked_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                self.model_id,
                scope,
                dimension,
                capability,
                status,
                reason,
                producer_run_id,
                _now(),
            ),
        )

    @staticmethod
    def _rollup_status(statuses: list[str]) -> str:
        if "failed" in statuses:
            return "failed"
        if "stale" in statuses:
            return "stale"
        if statuses and all(status == "verified" for status in statuses):
            return "verified"
        if statuses and all(status == "unsupported" for status in statuses):
            return "unsupported"
        return "partial"

    def _materialize_capability_contract(self) -> None:
        required = self.contract["required_capabilities"]
        for scope, capabilities in required.items():
            for capability in capabilities:
                declaration = self.capability_declarations[capability]
                exists = self.connection.execute(
                    """SELECT 1 FROM capability_coverage
                       WHERE model_id=? AND scope=? AND capability=?""",
                    (self.model_id, scope, capability),
                ).fetchone()
                if exists is None:
                    self.set_capability_coverage(
                        scope=scope,
                        dimension=declaration["dimension"],
                        capability=capability,
                        status="partial",
                        reason="required capability was not produced",
                        producer_run_id=None,
                    )

        pairs = self.connection.execute(
            """SELECT DISTINCT scope,dimension FROM capability_coverage
               WHERE model_id=? ORDER BY scope,dimension""",
            (self.model_id,),
        ).fetchall()
        reasons = {
            "verified": "all required capabilities verified",
            "partial": "required capabilities have mixed or incomplete status",
            "unsupported": "all required capabilities unsupported",
            "failed": "a required capability failed",
            "stale": "a required capability is stale",
        }
        for pair in pairs:
            rows = self.connection.execute(
                """SELECT status,producer_run_id FROM capability_coverage
                   WHERE model_id=? AND scope=? AND dimension=?
                   ORDER BY capability""",
                (self.model_id, pair["scope"], pair["dimension"]),
            ).fetchall()
            status = self._rollup_status([str(row["status"]) for row in rows])
            run_ids = {row["producer_run_id"] for row in rows}
            producer_run_id = next(iter(run_ids)) if len(run_ids) == 1 else None
            self.set_coverage(
                scope=str(pair["scope"]),
                dimension=str(pair["dimension"]),
                status=status,
                reason=reasons[status],
                producer_run_id=producer_run_id,
            )

    def _validate_private_dependencies(self) -> None:
        for predicate, declaration in self.predicate_declarations.items():
            if not predicate.startswith("project.") or not declaration["requires"]:
                continue
            placeholders = ",".join("?" for _ in declaration["requires"])
            rows = self.connection.execute(
                f"""SELECT capability,status FROM capability_coverage
                    WHERE model_id=? AND capability IN ({placeholders})""",
                (self.model_id, *declaration["requires"]),
            ).fetchall()
            statuses: dict[str, list[str]] = {}
            for row in rows:
                statuses.setdefault(str(row["capability"]), []).append(
                    str(row["status"])
                )
            dependencies_verified = all(
                statuses.get(capability)
                and all(status == "verified" for status in statuses[capability])
                for capability in declaration["requires"]
            )
            if dependencies_verified:
                continue
            assertion = self.connection.execute(
                """SELECT assertion_id FROM assertion
                   WHERE model_id=? AND predicate=? AND status='verified' LIMIT 1""",
                (self.model_id, predicate),
            ).fetchone()
            if assertion is not None:
                raise ModelError(
                    "knowledge-dependency",
                    f"verified Project assertion depends on incomplete capability: {predicate}",
                )

    def _validate_knowledge_rows(self) -> None:
        self.extension_predicate_dimensions.clear()
        for row in self.connection.execute(
            "SELECT predicate,dimension FROM assertion WHERE model_id=? ORDER BY predicate,assertion_id",
            (self.model_id,),
        ):
            dimension = _validate_dimension(row["dimension"])
            self._validate_predicate(row["predicate"], dimension)
        for row in self.connection.execute(
            "SELECT scope,dimension FROM coverage WHERE model_id=?",
            (self.model_id,),
        ):
            _validate_dimension(row["dimension"])
        for row in self.connection.execute(
            """SELECT scope,dimension,capability FROM capability_coverage
               WHERE model_id=? ORDER BY scope,dimension,capability""",
            (self.model_id,),
        ):
            dimension = _validate_dimension(row["dimension"])
            declaration = self.capability_declarations.get(row["capability"])
            if declaration is None or declaration["dimension"] != dimension:
                raise ModelError(
                    "knowledge-capability",
                    f"capability declaration does not match stored row: {row['capability']}",
                )
            if row["capability"] not in self.contract["required_capabilities"].get(
                row["scope"], []
            ):
                raise ModelError(
                    "knowledge-capability",
                    f"capability is not required for stored scope: {row['capability']}",
                )
        for scope, capabilities in self.contract["required_capabilities"].items():
            stored = {
                str(row["capability"])
                for row in self.connection.execute(
                    "SELECT capability FROM capability_coverage WHERE model_id=? AND scope=?",
                    (self.model_id, scope),
                )
            }
            if stored != set(capabilities):
                raise ModelError(
                    "knowledge-capability",
                    f"required capability rows are incomplete for scope {scope}",
                )
        pairs = self.connection.execute(
            """SELECT scope,dimension,group_concat(status) AS statuses
               FROM capability_coverage WHERE model_id=?
               GROUP BY scope,dimension ORDER BY scope,dimension""",
            (self.model_id,),
        ).fetchall()
        for pair in pairs:
            expected = self._rollup_status(str(pair["statuses"]).split(","))
            actual = self.connection.execute(
                """SELECT status FROM coverage
                   WHERE model_id=? AND scope=? AND dimension=?""",
                (self.model_id, pair["scope"], pair["dimension"]),
            ).fetchone()
            if actual is None or actual["status"] != expected:
                raise ModelError(
                    "knowledge-coverage",
                    f"coverage roll-up is inconsistent for {pair['scope']} / {pair['dimension']}",
                )
        self._validate_private_dependencies()

    def _logical_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for table in TABLE_ORDER:
            columns = [
                row["name"]
                for row in self.connection.execute(f"PRAGMA table_info({table})")
                if row["name"] not in OPERATIONAL_COLUMNS
                and not (table == "model_build" and row["name"] == "content_hash")
            ]
            order = ",".join(PRIMARY_KEYS[table])
            for row in self.connection.execute(
                f"SELECT {','.join(columns)} FROM {table} ORDER BY {order}"
            ):
                rows.append({"table": table, "row": dict(row)})
        return rows

    def finalize(self) -> str:
        self._materialize_capability_contract()
        self._validate_knowledge_rows()
        missing = self.connection.execute(
            """SELECT a.assertion_id FROM assertion a
               LEFT JOIN assertion_evidence ae ON ae.assertion_id=a.assertion_id
               WHERE a.model_id=? GROUP BY a.assertion_id HAVING count(ae.evidence_id)=0 LIMIT 1""",
            (self.model_id,),
        ).fetchone()
        if missing is not None:
            raise ModelError("assertion-evidence", "an assertion has no evidence")
        self.connection.execute(
            "UPDATE model_build SET status='complete' WHERE model_id=?",
            (self.model_id,),
        )
        content_hash = digest(self._logical_rows())
        self.connection.execute(
            "UPDATE model_build SET content_hash=? WHERE model_id=?",
            (content_hash, self.model_id),
        )
        self.connection.commit()
        result = self.integrity()
        if result != {"foreign_keys": [], "integrity": "ok"}:
            raise ModelError("model-integrity", f"model integrity failed: {result}")
        return content_hash

    def integrity(self) -> dict[str, Any]:
        return {
            "foreign_keys": [
                dict(row) for row in self.connection.execute("PRAGMA foreign_key_check")
            ],
            "integrity": str(
                self.connection.execute("PRAGMA integrity_check").fetchone()[0]
            ),
        }

    def query(self, view: str, **params: Any) -> dict[str, Any]:
        if view == "summary":
            return self._query_summary()
        if view == "search":
            return self._query_search(
                params.get("text"), params.get("kind"), params.get("limit", 20)
            )
        if view == "entity":
            return self._query_entity(params.get("name"), params.get("entity_id"))
        if view == "path":
            return self._query_path(
                params.get("start"),
                params.get("end"),
                params.get("max_depth", 8),
                start_id=params.get("start_id"),
                end_id=params.get("end_id"),
            )
        if view == "fsm":
            return self._query_fsm(params.get("name"))
        if view == "clock-reset":
            return self._query_clock_reset(params.get("name"))
        if view == "coverage":
            return {
                "coverage": self._query_rows("coverage", "scope,dimension")["coverage"],
                "capability_coverage": self._query_rows(
                    "capability_coverage", "scope,dimension,capability"
                )["capability_coverage"],
            }
        if view == "semantics":
            return self._query_semantics()
        if view == "evidence":
            return self._query_evidence(params.get("assertion_id"))
        raise ModelError("query-view", f"unknown model query view: {view}")

    def _query_search(
        self, text: str | None, kind: str | None, limit: int
    ) -> dict[str, Any]:
        needle = str(text or "").strip()
        if not needle:
            raise ModelError("query-parameter", "search text must be non-empty")
        try:
            bounded_limit = int(limit)
        except (TypeError, ValueError) as error:
            raise ModelError("query-limit", "search limit must be an integer") from error
        if bounded_limit < 1 or bounded_limit > 100:
            raise ModelError("query-limit", "search limit must be between 1 and 100")

        normalized = needle.casefold()
        parameters: list[Any] = [normalized, normalized, normalized, self.model_id]
        kind_clause = ""
        if kind:
            kind_clause = " AND kind=?"
            parameters.append(str(kind))
        parameters.append(bounded_limit)
        rows = self.connection.execute(
            f"""SELECT *, CASE
                    WHEN lower(qualified_name)=? THEN 0
                    WHEN instr(lower(qualified_name), ?)=1 THEN 1
                    WHEN instr(lower(qualified_name), ?)>0 THEN 2
                    ELSE 3 END AS match_rank
                FROM entity
                WHERE model_id=?{kind_clause}
                  AND instr(lower(qualified_name), ?)>0
                ORDER BY match_rank, qualified_name, kind, entity_id
                LIMIT ?""",
            (*parameters[:-1], normalized, parameters[-1]),
        )
        labels = ("exact", "prefix", "contains")
        entities = []
        for row in rows:
            decoded = _decoded(row)
            rank = int(decoded.pop("match_rank"))
            decoded["match"] = labels[rank]
            entities.append(decoded)
        return {
            "query": {"text": needle, "kind": kind, "limit": bounded_limit},
            "entities": entities,
        }

    def _query_rows(self, table: str, order: str) -> dict[str, Any]:
        rows = self.connection.execute(
            f"SELECT * FROM {table} WHERE model_id=? ORDER BY {order}", (self.model_id,)
        )
        return {table: [_decoded(row) for row in rows]}

    def _query_summary(self) -> dict[str, Any]:
        build = _decoded(self._model())
        producers = [
            _decoded(row)
            for row in self.connection.execute(
                "SELECT * FROM producer_run WHERE model_id=? ORDER BY producer,run_id",
                (self.model_id,),
            )
        ]
        kinds = {
            row["kind"]: row["count"]
            for row in self.connection.execute(
                "SELECT kind,count(*) AS count FROM entity WHERE model_id=? GROUP BY kind ORDER BY kind",
                (self.model_id,),
            )
        }
        predicates = {
            row["predicate"]: row["count"]
            for row in self.connection.execute(
                "SELECT predicate,count(*) AS count FROM assertion WHERE model_id=? GROUP BY predicate ORDER BY predicate",
                (self.model_id,),
            )
        }
        assertion_without_evidence = self.connection.execute(
            """SELECT count(*) FROM assertion a
               WHERE a.model_id=? AND NOT EXISTS (
                   SELECT 1 FROM assertion_evidence ae WHERE ae.assertion_id=a.assertion_id
               )""",
            (self.model_id,),
        ).fetchone()[0]
        return {
            "model": build,
            "producers": producers,
            "entity_kinds": kinds,
            "predicates": predicates,
            "coverage": self._query_rows("coverage", "scope,dimension")["coverage"],
            "capability_coverage": self._query_rows(
                "capability_coverage", "scope,dimension,capability"
            )["capability_coverage"],
            "assertion_without_evidence": assertion_without_evidence,
            "integrity": self.integrity(),
        }

    def _assertions_for(
        self, entity_ids: list[str], direction: str
    ) -> list[dict[str, Any]]:
        if not entity_ids:
            return []
        column = "subject_id" if direction == "outgoing" else "object_id"
        placeholders = ",".join("?" for _ in entity_ids)
        rows = self.connection.execute(
            f"SELECT * FROM assertion WHERE model_id=? AND {column} IN ({placeholders}) ORDER BY assertion_id",
            (self.model_id, *entity_ids),
        )
        return [_decoded(row) for row in rows]

    def _decorate_assertions(
        self, assertions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Attach display-ready endpoints and provenance to assertion rows."""
        endpoint_ids = sorted(
            {
                value
                for assertion in assertions
                for value in (assertion["subject_id"], assertion["object_id"])
                if value is not None
            }
        )
        entities: dict[str, dict[str, Any]] = {}
        if endpoint_ids:
            placeholders = ",".join("?" for _ in endpoint_ids)
            entities = {
                row["entity_id"]: row
                for row in (
                    _decoded(value)
                    for value in self.connection.execute(
                        f"SELECT * FROM entity WHERE model_id=? AND entity_id IN ({placeholders})",
                        (self.model_id, *endpoint_ids),
                    )
                )
            }
        for assertion in assertions:
            assertion["subject"] = entities.get(assertion["subject_id"])
            assertion["object"] = entities.get(assertion["object_id"])
            assertion["evidence"] = self._query_evidence(
                assertion["assertion_id"]
            )["evidence"]
        return assertions

    def _query_entity(self, name: str | None, entity_id: str | None) -> dict[str, Any]:
        if not name and not entity_id:
            raise ModelError(
                "query-parameter", "entity query needs a name or entity ID"
            )
        if entity_id:
            rows = self.connection.execute(
                "SELECT * FROM entity WHERE model_id=? AND entity_id=? ORDER BY kind",
                (self.model_id, entity_id),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM entity WHERE model_id=? AND qualified_name=? ORDER BY kind",
                (self.model_id, name),
            ).fetchall()
        entities = [_decoded(row) for row in rows]
        ids = [row["entity_id"] for row in entities]
        return {
            "entities": entities,
            "outgoing": self._decorate_assertions(
                self._assertions_for(ids, "outgoing")
            ),
            "incoming": self._decorate_assertions(
                self._assertions_for(ids, "incoming")
            ),
        }

    def _resolve_unique_entity(
        self,
        qualified_name: str | None,
        entity_id: str | None,
        endpoint: str,
    ) -> str:
        if qualified_name and entity_id:
            raise ModelError(
                "query-parameter", f"path {endpoint} accepts a name or entity ID, not both"
            )
        if not qualified_name and not entity_id:
            raise ModelError(
                "query-parameter", f"path query needs a {endpoint} name or entity ID"
            )
        if entity_id:
            rows = self.connection.execute(
                "SELECT entity_id FROM entity WHERE model_id=? AND entity_id=?",
                (self.model_id, entity_id),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT entity_id FROM entity WHERE model_id=? AND qualified_name=? ORDER BY entity_id",
                (self.model_id, qualified_name),
            ).fetchall()
        if len(rows) != 1:
            raise ModelError(
                "entity-not-unique",
                f"path {endpoint} resolves to {len(rows)} entities: {entity_id or qualified_name}",
            )
        return str(rows[0]["entity_id"])

    def _query_path(
        self,
        start: str | None,
        end: str | None,
        max_depth: int,
        *,
        start_id: str | None = None,
        end_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            depth = int(max_depth)
        except (TypeError, ValueError) as error:
            raise ModelError("query-depth", "path depth must be an integer") from error
        if depth < 1 or depth > 32:
            raise ModelError("query-depth", "path depth must be between 1 and 32")
        resolved_start_id = self._resolve_unique_entity(start, start_id, "start")
        resolved_end_id = self._resolve_unique_entity(end, end_id, "end")
        placeholders = ",".join("?" for _ in PATH_PREDICATES)
        rows = self.connection.execute(
            f"""SELECT assertion_id,subject_id,object_id FROM assertion
                WHERE model_id=? AND object_id IS NOT NULL AND predicate IN ({placeholders})
                ORDER BY subject_id,object_id,assertion_id""",
            (self.model_id, *PATH_PREDICATES),
        )
        adjacency: dict[str, list[tuple[str, str]]] = {}
        for edge in rows:
            adjacency.setdefault(str(edge["subject_id"]), []).append(
                (str(edge["object_id"]), str(edge["assertion_id"]))
            )
        frontier = [resolved_start_id]
        previous: dict[str, tuple[str, str] | None] = {resolved_start_id: None}
        for _ in range(depth):
            next_frontier: list[str] = []
            for subject_id in frontier:
                for object_id, assertion_id in adjacency.get(subject_id, []):
                    if object_id in previous:
                        continue
                    previous[object_id] = (subject_id, assertion_id)
                    next_frontier.append(object_id)
            frontier = next_frontier
            if resolved_end_id in previous or not frontier:
                break
        if resolved_end_id not in previous:
            return {"path": [], "hops": [], "max_depth": depth, "status": "not-found"}
        node_ids = [resolved_end_id]
        assertion_ids: list[str] = []
        cursor = resolved_end_id
        while previous[cursor] is not None:
            parent, assertion_id = previous[cursor]
            assertion_ids.append(assertion_id)
            node_ids.append(parent)
            cursor = parent
        node_ids.reverse()
        assertion_ids.reverse()
        nodes = []
        for value in node_ids:
            entity = self.connection.execute(
                "SELECT * FROM entity WHERE entity_id=?", (value,)
            ).fetchone()
            if entity is not None:
                nodes.append(_decoded(entity))
        hops = []
        for value in assertion_ids:
            assertion = self.connection.execute(
                "SELECT * FROM assertion WHERE assertion_id=?", (value,)
            ).fetchone()
            if assertion is not None:
                decoded = _decoded(assertion)
                decoded["evidence"] = self._query_evidence(value)["evidence"]
                hops.append(decoded)
        return {
            "path": nodes,
            "hops": hops,
            "max_depth": depth,
            "depth": len(hops),
            "status": "found",
        }

    def _query_fsm(self, name: str | None) -> dict[str, Any]:
        where = "AND e.qualified_name=?" if name else ""
        args: tuple[Any, ...] = (self.model_id, name) if name else (self.model_id,)
        fsms = [
            _decoded(row)
            for row in self.connection.execute(
                f"SELECT e.* FROM entity e WHERE e.model_id=? AND e.kind='rtl.fsm/1:fsm' {where} ORDER BY e.qualified_name",
                args,
            )
        ]
        ids = [fsm["entity_id"] for fsm in fsms]
        assertions = self._assertions_for(ids, "outgoing")
        prefixes = [f"{fsm['qualified_name']}." for fsm in fsms]
        transition_prefixes = [f"{fsm['qualified_name']}@transition:" for fsm in fsms]
        states: list[dict[str, Any]] = []
        transitions: list[dict[str, Any]] = []
        for row in self.connection.execute(
            "SELECT * FROM entity WHERE model_id=? AND kind IN ('rtl.fsm/1:state','rtl.fsm/1:transition') ORDER BY qualified_name",
            (self.model_id,),
        ):
            decoded = _decoded(row)
            qualified_name = decoded["qualified_name"]
            if decoded["kind"] == "rtl.fsm/1:state" and any(
                qualified_name.startswith(prefix) for prefix in prefixes
            ):
                states.append(decoded)
            elif decoded["kind"] == "rtl.fsm/1:transition" and any(
                qualified_name.startswith(prefix) for prefix in transition_prefixes
            ):
                transitions.append(decoded)
        control_assertions = [
            item
            for item in assertions
            if item["predicate"] == "rtl.fsm/1:controlled-by"
        ]
        controls: list[dict[str, Any]] = []
        for assertion in control_assertions:
            entity = self.connection.execute(
                "SELECT * FROM entity WHERE entity_id=?", (assertion["object_id"],)
            ).fetchone()
            if entity is not None:
                controls.append(_decoded(entity))
        transition_ids = [item["entity_id"] for item in transitions]
        transition_assertions = [
            *self._assertions_for(transition_ids, "incoming"),
            *self._assertions_for(transition_ids, "outgoing"),
        ]
        transition_assertions.sort(key=lambda item: item["assertion_id"])
        return {
            "fsms": fsms,
            "states": states,
            "transitions": transitions,
            "control_inputs": controls,
            "assertions": self._decorate_assertions(assertions),
            "transition_assertions": self._decorate_assertions(
                transition_assertions
            ),
        }

    def _query_clock_reset(self, name: str | None) -> dict[str, Any]:
        conditions = ["e.model_id=?", "e.kind='rtl.temporal/1:register'"]
        args: list[Any] = [self.model_id]
        if name:
            conditions.append("e.qualified_name LIKE ?")
            args.append(f"%{name}%")
        registers = [
            _decoded(row)
            for row in self.connection.execute(
                f"SELECT e.* FROM entity e WHERE {' AND '.join(conditions)} ORDER BY e.qualified_name",
                args,
            )
        ]
        assertions = self._assertions_for(
            [item["entity_id"] for item in registers], "outgoing"
        )
        all_derived = [
            _decoded(row)
            for row in self.connection.execute(
                """SELECT * FROM assertion WHERE model_id=?
                   AND predicate='rtl.clock-reset/1:derived-from' ORDER BY assertion_id""",
                (self.model_id,),
            )
        ]
        reachable_clock_ids = {
            item["object_id"]
            for item in assertions
            if item["predicate"] == "rtl.clock-reset/1:clocked-by"
            and item["object_id"] is not None
        }
        derived: list[dict[str, Any]] = []
        pending = list(all_derived)
        while pending:
            matched = [
                item for item in pending if item["subject_id"] in reachable_clock_ids
            ]
            if not matched:
                break
            for item in matched:
                derived.append(item)
                pending.remove(item)
                if item["object_id"] is not None:
                    reachable_clock_ids.add(item["object_id"])
        clock_ids = sorted(
            {
                value
                for assertion in [*assertions, *derived]
                for value in (assertion["subject_id"], assertion["object_id"])
                if value is not None
            }
        )
        clocks = []
        if clock_ids:
            placeholders = ",".join("?" for _ in clock_ids)
            clocks = [
                _decoded(row)
                for row in self.connection.execute(
                    f"SELECT * FROM entity WHERE entity_id IN ({placeholders}) AND kind IN ('rtl.clock/1:clock','rtl.reset/1:reset') ORDER BY qualified_name,kind",
                    clock_ids,
                )
            ]
        return {
            "registers": registers,
            "assertions": self._decorate_assertions(assertions),
            "derived_clock_assertions": self._decorate_assertions(derived),
            "clock_reset_entities": clocks,
        }

    def _query_semantics(self) -> dict[str, Any]:
        rows = self.connection.execute(
            """SELECT a.* FROM assertion a WHERE a.model_id=?
               AND (a.dimension='engineering-semantic' OR a.derivation IN ('llm','human'))
               ORDER BY a.assertion_id""",
            (self.model_id,),
        )
        return {
            "assertions": self._decorate_assertions(
                [_decoded(row) for row in rows]
            )
        }

    def _query_evidence(self, assertion_id: str | None) -> dict[str, Any]:
        if assertion_id:
            rows = self.connection.execute(
                """SELECT e.*,ae.role FROM evidence e JOIN assertion_evidence ae
                   ON ae.evidence_id=e.evidence_id WHERE e.model_id=? AND ae.assertion_id=?
                   ORDER BY e.evidence_id,ae.role""",
                (self.model_id, assertion_id),
            )
        else:
            rows = self.connection.execute(
                "SELECT * FROM evidence WHERE model_id=? ORDER BY evidence_id",
                (self.model_id,),
            )
        return {"evidence": [_decoded(row) for row in rows]}


def _export_rows(connection: sqlite3.Connection) -> Iterable[dict[str, Any]]:
    for table in TABLE_ORDER:
        columns = [
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})")
            if row["name"] not in OPERATIONAL_COLUMNS
        ]
        order = ",".join(PRIMARY_KEYS[table])
        for row in connection.execute(
            f"SELECT {','.join(columns)} FROM {table} ORDER BY {order}"
        ):
            yield {"table": table, "row": dict(row)}


def export_jsonl(database: Path, output: Path) -> None:
    store = ModelStore.open(database, readonly=True)
    try:
        if store._model()["status"] != "complete":
            raise ModelError(
                "model-incomplete", "only a complete model can be exported"
            )
        lines = [
            canonical_json({"format": JSONL_FORMAT, "schema_version": SCHEMA_VERSION})
        ]
        lines.extend(canonical_json(row) for row in _export_rows(store.connection))
        Path(output).write_text("\n".join(lines) + "\n", encoding="utf-8")
    finally:
        store.close()


def import_jsonl(source: Path, database: Path) -> None:
    source = Path(source)
    database = Path(database)
    if database.exists():
        raise ModelError("model-exists", "import target already exists")
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
        manifest = json.loads(lines[0])
    except (OSError, ValueError, IndexError) as error:
        raise ModelError(
            "import-format", "JSONL input is missing or invalid"
        ) from error
    if manifest != {"format": JSONL_FORMAT, "schema_version": SCHEMA_VERSION}:
        raise ModelError("import-format", "JSONL manifest is unsupported")
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect(database)
    try:
        connection.executescript(SCHEMA_SQL)
        seen_order = -1
        for line in lines[1:]:
            item = json.loads(line)
            table = item.get("table")
            row = item.get("row")
            if table not in TABLE_ORDER or not isinstance(row, dict):
                raise ModelError(
                    "import-format", "JSONL row has an invalid table or payload"
                )
            order = TABLE_ORDER.index(table)
            if order < seen_order:
                raise ModelError(
                    "import-order", "JSONL tables are out of canonical order"
                )
            seen_order = order
            columns = [
                entry["name"]
                for entry in connection.execute(f"PRAGMA table_info({table})")
            ]
            supplied = set(row)
            allowed = set(columns) - OPERATIONAL_COLUMNS
            if supplied != allowed:
                raise ModelError(
                    "import-format", f"JSONL {table} columns do not match schema"
                )
            if "created_at" in columns:
                row["created_at"] = _now()
            if "checked_at" in columns:
                row["checked_at"] = _now()
            names = list(row)
            placeholders = ",".join("?" for _ in names)
            connection.execute(
                f"INSERT INTO {table}({','.join(names)}) VALUES({placeholders})",
                [row[name] for name in names],
            )
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        build = connection.execute(
            "SELECT model_id,status,contract_json,content_hash FROM model_build"
        ).fetchall()
        if (
            foreign_keys
            or integrity != "ok"
            or len(build) != 1
            or build[0]["status"] != "complete"
        ):
            raise ModelError(
                "model-integrity", "imported model failed integrity checks"
            )
        try:
            contract = _validate_contract(json.loads(build[0]["contract_json"]))
            store = ModelStore(database, connection, str(build[0]["model_id"]), contract)
            store._validate_knowledge_rows()
            if build[0]["content_hash"] != digest(store._logical_rows()):
                raise ModelError(
                    "model-integrity", "imported model content hash is invalid"
                )
        except (ValueError, TypeError) as error:
            raise ModelError("model-integrity", "imported model contract is invalid") from error
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.IntegrityError as error:
        connection.close()
        database.unlink(missing_ok=True)
        raise ModelError(
            "import-format", "JSONL row violates the Project Model schema"
        ) from error
    except Exception:
        connection.close()
        database.unlink(missing_ok=True)
        raise
    connection.close()


def _verify_jsonl_round_trip(database: Path, directory: Path) -> None:
    exported = directory / "candidate.jsonl"
    imported = directory / "round-trip.sqlite"
    second = directory / "round-trip.jsonl"
    export_jsonl(database, exported)
    import_jsonl(exported, imported)
    export_jsonl(imported, second)
    if exported.read_bytes() != second.read_bytes():
        raise ModelError("model-round-trip", "candidate JSONL round-trip changed content")


def _file_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _stable_artifact(value: Any, source_files: dict[str, str]) -> Any:
    if isinstance(value, list):
        return [_stable_artifact(item, source_files) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key == "addr":
            continue
        if key.startswith("source_file") and isinstance(item, str):
            result[key] = _source_locator(item, source_files)
        elif key == "symbol" and isinstance(item, str):
            result[key] = item.split(" ", 1)[-1]
        else:
            result[key] = _stable_artifact(item, source_files)
    return result


def _slang_consumed_artifact(
    ast: dict[str, Any], source_files: dict[str, str]
) -> list[dict[str, Any]]:
    """Return only stable slang fields consumed by the adapter, excluding runtime addresses."""
    records: list[dict[str, Any]] = []
    for node in _walk(ast):
        kind = node.get("kind")
        if kind == "Definition":
            records.append(
                {
                    "kind": kind,
                    "name": node.get("name"),
                    "source": _source_locator(
                        str(node.get("source_file", "")), source_files
                    ),
                    "line": node.get("source_line"),
                }
            )
        elif kind == "Parameter":
            records.append(
                {
                    "kind": kind,
                    "name": node.get("name"),
                    "value": node.get("value"),
                    "source": _source_locator(
                        str(node.get("source_file", "")), source_files
                    ),
                    "line": node.get("source_line"),
                }
            )
        elif kind == "Assignment" and (
            node.get("left") is not None or node.get("right") is not None
        ):
            records.append(
                {
                    "kind": kind,
                    "left": _symbol_name(node.get("left")),
                    "right": _symbol_name(node.get("right")),
                    "source": _source_locator(
                        str(node.get("source_file_start", "")), source_files
                    ),
                    "line": node.get("source_line_start"),
                }
            )
        elif kind == "Instance":
            connections = []
            for connection in node.get("connections", []):
                if not isinstance(connection, dict) or not isinstance(
                    connection.get("port"), dict
                ):
                    continue
                port = connection["port"]
                expression = connection.get("expr", {})
                connections.append(
                    {
                        "port": port.get("name"),
                        "direction": port.get("direction"),
                        "port_source": _source_locator(
                            str(port.get("source_file", "")), source_files
                        ),
                        "port_line": port.get("source_line"),
                        "signal": _symbol_name(expression),
                        "binding_source": _source_locator(
                            str(expression.get("source_file_start", "")), source_files
                        ),
                        "binding_line": expression.get("source_line_start"),
                    }
                )
            records.append(
                {
                    "kind": kind,
                    "name": node.get("name"),
                    "module": node.get("body", {}).get("name")
                    if isinstance(node.get("body"), dict)
                    else None,
                    "source": _source_locator(
                        str(node.get("source_file", "")), source_files
                    ),
                    "line": node.get("source_line"),
                    "connections": connections,
                }
            )
    return records


def _source_locator(value: str, source_files: dict[str, str]) -> str:
    clean = value.split(":", 1)[0]
    name = Path(clean).name
    return source_files.get(name, name)


def _source_span(
    source: str | None, source_files: dict[str, str]
) -> tuple[str, int | None, int | None]:
    if not source:
        return "producer:generated", None, None
    match = re.match(r"^(.*?):(\d+)(?:\.\d+)?-(\d+)(?:\.\d+)?$", source)
    if match:
        return (
            _source_locator(match.group(1), source_files),
            int(match.group(2)),
            int(match.group(3)),
        )
    return _source_locator(source, source_files), None, None


def _symbol_name(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    symbol = value.get("symbol")
    if isinstance(symbol, str) and symbol:
        return symbol.split(" ", 1)[-1]
    for key in ("left", "right", "expr"):
        nested = _symbol_name(value.get(key))
        if nested:
            return nested
    return None


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _sv_binary(value: str, width: int) -> str | None:
    match = re.fullmatch(
        r"(?:(\d+)'([bBdDhH]))?([0-9a-fA-F_xXzZ]+)", value.replace("_", "")
    )
    if not match:
        return None
    radix = {None: 10, "b": 2, "d": 10, "h": 16}[
        match.group(2).lower() if match.group(2) else None
    ]
    digits = match.group(3)
    if any(letter in digits.lower() for letter in "xz"):
        return None
    try:
        integer = int(digits, radix)
    except ValueError:
        return None
    return format(integer, f"0{width}b")[-width:]


class _Extractor:
    def __init__(
        self,
        store: ModelStore,
        source_root: Path,
        source_files: dict[str, str],
        file_hashes: dict[str, str],
    ):
        self.store = store
        self.source_root = source_root
        self.source_files = source_files
        self.file_hashes = file_hashes
        self.evidence_cache: dict[tuple[Any, ...], str] = {}

    def evidence(
        self,
        run_id: str,
        locator: str,
        start_line: int | None = None,
        end_line: int | None = None,
        *,
        kind: str = "source",
        attrs: dict[str, Any] | None = None,
    ) -> str:
        locator = _source_locator(locator, self.source_files)
        key = (run_id, locator, start_line, end_line, kind, canonical_json(attrs or {}))
        if key not in self.evidence_cache:
            self.evidence_cache[key] = self.store.add_evidence(
                run_id,
                kind=kind,
                locator=locator,
                start_line=start_line,
                end_line=end_line,
                content_hash=self.file_hashes.get(locator),
                attrs=attrs,
            )
        return self.evidence_cache[key]

    def relation(
        self,
        subject: str,
        predicate: str,
        obj: str,
        dimension: str,
        run_id: str,
        evidence: Iterable[str],
        *,
        attrs: dict[str, Any] | None = None,
        status: str = "verified",
    ) -> str:
        return self.store.add_assertion(
            subject_id=subject,
            predicate=predicate,
            object_id=obj,
            dimension=dimension,
            derivation="tool",
            confidence=1.0,
            status=status,
            producer_run_id=run_id,
            evidence_ids=evidence,
            attrs=attrs,
        )

    def extract_slang(
        self, ast: dict[str, Any], spec: dict[str, Any], run_id: str, spec_ev: str
    ) -> dict[str, Any]:
        scopes = set(spec["scopes"])
        module_ids: dict[str, str] = {}
        module_evidence: dict[str, str] = {}
        parameters: dict[tuple[str, str], dict[str, Any]] = {}
        signal_evidence: dict[tuple[str, str], list[str]] = {}

        definitions = ast.get("definitions", [])
        if not isinstance(definitions, list):
            raise ModelError("slang-format", "slang AST definitions are missing")
        for definition in definitions:
            if (
                not isinstance(definition, dict)
                or definition.get("kind") != "Definition"
            ):
                continue
            name = definition.get("name")
            if name not in scopes:
                continue
            locator = _source_locator(
                str(definition.get("source_file", "")), self.source_files
            )
            line = definition.get("source_line")
            evidence = self.evidence(run_id, locator, line, line)
            module_ids[name] = self.store.add_entity(
                "rtl.sv/1:module",
                name,
                display_name=name,
                attrs={"source": locator, "line": line},
            )
            module_evidence[name] = evidence

        for node in _walk(ast.get("design", {})):
            kind = node.get("kind")
            if kind == "Parameter" and isinstance(node.get("name"), str):
                locator = _source_locator(
                    str(node.get("source_file", "")), self.source_files
                )
                module = next(
                    (scope for scope in scopes if Path(locator).stem == scope), None
                )
                if module:
                    value = node.get("value")
                    parameters[(module, node["name"])] = {
                        "value": value,
                        "source": locator,
                        "line": node.get("source_line"),
                    }
                    parameter_id = self.store.add_entity(
                        "rtl.sv/1:parameter",
                        f"{module}.{node['name']}",
                        attrs={"value": value},
                    )
                    ev = self.evidence(
                        run_id,
                        locator,
                        node.get("source_line"),
                        node.get("source_line"),
                    )
                    if module in module_ids:
                        self.relation(
                            module_ids[module],
                            "rtl.config/1:has-parameter",
                            parameter_id,
                            "configuration/elaboration",
                            run_id,
                            [ev],
                        )
            elif kind == "Assignment":
                left = _symbol_name(node.get("left"))
                right = _symbol_name(node.get("right"))
                location = node.get("source_file_start")
                line = node.get("source_line_start")
                if left and right and isinstance(location, str):
                    locator = _source_locator(location, self.source_files)
                    module = next(
                        (scope for scope in scopes if Path(locator).stem == scope), None
                    )
                    if module:
                        ev = self.evidence(
                            run_id, locator, line, node.get("source_line_end") or line
                        )
                        signal_evidence.setdefault((module, left), []).append(ev)
                        signal_evidence.setdefault((module, right), []).append(ev)
                        left_id = self.store.add_entity(
                            "rtl.sv/1:signal", f"{module}.{left}"
                        )
                        right_id = self.store.add_entity(
                            "rtl.sv/1:signal", f"{module}.{right}"
                        )
                        self.relation(
                            left_id,
                            "rtl.behavior/1:data-depends-on",
                            right_id,
                            "behavior/causality",
                            run_id,
                            [ev],
                            attrs={"source_form": "assignment"},
                        )

        design = ast.get("design", {})
        root_instance = next(
            (
                node
                for node in _walk(design)
                if node.get("kind") == "Instance" and node.get("name") == spec["top"]
            ),
            None,
        )
        if not isinstance(root_instance, dict):
            raise ModelError("slang-format", "slang AST has no elaborated top instance")
        top_id = module_ids.get(spec["top"])
        if top_id is None:
            raise ModelError("slang-format", "slang AST has no selected top definition")
        for instance in _walk(root_instance.get("body", {}).get("members", [])):
            if instance.get("kind") != "Instance" or not isinstance(
                instance.get("body"), dict
            ):
                continue
            module = instance["body"].get("name")
            name = instance.get("name")
            if (
                module not in scopes
                or module == spec["top"]
                or not isinstance(name, str)
            ):
                continue
            instance_name = f"{spec['top']}.{name}"
            instance_ev = self.evidence(
                run_id,
                str(instance.get("source_file", "")),
                instance.get("source_line"),
                instance.get("source_line"),
            )
            instance_id = self.store.add_entity(
                "rtl.sv/1:instance", instance_name, attrs={"module": module}
            )
            self.relation(
                top_id,
                "rtl.hierarchy/1:contains",
                instance_id,
                "hierarchy",
                run_id,
                [instance_ev],
            )
            self.relation(
                instance_id,
                "rtl.hierarchy/1:instance-of",
                module_ids[module],
                "hierarchy",
                run_id,
                [instance_ev, module_evidence[module]],
            )
            connections = instance.get("connections", [])
            if not isinstance(connections, list):
                continue
            for connection in connections:
                if not isinstance(connection, dict) or not isinstance(
                    connection.get("port"), dict
                ):
                    continue
                port = connection["port"]
                port_name = port.get("name")
                parent_signal = _symbol_name(connection.get("expr"))
                if not isinstance(port_name, str) or not parent_signal:
                    continue
                direction = str(port.get("direction", "unknown")).lower()
                port_ev = self.evidence(
                    run_id,
                    str(port.get("source_file", "")),
                    port.get("source_line"),
                    port.get("source_line"),
                )
                expr = connection.get("expr", {})
                bind_ev = self.evidence(
                    run_id,
                    str(expr.get("source_file_start", instance.get("source_file", ""))),
                    expr.get("source_line_start", instance.get("source_line")),
                    expr.get(
                        "source_line_end",
                        expr.get("source_line_start", instance.get("source_line")),
                    ),
                )
                module_port_id = self.store.add_entity(
                    "rtl.sv/1:port",
                    f"{module}.{port_name}",
                    attrs={"direction": direction},
                )
                instance_port_id = self.store.add_entity(
                    "rtl.sv/1:port",
                    f"{instance_name}.{port_name}",
                    attrs={"direction": direction, "instance": instance_name},
                )
                signal_id = self.store.add_entity(
                    "rtl.sv/1:signal", f"{spec['top']}.{parent_signal}"
                )
                self.relation(
                    module_ids[module],
                    "rtl.hierarchy/1:declares-port",
                    module_port_id,
                    "hierarchy",
                    run_id,
                    [port_ev],
                )
                self.relation(
                    instance_id,
                    "rtl.hierarchy/1:contains",
                    instance_port_id,
                    "hierarchy",
                    run_id,
                    [port_ev, instance_ev],
                )
                evidence = [
                    port_ev,
                    bind_ev,
                    *signal_evidence.get((module, port_name), []),
                ]
                if direction == "out":
                    self.relation(
                        instance_port_id,
                        "rtl.connectivity/1:binds-to",
                        signal_id,
                        "connectivity",
                        run_id,
                        evidence,
                        attrs={"direction": direction},
                    )
                else:
                    self.relation(
                        signal_id,
                        "rtl.connectivity/1:binds-to",
                        instance_port_id,
                        "connectivity",
                        run_id,
                        evidence,
                        attrs={"direction": direction},
                    )
        return {"modules": module_ids, "parameters": parameters}

    @staticmethod
    def _net_map(module: dict[str, Any]) -> tuple[dict[int, str], dict[str, list[Any]]]:
        candidates: dict[int, list[tuple[int, int, int, str]]] = {}
        net_bits: dict[str, list[Any]] = {}
        ports = set(module.get("ports", {}))
        for name, value in module.get("netnames", {}).items():
            if not isinstance(value, dict) or not isinstance(value.get("bits"), list):
                continue
            bits = value["bits"]
            net_bits[name] = bits
            score = (
                0 if name in ports else 1,
                int(bool(value.get("hide_name"))),
                len(name),
                name,
            )
            for bit in bits:
                if isinstance(bit, int):
                    candidates.setdefault(bit, []).append(score)
        return {bit: sorted(names)[0][3] for bit, names in candidates.items()}, net_bits

    @staticmethod
    def _names_for_bits(bit_names: dict[int, str], bits: Any) -> list[str]:
        if not isinstance(bits, list):
            return []
        return sorted(
            {
                bit_names[bit]
                for bit in bits
                if isinstance(bit, int) and bit in bit_names
            }
        )

    def extract_yosys(
        self, netlist: dict[str, Any], spec: dict[str, Any], run_id: str
    ) -> dict[str, Any]:
        modules = netlist.get("modules")
        if not isinstance(modules, dict):
            raise ModelError("yosys-format", "Yosys netlist modules are missing")
        register_counts: dict[str, dict[str, int]] = {}
        for module_name in spec["scopes"]:
            module = modules.get(module_name)
            if not isinstance(module, dict):
                raise ModelError(
                    "yosys-format",
                    f"Yosys netlist has no selected module: {module_name}",
                )
            bit_names, _ = self._net_map(module)
            for signal_name, signal in module.get("netnames", {}).items():
                if not isinstance(signal, dict):
                    continue
                locator, start, end = _source_span(
                    signal.get("attributes", {}).get("src")
                    if isinstance(signal.get("attributes"), dict)
                    else None,
                    self.source_files,
                )
                signal_id = self.store.add_entity(
                    "rtl.sv/1:signal",
                    f"{module_name}.{signal_name}",
                    attrs={
                        "width": len(signal.get("bits", [])),
                        "source": locator,
                        "line": start,
                    },
                )
                for bit in signal.get("bits", []):
                    if isinstance(bit, int):
                        self.store.add_external_ref(
                            run_id,
                            f"yosys-bit:{module_name}",
                            str(bit),
                            signal_id,
                            attrs={"netname": signal_name},
                        )
            register_cells = 0
            register_bits = 0
            for ordinal, (raw_name, cell) in enumerate(
                sorted(module.get("cells", {}).items())
            ):
                if not isinstance(cell, dict):
                    continue
                cell_type = str(cell.get("type", "unknown"))
                attrs = (
                    cell.get("attributes", {})
                    if isinstance(cell.get("attributes"), dict)
                    else {}
                )
                locator, start, end = _source_span(attrs.get("src"), self.source_files)
                cell_name = str(raw_name)
                for base, relative in self.source_files.items():
                    cell_name = cell_name.replace(f"/src/{base}", relative)
                if cell_name.startswith("$"):
                    cell_name = f"{cell_type}@{locator}:{start or 0}:{ordinal}"
                cell_id = self.store.add_entity(
                    "rtl.netlist/1:cell",
                    f"{module_name}.{cell_name}",
                    attrs={
                        "cell_type": cell_type,
                        "source": locator,
                        "start_line": start,
                        "end_line": end,
                    },
                )
                ev = self.evidence(
                    run_id, locator, start, end, kind="source" if start else "artifact"
                )
                directions = cell.get("port_directions", {})
                connections = cell.get("connections", {})
                if not isinstance(directions, dict) or not isinstance(
                    connections, dict
                ):
                    directions = {}
                    connections = connections if isinstance(connections, dict) else {}
                input_signals: dict[str, list[str]] = {}
                output_signals: dict[str, list[str]] = {}
                for port, bits in connections.items():
                    names = self._names_for_bits(bit_names, bits)
                    if directions.get(port) == "input":
                        input_signals[port] = names
                    elif directions.get(port) == "output":
                        output_signals[port] = names
                    for name in names:
                        signal_id = self.store.add_entity(
                            "rtl.sv/1:signal", f"{module_name}.{name}"
                        )
                        if directions.get(port) == "input":
                            self.relation(
                                signal_id,
                                "rtl.connectivity/1:loads",
                                cell_id,
                                "connectivity",
                                run_id,
                                [ev],
                                attrs={"port": port},
                            )
                        elif directions.get(port) == "output":
                            self.relation(
                                cell_id,
                                "rtl.connectivity/1:drives",
                                signal_id,
                                "connectivity",
                                run_id,
                                [ev],
                                attrs={"port": port},
                            )
                if cell_type.startswith("$"):
                    for output_port, output_names in output_signals.items():
                        for output_name in output_names:
                            output_id = self.store.add_entity(
                                "rtl.sv/1:signal", f"{module_name}.{output_name}"
                            )
                            for input_port, input_names in input_signals.items():
                                predicate = (
                                    "rtl.behavior/1:control-depends-on"
                                    if cell_type in ("$mux", "$pmux")
                                    and input_port == "S"
                                    else "rtl.behavior/1:data-depends-on"
                                )
                                for input_name in input_names:
                                    input_id = self.store.add_entity(
                                        "rtl.sv/1:signal", f"{module_name}.{input_name}"
                                    )
                                    self.relation(
                                        output_id,
                                        predicate,
                                        input_id,
                                        "behavior/causality",
                                        run_id,
                                        [ev],
                                        attrs={
                                            "cell": cell_name,
                                            "cell_type": cell_type,
                                            "input_port": input_port,
                                            "output_port": output_port,
                                        },
                                    )
                if cell_type == "$adff":
                    register_cells += 1
                    parameters = cell.get("parameters", {})
                    width_value = (
                        parameters.get("WIDTH", "0")
                        if isinstance(parameters, dict)
                        else "0"
                    )
                    try:
                        width = int(str(width_value), 2)
                    except ValueError:
                        width = len(connections.get("Q", []))
                    register_bits += width
                    q_names = output_signals.get("Q", [])
                    d_names = input_signals.get("D", [])
                    clock_names = input_signals.get("CLK", [])
                    reset_names = input_signals.get("ARST", [])
                    clock_polarity = (
                        parameters.get("CLK_POLARITY")
                        if isinstance(parameters, dict)
                        else None
                    )
                    reset_polarity = (
                        parameters.get("ARST_POLARITY")
                        if isinstance(parameters, dict)
                        else None
                    )
                    register_name = q_names[0] if q_names else f"cell-{ordinal}"
                    register_id = self.store.add_entity(
                        "rtl.temporal/1:register",
                        f"{module_name}.{register_name}@register:{ordinal}",
                        attrs={
                            "cell": cell_name,
                            "width": width,
                            "q": q_names,
                            "d": d_names,
                            "clock": clock_names,
                            "reset": reset_names,
                            "clock_polarity": clock_polarity,
                            "clock_edge": _yosys_polarity(
                                clock_polarity,
                                positive="posedge",
                                negative="negedge",
                            ),
                            "reset_polarity": reset_polarity,
                            "reset_active": _yosys_polarity(
                                reset_polarity, positive="high", negative="low"
                            ),
                            "reset_mode": "asynchronous",
                            "reset_value": parameters.get("ARST_VALUE")
                            if isinstance(parameters, dict)
                            else None,
                        },
                    )
                    for name in clock_names:
                        clock_id = self.store.add_entity(
                            "rtl.clock/1:clock", f"{module_name}.{name}"
                        )
                        self.relation(
                            register_id,
                            "rtl.clock-reset/1:clocked-by",
                            clock_id,
                            "clock/reset",
                            run_id,
                            [ev],
                        )
                    for name in reset_names:
                        reset_id = self.store.add_entity(
                            "rtl.reset/1:reset", f"{module_name}.{name}"
                        )
                        self.relation(
                            register_id,
                            "rtl.clock-reset/1:reset-by",
                            reset_id,
                            "clock/reset",
                            run_id,
                            [ev],
                            attrs={
                                "polarity": reset_polarity,
                                "active": _yosys_polarity(
                                    reset_polarity,
                                    positive="high",
                                    negative="low",
                                ),
                                "mode": "asynchronous",
                                "value": parameters.get("ARST_VALUE")
                                if isinstance(parameters, dict)
                                else None,
                            },
                        )
                    for q_name in q_names:
                        q_id = self.store.add_entity(
                            "rtl.sv/1:signal", f"{module_name}.{q_name}"
                        )
                        for d_name in d_names:
                            d_id = self.store.add_entity(
                                "rtl.sv/1:signal", f"{module_name}.{d_name}"
                            )
                            self.relation(
                                q_id,
                                "rtl.temporal/1:next-cycle-from",
                                d_id,
                                "temporal/ordering",
                                run_id,
                                [ev],
                                attrs={"register": register_name},
                            )
                elif cell_type == "gated_clk_cell":
                    inputs = self._names_for_bits(bit_names, connections.get("clk_in"))
                    outputs = self._names_for_bits(
                        bit_names, connections.get("clk_out")
                    )
                    for output_name in outputs:
                        output_id = self.store.add_entity(
                            "rtl.clock/1:clock", f"{module_name}.{output_name}"
                        )
                        for input_name in inputs:
                            input_id = self.store.add_entity(
                                "rtl.clock/1:clock", f"{module_name}.{input_name}"
                            )
                            self.relation(
                                output_id,
                                "rtl.clock-reset/1:derived-from",
                                input_id,
                                "clock/reset",
                                run_id,
                                [ev],
                                attrs={"cell": cell_name},
                            )
            register_counts[module_name] = {
                "cell_count": register_cells,
                "bit_count": register_bits,
            }
        return {"registers": register_counts}

    def extract_fsm(
        self,
        fsm_netlist: dict[str, Any],
        kiss: dict[str, Any],
        hint: dict[str, Any],
        parameters: dict[tuple[str, str], dict[str, Any]],
        run_id: str,
    ) -> dict[str, Any]:
        module_name = hint["module"]
        module = fsm_netlist.get("modules", {}).get(module_name, {})
        bit_names, _ = self._net_map(module)
        fsm_cells = [
            cell
            for cell in module.get("cells", {}).values()
            if isinstance(cell, dict) and cell.get("type") == "$fsm"
        ]
        if len(fsm_cells) != 1:
            raise ModelError(
                "fsm-format",
                f"expected one Yosys FSM for {module_name}, got {len(fsm_cells)}",
            )
        cell = fsm_cells[0]
        state_bits = int(str(cell.get("parameters", {}).get("STATE_BITS", "0")), 2)
        encoding_names: dict[str, str] = {}
        state_source: dict[str, dict[str, Any]] = {}
        for parameter_name in hint["encoding_parameters"]:
            parameter = parameters.get((module_name, parameter_name))
            if parameter and isinstance(parameter.get("value"), str):
                encoded = _sv_binary(parameter["value"], state_bits)
                if encoded:
                    encoding_names[encoded] = parameter_name
                    state_source[encoded] = parameter
        reset_encoding = kiss["reset_encoding"]
        fsm_name = f"{module_name}.{hint['state_signal']}@fsm"
        fsm_id = self.store.add_entity(
            "rtl.fsm/1:fsm",
            fsm_name,
            attrs={
                "state_count": kiss["state_count"],
                "transition_count": len(kiss["transitions"]),
                "reset_encoding": reset_encoding,
                "reset_state": encoding_names.get(reset_encoding, reset_encoding),
                "state_signal": hint["state_signal"],
            },
        )
        fsm_locator, fsm_start, fsm_end = _source_span(
            cell.get("attributes", {}).get("src")
            if isinstance(cell.get("attributes"), dict)
            else None,
            self.source_files,
        )
        fsm_ev = self.evidence(run_id, fsm_locator, fsm_start, fsm_end)
        state_ids: dict[str, str] = {}
        for encoding in sorted(
            {transition["current_state"] for transition in kiss["transitions"]}
            | {transition["next_state"] for transition in kiss["transitions"]}
        ):
            state_name = encoding_names.get(encoding, encoding)
            state = state_source.get(encoding, {})
            ev = self.evidence(
                run_id,
                str(state.get("source", fsm_locator)),
                state.get("line", fsm_start),
                state.get("line", fsm_end),
            )
            state_id = self.store.add_entity(
                "rtl.fsm/1:state",
                f"{fsm_name}.{state_name}",
                attrs={
                    "encoding": encoding,
                    "name": state_name,
                    "reset": encoding == reset_encoding,
                },
            )
            state_ids[encoding] = state_id
            self.relation(
                fsm_id,
                "rtl.fsm/1:has-state",
                state_id,
                "temporal/ordering",
                run_id,
                [ev],
            )
        transition_ids: list[str] = []
        for index, transition in enumerate(kiss["transitions"]):
            transition_id = self.store.add_entity(
                "rtl.fsm/1:transition",
                f"{fsm_name}@transition:{index:03d}",
                attrs={
                    "index": index,
                    "current_state": encoding_names.get(
                        transition["current_state"], transition["current_state"]
                    ),
                    "next_state": encoding_names.get(
                        transition["next_state"], transition["next_state"]
                    ),
                    "input_pattern": transition["input_pattern"],
                    "output_pattern": transition["output_pattern"],
                },
            )
            transition_ids.append(transition_id)
            self.relation(
                state_ids[transition["current_state"]],
                "rtl.fsm/1:transitions-to",
                transition_id,
                "temporal/ordering",
                run_id,
                [fsm_ev],
                attrs={
                    "index": index,
                    "input_pattern": transition["input_pattern"],
                    "output_pattern": transition["output_pattern"],
                },
            )
            self.relation(
                transition_id,
                "rtl.fsm/1:transitions-to",
                state_ids[transition["next_state"]],
                "temporal/ordering",
                run_id,
                [fsm_ev],
                attrs={"index": index, "role": "next-state"},
            )
        control_names = self._names_for_bits(
            bit_names, cell.get("connections", {}).get("CTRL_IN")
        )
        for name in control_names:
            signal_id = self.store.add_entity(
                "rtl.sv/1:signal", f"{module_name}.{name}"
            )
            self.relation(
                fsm_id,
                "rtl.fsm/1:controlled-by",
                signal_id,
                "behavior/causality",
                run_id,
                [fsm_ev],
            )
        return {
            "qualified_name": fsm_name,
            "state_count": len(state_ids),
            "transition_count": len(kiss["transitions"]),
            "reset_state": encoding_names.get(reset_encoding, reset_encoding),
            "control_inputs": control_names,
        }


def _run(
    command: list[str],
    cwd: Path,
    *,
    timeout: int,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        raise ModelError("tool-timeout", f"producer exceeded {timeout}s") from error
    except OSError as error:
        raise ModelError("tool-execution", f"cannot start producer: {error}") from error


def _resolve_project_output(project_dir: Path, relative: str, suffix: str) -> Path:
    supplied = Path(relative)
    if supplied.is_absolute() or "\n" in relative:
        raise ModelError("project-path", "model paths must be Project-relative")
    result = (project_dir / supplied).resolve()
    try:
        result.relative_to(project_dir.resolve())
    except ValueError as error:
        raise ModelError("project-path", "model path escapes the Project") from error
    if result.suffix != suffix:
        raise ModelError("project-path", f"model path must end with {suffix}")
    if result.exists() and result.is_symlink():
        raise ModelError("project-path", "model path must not be a symbolic link")
    return result


def resolve_project_file(project_dir: Path, relative: str, suffix: str) -> Path:
    result = _resolve_project_output(project_dir, relative, suffix)
    if not result.is_file() or result.is_symlink():
        raise ModelError(
            "project-path", "selected Project file does not exist or is unsafe"
        )
    return result


def build_project_model(
    *,
    project_dir: Path,
    target: dict[str, Any],
    source_root: Path,
    spec_path: Path,
    output_relative: str,
    slang_executable: Path,
    slang_version: str,
    yosys_executable: Path,
    yosys_version: str,
    temporary_root: Path,
) -> dict[str, Any]:
    project_dir = project_dir.resolve()
    spec_path = spec_path.resolve()
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ModelError(
            "model-spec", "model spec is missing or invalid JSON"
        ) from error
    required = {
        "schema",
        "name",
        "top",
        "files",
        "scopes",
        "slang",
        "yosys",
        "fsms",
        "semantic_assertions",
        "expected_coverage",
        "required_capabilities",
        "project_profile",
        "expectations",
    }
    if (
        not isinstance(spec, dict)
        or set(spec) != required
        or spec.get("schema") != "rtl-analysis-agent/project-model-spec-v2"
    ):
        raise ModelError("model-spec", "model spec shape or schema is invalid")
    contract = _validate_contract(
        {
            "required_capabilities": spec["required_capabilities"],
            "project_profile": spec["project_profile"],
        }
    )
    expected_scopes = {str(target["name"]), *(str(scope) for scope in spec["scopes"])}
    if set(contract["required_capabilities"]) != expected_scopes:
        raise ModelError(
            "model-spec", "required capabilities must be explicit for every model scope"
        )
    if not isinstance(spec["files"], list) or not spec["files"]:
        raise ModelError("model-spec", "model spec needs source files")
    if (
        not isinstance(spec["expected_coverage"], dict)
        or set(spec["expected_coverage"]) != DIMENSION_SET
        or any(
            status not in {"verified", "partial", "unsupported", "failed", "stale"}
            for status in spec["expected_coverage"].values()
        )
    ):
        raise ModelError(
            "model-spec", "expected coverage must declare all seven RTL dimensions"
        )
    source_paths: list[Path] = []
    file_hashes: dict[str, str] = {}
    source_files: dict[str, str] = {}
    for relative in spec["files"]:
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise ModelError(
                "model-spec", "source paths must be target-relative strings"
            )
        path = (source_root / relative).resolve()
        try:
            path.relative_to(source_root)
        except ValueError as error:
            raise ModelError(
                "model-spec", "source path escapes the Project source"
            ) from error
        if not path.is_file() or path.is_symlink():
            raise ModelError(
                "model-spec", f"source file is missing or unsafe: {relative}"
            )
        if path.name in source_files:
            raise ModelError("model-spec", "MVP source basenames must be unique")
        source_paths.append(path)
        normalized = Path(relative).as_posix()
        source_files[path.name] = normalized
        file_hashes[normalized] = _file_hash(path)
    config = {
        "top": spec["top"],
        "files": [
            {"path": relative, "sha256": file_hashes[Path(relative).as_posix()]}
            for relative in spec["files"]
        ],
        "scopes": spec["scopes"],
        "slang": spec["slang"],
        "yosys": spec["yosys"],
        "fsms": spec["fsms"],
    }
    toolchain = {"slang": slang_version, "yosys": yosys_version}
    identity = make_identity(
        project_name=str(target["name"]),
        source_revision=str(target["revision"]),
        config=config,
        toolchain=toolchain,
        spec=spec,
    )
    output = _resolve_project_output(project_dir, output_relative, ".sqlite")
    output.parent.mkdir(parents=True, exist_ok=True)
    candidate = output.with_name(f".{output.name}.candidate-{os.getpid()}")
    if candidate.exists() or candidate.is_symlink():
        raise ModelError("model-candidate", "model candidate path already exists")
    store: ModelStore | None = None
    candidate_created = False
    started = datetime.now(timezone.utc)
    try:
        store = ModelStore.create(candidate, identity, contract=contract)
        candidate_created = True
        project_id = store.add_entity("rtl.project/1:project", str(target["name"]))
        config_id = store.add_entity(
            "rtl.config/1:configuration",
            f"{target['name']}@{identity['config_hash'][:16]}",
            attrs=config,
        )
        with tempfile.TemporaryDirectory(
            prefix="rtl-model-", dir=temporary_root
        ) as temporary:
            work = Path(temporary)
            ast_path = work / "slang-ast.json"
            slang_command = [
                str(slang_executable),
                "--quiet",
                "--top",
                str(spec["top"]),
                "--ast-json",
                str(ast_path),
                "--ast-json-source-info",
            ]
            if spec["slang"].get("allow_unknown_modules"):
                slang_command.append("--ignore-unknown-modules")
            slang_command.extend(
                str(item) for item in spec["slang"].get("extra_args", [])
            )
            slang_command.extend(str(Path(relative)) for relative in spec["files"])
            slang_result = _run(slang_command, source_root, timeout=30)
            if slang_result.returncode != 0 or not ast_path.is_file():
                detail = (
                    (slang_result.stderr or slang_result.stdout)
                    .strip()
                    .splitlines()[:3]
                )
                raise ModelError(
                    "slang-analysis", " | ".join(detail) or "slang produced no AST"
                )
            ast = json.loads(ast_path.read_text(encoding="utf-8"))
            stable_slang_hash = digest(_slang_consumed_artifact(ast, source_files))
            source_input_hash = digest(config["files"])
            slang_run = store.add_producer_run(
                producer="slang",
                version=slang_version,
                command=[
                    "slang",
                    "--top",
                    spec["top"],
                    "--ast-json",
                    "$ARTIFACT",
                    *spec["files"],
                ],
                scope={"top": spec["top"], "scopes": spec["scopes"]},
                input_hash=source_input_hash,
                status="complete",
                diagnostics=[],
                artifact_hash=stable_slang_hash,
            )
            extractor = _Extractor(store, source_root, source_files, file_hashes)
            spec_ev = extractor.evidence(
                slang_run,
                spec_path.name,
                kind="configuration",
                attrs={"spec_hash": identity["spec_hash"]},
            )
            extractor.relation(
                project_id,
                "rtl.config/1:has-configuration",
                config_id,
                "configuration/elaboration",
                slang_run,
                [spec_ev],
            )
            file_ids: list[str] = []
            for relative in spec["files"]:
                file_id = store.add_entity(
                    "rtl.source/1:file",
                    relative,
                    attrs={"sha256": file_hashes[relative]},
                )
                file_ids.append(file_id)
                source_ev = extractor.evidence(slang_run, relative)
                extractor.relation(
                    config_id,
                    "rtl.config/1:uses-source",
                    file_id,
                    "configuration/elaboration",
                    slang_run,
                    [source_ev],
                )
            slang_summary = extractor.extract_slang(ast, spec, slang_run, spec_ev)
            top_id = slang_summary["modules"][spec["top"]]
            extractor.relation(
                config_id,
                "rtl.config/1:top",
                top_id,
                "configuration/elaboration",
                slang_run,
                [spec_ev],
            )

            yosys_work = work / "yosys"
            yosys_work.mkdir()
            for path in source_paths:
                shutil.copyfile(path, yosys_work / path.name)
            netlist_path = yosys_work / "netlist.json"
            read_files = " ".join(f"/src/{path.name}" for path in source_paths)
            passes = "; ".join(
                str(item) for item in spec["yosys"].get("netlist_passes", [])
            )
            yosys_script = (
                f"read_verilog -sv {read_files}; hierarchy -top {spec['top']}; "
                f"{passes}; write_json /src/netlist.json"
            )
            environment = os.environ.copy()
            environment["YOWASP_MOUNT"] = f"/src={yosys_work}"
            environment.setdefault(
                "YOWASP_CACHE_DIR", str(temporary_root.parent / "cache/yowasp")
            )
            yosys_result = _run(
                [str(yosys_executable), "-q", "-p", yosys_script],
                project_dir,
                timeout=90,
                environment=environment,
            )
            if yosys_result.returncode != 0 or not netlist_path.is_file():
                detail = (
                    (yosys_result.stderr or yosys_result.stdout)
                    .strip()
                    .splitlines()[-3:]
                )
                raise ModelError(
                    "yosys-analysis", " | ".join(detail) or "Yosys produced no netlist"
                )
            netlist = json.loads(netlist_path.read_text(encoding="utf-8"))
            yosys_run = store.add_producer_run(
                producer="yosys-netlist",
                version=yosys_version,
                command=[
                    "yosys",
                    "read_verilog",
                    *spec["files"],
                    "hierarchy",
                    spec["top"],
                    *spec["yosys"].get("netlist_passes", []),
                    "write_json",
                ],
                scope={"top": spec["top"], "scopes": spec["scopes"]},
                input_hash=source_input_hash,
                status="complete",
                diagnostics=[],
                artifact_hash=digest(_stable_artifact(netlist, source_files)),
            )
            yosys_summary = extractor.extract_yosys(netlist, spec, yosys_run)

            fsm_summaries: list[dict[str, Any]] = []
            for index, hint in enumerate(spec["fsms"]):
                fsm_json = yosys_work / f"fsm-{index}.json"
                fsm_kiss = yosys_work / f"fsm-{index}.kiss2"
                fsm_script = (
                    f"read_verilog -sv {read_files}; hierarchy -top {hint['module']}; "
                    f'{passes}; setattr -set fsm_encoding "auto" {hint["module"]}/{hint["state_signal"]}; '
                    f"fsm_extract; fsm_opt; fsm_export -origenc -o /src/{fsm_kiss.name}; write_json /src/{fsm_json.name}"
                )
                fsm_result = _run(
                    [str(yosys_executable), "-q", "-p", fsm_script],
                    project_dir,
                    timeout=90,
                    environment=environment,
                )
                if (
                    fsm_result.returncode != 0
                    or not fsm_json.is_file()
                    or not fsm_kiss.is_file()
                ):
                    detail = (
                        (fsm_result.stderr or fsm_result.stdout)
                        .strip()
                        .splitlines()[-3:]
                    )
                    raise ModelError(
                        "yosys-fsm",
                        " | ".join(detail) or "Yosys produced no FSM artifacts",
                    )
                fsm_netlist = json.loads(fsm_json.read_text(encoding="utf-8"))
                kiss = parse_kiss2(fsm_kiss.read_text(encoding="utf-8"))
                fsm_run = store.add_producer_run(
                    producer="yosys-fsm",
                    version=yosys_version,
                    command=[
                        "yosys",
                        "fsm_extract",
                        hint["module"],
                        hint["state_signal"],
                        "fsm_export",
                    ],
                    scope=hint,
                    input_hash=source_input_hash,
                    status="complete",
                    diagnostics=[],
                    artifact_hash=digest(
                        _stable_artifact(fsm_netlist, source_files), kiss
                    ),
                )
                fsm_summaries.append(
                    extractor.extract_fsm(
                        fsm_netlist, kiss, hint, slang_summary["parameters"], fsm_run
                    )
                )

            semantic_run = store.add_producer_run(
                producer="semantic-spec",
                version="1",
                command=["semantic-spec"],
                scope={"assertion_count": len(spec["semantic_assertions"])},
                input_hash=identity["spec_hash"],
                status="complete",
                diagnostics=[],
                artifact_hash=identity["spec_hash"],
            )
            for semantic in spec["semantic_assertions"]:
                subject = store.add_entity(
                    semantic["subject_kind"], semantic["subject_qualified_name"]
                )
                concept = store.add_entity(
                    "rtl.semantic/1:concept",
                    semantic["concept_qualified_name"],
                    attrs={"text": semantic["text"]},
                )
                evidence = [
                    extractor.evidence(
                        semantic_run, item["file"], item["start_line"], item["end_line"]
                    )
                    for item in semantic["evidence"]
                ]
                store.add_assertion(
                    subject_id=subject,
                    predicate=semantic["predicate"],
                    object_id=concept,
                    dimension="engineering-semantic",
                    derivation=semantic["derivation"],
                    confidence=float(semantic["confidence"]),
                    status=semantic["status"],
                    producer_run_id=semantic_run,
                    evidence_ids=evidence,
                    attrs={"text": semantic["text"]},
                )

            dimension_runs = {
                "configuration/elaboration": slang_run,
                "hierarchy": slang_run,
                "connectivity": yosys_run,
                "behavior/causality": yosys_run,
                "temporal/ordering": yosys_run,
                "clock/reset": yosys_run,
                "engineering-semantic": semantic_run,
            }
            for scope in [str(target["name"]), *spec["scopes"]]:
                for dimension, status in spec["expected_coverage"].items():
                    store.set_coverage(
                        scope=scope,
                        dimension=dimension,
                        status=status,
                        reason="MVP scope verified by selected producer"
                        if status == "verified"
                        else "MVP proves selected facts; complete sign-off is outside scope",
                        producer_run_id=dimension_runs[dimension],
                    )
                for capability in contract["required_capabilities"][scope]:
                    declaration = store.capability_declarations[capability]
                    dimension = declaration["dimension"]
                    if dimension not in spec["expected_coverage"]:
                        raise ModelError(
                            "model-spec",
                            f"required capability dimension has no expected coverage: {dimension}",
                        )
                    status = spec["expected_coverage"][dimension]
                    store.set_capability_coverage(
                        scope=scope,
                        dimension=dimension,
                        capability=capability,
                        status=status,
                        reason="MVP capability verified by selected producer"
                        if status == "verified"
                        else "MVP capability is incomplete in the selected scope",
                        producer_run_id=dimension_runs[dimension],
                    )
            content_hash = store.finalize()
            integrity = store.integrity()
            store.close()
            store = None
            _verify_jsonl_round_trip(candidate, work)
        with candidate.open("rb") as database_file:
            os.fsync(database_file.fileno())
        os.replace(candidate, output)
        directory_fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        return {
            "model_id": identity["model_id"],
            "content_hash": content_hash,
            "output": Path(output_relative).as_posix(),
            "size_bytes": output.stat().st_size,
            "duration_ms": round(elapsed * 1000),
            "integrity": integrity,
            "registers": yosys_summary["registers"],
            "fsms": fsm_summaries,
        }
    except Exception:
        if store is not None:
            store.close()
        if candidate_created:
            candidate.unlink(missing_ok=True)
        raise
