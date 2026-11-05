#!/usr/bin/env python3
"""Small source-only Knowledge Contract and model prototype for T911."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import json
import hashlib
import os
import re
import sqlite3
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


CONTRACT_ROOT = Path(__file__).resolve().parents[1] / "config"
CONTRACT_PATHS = {
    "entities": CONTRACT_ROOT / "knowledge-contract-v01-entities.json",
    "facts": CONTRACT_ROOT / "knowledge-contract-v01-facts.json",
    "ai_knowledge": CONTRACT_ROOT / "knowledge-contract-v01-ai.json",
}
CONTRACT_SCHEMAS = {
    "entities": "rtl-analysis-agent/entity-contract-v01-candidate",
    "facts": "rtl-analysis-agent/fact-contract-v01-candidate",
    "ai_knowledge": "rtl-analysis-agent/ai-knowledge-contract-v01-candidate",
}
CONTRACT_VERSION = "0.1-candidate"
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*/[1-9][0-9]*:[a-z][a-z0-9_.-]*$")
CATEGORIES = {"analysis-core", "infrastructure", "experimental"}
MATURITIES = {"supported", "experimental", "unsupported"}
EVIDENCE_KINDS = {"source", "provenance", "source-or-provenance"}
PRODUCERS = {"slang", "build-spec", "agent"}
RATIONALE_FIELDS = {"real_scenario", "expression_gap", "producer", "query_consumer"}
FORBIDDEN_SOURCE_ONLY_TERMS = {"yosys", "kiss2", "netlist_cell"}
CONTEXT_PROFILES = {"driver", "cross-module", "register-state", "module-overview"}
MODULE_KNOWLEDGE_SECTIONS = (
    "function", "microarchitecture", "submodule-role", "collaboration"
)
COVERAGE_STATUSES = {"ready", "partial", "unsupported"}
ANALYSIS_SCOPE_SCHEMA = "rtl-analysis-agent/analysis-scope-v1"
ANALYSIS_SCOPE_MODES = {
    "ACTUAL_INSTANCE_ROOT",
    "MODULE_DEFINITION_ROOT",
    "MULTI_ROOT",
    "SOURCE_ONLY",
}
ANALYSIS_SCOPE_ROOT_STATES = {"RESOLVED"}
ANALYSIS_SCOPE_BOUNDARIES = {"ROOTED_CONTAINS_CLOSURE", "SOURCE_PATH_SET"}
ANALYSIS_SCOPE_DEPENDENCY = "REFERENCED_DEFINITIONS_AND_EVIDENCE"
APPLICABILITY_SCHEMA = "rtl-analysis-agent/applicability-v1"
APPLICABILITY_PARAMETER = re.compile(r"^parameter\.[A-Za-z_][A-Za-z0-9_$]*$")
APPLICABILITY_VALUE_LIMIT = 4_096
SCHEMA_IDENTITIES = {
    "v0.1": "rtl-analysis-agent/knowledge-prototype-v01-candidate",
    "v0.2": "rtl-analysis-agent/knowledge-prototype-v02-candidate",
}
SYNTHETIC_PUBLICATION_SHA256 = "78e276c872b597aae513b5db06dbc76c15f4c05c320a67300ba8e5e760dce168"
MODULE_Q2_PUBLICATION_SHA256 = "c4fd7592691dfe3ebcc5e040fc4d9451a95d5953877edb07d8fc435b36d5dce5"
MODULE_Q2_PUBLICATION_PATH = CONTRACT_ROOT / "module-q2-publication-profile.v2.json"
Q2_FIFO_PUBLICATION_SHA256 = "9c9755efb38c488b2607d113bc050c352d080983bd40d8034396ee369e307fe1"
Q2_FIFO_PUBLICATION_PATH = CONTRACT_ROOT / "ibex-q2-fifo-publication-profile.v1.json"
Q2_FIFO_V2_PUBLICATION_SHA256 = "ddfa55bbc1728fb67864e0ab0d32f7b306c17e6041756fd2fbc9334f1ad05902"
Q2_FIFO_V2_PUBLICATION_PATH = CONTRACT_ROOT / "ibex-q2-fifo-publication-profile.v2.json"
PUBLICATION_REVIEW_SCHEMA = "rtl-analysis-agent/publication-review-input-v1"
MODULE_Q2_GOALS = tuple(
    ["OV01", "OV02"] + [f"R{index:02d}" for index in range(1, 5)]
    + [f"I{index:02d}" for index in range(1, 4)] + ["C01", "F01", "F02"]
)
Q2_FIFO_GOALS = MODULE_Q2_GOALS + tuple(f"Q4-F{index:02d}" for index in range(1, 21)) + tuple(
    f"Q4-R{index:02d}" for index in range(1, 34)
)
IBEX_OLD_Q2_COMPONENT_SHA256 = {
    "old_q2_input": "80990efdc428da9537a951993f36e5d9904800015bce6ace6e4b2e16c3098c85",
    "old_q2_review": "88850242cb5220c9e2852a6fa9305cf0d22caf2ee1ccc759ceff06c7ed962001",
    "old_q2_approval": "76a1b9b2d8f6a39d86fdd35cb7802a8553d61213aeecbb2a4a58bfa56db79143",
}


class ContractError(RuntimeError):
    pass


class StoreError(RuntimeError):
    pass


def _contract_path(group: str, root: Path | None) -> Path:
    path = CONTRACT_PATHS[group]
    return path if root is None else root / path.name


def _read_contract(group: str, root: Path | None) -> list[dict[str, Any]]:
    path = _contract_path(group, root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ContractError(f"cannot read {group} contract: {error}") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "version", "entries"}
        or payload.get("schema") != CONTRACT_SCHEMAS[group]
        or payload.get("version") != CONTRACT_VERSION
        or not isinstance(payload.get("entries"), list)
    ):
        raise ContractError(f"invalid {group} contract shape")
    return payload["entries"]


def _nonempty_strings(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and bool(item.strip()) for item in value)
        and len(value) == len(set(value))
    )


def _validate_common(group: str, entry: Any, seen: set[str]) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ContractError(f"invalid {group} entry")
    identifier = entry.get("id")
    if not isinstance(identifier, str) or not ID_PATTERN.fullmatch(identifier):
        raise ContractError(f"invalid {group} ID")
    if identifier in seen:
        raise ContractError(f"duplicate {group} ID: {identifier}")
    seen.add(identifier)
    if not isinstance(entry.get("name"), str) or not entry["name"].strip():
        raise ContractError(f"invalid {group} name")
    if entry.get("category") not in CATEGORIES:
        raise ContractError(f"invalid {group} category")
    if entry.get("maturity") not in MATURITIES:
        raise ContractError(f"invalid {group} maturity")
    if not _nonempty_strings(entry.get("query_roles")):
        raise ContractError(f"invalid {group} query roles")
    producers = entry.get("producers")
    if not isinstance(producers, list) or len(producers) != len(set(producers)):
        raise ContractError(f"invalid {group} producers")
    if any(producer not in PRODUCERS for producer in producers):
        raise ContractError(f"invalid {group} producer")
    if entry["maturity"] == "supported" and not producers:
        raise ContractError(f"supported {group} entry needs a producer")
    rationale = entry.get("extension_rationale")
    if entry["category"] == "analysis-core":
        if rationale is not None:
            raise ContractError(f"core {group} entry cannot have extension rationale")
    elif (
        not isinstance(rationale, dict)
        or set(rationale) != RATIONALE_FIELDS
        or not all(isinstance(value, str) and value.strip() for value in rationale.values())
    ):
        raise ContractError(f"extended {group} entry needs four-part rationale")
    return entry


def load_contracts(root: Path | None = None) -> dict[str, Any]:
    """Load and cross-validate the three v0.1 candidate contracts."""
    raw = {group: _read_contract(group, root) for group in CONTRACT_PATHS}
    validated: dict[str, list[dict[str, Any]]] = {}
    for group, entries in raw.items():
        seen: set[str] = set()
        validated[group] = [_validate_common(group, entry, seen) for entry in entries]

    entity_ids = {entry["id"] for entry in validated["entities"]}
    for entry in validated["entities"]:
        expected = {
            "name", "id", "category", "maturity", "evidence",
            "producers", "query_roles",
        }
        if entry["category"] != "analysis-core":
            expected.add("extension_rationale")
        if set(entry) != expected or entry.get("evidence") not in EVIDENCE_KINDS:
            raise ContractError("invalid entities contract entry shape")

    for entry in validated["facts"]:
        expected = {
            "name", "id", "category", "maturity", "subject_kinds",
            "object_kinds", "producers", "evidence_required", "query_roles",
        }
        if entry["category"] != "analysis-core":
            expected.add("extension_rationale")
        if set(entry) != expected:
            raise ContractError("invalid facts contract entry shape")
        if not _nonempty_strings(entry["subject_kinds"]) or not _nonempty_strings(entry["object_kinds"]):
            raise ContractError("invalid fact endpoint declaration")
        endpoints = set(entry["subject_kinds"]) | set(entry["object_kinds"])
        if not endpoints <= entity_ids:
            raise ContractError("fact endpoint references unknown entity kind")
        if entry["evidence_required"] is not True:
            raise ContractError("fact evidence must be required")

    for entry in validated["ai_knowledge"]:
        expected = {
            "name", "id", "category", "maturity", "subject_kinds",
            "producers", "requires_fact_support", "evidence_required",
            "query_roles",
        }
        if set(entry) != expected or entry["category"] != "analysis-core":
            raise ContractError("invalid AI knowledge contract entry shape")
        if not _nonempty_strings(entry["subject_kinds"]):
            raise ContractError("invalid AI knowledge subject declaration")
        if not set(entry["subject_kinds"]) <= entity_ids:
            raise ContractError("AI knowledge subject references unknown entity kind")
        if entry["producers"] != ["agent"]:
            raise ContractError("AI knowledge producer must be agent")
        if entry["requires_fact_support"] is not True:
            raise ContractError("AI knowledge requires Fact support")
        if entry["evidence_required"] is not True:
            raise ContractError("AI knowledge evidence must be required")

    serialized = json.dumps(validated, ensure_ascii=False).casefold()
    if any(term in serialized for term in FORBIDDEN_SOURCE_ONLY_TERMS):
        raise ContractError("source-only contract contains a forbidden producer or kind")
    return {"version": CONTRACT_VERSION, **validated}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identity(prefix: str, *parts: Any) -> str:
    payload = "\0".join(part if isinstance(part, str) else _canonical(part) for part in parts)
    return f"{prefix}:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_publication_contract(path: Path) -> tuple[dict[str, Any], str, str]:
    """Accept exact, purpose-separated Profile byte sequences."""
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    purpose = {
        SYNTHETIC_PUBLICATION_SHA256: "SYNTHETIC_ONLY",
        MODULE_Q2_PUBLICATION_SHA256: "REAL_MODULE_Q2",
        Q2_FIFO_PUBLICATION_SHA256: "REAL_Q2_FIFO_STAGE1_ONLY",
        Q2_FIFO_V2_PUBLICATION_SHA256: "REAL_Q2_FIFO",
    }.get(digest)
    if purpose is None:
        raise StoreError("publication contract is not the pinned version")
    contract = json.loads(data)
    status = {
        "SYNTHETIC_ONLY": "UNAPPROVED_SYNTHETIC_B6_0",
        "REAL_MODULE_Q2": "CONTROLLED_REAL_Q2_V1",
        "REAL_Q2_FIFO_STAGE1_ONLY": "STAGE1_ONLY_Q2_FIFO_V1",
        "REAL_Q2_FIFO": "CONTROLLED_REAL_Q2_FIFO_V2",
    }[purpose]
    if contract.get("status") != status:
        raise StoreError("publication contract purpose is invalid")
    if purpose in {"REAL_MODULE_Q2", "REAL_Q2_FIFO_STAGE1_ONLY", "REAL_Q2_FIFO"}:
        declared = {item["id"] for item in load_contracts()["ai_knowledge"]}
        if (contract.get("vocabulary") != {
            "id": "rtl-analysis-agent/knowledge-contract", "version": CONTRACT_VERSION,
        } or {shape["knowledge_type"] for shape in contract["claim_shapes"].values()} - declared):
            raise StoreError("real publication Profile uses an undeclared vocabulary")
    return contract, digest, purpose


def _publication_identity(contract: dict[str, Any], digest: str, purpose: str) -> dict[str, Any]:
    return {"profile": contract["profile"], "vocabulary": contract["vocabulary"],
            "sha256": digest, "purpose": purpose}


def _verify_controlled_review(review_root: Path, review: dict[str, Any], digest: str) -> None:
    """A real independent review trust source is deliberately unavailable in B6.1.

    Synthetic tests replace this function in their isolated process. No caller flag,
    review identity string, or self-declared fixture bit can enable publication.
    """
    raise StoreError("controlled independent review provenance is not configured")


def _bound_json(root: Path, relative: str) -> tuple[dict[str, Any], str]:
    """Read one canonical artifact inside the caller's review material root."""
    if not PrototypeStore._scope_relative_path(relative):
        raise StoreError("approval artifact path is unsafe")
    path = root / relative
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise StoreError("approval artifact path is unsafe")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise StoreError("approval artifact JSON is invalid") from error
    if not isinstance(value, dict) or raw != (_canonical(value) + "\n").encode():
        raise StoreError("approval artifact is not canonical")
    return value, hashlib.sha256(raw).hexdigest()


def _publication_review_attrs(review_input: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    """Keep the exact reviewed input in its content-addressed provenance Evidence."""
    return {
        "input_sha256": review["input_sha256"], "reviewer": review["reviewer"],
        "generator": review["generator"], "status": "VERIFIED",
        "artifact_sha256": review["artifact_sha256"],
        "review_input_json": _canonical(review_input),
    }


def _publication_missing_fact_fields(shape: dict[str, Any], links: list[dict[str, Any]]) -> set[str]:
    """A quality label alone cannot prove the required structured fields."""
    proven = set().union(*(set(link["fields_proven"]) for link in links if link["quality"] != "SCOPE_ONLY"))
    return set(shape["fact_content"]["required_fields"]) - proven


def knowledge_id_digest(knowledge_ids: list[str]) -> str:
    """Hash one exact published knowledge set independently of insertion order."""
    return hashlib.sha256(_canonical(sorted(set(knowledge_ids))).encode("utf-8")).hexdigest()


STAGE1_SCHEMA = """
CREATE TABLE model_metadata(
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL
) STRICT;
CREATE TABLE entity(
  entity_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  qualified_name TEXT NOT NULL,
  attrs_json TEXT NOT NULL,
  UNIQUE(kind, qualified_name)
) STRICT;
CREATE TABLE evidence(
  evidence_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('source', 'provenance')),
  locator TEXT NOT NULL,
  start_line INTEGER,
  end_line INTEGER,
  attrs_json TEXT NOT NULL,
  CHECK(start_line IS NULL OR start_line > 0),
  CHECK(end_line IS NULL OR end_line >= start_line)
) STRICT;
CREATE TABLE fact(
  fact_id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES entity(entity_id),
  predicate TEXT NOT NULL,
  object_id TEXT NOT NULL REFERENCES entity(entity_id),
  producer TEXT NOT NULL CHECK(producer IN ('slang', 'build-spec')),
  attrs_json TEXT NOT NULL,
  UNIQUE(subject_id, predicate, object_id, attrs_json)
) STRICT;
CREATE TABLE fact_evidence(
  fact_id TEXT NOT NULL REFERENCES fact(fact_id) ON DELETE CASCADE,
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  PRIMARY KEY(fact_id, evidence_id)
) STRICT;
"""

PROTOTYPE_SCHEMA = STAGE1_SCHEMA + """
CREATE TABLE ai_knowledge(
  ai_id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES entity(entity_id),
  knowledge_type TEXT NOT NULL,
  text TEXT NOT NULL CHECK(length(trim(text)) > 0),
  producer TEXT NOT NULL CHECK(producer = 'agent'),
  attrs_json TEXT NOT NULL,
  UNIQUE(subject_id, knowledge_type, text)
) STRICT;
CREATE TABLE ai_fact_support(
  ai_id TEXT NOT NULL REFERENCES ai_knowledge(ai_id) ON DELETE CASCADE,
  fact_id TEXT NOT NULL REFERENCES fact(fact_id),
  PRIMARY KEY(ai_id, fact_id)
) STRICT;
CREATE TABLE ai_evidence(
  ai_id TEXT NOT NULL REFERENCES ai_knowledge(ai_id) ON DELETE CASCADE,
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  PRIMARY KEY(ai_id, evidence_id)
) STRICT;
CREATE TABLE knowledge_coverage(
  subject_id TEXT NOT NULL REFERENCES entity(entity_id),
  section TEXT NOT NULL CHECK(section IN ('function', 'microarchitecture', 'submodule-role', 'collaboration')),
  subtree_scope TEXT NOT NULL CHECK(subtree_scope = 'direct'),
  status TEXT NOT NULL CHECK(status IN ('ready', 'partial', 'unsupported')),
  child_instance_ids_json TEXT NOT NULL,
  request_kinds_json TEXT NOT NULL,
  attrs_json TEXT NOT NULL,
  PRIMARY KEY(subject_id, section, subtree_scope)
) STRICT;
CREATE TABLE knowledge_coverage_evidence(
  subject_id TEXT NOT NULL,
  section TEXT NOT NULL,
  subtree_scope TEXT NOT NULL,
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  PRIMARY KEY(subject_id, section, subtree_scope, evidence_id),
  FOREIGN KEY(subject_id, section, subtree_scope)
    REFERENCES knowledge_coverage(subject_id, section, subtree_scope) ON DELETE CASCADE
) STRICT;
"""

PROTOTYPE_SCHEMA_V02 = STAGE1_SCHEMA + """
CREATE TABLE behavior_object(
  behavior_id TEXT PRIMARY KEY,
  scope_id TEXT NOT NULL,
  owner_subject_id TEXT REFERENCES entity(entity_id),
  behavior_type TEXT NOT NULL,
  behavior_key TEXT NOT NULL,
  applicability_json TEXT NOT NULL,
  lifecycle TEXT NOT NULL CHECK(lifecycle IN ('PUBLISHED', 'STALE')),
  UNIQUE(scope_id, behavior_type, behavior_key)
) STRICT;
CREATE TABLE ai_knowledge(
  ai_id TEXT PRIMARY KEY,
  subject_id TEXT REFERENCES entity(entity_id),
  behavior_id TEXT REFERENCES behavior_object(behavior_id),
  claim_key TEXT NOT NULL,
  knowledge_type TEXT NOT NULL,
  text TEXT NOT NULL CHECK(length(trim(text)) > 0),
  producer TEXT NOT NULL CHECK(producer = 'agent'),
  structured_schema_id TEXT NOT NULL,
  structured_fields_json TEXT NOT NULL,
  applicability_json TEXT NOT NULL,
  limitations_json TEXT NOT NULL,
  review_evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  lifecycle TEXT NOT NULL CHECK(lifecycle IN ('PUBLISHED', 'STALE')),
  attrs_json TEXT NOT NULL,
  CHECK((subject_id IS NOT NULL) != (behavior_id IS NOT NULL))
) STRICT;
CREATE UNIQUE INDEX ai_current_subject_key
  ON ai_knowledge(subject_id, claim_key)
  WHERE lifecycle = 'PUBLISHED' AND subject_id IS NOT NULL;
CREATE UNIQUE INDEX ai_current_behavior_key
  ON ai_knowledge(behavior_id, claim_key)
  WHERE lifecycle = 'PUBLISHED' AND behavior_id IS NOT NULL;
CREATE TABLE ai_fact_support(
  ai_id TEXT NOT NULL REFERENCES ai_knowledge(ai_id) ON DELETE CASCADE,
  fact_id TEXT NOT NULL REFERENCES fact(fact_id),
  support_quality TEXT NOT NULL CHECK(support_quality IN ('EXACT', 'PARTIAL', 'SCOPE_ONLY')),
  PRIMARY KEY(ai_id, fact_id)
) STRICT;
CREATE TABLE ai_evidence(
  ai_id TEXT NOT NULL REFERENCES ai_knowledge(ai_id) ON DELETE CASCADE,
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  PRIMARY KEY(ai_id, evidence_id)
) STRICT;
CREATE TABLE obligation_coverage(
  coverage_id TEXT PRIMARY KEY,
  scope_id TEXT NOT NULL,
  target_kind TEXT NOT NULL CHECK(target_kind IN ('SCOPE', 'SUBJECT', 'BEHAVIOR')),
  target_subject_id TEXT REFERENCES entity(entity_id),
  target_behavior_id TEXT REFERENCES behavior_object(behavior_id),
  obligation_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  profile_version TEXT NOT NULL,
  vocabulary_id TEXT NOT NULL,
  vocabulary_version TEXT NOT NULL,
  closure_status TEXT NOT NULL CHECK(closure_status IN ('READY', 'PARTIAL', 'GAP')),
  validity TEXT NOT NULL CHECK(validity IN ('CURRENT', 'STALE')),
  applicability_json TEXT NOT NULL,
  claim_set_digest TEXT NOT NULL,
  review_evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  limitations_json TEXT NOT NULL,
  conflicts_json TEXT NOT NULL,
  unresolved_json TEXT NOT NULL,
  CHECK(
    (target_kind = 'SCOPE' AND target_subject_id IS NULL AND target_behavior_id IS NULL)
    OR (target_kind = 'SUBJECT' AND target_subject_id IS NOT NULL AND target_behavior_id IS NULL)
    OR (target_kind = 'BEHAVIOR' AND target_subject_id IS NULL AND target_behavior_id IS NOT NULL)
  )
) STRICT;
CREATE UNIQUE INDEX coverage_current_slice ON obligation_coverage(
  scope_id, target_kind, ifnull(target_subject_id, ''), ifnull(target_behavior_id, ''),
  obligation_id, profile_id, profile_version, vocabulary_id, vocabulary_version,
  applicability_json
) WHERE validity = 'CURRENT';
CREATE TABLE obligation_coverage_claim(
  coverage_id TEXT NOT NULL REFERENCES obligation_coverage(coverage_id) ON DELETE CASCADE,
  ai_id TEXT NOT NULL REFERENCES ai_knowledge(ai_id),
  PRIMARY KEY(coverage_id, ai_id)
) STRICT;
"""


class PrototypeStore:
    """Thin candidate-only persistence for the source-only Prototype."""

    def __init__(
        self,
        database: Path,
        connection: sqlite3.Connection,
        contracts: dict[str, Any],
        *,
        schema_version: str = "v0.1",
    ):
        self.database = database
        self.connection = connection
        self.schema_version = schema_version
        self._discarded = False
        self._stage1_expectations: dict[str, set[bytes]] | None = None
        self.contracts = contracts
        self.contract_ids = {
            group: {item["name"]: item["id"] for item in contracts[group]}
            for group in ("entities", "facts", "ai_knowledge")
        }
        self.entity_contract = {item["id"]: item for item in contracts["entities"]}
        self.fact_contract = {item["id"]: item for item in contracts["facts"]}
        self.ai_contract = {item["id"]: item for item in contracts["ai_knowledge"]}
        self._batch_depth = 0
        self._entity_kind_cache: dict[str, str] = {}
        self._known_ids: dict[str, set[str]] = {
            "evidence": set(), "fact": set(), "ai_knowledge": set(),
        }

    def _write_context(self):
        if self._discarded:
            raise StoreError("Store connection was discarded after an uncertain transaction")
        return nullcontext() if self._batch_depth else self.connection

    def _clear_identity_caches(self) -> None:
        self._entity_kind_cache.clear()
        for identifiers in self._known_ids.values():
            identifiers.clear()
        self._stage1_expectations = None

    def begin_stage1_audit(self) -> None:
        """Capture the producer's expected persisted rows before population."""
        if self._stage1_expectations is not None:
            raise StoreError("Stage1 audit is already active")
        self._stage1_expectations = {
            "fact": set(), "evidence": set(), "fact_evidence": set(),
        }

    def _expect_stage1_row(self, table: str, values: tuple[Any, ...]) -> None:
        if self._stage1_expectations is not None:
            digest = hashlib.sha256(_canonical(values).encode("utf-8")).digest()
            self._stage1_expectations[table].add(digest)

    def audit_stage1_rows(self) -> None:
        """Match every Fact, Evidence and link to producer calls, including omissions."""
        expected = self._stage1_expectations
        if expected is None:
            raise StoreError("Stage1 producer expectations are unavailable")
        orphan = self.connection.execute(
            "SELECT f.fact_id FROM fact AS f WHERE NOT EXISTS "
            "(SELECT 1 FROM fact_evidence AS fe WHERE fe.fact_id=f.fact_id) LIMIT 1"
        ).fetchone()
        if orphan is not None:
            raise StoreError("Stage1 Fact has no Evidence link")
        columns = {
            "fact": "fact_id, subject_id, predicate, object_id, producer, attrs_json",
            "evidence": "evidence_id, kind, locator, start_line, end_line, attrs_json",
            "fact_evidence": "fact_id, evidence_id",
        }
        for table, projection in columns.items():
            remaining = expected[table]
            for row in self.connection.execute(f"SELECT {projection} FROM {table}"):
                digest = hashlib.sha256(
                    _canonical(tuple(row)).encode("utf-8")
                ).digest()
                if digest not in remaining:
                    raise StoreError(f"Stage1 {table} has an unexpected row")
                remaining.remove(digest)
            if remaining:
                raise StoreError(f"Stage1 {table} is missing producer rows")
        self._stage1_expectations = None

    def _discard_connection(self) -> None:
        self._discarded = True
        try:
            self.connection.close()
        except BaseException:
            pass

    @contextmanager
    def transaction(self):
        """Use one native SQLite transaction for a bounded candidate write batch."""
        if self._discarded:
            raise StoreError("Store connection was discarded after an uncertain transaction")
        if self._batch_depth:
            raise StoreError("nested Store transactions are not supported")
        started = False
        commit_attempted = False
        try:
            if self.connection.in_transaction:
                raise StoreError("Store connection already has an active transaction")
            self.connection.execute("BEGIN IMMEDIATE")
            started = True
            self._batch_depth = 1
            yield
            commit_attempted = True
            self.connection.commit()
        except BaseException as error:
            uncertain = False
            if started:
                try:
                    active = self.connection.in_transaction
                except BaseException:
                    active = None
                if active:
                    try:
                        self.connection.rollback()
                    except BaseException as cleanup_error:
                        uncertain = True
                        error.add_note(f"transaction rollback failed: {cleanup_error}")
                elif active is None or commit_attempted:
                    # COMMIT can have succeeded even when its response was lost.
                    uncertain = True
                else:
                    # A body that ended its own transaction bypassed this helper.
                    uncertain = True
            self._clear_identity_caches()
            if uncertain:
                self._discard_connection()
                error.add_note("transaction outcome requires recovery on a fresh connection")
            raise
        finally:
            self._batch_depth = 0

    def contract_id(self, group: str, name: str) -> str:
        """Resolve a vocabulary name from the Contract instead of duplicating its ID."""
        try:
            return self.contract_ids[group][name]
        except KeyError as error:
            raise StoreError(f"unknown Contract vocabulary name: {group}.{name}") from error

    @classmethod
    def create(
        cls,
        database: Path,
        metadata: dict[str, Any],
        *,
        contract_root: Path | None = None,
        schema_version: str = "v0.1",
        publication_contract: Path | None = None,
    ) -> "PrototypeStore":
        if schema_version not in SCHEMA_IDENTITIES:
            raise StoreError("candidate schema version is unsupported")
        if "schema" in metadata or "contract_version" in metadata:
            raise StoreError("candidate metadata cannot override schema identity")
        if "semantic_publication_contract" in metadata:
            raise StoreError("candidate metadata cannot override publication identity")
        if publication_contract is not None:
            if schema_version != "v0.2":
                raise StoreError("semantic publication requires v0.2")
            publication, digest, purpose = _load_publication_contract(publication_contract)
            metadata = {
                **metadata,
                "semantic_publication_contract": _publication_identity(publication, digest, purpose),
            }
        if database.is_symlink():
            raise StoreError("candidate database already exists")
        database = database.parent.resolve() / database.name
        database.parent.mkdir(parents=True, exist_ok=True)
        contracts = load_contracts(contract_root)
        connection: sqlite3.Connection | None = None
        owned = False
        try:
            try:
                fd = os.open(database, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            except FileExistsError as error:
                raise StoreError("candidate database already exists") from error
            os.close(fd)
            owned = True
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = MEMORY")
            connection.execute("PRAGMA synchronous = OFF")
            connection.executescript(
                PROTOTYPE_SCHEMA if schema_version == "v0.1" else PROTOTYPE_SCHEMA_V02
            )
            rows = {
                "schema": SCHEMA_IDENTITIES[schema_version],
                "contract_version": contracts["version"],
                **metadata,
            }
            with connection:
                connection.executemany(
                    "INSERT INTO model_metadata(key, value_json) VALUES (?, ?)",
                    [(key, _canonical(value)) for key, value in sorted(rows.items())],
                )
            return cls(database, connection, contracts, schema_version=schema_version)
        except BaseException:
            if connection is not None:
                connection.close()
            if owned:
                database.unlink(missing_ok=True)
            raise

    @classmethod
    def open(
        cls,
        database: Path,
        *,
        readonly: bool = False,
        contract_root: Path | None = None,
    ) -> "PrototypeStore":
        if database.is_symlink():
            raise StoreError("candidate database does not exist or is unsafe")
        database = database.parent.resolve() / database.name
        if not database.is_file():
            raise StoreError("candidate database does not exist or is unsafe")
        if readonly:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        else:
            connection = sqlite3.connect(database)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            if readonly:
                connection.execute("PRAGMA query_only = ON")
            row = connection.execute(
                "SELECT value_json FROM model_metadata WHERE key = 'schema'"
            ).fetchone()
            identity = json.loads(row[0]) if row is not None else None
            versions = {value: key for key, value in SCHEMA_IDENTITIES.items()}
            if identity not in versions:
                raise StoreError("candidate database schema is invalid")
            store = cls(database, connection, load_contracts(contract_root),
                        schema_version=versions[identity])
            if store.schema_version == "v0.2":
                store.analysis_scope()
            return store
        except BaseException:
            connection.close()
            raise

    @classmethod
    def open_publication(cls, database: Path, *, publication_contract: Path) -> "PrototypeStore":
        """Open the unique candidate path with durable SQLite publication settings."""
        publication, digest, purpose = _load_publication_contract(publication_contract)
        store = cls.open(database)
        try:
            if store.schema_version != "v0.2":
                raise StoreError("semantic publication requires v0.2")
            if store.database.stat().st_nlink != 1:
                raise StoreError("publication requires one recovered candidate path")
            identity = store.metadata("semantic_publication_contract")
            if identity != _publication_identity(publication, digest, purpose):
                raise StoreError("candidate publication identity is not pinned")
            store.connection.execute("PRAGMA journal_mode=DELETE")
            store.connection.execute("PRAGMA synchronous=EXTRA")
            store.connection.execute("PRAGMA foreign_keys=ON")
            modes = (
                store.connection.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                store.connection.execute("PRAGMA synchronous").fetchone()[0],
                store.connection.execute("PRAGMA foreign_keys").fetchone()[0],
            )
            if modes != ("delete", 3, 1):
                raise StoreError("durable publication PRAGMA verification failed")
            return store
        except BaseException:
            store.close()
            raise

    def close(self) -> None:
        self.connection.close()
        self._discarded = True

    def add_entity(
        self, kind: str, qualified_name: str, *, attrs: dict[str, Any] | None = None
    ) -> str:
        if kind not in self.entity_contract:
            raise StoreError(f"unknown entity kind: {kind}")
        if not isinstance(qualified_name, str) or not qualified_name.strip():
            raise StoreError("entity qualified name is empty")
        attrs_json = _canonical(attrs or {})
        entity_id = _identity("e", kind, qualified_name)
        with self._write_context():
            self.connection.execute(
                "INSERT OR IGNORE INTO entity(entity_id, kind, qualified_name, attrs_json) VALUES (?, ?, ?, ?)",
                (entity_id, kind, qualified_name, attrs_json),
            )
        self._entity_kind_cache[entity_id] = kind
        return entity_id

    def add_evidence(
        self,
        *,
        kind: str,
        locator: str,
        start_line: int | None = None,
        end_line: int | None = None,
        attrs: dict[str, Any] | None = None,
    ) -> str:
        if kind not in {"source", "provenance"}:
            raise StoreError("evidence kind must be source or provenance")
        if not isinstance(locator, str) or not locator.strip() or "\n" in locator:
            raise StoreError("evidence locator is invalid")
        if start_line is not None and (not isinstance(start_line, int) or start_line < 1):
            raise StoreError("evidence start line is invalid")
        if end_line is None:
            end_line = start_line
        if end_line is not None and (start_line is None or end_line < start_line):
            raise StoreError("evidence end line is invalid")
        attrs_json = _canonical(attrs or {})
        evidence_id = _identity("ev", kind, locator, start_line, end_line, attrs_json)
        with self._write_context():
            self.connection.execute(
                "INSERT OR IGNORE INTO evidence(evidence_id, kind, locator, start_line, end_line, attrs_json) VALUES (?, ?, ?, ?, ?, ?)",
                (evidence_id, kind, locator, start_line, end_line, attrs_json),
            )
        self._expect_stage1_row(
            "evidence", (evidence_id, kind, locator, start_line, end_line, attrs_json)
        )
        self._known_ids["evidence"].add(evidence_id)
        return evidence_id

    def _entity_kind(self, entity_id: str) -> str:
        cached = self._entity_kind_cache.get(entity_id)
        if cached is not None:
            return cached
        row = self.connection.execute(
            "SELECT kind FROM entity WHERE entity_id = ?", (entity_id,)
        ).fetchone()
        if row is None:
            raise StoreError("fact or AI endpoint does not exist")
        kind = str(row["kind"])
        self._entity_kind_cache[entity_id] = kind
        return kind

    def _existing_ids(self, table: str, column: str, identifiers: list[str]) -> bool:
        if not identifiers:
            return False
        known = self._known_ids.get(table)
        if known is not None and set(identifiers) <= known:
            return True
        placeholders = ",".join("?" for _ in identifiers)
        count = self.connection.execute(
            f"SELECT count(*) FROM {table} WHERE {column} IN ({placeholders})",
            identifiers,
        ).fetchone()[0]
        exists = count == len(set(identifiers))
        if exists and known is not None:
            known.update(identifiers)
        return exists

    def add_fact(
        self,
        subject_id: str,
        predicate: str,
        object_id: str,
        *,
        producer: str,
        evidence_ids: list[str],
        attrs: dict[str, Any] | None = None,
    ) -> str:
        declaration = self.fact_contract.get(predicate)
        if declaration is None:
            raise StoreError(f"unknown fact predicate: {predicate}")
        if producer not in declaration["producers"] or producer == "agent":
            raise StoreError("fact producer is not allowed by the Contract")
        subject_kind = self._entity_kind(subject_id)
        object_kind = self._entity_kind(object_id)
        if subject_kind not in declaration["subject_kinds"] or object_kind not in declaration["object_kinds"]:
            raise StoreError("fact endpoint kind is not allowed by the Contract")
        if not evidence_ids or not self._existing_ids("evidence", "evidence_id", evidence_ids):
            raise StoreError("fact evidence is missing or unknown")
        attrs_json = _canonical(attrs or {})
        fact_id = _identity("f", subject_id, predicate, object_id, attrs_json)
        with self._write_context():
            self.connection.execute(
                "INSERT OR IGNORE INTO fact(fact_id, subject_id, predicate, object_id, producer, attrs_json) VALUES (?, ?, ?, ?, ?, ?)",
                (fact_id, subject_id, predicate, object_id, producer, attrs_json),
            )
            self.connection.executemany(
                "INSERT OR IGNORE INTO fact_evidence(fact_id, evidence_id) VALUES (?, ?)",
                [(fact_id, evidence_id) for evidence_id in evidence_ids],
            )
        self._expect_stage1_row(
            "fact", (fact_id, subject_id, predicate, object_id, producer, attrs_json)
        )
        for evidence_id in evidence_ids:
            self._expect_stage1_row("fact_evidence", (fact_id, evidence_id))
        self._known_ids["fact"].add(fact_id)
        return fact_id

    def add_ai_knowledge(
        self,
        subject_id: str,
        knowledge_type: str,
        text: str,
        *,
        support_fact_ids: list[str],
        evidence_ids: list[str],
        attrs: dict[str, Any] | None = None,
    ) -> str:
        if self.schema_version != "v0.1":
            raise StoreError("legacy AI writer is unavailable for v0.2 candidates")
        declaration = self.ai_contract.get(knowledge_type)
        if declaration is None:
            raise StoreError(f"unknown AI knowledge type: {knowledge_type}")
        if self._entity_kind(subject_id) not in declaration["subject_kinds"]:
            raise StoreError("AI knowledge subject kind is not allowed")
        if not isinstance(text, str) or not text.strip():
            raise StoreError("AI knowledge text is empty")
        if not support_fact_ids or not self._existing_ids("fact", "fact_id", support_fact_ids):
            raise StoreError("AI knowledge support is missing or unknown")
        if not evidence_ids or not self._existing_ids("evidence", "evidence_id", evidence_ids):
            raise StoreError("AI knowledge evidence is missing or unknown")
        attrs_json = _canonical(attrs or {})
        ai_id = _identity("ai", subject_id, knowledge_type, text)
        with self._write_context():
            self.connection.execute(
                "INSERT OR IGNORE INTO ai_knowledge(ai_id, subject_id, knowledge_type, text, producer, attrs_json) VALUES (?, ?, ?, ?, 'agent', ?)",
                (ai_id, subject_id, knowledge_type, text.strip(), attrs_json),
            )
            self.connection.executemany(
                "INSERT OR IGNORE INTO ai_fact_support(ai_id, fact_id) VALUES (?, ?)",
                [(ai_id, fact_id) for fact_id in support_fact_ids],
            )
            self.connection.executemany(
                "INSERT OR IGNORE INTO ai_evidence(ai_id, evidence_id) VALUES (?, ?)",
                [(ai_id, evidence_id) for evidence_id in evidence_ids],
            )
        self._known_ids["ai_knowledge"].add(ai_id)
        return ai_id

    def metadata(self, key: str) -> Any:
        row = self.connection.execute(
            "SELECT value_json FROM model_metadata WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            raise StoreError(f"candidate metadata is missing: {key}")
        return json.loads(row[0])

    def add_metadata_once(self, key: str, value: Any) -> None:
        """Append derived audit metadata without offering an overwrite path."""
        if not isinstance(key, str) or not key.strip() or "\n" in key:
            raise StoreError("candidate metadata key is invalid")
        if key == "semantic_publication_contract":
            raise StoreError("publication identity is fixed at candidate creation")
        with self._write_context():
            self.connection.execute(
                "INSERT INTO model_metadata(key, value_json) VALUES (?, ?)",
                (key, _canonical(value)),
            )

    @staticmethod
    def _scope_relative_path(value: Any) -> bool:
        if not isinstance(value, str) or not value or "\\" in value:
            return False
        path = PurePosixPath(value)
        return not path.is_absolute() and ".." not in path.parts and value == path.as_posix()

    def validate_analysis_scope(self, scope: Any) -> dict[str, Any]:
        """Validate the one-candidate Build1 Analysis Scope metadata envelope."""
        required = {
            "schema", "scope_id", "build_identity", "scope_mode", "roots",
            "boundary_policy", "dependency_policy", "applicability", "profile",
            "vocabulary", "producer_capability", "completeness", "limitations",
        }
        if not isinstance(scope, dict) or set(scope) != required:
            raise StoreError("analysis scope shape is invalid")
        if scope.get("schema") != ANALYSIS_SCOPE_SCHEMA:
            raise StoreError("analysis scope schema is invalid")
        mode = scope.get("scope_mode")
        if mode not in ANALYSIS_SCOPE_MODES:
            raise StoreError("analysis scope mode is invalid")

        expected_build_keys = {
            "build_sha256", "build_spec_sha256", "source_closure_sha256", "source_revision"
        }
        build_identity = scope.get("build_identity")
        if not isinstance(build_identity, dict) or set(build_identity) != expected_build_keys:
            raise StoreError("analysis scope build identity is invalid")
        for key in expected_build_keys:
            if build_identity[key] != self.metadata(key):
                raise StoreError(f"analysis scope build identity does not match candidate: {key}")

        roots = scope.get("roots")
        if not isinstance(roots, list):
            raise StoreError("analysis scope roots are invalid")
        root_key = lambda root: (
            root.get("entity_id", ""), root.get("kind", ""),
            root.get("role", ""), root.get("resolution_state", ""),
        ) if isinstance(root, dict) else ("", "", "", "")
        if roots != sorted(roots, key=root_key):
            raise StoreError("analysis scope roots are not canonical")
        root_ids: set[str] = set()
        instance_kind = self.contract_id("entities", "INSTANCE")
        module_kind = self.contract_id("entities", "MODULE")
        for root in roots:
            if not isinstance(root, dict) or set(root) != {
                "entity_id", "kind", "role", "resolution_state"
            }:
                raise StoreError("analysis scope root shape is invalid")
            entity_id = root.get("entity_id")
            if not isinstance(entity_id, str) or entity_id in root_ids:
                raise StoreError("analysis scope root identity is invalid")
            root_ids.add(entity_id)
            row = self.connection.execute(
                "SELECT kind FROM entity WHERE entity_id=?", (entity_id,)
            ).fetchone()
            if row is None or root.get("kind") != row[0] or row[0] not in {instance_kind, module_kind}:
                raise StoreError("analysis scope root does not match a supported entity")
            if not isinstance(root.get("role"), str) or not root["role"].strip():
                raise StoreError("analysis scope root role is invalid")
            if root.get("resolution_state") not in ANALYSIS_SCOPE_ROOT_STATES:
                raise StoreError("analysis scope root resolution is invalid")
        if mode == "ACTUAL_INSTANCE_ROOT" and (
            len(roots) != 1 or roots[0]["kind"] != instance_kind
        ):
            raise StoreError("actual-instance scope requires exactly one instance root")
        if mode == "MODULE_DEFINITION_ROOT" and (
            len(roots) != 1 or roots[0]["kind"] != module_kind
        ):
            raise StoreError("module-definition scope requires exactly one module root")
        if mode == "MULTI_ROOT" and len(roots) < 2:
            raise StoreError("multi-root scope requires at least two roots")
        if mode == "SOURCE_ONLY" and roots:
            raise StoreError("source-only scope cannot declare roots")

        boundary = scope.get("boundary_policy")
        if not isinstance(boundary, dict) or set(boundary) != {
            "kind", "max_depth", "knowledge_source_paths"
        }:
            raise StoreError("analysis scope boundary policy is invalid")
        if boundary.get("kind") not in ANALYSIS_SCOPE_BOUNDARIES:
            raise StoreError("analysis scope boundary kind is invalid")
        if mode == "SOURCE_ONLY" and boundary["kind"] != "SOURCE_PATH_SET":
            raise StoreError("source-only scope requires a source-path boundary")
        if mode != "SOURCE_ONLY" and boundary["kind"] != "ROOTED_CONTAINS_CLOSURE":
            raise StoreError("rooted scope requires a rooted-closure boundary")
        max_depth = boundary.get("max_depth")
        if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
            raise StoreError("analysis scope max depth is invalid")
        source_paths = boundary.get("knowledge_source_paths")
        if (
            not isinstance(source_paths, list)
            or source_paths != sorted(set(source_paths))
            or any(not self._scope_relative_path(path) for path in source_paths)
        ):
            raise StoreError("analysis scope source paths are invalid")
        source_kind = self.contract_id("entities", "SOURCE_FILE")
        registered_sources = {
            row[0]
            for row in self.connection.execute(
                "SELECT qualified_name FROM entity WHERE kind=?", (source_kind,)
            )
        }
        if not set(source_paths) <= registered_sources:
            raise StoreError("analysis scope source path is not registered")

        dependency = scope.get("dependency_policy")
        if dependency != {"kind": ANALYSIS_SCOPE_DEPENDENCY}:
            raise StoreError("analysis scope dependency policy is invalid")
        applicability = scope.get("applicability")
        if (
            not isinstance(applicability, dict)
            or set(applicability) != {"schema", "constraints"}
            or applicability.get("schema") != APPLICABILITY_SCHEMA
            or not isinstance(applicability.get("constraints"), dict)
        ):
            raise StoreError("analysis scope applicability is invalid")
        for dimension, values in applicability["constraints"].items():
            if dimension != "instance_id" and not APPLICABILITY_PARAMETER.fullmatch(dimension):
                raise StoreError("analysis scope applicability dimension is invalid")
            if (
                not isinstance(values, list)
                or not values
                or any(
                    not isinstance(value, str)
                    or not value
                    or "\n" in value
                    or len(value) > APPLICABILITY_VALUE_LIMIT
                    for value in values
                )
                or values != sorted(set(values))
            ):
                raise StoreError("analysis scope applicability values are invalid")
            if dimension == "instance_id":
                rows = {
                    row[0]: row[1]
                    for row in self.connection.execute(
                        "SELECT entity_id, kind FROM entity WHERE entity_id IN ({})".format(
                            ",".join("?" for _ in values)
                        ),
                        values,
                    )
                }
                if len(rows) != len(values) or any(
                    rows.get(value) != instance_kind for value in values
                ):
                    raise StoreError("analysis scope instance applicability is invalid")
        for field in ("profile", "vocabulary"):
            value = scope.get(field)
            if (
                not isinstance(value, dict)
                or set(value) != {"id", "version"}
                or not all(isinstance(value[key], str) and value[key].strip() for key in value)
            ):
                raise StoreError(f"analysis scope {field} identity is invalid")
        semantic_row = self.connection.execute(
            "SELECT value_json FROM model_metadata WHERE key='semantic_publication_contract'"
        ).fetchone()
        if semantic_row is None:
            if scope["vocabulary"]["version"] != self.metadata("contract_version"):
                raise StoreError("analysis scope vocabulary version does not match candidate")
        else:
            semantic = json.loads(semantic_row[0])
            if (
                self.schema_version != "v0.2"
                or semantic.get("purpose") not in {
                    "SYNTHETIC_ONLY", "REAL_MODULE_Q2", "REAL_Q2_FIFO_STAGE1_ONLY", "REAL_Q2_FIFO",
                }
                or semantic.get("sha256") != {
                    "SYNTHETIC_ONLY": SYNTHETIC_PUBLICATION_SHA256,
                    "REAL_MODULE_Q2": MODULE_Q2_PUBLICATION_SHA256,
                    "REAL_Q2_FIFO_STAGE1_ONLY": Q2_FIFO_PUBLICATION_SHA256,
                    "REAL_Q2_FIFO": Q2_FIFO_V2_PUBLICATION_SHA256,
                }[semantic["purpose"]]
                or scope["profile"] != semantic.get("profile")
                or scope["vocabulary"] != semantic.get("vocabulary")
            ):
                raise StoreError("analysis scope semantic identity does not match candidate")
        if not isinstance(scope.get("producer_capability"), dict) or not scope["producer_capability"]:
            raise StoreError("analysis scope producer capability is invalid")
        if not isinstance(scope.get("completeness"), dict) or not scope["completeness"]:
            raise StoreError("analysis scope completeness is invalid")
        limitations = scope.get("limitations")
        if (
            not isinstance(limitations, list)
            or len(limitations) != len(set(limitations))
            or any(not isinstance(item, str) or not item.strip() for item in limitations)
        ):
            raise StoreError("analysis scope limitations are invalid")

        identity_payload = {key: value for key, value in scope.items() if key != "scope_id"}
        if scope.get("scope_id") != _identity("scope", identity_payload):
            raise StoreError("analysis scope identity digest is invalid")
        return json.loads(_canonical(scope))

    def register_analysis_scope(
        self,
        *,
        scope_mode: str,
        roots: list[dict[str, Any]],
        boundary_policy: dict[str, Any],
        dependency_policy: dict[str, Any],
        applicability: dict[str, Any],
        profile: dict[str, Any],
        vocabulary: dict[str, Any],
        producer_capability: dict[str, Any],
        completeness: dict[str, Any],
        limitations: list[str],
    ) -> dict[str, Any]:
        """Create and append the immutable Analysis Scope metadata exactly once."""
        normalized_boundary = json.loads(_canonical(boundary_policy))
        if isinstance(normalized_boundary.get("knowledge_source_paths"), list) and all(
            isinstance(path, str) for path in normalized_boundary["knowledge_source_paths"]
        ):
            normalized_boundary["knowledge_source_paths"] = sorted(set(
                normalized_boundary["knowledge_source_paths"]
            ))
        normalized_applicability = json.loads(_canonical(applicability))
        constraints = normalized_applicability.get("constraints")
        if isinstance(constraints, dict):
            for dimension, values in list(constraints.items()):
                if isinstance(values, list) and all(isinstance(value, str) for value in values):
                    constraints[dimension] = sorted(set(values))
        normalized_roots = sorted(
            json.loads(_canonical(roots)),
            key=lambda root: (
                root.get("entity_id", ""), root.get("kind", ""),
                root.get("role", ""), root.get("resolution_state", ""),
            ),
        )
        scope = {
            "schema": ANALYSIS_SCOPE_SCHEMA,
            "build_identity": {
                key: self.metadata(key)
                for key in (
                    "build_sha256", "build_spec_sha256", "source_closure_sha256", "source_revision"
                )
            },
            "scope_mode": scope_mode,
            "roots": normalized_roots,
            "boundary_policy": normalized_boundary,
            "dependency_policy": json.loads(_canonical(dependency_policy)),
            "applicability": normalized_applicability,
            "profile": json.loads(_canonical(profile)),
            "vocabulary": json.loads(_canonical(vocabulary)),
            "producer_capability": json.loads(_canonical(producer_capability)),
            "completeness": json.loads(_canonical(completeness)),
            "limitations": (
                sorted(set(limitations))
                if isinstance(limitations, list)
                and all(isinstance(item, str) for item in limitations)
                else limitations
            ),
        }
        scope["scope_id"] = _identity("scope", scope)
        validated = self.validate_analysis_scope(scope)
        try:
            self.add_metadata_once("analysis_scope", validated)
        except sqlite3.IntegrityError as error:
            raise StoreError("candidate analysis scope already exists") from error
        return validated

    def analysis_scope(self) -> dict[str, Any]:
        """Return a validated Analysis Scope envelope from this candidate."""
        return self.validate_analysis_scope(self.metadata("analysis_scope"))

    def _publication_members(self, scope: dict[str, Any]) -> set[str]:
        roots = [root["entity_id"] for root in scope["roots"]]
        members = set(roots)
        frontier = roots
        for _ in range(scope["boundary_policy"]["max_depth"]):
            if not frontier:
                break
            rows = self.connection.execute(
                "SELECT object_id FROM fact WHERE predicate=? AND subject_id IN ({})".format(
                    ",".join("?" for _ in frontier)
                ), [self.contract_id("facts", "CONTAINS"), *frontier],
            )
            frontier = [row[0] for row in rows if row[0] not in members]
            members.update(frontier)
        # A definition is visible only through an evidenced instance in this scope.
        instances = [entity for entity in members if self._entity_kind(entity) == self.contract_id("entities", "INSTANCE")]
        if instances:
            rows = self.connection.execute(
                "SELECT f.object_id FROM fact f JOIN fact_evidence e ON e.fact_id=f.fact_id "
                "WHERE f.predicate=? AND f.subject_id IN ({})".format(",".join("?" for _ in instances)),
                [self.contract_id("facts", "INSTANCE_OF"), *instances],
            )
            members.update(row[0] for row in rows if self._entity_kind(row[0]) == self.contract_id("entities", "MODULE"))
        return members

    def _publication_module_instances(self, module_id: str, instances: list[str], members: set[str]) -> None:
        if not instances or instances != sorted(set(instances)):
            raise StoreError("MODULE applicability needs canonical instances")
        for instance_id in instances:
            if instance_id not in members or self._entity_kind(instance_id) != self.contract_id("entities", "INSTANCE") or not self.connection.execute(
                "SELECT 1 FROM fact f JOIN fact_evidence e ON e.fact_id=f.fact_id "
                "WHERE f.predicate=? AND f.subject_id=? AND f.object_id=? LIMIT 1",
                (self.contract_id("facts", "INSTANCE_OF"), instance_id, module_id),
            ).fetchone():
                raise StoreError("MODULE applicability lacks evidenced INSTANCE_OF")

    def _publication_interface_ports(
        self, fields: dict[str, Any], instances: list[str], members: set[str],
    ) -> list[dict[str, str]]:
        """Bind reviewed interface refs to evidenced PORTs of one exact actual."""
        refs = fields.get("signal_refs")
        if (len(instances) != 1 or not isinstance(refs, list) or not refs
            or refs != sorted(set(refs))):
            raise StoreError("interface needs canonical PORT refs for one actual")
        actual = self.connection.execute(
            "SELECT qualified_name FROM entity WHERE entity_id=? AND kind=?",
            (instances[0], self.contract_id("entities", "INSTANCE")),
        ).fetchone()
        if actual is None:
            raise StoreError("interface actual is absent")
        result = []
        for ref in refs:
            port = self.connection.execute(
                "SELECT kind,qualified_name,attrs_json FROM entity WHERE entity_id=?", (ref,),
            ).fetchone()
            if (ref not in members or port is None
                or port["kind"] != self.contract_id("entities", "PORT")
                or not port["qualified_name"].startswith(actual[0] + ".")
                or not self.connection.execute(
                    "SELECT 1 FROM fact f JOIN fact_evidence e ON e.fact_id=f.fact_id "
                    "WHERE f.predicate=? AND f.subject_id=? AND f.object_id=? LIMIT 1",
                    (self.contract_id("facts", "CONTAINS"), instances[0], ref),
                ).fetchone()):
                raise StoreError("interface signal is not an evidenced PORT of the applicable actual")
            direction = json.loads(port["attrs_json"]).get("direction")
            if direction not in {"In", "Out", "InOut"}:
                raise StoreError("interface PORT direction is invalid")
            result.append({"entity_id": ref, "name": port["qualified_name"].rsplit(".", 1)[-1],
                           "direction": direction})
        return sorted(result, key=lambda item: (item["name"], item["entity_id"]))

    def _publication_review_context(
        self, scope: dict[str, Any], contract: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        candidate = {
            "candidate_id": self.metadata("candidate_id"),
            "schema_identity": "v0.2", "contract_version": contract["vocabulary"]["version"],
            "build_sha256": self.metadata("build_sha256"),
            "source_revision": self.metadata("source_revision"),
            "source_closure_sha256": self.metadata("source_closure_sha256"),
            "scope_id": scope["scope_id"],
            "scope_envelope_sha256": hashlib.sha256(_canonical(scope).encode()).hexdigest(),
        }
        publication = {
            "profile": contract["profile"], "vocabulary": contract["vocabulary"],
            "canonical_sha256": hashlib.sha256(_canonical(contract).encode()).hexdigest(),
        }
        return candidate, publication

    @staticmethod
    def _publication_applicability(
        parent: dict[str, list[str]], child: dict[str, list[str]],
        allowed: dict[str, Any], members: set[str],
    ) -> dict[str, list[str]]:
        if not isinstance(child, dict) or set(child) - set(allowed):
            raise StoreError("publication applicability dimension is invalid")
        merged = dict(parent)
        for dimension, values in child.items():
            if (
                not isinstance(values, list) or not values
                or any(not isinstance(value, str) for value in values)
                or values != sorted(set(values))
                or any(not value for value in values)
            ):
                raise StoreError("publication applicability values are invalid")
            spec = allowed[dimension]
            if spec["kind"] == "enum" and not set(values) <= set(spec["values"]):
                raise StoreError("publication applicability enum is invalid")
            if spec["kind"] == "entity_ref" and not set(values) <= members:
                raise StoreError("publication applicability entity is outside scope")
            if dimension in parent and not set(values) <= set(parent[dimension]):
                raise StoreError("publication applicability widens its parent")
            merged[dimension] = values
        return merged

    def _publication_source(
        self, source: dict[str, Any], source_root: Path, scope: dict[str, Any],
        *, allow_missing: bool = False,
    ) -> str:
        if set(source) not in (
            {"evidence_id", "path", "span", "content_sha256", "current"},
            {"evidence_id", "path", "span", "content_sha256", "file_sha256", "current"},
        ) or source["current"] is not True:
            raise StoreError("publication Source binding is invalid")
        path = source["path"]
        if not self._scope_relative_path(path) or path not in scope["boundary_policy"]["knowledge_source_paths"]:
            raise StoreError("publication Source is outside frozen scope")
        target = source_root / path
        if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(source_root.resolve()):
            raise StoreError("publication Source path is unsafe")
        match = re.fullmatch(r"L([1-9][0-9]*)(?:-L([1-9][0-9]*))?", source["span"])
        if match is None:
            raise StoreError("publication Source span is invalid")
        first = int(match[1]); last = int(match[2] or match[1])
        raw = target.read_bytes()
        file_digest = hashlib.sha256(raw).hexdigest()
        if "file_sha256" in source and source["file_sha256"] != file_digest:
            raise StoreError("publication Source file hash is stale")
        if "file_sha256" in source:
            frozen = self.connection.execute(
                "SELECT attrs_json FROM entity WHERE kind=? AND qualified_name=?",
                (self.contract_id("entities", "SOURCE_FILE"), path),
            ).fetchone()
            if frozen is None or json.loads(frozen[0]).get("sha256") != file_digest:
                raise StoreError("publication Source differs from frozen Stage1 file")
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise StoreError("publication Source is not UTF-8") from error
        if last < first or last > len(lines):
            raise StoreError("publication Source span is stale")
        digest = hashlib.sha256("\n".join(lines[first - 1:last]).encode()).hexdigest()
        if digest != source["content_sha256"]:
            raise StoreError("publication Source hash is stale")
        attrs = {"sha256": digest}
        if "file_sha256" in source:
            attrs = {"sha256": digest, "file_sha256": file_digest,
                     "hash_kind": "utf8-line-span-v1"}
        expected = ("source", path, first, last, _canonical(attrs))
        row = self.connection.execute(
            "SELECT kind,locator,start_line,end_line,attrs_json FROM evidence WHERE evidence_id=?",
            (source["evidence_id"],),
        ).fetchone()
        if row is None:
            if not allow_missing or source["evidence_id"] != _identity(
                "ev", "source", path, first, last, expected[-1]
            ):
                raise StoreError("publication Source Evidence does not match current bytes")
        elif tuple(row) != expected:
            raise StoreError("publication Source Evidence does not match current bytes")
        return source["evidence_id"]

    def _insert_publication_source(
        self, source: dict[str, Any], source_root: Path, scope: dict[str, Any],
    ) -> None:
        """Insert only a reviewed range under the caller's publication write lock."""
        self._publication_source(source, source_root, scope, allow_missing=True)
        if self.connection.execute(
            "SELECT 1 FROM evidence WHERE evidence_id=?", (source["evidence_id"],)
        ).fetchone() is None:
            match = re.fullmatch(r"L([1-9][0-9]*)(?:-L([1-9][0-9]*))?", source["span"])
            first, last = int(match[1]), int(match[2] or match[1])
            attrs = {"sha256": source["content_sha256"]}
            if "file_sha256" in source:
                attrs.update(file_sha256=source["file_sha256"], hash_kind="utf8-line-span-v1")
            self.connection.execute(
                "INSERT INTO evidence VALUES (?,?,?,?,?,?)",
                (source["evidence_id"], "source", source["path"], first, last, _canonical(attrs)),
            )
        self._publication_source(source, source_root, scope)

    def _assert_current_coverage_members_published(self) -> None:
        """Guard the Store invariant before a publication transaction can commit."""
        if self.connection.execute(
            "SELECT 1 FROM obligation_coverage c "
            "JOIN obligation_coverage_claim l ON l.coverage_id=c.coverage_id "
            "JOIN ai_knowledge a ON a.ai_id=l.ai_id "
            "WHERE c.validity='CURRENT' AND a.lifecycle!='PUBLISHED' LIMIT 1"
        ).fetchone():
            raise StoreError("publication CURRENT coverage references a non-published claim")

    def _verify_real_approval(
        self, *, review_root: Path, approval_file: str | None,
        approved_sha256: str | None, review: dict[str, Any], digest: str,
        scope: dict[str, Any], contract: dict[str, Any],
    ) -> dict[str, Any]:
        """Bind a complete I, independent R and Human A to one external H(A)."""
        if (not isinstance(approval_file, str) or not isinstance(approved_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", approved_sha256)):
            raise StoreError("real publication needs a Controller-pinned approval digest")
        combo = contract["status"] == "CONTROLLED_REAL_Q2_FIFO_V2"
        profile_sha = Q2_FIFO_V2_PUBLICATION_SHA256 if combo else MODULE_Q2_PUBLICATION_SHA256
        goals = Q2_FIFO_GOALS if combo else MODULE_Q2_GOALS
        suffix = "q2-fifo" if combo else "module-q2"
        extra = {"component_evidence", "expected_members"} if combo else set()
        approval, approval_sha = _bound_json(review_root, approval_file)
        if approval_sha != approved_sha256 or set(approval) != {
            "schema", "decision", "input_path", "input_sha256", "review_path",
            "review_sha256", "profile_sha256", "candidate", "intake_sha256",
            "batch_sha256", "goal_ids",
        } | extra or approval["schema"] != f"rtl-analysis-agent/{suffix}-approval-v1" or approval["decision"] != "APPROVED":
            raise StoreError("real publication approval anchor is invalid")
        whole, whole_sha = _bound_json(review_root, approval["input_path"])
        independent, independent_sha = _bound_json(review_root, approval["review_path"])
        if (whole_sha != approval["input_sha256"] or independent_sha != approval["review_sha256"]
            or set(whole) != {"schema", "candidate", "contract", "profile_sha256", "intake", "batches", "dispositions"} | extra
            or whole["schema"] != f"rtl-analysis-agent/{suffix}-complete-input-v1"
            or whole["profile_sha256"] != profile_sha
            or approval["profile_sha256"] != profile_sha
            or whole["candidate"] != approval["candidate"]
            or whole["candidate"] != self._publication_review_context(scope, contract)[0]
            or whole["contract"] != self._publication_review_context(scope, contract)[1]):
            raise StoreError("real publication complete input identity is invalid")
        batches = whole["batches"]
        if not isinstance(batches, list) or not batches:
            raise StoreError("real publication has no reviewed batches")
        batch_sha = [hashlib.sha256(_canonical(batch).encode()).hexdigest() for batch in batches]
        if batch_sha != sorted(set(batch_sha)) or digest not in batch_sha or approval["batch_sha256"] != batch_sha:
            raise StoreError("real publication batch set is incomplete or changed")
        if any(batch.get("candidate") != whole["candidate"] or batch.get("contract") != whole["contract"]
               or batch.get("schema") != PUBLICATION_REVIEW_SCHEMA for batch in batches):
            raise StoreError("real publication batch identity is invalid")
        batch_claim_ids = [claim["ai_id"] for batch in batches for claim in batch["claims"]]
        all_claims = set(batch_claim_ids)
        if len(all_claims) != len(batch_claim_ids):
            raise StoreError("real publication repeats a claim across batches")
        expected_members = {
            "claim_ids": sorted(all_claims),
            "coverage_ids": sorted(_identity("cov", scope["scope_id"], coverage)
                                   for batch in batches for coverage in batch["coverage"]),
            "behavior_ids": sorted(_identity("behavior", scope["scope_id"], batch["behavior"]["behavior_type"],
                                             batch["behavior"]["behavior_key"])
                                   for batch in batches if batch["behavior"] is not None),
        }
        if combo:
            components = whole["component_evidence"]
            if (not isinstance(components, dict) or set(components) != {"old_q2_input", "old_q2_review", "old_q2_approval", "q2_equivalence", "q4_review_input"}
                or any(not isinstance(value, dict) or set(value) != {"path", "sha256"}
                       or _bound_json(review_root, value["path"])[1] != value["sha256"]
                       for value in components.values())
                or approval["component_evidence"] != components
                or whole["expected_members"] != expected_members
                or approval["expected_members"] != expected_members):
                raise StoreError("combination component evidence or complete members differ")
            if any(components[key]["sha256"] != value for key, value in IBEX_OLD_Q2_COMPONENT_SHA256.items()):
                raise StoreError("combination old Q2 component is not the protected approved object")
            old_input, _ = _bound_json(review_root, components["old_q2_input"]["path"])
            old_review, _ = _bound_json(review_root, components["old_q2_review"]["path"])
            old_approval, _ = _bound_json(review_root, components["old_q2_approval"]["path"])
            if (old_approval.get("decision") != "APPROVED"
                or old_approval.get("input_sha256") != components["old_q2_input"]["sha256"]
                or old_approval.get("review_sha256") != components["old_q2_review"]["sha256"]
                or old_review.get("decision") != "VERIFIED"
                or old_review.get("input_sha256") != components["old_q2_input"]["sha256"]
                or old_input.get("profile_sha256") != MODULE_Q2_PUBLICATION_SHA256):
                raise StoreError("combination old Q2 review chain is invalid")
            old_batches = []
            for original in old_input["batches"]:
                batch = json.loads(_canonical(original))
                batch["candidate"] = whole["candidate"]
                batch["contract"] = whole["contract"]
                if batch["behavior"] is not None:
                    batch["behavior"]["scope_id"] = scope["scope_id"]
                for coverage in batch["coverage"]:
                    coverage["profile"] = contract["profile"]
                old_batches.append(batch)
            actual_q2 = [batch for batch in batches if any(
                claim["structured_schema_id"].startswith("module-q2.") for claim in batch["claims"])]
            if (sorted(map(_canonical, actual_q2)) != sorted(map(_canonical, old_batches))
                or len(actual_q2) != len(old_batches)
                or any(any(claim["structured_schema_id"].startswith("fifo.") for claim in batch["claims"])
                       for batch in actual_q2)):
                raise StoreError("combination Q2 payload, support or limitation differs from approved Q2")
            q4_input, _ = _bound_json(review_root, components["q4_review_input"]["path"])
            q4_subject = q4_input.get("subject", {})
            fifo_obligations = {name for name in contract["obligations"] if name.startswith("fifo.")}
            fifo_claims = [claim for batch in batches for claim in batch["claims"]
                           if claim["structured_schema_id"].startswith("fifo.")]
            fifo_coverage = [coverage for batch in batches for coverage in batch["coverage"]
                             if coverage["obligation_id"].startswith("fifo.")]
            if (q4_input.get("candidate_id") != whole["candidate"]["candidate_id"]
                or q4_input.get("scope_id") != scope["scope_id"]
                or q4_input.get("profile") != contract["profile"]
                or q4_input.get("profile_sha256") != profile_sha
                or q4_input.get("build_sha256") != scope["build_identity"]["build_sha256"]
                or q4_input.get("source_revision") != scope["build_identity"]["source_revision"]
                or not isinstance(q4_subject, dict)
                or len(fifo_claims) != len(fifo_obligations)
                or len(fifo_coverage) != len(fifo_obligations)
                or {item["obligation_id"] for item in fifo_coverage} != fifo_obligations
                or any(claim["target_subject_id"] != q4_subject.get("module_id")
                       or claim["applicability"].get("instance_id") != [q4_subject.get("actual_id")]
                       or claim["structured_fields"].get("config_boundary") != q4_subject.get("configuration")
                       for claim in fifo_claims)
                or any(coverage["target_subject_id"] != q4_subject.get("module_id")
                       or coverage["applicability"].get("instance_id") != [q4_subject.get("actual_id")]
                       or coverage["closure_status"] != "PARTIAL"
                       for coverage in fifo_coverage)):
                raise StoreError("combination FIFO actual, configuration or five obligations differ")
        # Check the complete approved package against the existing CURRENT index
        # before the first batch can commit. This same approval check runs again
        # under BEGIN IMMEDIATE, so the package cannot change between checks.
        scope_members = self._publication_members(scope)
        allowed = contract["applicability"]["dimensions"]
        slices: set[tuple[str, ...]] = set()
        for batch in batches:
            behavior = batch["behavior"]
            behavior_id = (_identity("behavior", scope["scope_id"],
                                     behavior["behavior_type"], behavior["behavior_key"])
                           if behavior is not None else "")
            parent = self._publication_applicability(
                scope["applicability"]["constraints"],
                behavior["applicability"] if behavior is not None else {},
                allowed, scope_members,
            )
            claims = {claim["ai_id"]: claim for claim in batch["claims"]}
            for coverage in batch["coverage"]:
                if (coverage["profile"] != contract["profile"]
                    or coverage["vocabulary"] != contract["vocabulary"]):
                    raise StoreError("real publication coverage contract is invalid")
                effective = None
                for ai_id in coverage["claim_members"]:
                    claim = claims.get(ai_id)
                    if claim is None:
                        raise StoreError("real publication coverage member is absent")
                    claim_slice = self._publication_applicability(
                        parent, claim["applicability"], allowed, scope_members,
                    )
                    member_slice = self._publication_applicability(
                        claim_slice, coverage["applicability"], allowed, scope_members,
                    )
                    if effective is not None and effective != member_slice:
                        raise StoreError("real publication coverage has no common applicability slice")
                    effective = member_slice
                if effective is None:
                    raise StoreError("real publication coverage has no claims")
                slice_key = (
                    scope["scope_id"], coverage["target_kind"],
                    coverage["target_subject_id"] or "", behavior_id,
                    coverage["obligation_id"], contract["profile"]["id"],
                    contract["profile"]["version"], contract["vocabulary"]["id"],
                    contract["vocabulary"]["version"], _canonical(coverage["applicability"]),
                )
                if slice_key in slices:
                    raise StoreError("real publication has duplicate CURRENT coverage slice")
                slices.add(slice_key)
        all_sources = {_canonical(source) for batch in batches for claim in batch["claims"]
                       for source in claim["direct_source_evidence"]}
        if (not isinstance(whole["intake"], list)
            or {_canonical(source) for source in whole["intake"]} != all_sources
            or len(whole["intake"]) != len(all_sources)
            or any("file_sha256" not in source for source in whole["intake"])
            or approval["intake_sha256"] != hashlib.sha256(_canonical(whole["intake"]).encode()).hexdigest()):
            raise StoreError("real publication Source intake differs from reviewed batches")
        dispositions = whole["dispositions"]
        if (not isinstance(dispositions, list) or [item.get("goal_id") for item in dispositions] != list(goals)
            or approval["goal_ids"] != list(goals)):
            raise StoreError("real publication goal disposition is incomplete")
        mapped_claims: set[str] = set()
        for item in dispositions:
            valid_status = {"COVERED", "MERGED", "PAGE_ONLY", "DEFERRED"} if combo else {"COVERED", "DEFERRED"}
            if set(item) != {"goal_id", "status", "claim_ids", "reason"} or item["status"] not in valid_status:
                raise StoreError("real publication goal disposition is invalid")
            ids = item["claim_ids"]
            if (not isinstance(ids, list) or ids != sorted(set(ids)) or not set(ids) <= all_claims
                or (item["status"] in {"COVERED", "MERGED"}) != bool(ids)
                or (item["status"] in {"PAGE_ONLY", "DEFERRED"} and bool(ids))
                or (combo and item["goal_id"] in MODULE_Q2_GOALS and item["status"] != "COVERED")
                or not isinstance(item["reason"], str) or not item["reason"].strip()):
                raise StoreError("real publication goal mapping is invalid")
            mapped_claims.update(ids)
        if mapped_claims != all_claims:
            raise StoreError("real publication has claims outside the 12-goal mapping")
        if (set(independent) != {"schema", "decision", "input_sha256", "batch_sha256", "reviewer", "generator", "findings"}
            or independent["schema"] != f"rtl-analysis-agent/{suffix}-independent-review-v1"
            or independent["decision"] != "VERIFIED"
            or independent["input_sha256"] != whole_sha or independent["batch_sha256"] != batch_sha
            or not independent["reviewer"] or not independent["generator"]
            or independent["reviewer"] == independent["generator"]
            or not isinstance(independent["findings"], list)
            or review["locator"] != approval["review_path"]
            or review["artifact_sha256"] != independent_sha
            or review["reviewer"] != independent["reviewer"]
            or review["generator"] != independent["generator"]):
            raise StoreError("real publication independent review is invalid")
        return {
            "input_sha256": whole_sha, "review_sha256": independent_sha,
            "approval_sha256": approval_sha, "profile_sha256": profile_sha,
            "candidate": whole["candidate"], "batch_sha256": batch_sha,
            "goal_dispositions": dispositions,
            "expected_claim_ids": expected_members["claim_ids"],
            "expected_coverage_ids": expected_members["coverage_ids"],
            "expected_behavior_ids": expected_members["behavior_ids"],
            **({"component_evidence": whole["component_evidence"]} if combo else {}),
        }

    def publish_reviewed_semantic_overlay(
        self, overlay: dict[str, Any], *, source_root: Path,
        review_root: Path, publication_contract: Path,
        approval_file: str | None = None, approved_sha256: str | None = None,
        validate_only: bool = False,
    ) -> dict[str, Any]:
        """Validate, then publish one reviewed v0.2 batch through the sole writer."""
        contract, contract_digest, purpose = _load_publication_contract(publication_contract)
        if purpose == "REAL_Q2_FIFO_STAGE1_ONLY":
            raise StoreError("combination semantic publication is not implemented")
        if self.schema_version != "v0.2" or self._discarded:
            raise StoreError("semantic publication requires an active v0.2 Store")
        if self.metadata("semantic_publication_contract") != _publication_identity(contract, contract_digest, purpose):
            raise StoreError("semantic publication contract does not match candidate")
        modes = (
            self.connection.execute("PRAGMA journal_mode").fetchone()[0].lower(),
            self.connection.execute("PRAGMA synchronous").fetchone()[0],
            self.connection.execute("PRAGMA foreign_keys").fetchone()[0],
        )
        if not validate_only and modes != ("delete", 3, 1):
            raise StoreError("semantic publication requires durable connection")
        if not isinstance(overlay, dict) or set(overlay) != {
            "schema", "contract", "candidate", "behavior", "claims", "coverage", "review"
        }:
            raise StoreError("publication overlay shape is invalid")
        review = overlay["review"]
        review_input = {key: value for key, value in overlay.items() if key != "review"}
        if review_input["schema"] != PUBLICATION_REVIEW_SCHEMA:
            raise StoreError("publication review-input schema is invalid")
        digest = hashlib.sha256(_canonical(review_input).encode()).hexdigest()
        if not validate_only and (
            not isinstance(review, dict) or review.get("status") != "VERIFIED"
            or review.get("input_sha256") != digest
            or not isinstance(review.get("artifact_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", review["artifact_sha256"])
            or review.get("reviewer") == review.get("generator")
            or not review.get("reviewer") or not review.get("generator")
        ):
            raise StoreError("publication review binding is invalid")
        scope = self.analysis_scope()
        if (
            scope["scope_mode"] not in contract["scope"]["modes"]
            or scope["boundary_policy"]["kind"] not in contract["scope"]["boundary_policies"]
            or scope["dependency_policy"]["kind"] not in contract["scope"]["dependency_policies"]
        ):
            raise StoreError("publication scope policy is not in pinned contract")
        expected_candidate, expected_contract = self._publication_review_context(scope, contract)
        if review_input["candidate"] != expected_candidate or review_input["contract"] != expected_contract:
            raise StoreError("publication candidate or contract binding is stale")
        members = self._publication_members(scope)
        self._publication_applicability({}, scope["applicability"]["constraints"], contract["applicability"]["dimensions"], members)
        behavior = review_input["behavior"]
        subject_mode = behavior is None
        if not subject_mode and (not isinstance(behavior, dict) or set(behavior) != {
            "behavior_type", "behavior_key", "scope_id", "ownership_mode", "owner_subject_id",
            "applicability", "participants", "related_subject_ids",
        }):
            raise StoreError("publication Behavior shape is invalid")
        allowed = contract["applicability"]["dimensions"]
        behavior_id = None
        subject_id = None
        key = None
        behavior_type = None
        if subject_mode:
            effective_behavior = scope["applicability"]["constraints"]
        else:
            behavior_type = behavior.get("behavior_type")
            spec = contract["behavior_types"].get(behavior_type)
            if spec is None or behavior.get("scope_id") != scope["scope_id"]:
                raise StoreError("publication Behavior type or scope is invalid")
            owner = behavior.get("owner_subject_id")
            ownership = behavior.get("ownership_mode")
            if ownership not in spec["ownership_modes"] or (ownership == "SCOPE") != (owner is None):
                raise StoreError("publication Behavior ownership is invalid")
            if owner is not None and (owner not in members or self._entity_kind(owner) not in spec["owner_kinds"]):
                raise StoreError("publication Behavior owner is outside scope or has invalid kind")
            key = behavior.get("behavior_key")
            if not isinstance(key, str) or not key.strip():
                raise StoreError("publication Behavior key is invalid")
            effective_behavior = self._publication_applicability(
                scope["applicability"]["constraints"], behavior.get("applicability"), allowed, members,
            )
            if not isinstance(behavior.get("participants"), list) or not isinstance(behavior.get("related_subject_ids"), list):
                raise StoreError("publication relation endpoints are invalid")
            if any(not isinstance(endpoint, str) for endpoint in behavior["participants"] + behavior["related_subject_ids"]):
                raise StoreError("publication relation endpoint is invalid")
            for endpoint in behavior["participants"] + behavior["related_subject_ids"]:
                if endpoint not in members:
                    raise StoreError("publication relation endpoint is outside scope")
            if ownership == "SCOPE" and not behavior["participants"]:
                raise StoreError("scope-owned Behavior requires participants")
            behavior_id = _identity("behavior", scope["scope_id"], behavior_type, key)
        claims = review_input["claims"]
        coverages = review_input["coverage"]
        if not isinstance(claims, list) or not claims or not isinstance(coverages, list) or not coverages:
            raise StoreError("publication claims and coverage must be nonempty")
        prepared_claims = {}
        claim_keys: set[str] = set()
        for claim in claims:
            expected_keys = {
                "ai_id", "target_kind", "behavior_key", "claim_key", "knowledge_type", "text",
                "structured_schema_id", "structured_fields", "applicability", "limitations",
                "fact_support", "direct_source_evidence",
            } | ({"target_subject_id"} if subject_mode else set())
            if not isinstance(claim, dict) or set(claim) != expected_keys:
                raise StoreError("publication claim shape is invalid")
            shape_id = claim.get("structured_schema_id")
            shape = contract["claim_shapes"].get(shape_id)
            target = "SUBJECT:rtl.sv/1:module" if subject_mode else f"BEHAVIOR:{behavior_type}"
            if shape is None or shape["target"] != target:
                raise StoreError("publication claim shape is invalid")
            if subject_mode:
                candidate_subject = claim.get("target_subject_id")
                if (claim.get("target_kind") != "SUBJECT" or claim.get("behavior_key") is not None
                    or not isinstance(candidate_subject, str) or candidate_subject not in members
                    or self._entity_kind(candidate_subject) != self.contract_id("entities", "MODULE")
                    or (subject_id is not None and candidate_subject != subject_id)):
                    raise StoreError("publication MODULE target is invalid")
                subject_id = candidate_subject
            elif claim.get("target_kind") != "BEHAVIOR" or claim.get("behavior_key") != key:
                raise StoreError("publication claim target is invalid")
            claim_key = claim.get("claim_key")
            if not isinstance(claim_key, str) or not claim_key.strip():
                raise StoreError("publication claim key is invalid")
            if claim_key in claim_keys:
                raise StoreError("publication same batch claim key conflicts")
            claim_keys.add(claim_key)
            if claim.get("knowledge_type") != shape["knowledge_type"]:
                raise StoreError("publication claim vocabulary is invalid")
            fields = claim.get("structured_fields")
            if not isinstance(fields, dict) or set(fields) - set(shape["fields"]):
                raise StoreError("publication structured fields are invalid")
            for name, field_spec in shape["fields"].items():
                if field_spec.get("required") and name not in fields:
                    raise StoreError("publication required field is missing")
                if name not in fields:
                    continue
                value = fields[name]
                if field_spec["kind"] == "scalar" and (not isinstance(value, str) or not value.strip()):
                    raise StoreError("publication scalar field is invalid")
                if field_spec["kind"] == "list" and (
                    not isinstance(value, list)
                    or len(value) < field_spec.get("min_items", 0)
                    or any(not isinstance(item, str) for item in value)
                    or len(value) != len(set(value))
                    or (field_spec["item_kind"] == "entity_ref" and not set(value) <= members)
                ):
                    raise StoreError("publication relation field is invalid")
                if field_spec["kind"] == "object" and not isinstance(value, dict):
                    raise StoreError("publication object field is invalid")
            if not subject_mode and (fields.get("participants", []) != behavior["participants"] or fields.get("related_subject_ids", []) != behavior["related_subject_ids"]):
                raise StoreError("publication relation fields disagree with Behavior")
            effective_claim = self._publication_applicability(effective_behavior, claim.get("applicability"), allowed, members)
            if purpose in {"REAL_MODULE_Q2", "REAL_Q2_FIFO"} and not effective_claim.get("instance_id"):
                raise StoreError("real module-q2 claim requires exact instance applicability")
            if subject_mode:
                instances = effective_claim.get("instance_id", [])
                self._publication_module_instances(subject_id, instances, members)
                if purpose in {"REAL_MODULE_Q2", "REAL_Q2_FIFO"} and shape_id == "module-q2.module-interface@2":
                    self._publication_interface_ports(fields, instances, members)
                basis = fields.get("reuse_basis")
                if len(instances) > 1 and (not isinstance(basis, dict)
                    or set(basis) != {"instance_ids", "equivalence_reason"}
                    or basis["instance_ids"] != instances
                    or not isinstance(basis["equivalence_reason"], str)
                    or not basis["equivalence_reason"].strip()):
                    raise StoreError("cross-instance MODULE reuse needs reviewed basis")
                if basis is not None and (not isinstance(basis, dict)
                    or set(basis) != {"instance_ids", "equivalence_reason"}
                    or len(instances) < 2 or basis["instance_ids"] != instances
                    or not isinstance(basis["equivalence_reason"], str)
                    or not basis["equivalence_reason"].strip()):
                    raise StoreError("MODULE reuse basis disagrees with applicability")
            sources = claim.get("direct_source_evidence")
            if not isinstance(sources, list) or not sources:
                raise StoreError("publication direct Source is missing")
            source_ids = [self._publication_source(source, source_root, scope, allow_missing=True) for source in sources]
            if source_ids != sorted(set(source_ids)):
                raise StoreError("publication Source Evidence set is not canonical")
            fact_links = claim.get("fact_support")
            if not isinstance(fact_links, list):
                raise StoreError("publication Fact support is invalid")
            fact_ids = set()
            for link in fact_links:
                if set(link) != {"fact_id", "quality", "fields_proven"} or link["quality"] not in {"EXACT", "PARTIAL", "SCOPE_ONLY"}:
                    raise StoreError("publication Fact quality is invalid")
                fact_id = link["fact_id"]
                proof = link["fields_proven"]
                if fact_id in fact_ids or not isinstance(proof, list) or any(not isinstance(item, str) for item in proof) or proof != sorted(set(proof)) or not set(proof) <= set(fields):
                    raise StoreError("publication Fact field proof is invalid")
                fact_ids.add(fact_id)
                if (link["quality"] == "SCOPE_ONLY") != (not proof):
                    raise StoreError("publication Fact quality contradicts field proof")
                row = self.connection.execute("SELECT subject_id,object_id FROM fact WHERE fact_id=?", (fact_id,)).fetchone()
                if row is None or row["subject_id"] not in members or row["object_id"] not in members:
                    raise StoreError("publication Fact is absent or outside scope")
                if not self.connection.execute("SELECT 1 FROM fact_evidence WHERE fact_id=? LIMIT 1", (fact_id,)).fetchone():
                    raise StoreError("publication Fact has no Evidence")
            if [link["fact_id"] for link in fact_links] != sorted(fact_ids):
                raise StoreError("publication Fact link set is not canonical")
            source_only = bool(_publication_missing_fact_fields(shape, fact_links))
            gap = claim.get("limitations")
            if source_only:
                if not shape["source_only_publication"] or not isinstance(gap, list) or len(gap) != 1:
                    raise StoreError("publication content Fact or permitted Gap is missing")
                gap_value = gap[0]
                gap_spec = shape["fact_gap"]
                if (
                    not isinstance(gap_value, dict) or set(gap_value) != set(gap_spec["required"]) | {"schema"}
                    or gap_value["schema"] != gap_spec["schema"]
                    or gap_value["gap_kind"] not in gap_spec["gap_kinds"]
                    or gap_value["gap_kind"] == "SEMANTICS_UNRESOLVED"
                    or any(not gap_value[name] for name in gap_spec["required"])
                    or not isinstance(gap_value["affected_fields"], list)
                    or any(not isinstance(item, str) for item in gap_value["affected_fields"])
                    or len(gap_value["affected_fields"]) != len(set(gap_value["affected_fields"]))
                    or not set(gap_value["affected_fields"]) <= set(fields)
                    or any(
                        not isinstance(gap_value[name], list)
                        or not all(isinstance(item, str) and item for item in gap_value[name])
                        for name in ("missing_fact_content", "affected_obligations", "remaining_limitations")
                    )
                    or not isinstance(gap_value["direct_source_rationale"], str)
                    or not gap_value["direct_source_rationale"].strip()
                ):
                    raise StoreError("publication Fact Gap is invalid")
            elif gap not in ([], None):
                raise StoreError("publication unnecessary Fact Gap is invalid")
            claim_payload = {k: v for k, v in claim.items() if k not in {"ai_id", "fact_support", "direct_source_evidence"}}
            expected_ai = "ai:" + hashlib.sha256(_canonical(claim_payload).encode()).hexdigest()
            if claim.get("ai_id") != expected_ai or expected_ai in prepared_claims:
                raise StoreError("publication claim identity is invalid")
            if not isinstance(claim.get("text"), str) or not claim["text"].strip() or not isinstance(claim.get("claim_key"), str) or not claim["claim_key"].strip():
                raise StoreError("publication claim text or key is invalid")
            prepared_claims[expected_ai] = (claim, source_ids, source_only, effective_claim)
        if list(prepared_claims) != sorted(prepared_claims):
            raise StoreError("publication claim set is not canonical")
        for coverage in coverages:
            if not isinstance(coverage, dict) or set(coverage) != {
                "target_kind", "target_behavior_key", "target_subject_id", "obligation_id",
                "profile", "vocabulary", "claim_members", "claim_set_digest", "applicability",
                "closure_status", "limitations", "conflicts", "unresolved",
            }:
                raise StoreError("publication coverage shape is invalid")
            oid = coverage.get("obligation_id")
            obligation = contract["obligations"].get(oid)
            members_ids = coverage.get("claim_members")
            if (
                obligation is None or obligation["target"] != ("SUBJECT:rtl.sv/1:module" if subject_mode else f"BEHAVIOR:{behavior_type}")
                or coverage.get("target_kind") != ("SUBJECT" if subject_mode else "BEHAVIOR")
                or coverage.get("target_behavior_key") != key
                or coverage.get("target_subject_id") != subject_id
                or coverage.get("profile") != contract["profile"]
                or coverage.get("vocabulary") != contract["vocabulary"]
                or not isinstance(members_ids, list) or not members_ids
                or members_ids != sorted(set(members_ids))
                or not set(members_ids) <= set(prepared_claims)
                or coverage.get("claim_set_digest") != hashlib.sha256(_canonical(members_ids).encode()).hexdigest()
            ):
                raise StoreError("publication coverage identity or members are invalid")
            if not isinstance(coverage.get("limitations"), list) or not isinstance(coverage.get("conflicts"), list) or not isinstance(coverage.get("unresolved"), list):
                raise StoreError("publication coverage findings are invalid")
            common_coverage: dict[str, list[str]] | None = None
            for ai_id in members_ids:
                claim, _, _, claim_effective = prepared_claims[ai_id]
                if claim["structured_schema_id"] not in obligation["claim_shapes"]:
                    raise StoreError("publication obligation claim shape is invalid")
                effective_coverage = self._publication_applicability(
                    claim_effective, coverage.get("applicability"), allowed, members
                )
                if common_coverage is None:
                    common_coverage = effective_coverage
                elif effective_coverage != common_coverage:
                    raise StoreError("publication coverage has no single common applicability slice")
                if not set(obligation["required_fields"]) <= set(claim["structured_fields"]):
                    raise StoreError("publication obligation fields are missing")
                source_only = prepared_claims[ai_id][2]
                if source_only and oid not in claim["limitations"][0]["affected_obligations"]:
                    raise StoreError("publication Fact Gap does not cover obligation")
                if coverage.get("closure_status") == "READY" and source_only:
                    gap = claim["limitations"][0]
                    if not obligation["source_only_ready"] or gap["gap_kind"] != "COMPILER_FACT_ABSENT":
                        raise StoreError("publication Source-only READY is forbidden")
                if coverage.get("closure_status") == "READY" and (coverage["conflicts"] or coverage["unresolved"]):
                    raise StoreError("publication READY has unresolved findings")
                if source_only and not obligation["source_only_ready"] and coverage.get("closure_status") == "READY":
                    raise StoreError("publication-only obligation cannot be READY")
            if coverage.get("closure_status") not in {"READY", "PARTIAL", "GAP"}:
                raise StoreError("publication coverage status is invalid")
            if coverage.get("closure_status") == "GAP" and not coverage["unresolved"]:
                raise StoreError("publication GAP needs unresolved findings")
            if coverage.get("closure_status") == "PARTIAL" and not coverage["limitations"]:
                raise StoreError("publication PARTIAL needs limitations")
            if coverage.get("closure_status") == "READY" and not members_ids:
                raise StoreError("publication READY needs claims")
        if [coverage["obligation_id"] for coverage in coverages] != sorted({coverage["obligation_id"] for coverage in coverages}):
            raise StoreError("publication coverage set is not canonical")
        if validate_only:
            return {
                "status": "FIELD_VALID_TRUST_PENDING",
                "input_sha256": digest,
                "scope_id": scope["scope_id"],
                "claim_count": len(prepared_claims),
                "coverage_count": len(coverages),
            }
        bundle = (self._verify_real_approval(
            review_root=review_root, approval_file=approval_file,
            approved_sha256=approved_sha256, review=review, digest=digest,
            scope=scope, contract=contract,
        ) if purpose in {"REAL_MODULE_Q2", "REAL_Q2_FIFO"} else None)
        if purpose == "SYNTHETIC_ONLY":
            _verify_controlled_review(review_root, review, digest)
        # The lock is the final authority for mutable rows, Source and review identity.
        with self.transaction():
            if self.analysis_scope() != scope or self.metadata("semantic_publication_contract") != _publication_identity(contract, contract_digest, purpose):
                raise StoreError("publication identity changed under write lock")
            for claim, _, _, effective_claim in prepared_claims.values():
                for source in claim["direct_source_evidence"]:
                    self._insert_publication_source(source, source_root, scope)
                if (purpose in {"REAL_MODULE_Q2", "REAL_Q2_FIFO"}
                    and claim["structured_schema_id"] == "module-q2.module-interface@2"):
                    self._publication_interface_ports(
                        claim["structured_fields"], effective_claim["instance_id"], members,
                    )
                for link in claim["fact_support"]:
                    fact = self.connection.execute(
                        "SELECT subject_id,object_id FROM fact WHERE fact_id=?", (link["fact_id"],)
                    ).fetchone()
                    if fact is None or fact["subject_id"] not in members or fact["object_id"] not in members or not self.connection.execute(
                        "SELECT 1 FROM fact_evidence WHERE fact_id=? LIMIT 1", (link["fact_id"],)
                    ).fetchone():
                        raise StoreError("publication Fact dependency changed under write lock")
            if purpose in {"REAL_MODULE_Q2", "REAL_Q2_FIFO"}:
                if self._verify_real_approval(
                    review_root=review_root, approval_file=approval_file,
                    approved_sha256=approved_sha256, review=review, digest=digest,
                    scope=scope, contract=contract,
                ) != bundle:
                    raise StoreError("real publication approval changed under write lock")
                existing_bundle = self.connection.execute(
                    "SELECT value_json FROM model_metadata WHERE key='publication_bundle'"
                ).fetchone()
                if existing_bundle is None:
                    self.connection.execute(
                        "INSERT INTO model_metadata VALUES (?,?)",
                        ("publication_bundle", _canonical(bundle)),
                    )
                elif json.loads(existing_bundle[0]) != bundle:
                    raise StoreError("real publication approval conflicts with candidate bundle")
            else:
                _verify_controlled_review(review_root, review, digest)
            review_attrs = _publication_review_attrs(review_input, review)
            review_id = _identity("ev", "provenance", review["locator"], None, None, _canonical(review_attrs))
            if review.get("evidence_id") != review_id:
                raise StoreError("publication review Evidence ID is invalid")
            if subject_mode:
                for claim, _, _, effective_claim in prepared_claims.values():
                    self._publication_module_instances(subject_id, effective_claim.get("instance_id", []), self._publication_members(scope))
                existing_behavior = None
            else:
                existing_behavior = self.connection.execute("SELECT * FROM behavior_object WHERE behavior_id=?", (behavior_id,)).fetchone()
                behavior_row = (behavior_id, scope["scope_id"], owner, behavior_type, key, _canonical(behavior["applicability"]), "PUBLISHED")
                if existing_behavior is not None and tuple(existing_behavior) != behavior_row:
                    raise StoreError("publication Behavior identity conflicts")
            previous = {}
            for ai_id, (claim, source_ids, _, _) in prepared_claims.items():
                prior = self.connection.execute("SELECT * FROM ai_knowledge WHERE ai_id=?", (ai_id,)).fetchone()
                desired = (ai_id, subject_id, behavior_id, claim["claim_key"], claim["knowledge_type"], claim["text"], "agent", claim["structured_schema_id"], _canonical(claim["structured_fields"]), _canonical(claim["applicability"]), _canonical(claim["limitations"]), review_id, "PUBLISHED", "{}")
                if prior is not None:
                    if tuple(prior) != desired:
                        raise StoreError("publication same AI ID has conflicting authoritative payload")
                    supports = {(row[0], row[1]) for row in self.connection.execute("SELECT fact_id,support_quality FROM ai_fact_support WHERE ai_id=?", (ai_id,))}
                    evidence = {row[0] for row in self.connection.execute("SELECT evidence_id FROM ai_evidence WHERE ai_id=?", (ai_id,))}
                    if supports != {(link["fact_id"], link["quality"]) for link in claim["fact_support"]} or evidence != set(source_ids):
                        raise StoreError("publication same AI ID has conflicting support")
                previous[ai_id] = (prior, desired)
            if any(item[0] is not None for item in previous.values()) and not all(item[0] is not None for item in previous.values()):
                raise StoreError("publication mixed existing and new AI IDs conflict")
            for coverage in coverages:
                coverage_id = _identity("cov", scope["scope_id"], coverage)
                row = self.connection.execute("SELECT * FROM obligation_coverage WHERE coverage_id=?", (coverage_id,)).fetchone()
                if row is not None:
                    linked = {r[0] for r in self.connection.execute("SELECT ai_id FROM obligation_coverage_claim WHERE coverage_id=?", (coverage_id,))}
                    expected_row = (
                        coverage_id, scope["scope_id"], "SUBJECT" if subject_mode else "BEHAVIOR", subject_id, behavior_id,
                        coverage["obligation_id"], contract["profile"]["id"], contract["profile"]["version"],
                        contract["vocabulary"]["id"], contract["vocabulary"]["version"],
                        coverage["closure_status"], "CURRENT", _canonical(coverage["applicability"]),
                        coverage["claim_set_digest"], review_id, _canonical(coverage["limitations"]),
                        _canonical(coverage["conflicts"]), _canonical(coverage["unresolved"]),
                    )
                    if tuple(row) != expected_row or linked != set(coverage["claim_members"]):
                        raise StoreError("publication same coverage ID conflicts")
            all_existing = all(item[0] is not None for item in previous.values())
            if all_existing:
                current_ids = {
                    row[0] for row in self.connection.execute(
                        "SELECT c.coverage_id FROM obligation_coverage c JOIN obligation_coverage_claim l ON c.coverage_id=l.coverage_id WHERE l.ai_id IN ({}) AND c.validity='CURRENT'".format(
                            ",".join("?" for _ in prepared_claims)
                        ), list(prepared_claims),
                    )
                }
                expected_ids = {_identity("cov", scope["scope_id"], coverage) for coverage in coverages}
                if current_ids != expected_ids:
                    raise StoreError("publication same AI ID has conflicting coverage")
                self._assert_current_coverage_members_published()
                return {"behavior_id": behavior_id, **({"subject_id": subject_id} if subject_mode else {}), "ai_ids": sorted(prepared_claims), "coverage_ids": sorted(expected_ids), "review_input_sha256": digest, "idempotent": True}
            if not subject_mode and existing_behavior is None:
                self.connection.execute("INSERT INTO behavior_object VALUES (?,?,?,?,?,?,?)", behavior_row)
            self.connection.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?)", (review_id, "provenance", review["locator"], None, None, _canonical(review_attrs)))
            for ai_id, (prior, desired) in previous.items():
                if prior is not None:
                    continue
                target_column = "subject_id" if subject_mode else "behavior_id"
                old = self.connection.execute(
                    f"SELECT ai_id FROM ai_knowledge WHERE {target_column}=? AND claim_key=? AND lifecycle='PUBLISHED'",
                    (subject_id if subject_mode else behavior_id, desired[3]),
                ).fetchone()
                if old is not None:
                    old_id = old[0]
                    self.connection.execute("UPDATE obligation_coverage SET validity='STALE' WHERE coverage_id IN (SELECT coverage_id FROM obligation_coverage_claim WHERE ai_id=?) AND validity='CURRENT'", (old_id,))
                    self.connection.execute("UPDATE ai_knowledge SET lifecycle='STALE' WHERE ai_id=?", (old_id,))
                self.connection.execute("INSERT INTO ai_knowledge VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", desired)
                claim, source_ids, _, _ = prepared_claims[ai_id]
                self.connection.executemany("INSERT INTO ai_fact_support VALUES (?,?,?)", [(ai_id, link["fact_id"], link["quality"]) for link in claim["fact_support"]])
                self.connection.executemany("INSERT INTO ai_evidence VALUES (?,?)", [(ai_id, source_id) for source_id in source_ids])
            result_coverage = []
            for coverage in coverages:
                coverage_id = _identity("cov", scope["scope_id"], coverage)
                self.connection.execute(
                    "INSERT INTO obligation_coverage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (coverage_id, scope["scope_id"], "SUBJECT" if subject_mode else "BEHAVIOR", subject_id, behavior_id, coverage["obligation_id"],
                     contract["profile"]["id"], contract["profile"]["version"], contract["vocabulary"]["id"],
                     contract["vocabulary"]["version"], coverage["closure_status"], "CURRENT", _canonical(coverage["applicability"]),
                     coverage["claim_set_digest"], review_id, _canonical(coverage["limitations"]), _canonical(coverage["conflicts"]), _canonical(coverage["unresolved"])),
                )
                self.connection.executemany("INSERT INTO obligation_coverage_claim VALUES (?,?)", [(coverage_id, ai_id) for ai_id in coverage["claim_members"]])
                result_coverage.append(coverage_id)
            self._assert_current_coverage_members_published()
        return {"behavior_id": behavior_id, **({"subject_id": subject_id} if subject_mode else {}), "ai_ids": sorted(prepared_claims), "coverage_ids": sorted(result_coverage), "review_input_sha256": digest, "idempotent": False}

    def read_published_semantic_overlay(
        self, request_applicability: dict[str, str], *, publication_contract: Path,
    ) -> dict[str, Any]:
        """Read the bounded synthetic publication from one SQLite snapshot.

        The caller owns this readonly Store. No cursor or transaction escapes the call.
        A missing or inconsistent CURRENT coverage fails the entire projection closed.
        """
        if self.schema_version != "v0.2" or self._discarded:
            raise StoreError("semantic reader requires an active v0.2 Store")
        if self.connection.in_transaction or self.connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise StoreError("semantic reader requires an idle readonly connection")
        contract, _, _ = _load_publication_contract(publication_contract)
        if contract["status"] == "STAGE1_ONLY_Q2_FIFO_V1":
            raise StoreError("combination semantic reader is not implemented")
        allowed = contract["applicability"]["dimensions"]
        if not isinstance(request_applicability, dict) or set(request_applicability) - set(allowed):
            raise StoreError("semantic reader request applicability is invalid")
        if any(not isinstance(value, str) or not value for value in request_applicability.values()):
            raise StoreError("semantic reader request applicability is invalid")
        self.connection.execute("BEGIN")
        try:
            result = self._read_published_semantic_snapshot(request_applicability, contract)
        finally:
            # ROLLBACK only releases this reader's snapshot. Never commit a caller transaction.
            try:
                self.connection.rollback()
            except BaseException:
                self._discard_connection()
                raise StoreError("semantic reader could not release its snapshot; connection discarded")
        return result

    def _read_published_semantic_snapshot(
        self, request: dict[str, str], contract: dict[str, Any],
    ) -> dict[str, Any]:
        scope = self.analysis_scope()
        identity = {
            "CONTROLLED_REAL_Q2_V1": (MODULE_Q2_PUBLICATION_SHA256, "REAL_MODULE_Q2"),
            "CONTROLLED_REAL_Q2_FIFO_V2": (Q2_FIFO_V2_PUBLICATION_SHA256, "REAL_Q2_FIFO"),
            "UNAPPROVED_SYNTHETIC_B6_0": (SYNTHETIC_PUBLICATION_SHA256, "SYNTHETIC_ONLY"),
        }
        try:
            expected_digest, expected_purpose = identity[contract["status"]]
        except KeyError as exc:
            raise StoreError("semantic reader Profile is not publishable") from exc
        if self.metadata("semantic_publication_contract") != _publication_identity(contract, expected_digest, expected_purpose) or scope["profile"] != contract["profile"] or scope["vocabulary"] != contract["vocabulary"]:
            raise StoreError("semantic reader contract does not match candidate")
        members = self._publication_members(scope)
        allowed = contract["applicability"]["dimensions"]
        for key, value in request.items():
            spec = allowed[key]
            if (spec["kind"] == "enum" and value not in spec["values"]) or (
                spec["kind"] == "entity_ref" and value not in members
            ):
                raise StoreError("semantic reader request is outside scope or vocabulary")
        coverage_rows = self.connection.execute(
            "SELECT * FROM obligation_coverage WHERE validity='CURRENT' ORDER BY coverage_id"
        ).fetchall()
        result = {
            "scope_id": scope["scope_id"], "scope_completeness": scope["completeness"],
            "request_applicability": dict(request), "claims": [], "coverage": [],
        }
        selected_claims: dict[str, dict[str, Any]] = {}
        for coverage in coverage_rows:
            if coverage["scope_id"] != scope["scope_id"] or coverage["target_kind"] not in {"BEHAVIOR", "SUBJECT"}:
                raise StoreError("semantic reader unsupported coverage target or scope")
            subject_mode = coverage["target_kind"] == "SUBJECT"
            obligation = contract["obligations"].get(coverage["obligation_id"])
            behavior = None if subject_mode else self.connection.execute(
                "SELECT * FROM behavior_object WHERE behavior_id=?", (coverage["target_behavior_id"],)
            ).fetchone()
            subject_id = coverage["target_subject_id"] if subject_mode else None
            if subject_mode:
                if (coverage["target_behavior_id"] is not None or subject_id not in members
                    or self._entity_kind(subject_id) != self.contract_id("entities", "MODULE") or obligation is None
                    or obligation["target"] != "SUBJECT:rtl.sv/1:module"):
                    raise StoreError("semantic reader unsupported MODULE target")
                behavior_type = None
                owner = None
            else:
                if behavior is None or behavior["lifecycle"] != "PUBLISHED" or behavior["scope_id"] != scope["scope_id"]:
                    raise StoreError("semantic reader CURRENT coverage has noncurrent Behavior")
                behavior_type = behavior["behavior_type"]
                behavior_spec = contract["behavior_types"].get(behavior_type)
                if obligation is None or obligation["target"] != f"BEHAVIOR:{behavior_type}" or behavior_spec is None:
                    raise StoreError("semantic reader unsupported obligation or Behavior shape")
                owner = behavior["owner_subject_id"]
                if (owner is None and "SCOPE" not in behavior_spec["ownership_modes"]) or (
                    owner is not None and (owner not in members or self._entity_kind(owner) not in behavior_spec["owner_kinds"])
                ):
                    raise StoreError("semantic reader Behavior owner is invalid")
            if (coverage["profile_id"], coverage["profile_version"]) != (
                contract["profile"]["id"], contract["profile"]["version"]
            ) or (coverage["vocabulary_id"], coverage["vocabulary_version"]) != (
                contract["vocabulary"]["id"], contract["vocabulary"]["version"]
            ):
                raise StoreError("semantic reader coverage contract is invalid")
            review = self.connection.execute(
                "SELECT * FROM evidence WHERE evidence_id=?", (coverage["review_evidence_id"],)
            ).fetchone()
            review_input = self._publication_review_input(review, scope, contract)
            reviewed_behavior = review_input["behavior"]
            if subject_mode:
                if reviewed_behavior is not None:
                    raise StoreError("semantic reader MODULE review has Behavior")
            else:
                if not isinstance(reviewed_behavior, dict):
                    raise StoreError("semantic reader Behavior review is absent")
                expected_behavior_id = _identity(
                    "behavior", scope["scope_id"], reviewed_behavior["behavior_type"],
                    reviewed_behavior["behavior_key"],
                )
                expected_behavior = (
                    expected_behavior_id, scope["scope_id"], reviewed_behavior["owner_subject_id"],
                    reviewed_behavior["behavior_type"], reviewed_behavior["behavior_key"],
                    _canonical(reviewed_behavior["applicability"]), "PUBLISHED",
                )
                if tuple(behavior) != expected_behavior or reviewed_behavior["ownership_mode"] != (
                    "SCOPE" if owner is None else "SUBJECT"
                ):
                    raise StoreError("semantic reader Behavior differs from reviewed input")
            reviewed_coverages = [item for item in review_input["coverage"] if
                                  _identity("cov", scope["scope_id"], item) == coverage["coverage_id"]]
            if len(reviewed_coverages) != 1:
                raise StoreError("semantic reader coverage is absent from reviewed input")
            reviewed_coverage = reviewed_coverages[0]
            if (
                reviewed_coverage["target_kind"] != coverage["target_kind"]
                or reviewed_coverage["target_behavior_key"] != (None if subject_mode else behavior["behavior_key"])
                or reviewed_coverage["target_subject_id"] != subject_id
                or reviewed_coverage["profile"] != contract["profile"]
                or reviewed_coverage["vocabulary"] != contract["vocabulary"]
            ):
                raise StoreError("semantic reader reviewed coverage target or contract is invalid")
            expected_coverage = (
                coverage["coverage_id"], scope["scope_id"], coverage["target_kind"], subject_id,
                None if subject_mode else behavior["behavior_id"], reviewed_coverage["obligation_id"],
                contract["profile"]["id"], contract["profile"]["version"],
                contract["vocabulary"]["id"], contract["vocabulary"]["version"],
                reviewed_coverage["closure_status"], "CURRENT",
                _canonical(reviewed_coverage["applicability"]),
                reviewed_coverage["claim_set_digest"], coverage["review_evidence_id"],
                _canonical(reviewed_coverage["limitations"]),
                _canonical(reviewed_coverage["conflicts"]),
                _canonical(reviewed_coverage["unresolved"]),
            )
            if tuple(coverage) != expected_coverage:
                raise StoreError("semantic reader coverage differs from reviewed input")
            findings = [json.loads(coverage[key]) for key in ("limitations_json", "conflicts_json", "unresolved_json")]
            if any(not isinstance(item, list) for item in findings) or (
                coverage["closure_status"] == "READY" and (findings[1] or findings[2])
            ) or (coverage["closure_status"] == "PARTIAL" and not findings[0]):
                raise StoreError("semantic reader coverage status or findings are inconsistent")
            linked = self.connection.execute(
                "SELECT ai_id FROM obligation_coverage_claim WHERE coverage_id=? ORDER BY ai_id",
                (coverage["coverage_id"],),
            ).fetchall()
            claim_ids = [row["ai_id"] for row in linked]
            if not claim_ids or coverage["claim_set_digest"] != hashlib.sha256(_canonical(claim_ids).encode()).hexdigest():
                raise StoreError("semantic reader CURRENT coverage claim-set digest is invalid")
            if claim_ids != reviewed_coverage["claim_members"]:
                raise StoreError("semantic reader CURRENT coverage members differ from review")
            effective_behavior = scope["applicability"]["constraints"] if subject_mode else self._publication_applicability(
                scope["applicability"]["constraints"], json.loads(behavior["applicability_json"]), allowed, members,
            )
            coverage_delta = json.loads(coverage["applicability_json"])
            common_slice = None
            current_claims = []
            for ai_id in claim_ids:
                claim = self.connection.execute("SELECT * FROM ai_knowledge WHERE ai_id=?", (ai_id,)).fetchone()
                if (claim is None or claim["lifecycle"] != "PUBLISHED"
                    or claim["behavior_id"] != (None if subject_mode else behavior["behavior_id"])
                    or claim["subject_id"] != subject_id):
                    raise StoreError("semantic reader CURRENT coverage references noncurrent claim")
                shape = contract["claim_shapes"].get(claim["structured_schema_id"])
                if shape is None or shape["target"] != ("SUBJECT:rtl.sv/1:module" if subject_mode else f"BEHAVIOR:{behavior_type}") or claim["structured_schema_id"] not in obligation["claim_shapes"] or claim["knowledge_type"] != shape["knowledge_type"]:
                    raise StoreError("semantic reader unsupported claim shape")
                if claim["review_evidence_id"] != coverage["review_evidence_id"]:
                    raise StoreError("semantic reader claim review differs from coverage")
                fields = json.loads(claim["structured_fields_json"])
                if not isinstance(fields, dict) or set(fields) - set(shape["fields"]) or any(
                    spec.get("required") and name not in fields for name, spec in shape["fields"].items()
                ) or not set(obligation["required_fields"]) <= set(fields):
                    raise StoreError("semantic reader claim fields are invalid")
                for name, value in fields.items():
                    spec = shape["fields"][name]
                    kind = spec["kind"]
                    if (kind == "scalar" and (not isinstance(value, str) or not value.strip())) or (
                        kind == "list" and (not isinstance(value, list) or len(value) < spec.get("min_items", 0)
                                            or any(not isinstance(item, str) for item in value)
                                            or len(value) != len(set(value)))
                    ) or (kind == "list" and spec.get("item_kind") == "entity_ref" and not set(value) <= members) or (
                        kind == "object" and not isinstance(value, dict)
                    ) or (kind == "entity_ref" and (not isinstance(value, str) or value not in members)) or (
                        kind == "enum" and (not isinstance(value, str) or value not in spec["values"])
                    ):
                        raise StoreError("semantic reader claim field value is invalid")
                claim_slice = self._publication_applicability(
                    effective_behavior, json.loads(claim["applicability_json"]), allowed, members,
                )
                if contract["status"] in {"CONTROLLED_REAL_Q2_V1", "CONTROLLED_REAL_Q2_FIFO_V2"} and not claim_slice.get("instance_id"):
                    raise StoreError("semantic reader real module-q2 instance applicability is absent")
                if subject_mode:
                    self._publication_module_instances(subject_id, claim_slice.get("instance_id", []), members)
                    if (contract["status"] in {"CONTROLLED_REAL_Q2_V1", "CONTROLLED_REAL_Q2_FIFO_V2"}
                        and claim["structured_schema_id"] == "module-q2.module-interface@2"):
                        interface_ports = self._publication_interface_ports(fields, claim_slice["instance_id"], members)
                    basis = fields.get("reuse_basis")
                    if basis is not None and (not isinstance(basis, dict)
                        or set(basis) != {"instance_ids", "equivalence_reason"}
                        or len(claim_slice["instance_id"]) < 2
                        or basis.get("instance_ids") != claim_slice["instance_id"]
                        or not isinstance(basis.get("equivalence_reason"), str)
                        or not basis["equivalence_reason"].strip()):
                        raise StoreError("semantic reader MODULE reuse basis is invalid")
                    if len(claim_slice["instance_id"]) > 1 and basis is None:
                        raise StoreError("semantic reader MODULE reuse basis is invalid")
                coverage_slice = self._publication_applicability(claim_slice, coverage_delta, allowed, members)
                if common_slice is None:
                    common_slice = coverage_slice
                elif coverage_slice != common_slice:
                    raise StoreError("semantic reader CURRENT coverage has no common applicability slice")
                current_claims.append(self._read_published_semantic_claim(claim, shape, fields, members, behavior, scope))
                if (subject_mode and contract["status"] in {"CONTROLLED_REAL_Q2_V1", "CONTROLLED_REAL_Q2_FIFO_V2"}
                    and claim["structured_schema_id"] == "module-q2.module-interface@2"):
                    current_claims[-1]["signal_ports"] = interface_ports
                reviewed_claims = [item for item in review_input["claims"] if item["ai_id"] == ai_id]
                if len(reviewed_claims) != 1:
                    raise StoreError("semantic reader claim is absent from reviewed input")
                reviewed_claim = reviewed_claims[0]
                self._audit_published_claim_binding(
                    claim, current_claims[-1], reviewed_claim, shape, behavior,
                    reviewed_behavior, coverage, obligation,
                )
                for projected_link, reviewed_link in zip(
                    current_claims[-1]["fact_support"], reviewed_claim["fact_support"]
                ):
                    projected_link["fields_proven"] = reviewed_link["fields_proven"]
            if not all(key in request and request[key] in values for key, values in common_slice.items()):
                continue
            for item in current_claims:
                selected_claims[item["ai_id"]] = item
            projected_coverage = {
                "coverage_id": coverage["coverage_id"], "target_kind": coverage["target_kind"],
                "behavior_id": None if subject_mode else behavior["behavior_id"],
                "behavior_type": behavior_type,
                "owner_subject_id": owner,
                "obligation_id": coverage["obligation_id"], "closure_status": coverage["closure_status"],
                "applicability": common_slice, "claim_ids": claim_ids,
                "claim_set_digest": coverage["claim_set_digest"], "review_evidence_id": coverage["review_evidence_id"],
                "limitations": findings[0], "conflicts": findings[1], "unresolved": findings[2],
            }
            if subject_mode:
                projected_coverage["target_subject_id"] = subject_id
            result["coverage"].append(projected_coverage)
        result["claims"] = [selected_claims[key] for key in sorted(selected_claims)]
        return result

    def audit_real_publication_bundle(self, contract: dict[str, Any]) -> dict[str, Any]:
        """Gate formal Mapping on the exact approved complete batch set."""
        if not self.connection.in_transaction or self.connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise StoreError("bundle audit requires a caller-owned readonly snapshot")
        combo = contract.get("status") == "CONTROLLED_REAL_Q2_FIFO_V2"
        if not combo and contract.get("status") != "CONTROLLED_REAL_Q2_V1":
            raise StoreError("bundle audit requires a controlled real Profile")
        bundle = self.metadata("publication_bundle")
        if (not isinstance(bundle, dict)
            or bundle.get("profile_sha256") != (Q2_FIFO_V2_PUBLICATION_SHA256 if combo else MODULE_Q2_PUBLICATION_SHA256)
            or [item.get("goal_id") for item in bundle.get("goal_dispositions", [])] != list(Q2_FIFO_GOALS if combo else MODULE_Q2_GOALS)):
            raise StoreError("complete Q2 bundle metadata is absent or invalid")
        if any(item["status"] != "COVERED" for item in bundle["goal_dispositions"][:len(MODULE_Q2_GOALS)]):
            raise StoreError("complete Q2 bundle retains an unfulfilled goal")
        if not combo and any(item["status"] != "COVERED" for item in bundle["goal_dispositions"]):
            raise StoreError("complete Q2 bundle retains an unfulfilled goal")
        self._read_published_semantic_snapshot({}, contract)
        actual_claims = [row[0] for row in self.connection.execute(
            "SELECT ai_id FROM ai_knowledge WHERE lifecycle='PUBLISHED' ORDER BY ai_id"
        )]
        actual_coverage = [row[0] for row in self.connection.execute(
            "SELECT coverage_id FROM obligation_coverage WHERE validity='CURRENT' ORDER BY coverage_id"
        )]
        actual_behaviors = [row[0] for row in self.connection.execute(
            "SELECT behavior_id FROM behavior_object WHERE lifecycle='PUBLISHED' ORDER BY behavior_id"
        )]
        if (actual_claims != bundle["expected_claim_ids"]
            or actual_coverage != bundle["expected_coverage_ids"]
            or actual_behaviors != bundle["expected_behavior_ids"]):
            raise StoreError("complete Q2 bundle is missing a batch or has extra semantic rows")
        review_rows = self.connection.execute(
            "SELECT DISTINCT e.attrs_json FROM obligation_coverage c "
            "JOIN evidence e ON e.evidence_id=c.review_evidence_id WHERE c.validity='CURRENT'"
        ).fetchall()
        reviews = [json.loads(row[0]) for row in review_rows]
        if (sorted({item["input_sha256"] for item in reviews}) != bundle["batch_sha256"]
            or any(item["artifact_sha256"] != bundle["review_sha256"] for item in reviews)):
            raise StoreError("complete Q2 review set differs from approved batches")
        return bundle

    def _publication_review_input(
        self, evidence: sqlite3.Row | None, scope: dict[str, Any], contract: dict[str, Any],
    ) -> dict[str, Any]:
        """Recover the exact reviewed input from its existing provenance Evidence."""
        try:
            if evidence is None or evidence["kind"] != "provenance":
                raise ValueError("missing provenance")
            attrs = json.loads(evidence["attrs_json"])
            raw_input = attrs["review_input_json"]
            if not isinstance(raw_input, str):
                raise ValueError("reviewed input is not canonical text")
            review_input = json.loads(raw_input)
            digest = hashlib.sha256(raw_input.encode("utf-8")).hexdigest()
            if (
                not isinstance(attrs, dict) or not isinstance(review_input, dict)
                or raw_input != _canonical(review_input)
                or attrs["status"] != "VERIFIED" or attrs["input_sha256"] != digest
                or not re.fullmatch(r"[0-9a-f]{64}", attrs["artifact_sha256"])
                or not attrs["reviewer"] or not attrs["generator"]
                or attrs["reviewer"] == attrs["generator"]
                or attrs != _publication_review_attrs(review_input, attrs)
                or evidence["evidence_id"] != _identity(
                    "ev", "provenance", evidence["locator"], None, None, _canonical(attrs),
                )
            ):
                raise ValueError("provenance identity or digest differs")
            candidate, publication = self._publication_review_context(scope, contract)
            if (
                review_input["schema"] != PUBLICATION_REVIEW_SCHEMA
                or review_input["candidate"] != candidate
                or review_input["contract"] != publication
                or (review_input["behavior"] is not None and not isinstance(review_input["behavior"], dict))
                or not isinstance(review_input["claims"], list)
                or not isinstance(review_input["coverage"], list)
            ):
                raise ValueError("reviewed candidate or contract differs")
            return review_input
        except (KeyError, TypeError, ValueError, UnicodeError) as error:
            raise StoreError("semantic reader review binding is invalid") from error

    def _audit_published_claim_binding(
        self, row: sqlite3.Row, projected: dict[str, Any], reviewed: dict[str, Any],
        shape: dict[str, Any], behavior: sqlite3.Row | None, reviewed_behavior: dict[str, Any] | None,
        coverage: sqlite3.Row, obligation: dict[str, Any],
    ) -> None:
        """Compare returned truth with the frozen writer input; reuse its Fact field rule."""
        try:
            payload = {key: value for key, value in reviewed.items() if key not in {
                "ai_id", "fact_support", "direct_source_evidence",
            }}
            expected_id = "ai:" + hashlib.sha256(_canonical(payload).encode()).hexdigest()
            subject_id = coverage["target_subject_id"] if behavior is None else None
            expected_row = (
                expected_id, subject_id, None if behavior is None else behavior["behavior_id"], reviewed["claim_key"],
                reviewed["knowledge_type"], reviewed["text"], "agent",
                reviewed["structured_schema_id"], _canonical(reviewed["structured_fields"]),
                _canonical(reviewed["applicability"]), _canonical(reviewed["limitations"]),
                coverage["review_evidence_id"], "PUBLISHED", "{}",
            )
            if (tuple(row) != expected_row
                or reviewed["target_kind"] != ("SUBJECT" if behavior is None else "BEHAVIOR")
                or reviewed["behavior_key"] != (None if behavior is None else behavior["behavior_key"])
                or (behavior is None and reviewed["target_subject_id"] != subject_id)):
                raise ValueError("claim row differs")
            fields = reviewed["structured_fields"]
            if behavior is not None and (fields.get("participants", []) != reviewed_behavior["participants"] or
                fields.get("related_subject_ids", []) != reviewed_behavior["related_subject_ids"]):
                raise ValueError("relation fields differ")
            links = reviewed["fact_support"]
            if not isinstance(links, list) or [item["fact_id"] for item in links] != [
                item["fact_id"] for item in projected["fact_support"]
            ] or [(item["fact_id"], item["quality"]) for item in links] != [
                (item["fact_id"], item["support_quality"]) for item in projected["fact_support"]
            ]:
                raise ValueError("Fact links differ")
            for link in links:
                proof = link["fields_proven"]
                if (not isinstance(proof, list) or proof != sorted(set(proof))
                    or not set(proof) <= set(fields)
                    or (link["quality"] == "SCOPE_ONLY") != (not proof)):
                    raise ValueError("Fact field proof differs")
            sources = reviewed["direct_source_evidence"]
            if not isinstance(sources, list) or len(sources) != len(projected["source_evidence"]):
                raise ValueError("Source links differ")
            for source, actual in zip(sources, projected["source_evidence"]):
                match = re.fullmatch(r"L([1-9][0-9]*)(?:-L([1-9][0-9]*))?", source["span"])
                if (match is None or source["current"] is not True
                    or source["evidence_id"] != actual["evidence_id"]
                    or source["path"] != actual["locator"]
                    or int(match[1]) != actual["start_line"]
                    or int(match[2] or match[1]) != actual["end_line"]
                    or source["content_sha256"] != actual["attrs"].get("sha256")):
                    raise ValueError("Source Evidence differs")
            missing = _publication_missing_fact_fields(shape, links)
            if missing:
                gaps = reviewed["limitations"]
                if (not shape["source_only_publication"] or len(gaps) != 1
                    or coverage["obligation_id"] not in gaps[0]["affected_obligations"]):
                    raise ValueError("Source-only publication gate differs")
                if coverage["closure_status"] == "READY" and (
                    not obligation["source_only_ready"] or gaps[0]["gap_kind"] != "COMPILER_FACT_ABSENT"
                ):
                    raise ValueError("Source-only READY gate differs")
            elif reviewed["limitations"] not in ([], None):
                raise ValueError("content Fact support and Gap disagree")
        except (KeyError, TypeError, ValueError) as error:
            raise StoreError("semantic reader claim differs from reviewed field/support binding") from error

    def _read_published_semantic_claim(
        self, claim: sqlite3.Row, shape: dict[str, Any], fields: dict[str, Any],
        members: set[str], behavior: sqlite3.Row | None, scope: dict[str, Any],
    ) -> dict[str, Any]:
        ai_id = claim["ai_id"]
        relations = []
        for name, spec in shape["fields"].items():
            role = spec.get("relation_role")
            if role is None or name not in fields:
                continue
            endpoints = fields[name]
            if behavior is None:
                raise StoreError("semantic reader MODULE relation is unsupported")
            if spec["kind"] != "list" or spec.get("item_kind") != "entity_ref" or not isinstance(endpoints, list) or len(endpoints) < spec.get("min_items", 0) or len(endpoints) != len(set(endpoints)) or not set(endpoints) <= members:
                raise StoreError("semantic reader relation endpoint is invalid")
            for endpoint in endpoints:
                if role == "PARTICIPANT":
                    relations.append({"type": "PARTICIPATES_IN", "source_id": endpoint,
                                      "target_id": behavior["behavior_id"]})
                elif role == "RELATED_SUBJECT":
                    relations.append({"type": "RELATES_TO",
                                      "source_id": behavior["owner_subject_id"] or behavior["behavior_id"],
                                      "target_id": endpoint})
                else:
                    raise StoreError("semantic reader unsupported relation role")
        supports = []
        for support in self.connection.execute(
            "SELECT s.fact_id,s.support_quality,f.subject_id,f.object_id FROM ai_fact_support s "
            "LEFT JOIN fact f ON f.fact_id=s.fact_id WHERE s.ai_id=? ORDER BY s.fact_id", (ai_id,),
        ):
            if support["subject_id"] not in members or support["object_id"] not in members or not self.connection.execute(
                "SELECT 1 FROM fact_evidence WHERE fact_id=? LIMIT 1", (support["fact_id"],)
            ).fetchone():
                raise StoreError("semantic reader claim Fact support is missing or outside scope")
            supports.append({"fact_id": support["fact_id"], "support_quality": support["support_quality"]})
        source_rows = self.connection.execute(
            "SELECT e.* FROM ai_evidence l LEFT JOIN evidence e ON e.evidence_id=l.evidence_id "
            "WHERE l.ai_id=? ORDER BY l.evidence_id", (ai_id,),
        ).fetchall()
        if not source_rows or any(row["evidence_id"] is None or row["kind"] != "source" for row in source_rows):
            raise StoreError("semantic reader claim Source Evidence is missing")
        if any(row["locator"] not in scope["boundary_policy"]["knowledge_source_paths"] or
               not isinstance(json.loads(row["attrs_json"]).get("sha256"), str)
               for row in source_rows):
            raise StoreError("semantic reader claim Source Evidence is outside scope")
        projected = {
            "ai_id": ai_id, "behavior_id": None if behavior is None else behavior["behavior_id"],
            "owner_subject_id": None if behavior is None else behavior["owner_subject_id"], "claim_key": claim["claim_key"],
            "knowledge_type": claim["knowledge_type"], "text": claim["text"],
            "structured_schema_id": claim["structured_schema_id"], "structured_fields": fields,
            "applicability": json.loads(claim["applicability_json"]),
            "limitations": json.loads(claim["limitations_json"]),
            "review_evidence_id": claim["review_evidence_id"], "fact_support": supports,
            "source_evidence": [{"evidence_id": row["evidence_id"], "locator": row["locator"],
                                 "start_line": row["start_line"], "end_line": row["end_line"],
                                 "attrs": json.loads(row["attrs_json"])} for row in source_rows],
            "relations": relations,
        }
        if behavior is None:
            projected["subject_id"] = claim["subject_id"]
        return projected

    def add_knowledge_coverage(
        self,
        subject_id: str,
        *,
        section: str,
        subtree_scope: str,
        status: str,
        child_instance_ids: list[str],
        request_kinds: list[str],
        evidence_ids: list[str],
        attrs: dict[str, Any] | None = None,
    ) -> None:
        """Persist minimal request coverage in the same candidate database."""
        if self.schema_version != "v0.1":
            raise StoreError("legacy coverage writer is unavailable for v0.2 candidates")
        if self._entity_kind(subject_id) != self.contract_id("entities", "MODULE"):
            raise StoreError("knowledge coverage subject must be a module")
        if section not in MODULE_KNOWLEDGE_SECTIONS:
            raise StoreError("knowledge coverage section is invalid")
        if subtree_scope != "direct" or status not in COVERAGE_STATUSES:
            raise StoreError("knowledge coverage scope or status is invalid")
        if len(child_instance_ids) != len(set(child_instance_ids)):
            raise StoreError("knowledge coverage child identities contain duplicates")
        if child_instance_ids and not self._existing_ids(
            "entity", "entity_id", child_instance_ids
        ):
            raise StoreError("knowledge coverage child identity is unknown")
        if any(self._entity_kind(item) != self.contract_id("entities", "INSTANCE") for item in child_instance_ids):
            raise StoreError("knowledge coverage child must be an instance")
        if (
            not request_kinds
            or len(request_kinds) != len(set(request_kinds))
            or not all(isinstance(item, str) and item.strip() for item in request_kinds)
        ):
            raise StoreError("knowledge coverage request kinds are invalid")
        if not evidence_ids or not self._existing_ids("evidence", "evidence_id", evidence_ids):
            raise StoreError("knowledge coverage evidence is missing or unknown")
        coverage_attrs = attrs or {}
        knowledge_ids = coverage_attrs.get("knowledge_ids", [])
        required_claim_ids = coverage_attrs.get("required_claim_ids", [])
        covered_requirements = coverage_attrs.get("covered_additional_requirements", [])
        if (
            not isinstance(knowledge_ids, list)
            or not all(isinstance(item, str) and item for item in knowledge_ids)
            or len(knowledge_ids) != len(set(knowledge_ids))
            or not isinstance(required_claim_ids, list)
            or not all(isinstance(item, str) and item for item in required_claim_ids)
            or len(required_claim_ids) != len(set(required_claim_ids))
            or not isinstance(covered_requirements, list)
            or not all(isinstance(item, str) and item for item in covered_requirements)
            or len(covered_requirements) != len(set(covered_requirements))
        ):
            raise StoreError("knowledge coverage publication lists are invalid")
        if knowledge_ids and not self._existing_ids("ai_knowledge", "ai_id", knowledge_ids):
            raise StoreError("knowledge coverage references unknown knowledge")
        if coverage_attrs.get("knowledge_digest") != knowledge_id_digest(knowledge_ids):
            raise StoreError("knowledge coverage digest does not match its knowledge IDs")
        if status == "ready":
            review_sha = coverage_attrs.get("review_artifact_sha256")
            if not knowledge_ids:
                raise StoreError("ready knowledge coverage requires published knowledge IDs")
            if not isinstance(review_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", review_sha):
                raise StoreError("ready knowledge coverage requires a review artifact hash")
            review_rows = list(self.connection.execute(
                "SELECT kind, attrs_json FROM evidence WHERE evidence_id IN ({})".format(
                    ",".join("?" for _ in evidence_ids)
                ), evidence_ids,
            ))
            if not any(
                row["kind"] == "provenance"
                and json.loads(row["attrs_json"]).get("sha256") == review_sha
                and json.loads(row["attrs_json"]).get("role") == "independent-review"
                for row in review_rows
            ):
                raise StoreError("ready knowledge coverage lacks matching independent review evidence")
            claim_ids: set[str] = set()
            for row in self.connection.execute(
                "SELECT ai_id, attrs_json FROM ai_knowledge WHERE ai_id IN ({})".format(
                    ",".join("?" for _ in knowledge_ids)
                ), knowledge_ids,
            ):
                item_attrs = json.loads(row["attrs_json"])
                claim_id = item_attrs.get("claim_id")
                if (
                    item_attrs.get("section") != section
                    or item_attrs.get("review_status") != "verified"
                    or item_attrs.get("review_artifact_sha256") != review_sha
                    or not isinstance(claim_id, str)
                    or not claim_id
                ):
                    raise StoreError("ready coverage contains unverified or mismatched knowledge")
                supports = self.connection.execute(
                    "SELECT count(*) FROM ai_fact_support WHERE ai_id=?", (row["ai_id"],)
                ).fetchone()[0]
                sources = self.connection.execute(
                    "SELECT count(*) FROM ai_evidence WHERE ai_id=?", (row["ai_id"],)
                ).fetchone()[0]
                if supports < 1 or sources < 1:
                    raise StoreError("ready knowledge lacks Fact or evidence support")
                claim_ids.add(claim_id)
            missing_claims = sorted(set(required_claim_ids) - claim_ids)
            if missing_claims:
                raise StoreError("ready knowledge coverage has a required claim gap")
            if coverage_attrs.get("conflicts") not in (None, []):
                raise StoreError("ready knowledge coverage has unresolved conflicts")
        with self._write_context():
            self.connection.execute(
                """
                INSERT INTO knowledge_coverage(
                  subject_id, section, subtree_scope, status,
                  child_instance_ids_json, request_kinds_json, attrs_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subject_id, section, subtree_scope) DO UPDATE SET
                  status=excluded.status,
                  child_instance_ids_json=excluded.child_instance_ids_json,
                  request_kinds_json=excluded.request_kinds_json,
                  attrs_json=excluded.attrs_json
                """,
                (
                    subject_id, section, subtree_scope, status,
                    _canonical(sorted(child_instance_ids)),
                    _canonical(sorted(request_kinds)), _canonical(coverage_attrs),
                ),
            )
            self.connection.execute(
                "DELETE FROM knowledge_coverage_evidence WHERE subject_id=? AND section=? AND subtree_scope=?",
                (subject_id, section, subtree_scope),
            )
            self.connection.executemany(
                "INSERT INTO knowledge_coverage_evidence(subject_id, section, subtree_scope, evidence_id) VALUES (?, ?, ?, ?)",
                [(subject_id, section, subtree_scope, item) for item in sorted(set(evidence_ids))],
            )

    def integrity(self) -> dict[str, Any]:
        integrity = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = [list(row) for row in self.connection.execute("PRAGMA foreign_key_check")]
        return {"integrity_check": integrity, "foreign_key_check": foreign_keys}


def empty_module_knowledge_response(
    *,
    reason: str,
    candidate_accessed: bool,
    requested_sections: list[str] | None = None,
    required_child_instance_ids: list[str] | None = None,
    additional_requirements: list[str] | None = None,
) -> dict[str, Any]:
    """Return the stable no-material form of the module knowledge contract."""
    requested = list(dict.fromkeys(requested_sections or []))
    children = sorted(set(required_child_instance_ids or []))
    requested_additional = sorted(set(additional_requirements or []))
    return {
        "route": "BYPASS", "reasons": [reason],
        "candidate_accessed": candidate_accessed, "returned_items": 0,
        "returned_counts": {"knowledge": 0, "support_facts": 0, "evidence": 0},
        "knowledge": [], "support_facts": [], "evidence": [],
        "truncated": False,
        "coverage": {
            "requested_sections": requested, "covered_sections": [],
            "ready_sections": [], "missing_sections": requested,
            "required_child_instance_ids": children,
            "covered_child_instance_ids": [],
            "missing_child_instance_ids": children,
            "requested_additional_requirements": requested_additional,
            "covered_additional_requirements": [],
            "missing_additional_requirements": requested_additional,
            "unresolved_additional_requirements": [],
            "unexpected_child_instance_ids": [],
        },
    }


def _compact_returned_fact_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """Bound Runtime expression payloads while retaining their answer-bearing meaning."""
    result = dict(attrs)
    for key in ("actual", "internal_expression"):
        expression = result.get(key)
        if not isinstance(expression, dict):
            continue
        result[key] = {
            name: expression[name]
            for name in ("kind", "text", "text_truncated", "nodes_truncated")
            if name in expression
        }
        references = expression.get("references")
        if isinstance(references, list):
            result[key]["references"] = [
                {
                    name: reference[name]
                    for name in ("kind", "name", "path", "type")
                    if isinstance(reference, dict) and name in reference
                }
                for reference in references
                if isinstance(reference, dict)
            ]
    return result


def route_module_knowledge(
    store: PrototypeStore,
    *,
    enabled: bool,
    target_id: str,
    build_sha256: str,
    requested_sections: list[str],
    required_child_instance_ids: list[str],
    request_kind: str,
    additional_requirements: list[str],
    response_view: str = "answer",
    audit_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Validate one published request, then assemble answer/full/audit material."""
    if response_view not in {"answer", "full", "audit"}:
        raise StoreError("module knowledge response view is invalid")
    if response_view == "audit" and not audit_refs:
        raise StoreError("audit view requires at least one exact audit reference")
    if not enabled:
        result = empty_module_knowledge_response(
            reason="knowledge-disabled", candidate_accessed=False,
            requested_sections=requested_sections,
            required_child_instance_ids=required_child_instance_ids,
            additional_requirements=additional_requirements,
        )
        result["response_view"] = response_view
        return result
    reasons: list[str] = []

    def reason_once(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    try:
        recorded_build = store.metadata("build_sha256")
    except StoreError:
        recorded_build = None
    if recorded_build != build_sha256:
        result = empty_module_knowledge_response(
            reason="build-identity-mismatch", candidate_accessed=True,
            requested_sections=requested_sections,
            required_child_instance_ids=required_child_instance_ids,
            additional_requirements=additional_requirements,
        )
        result["response_view"] = response_view
        return result
    target = store.connection.execute(
        "SELECT entity_id, kind, qualified_name, attrs_json FROM entity WHERE entity_id = ?",
        (target_id,),
    ).fetchone()
    if target is None or target["kind"] != store.contract_id("entities", "MODULE"):
        result = empty_module_knowledge_response(
            reason="target-not-covered", candidate_accessed=True,
            requested_sections=requested_sections,
            required_child_instance_ids=required_child_instance_ids,
            additional_requirements=additional_requirements,
        )
        result["response_view"] = response_view
        return result
    try:
        rows = list(store.connection.execute(
            "SELECT * FROM knowledge_coverage WHERE subject_id=? AND subtree_scope='direct' ORDER BY section",
            (target_id,),
        ))
    except sqlite3.OperationalError:
        result = empty_module_knowledge_response(
            reason="coverage-schema-unavailable", candidate_accessed=True,
            requested_sections=requested_sections,
            required_child_instance_ids=required_child_instance_ids,
            additional_requirements=additional_requirements,
        )
        result["response_view"] = response_view
        return result
    by_section = {row["section"]: row for row in rows}
    requested_sections = list(dict.fromkeys(requested_sections))
    unknown_sections = sorted(set(requested_sections) - set(MODULE_KNOWLEDGE_SECTIONS))
    expected_children = sorted(set(required_child_instance_ids))
    published: list[dict[str, Any]] = []
    coverage_evidence_ids: set[str] = set()
    covered_sections: list[str] = []
    ready_sections: list[str] = []
    invalid_sections: set[str] = set()
    coverage_attrs_by_section: dict[str, dict[str, Any]] = {}
    child_ids_by_section: dict[str, set[str]] = {}

    for section in requested_sections:
        row = by_section.get(section)
        if row is None:
            continue
        coverage_attrs = json.loads(row["attrs_json"])
        section_children = sorted(json.loads(row["child_instance_ids_json"]))
        coverage_attrs_by_section[section] = coverage_attrs
        child_ids_by_section[section] = set(section_children)
        if section_children != expected_children:
            reason_once("child-identity-mismatch")
        if request_kind not in json.loads(row["request_kinds_json"]):
            reason_once("request-kind-gap")
        covered_requirements = set(coverage_attrs.get("covered_additional_requirements", []))
        if not set(additional_requirements) <= covered_requirements:
            reason_once("additional-requirement-gap")
        knowledge_ids = coverage_attrs.get("knowledge_ids", [])
        if (
            not isinstance(knowledge_ids, list)
            or coverage_attrs.get("knowledge_digest") != knowledge_id_digest(knowledge_ids)
        ):
            invalid_sections.add(section)
            continue
        review_sha = coverage_attrs.get("review_artifact_sha256")
        claims: set[str] = set()
        section_knowledge: list[dict[str, Any]] = []
        for ai_id in knowledge_ids:
            knowledge_row = store.connection.execute(
                "SELECT ai_id, subject_id, knowledge_type, text, attrs_json FROM ai_knowledge WHERE ai_id=?",
                (ai_id,),
            ).fetchone()
            if knowledge_row is None:
                invalid_sections.add(section)
                continue
            attrs = json.loads(knowledge_row["attrs_json"])
            if (
                attrs.get("section") != section
                or attrs.get("review_status") != "verified"
                or attrs.get("review_artifact_sha256") != review_sha
                or not isinstance(attrs.get("claim_id"), str)
            ):
                invalid_sections.add(section)
                continue
            fact_ids = sorted(row[0] for row in store.connection.execute(
                "SELECT fact_id FROM ai_fact_support WHERE ai_id=?", (ai_id,)
            ))
            evidence_ids = sorted(row[0] for row in store.connection.execute(
                "SELECT evidence_id FROM ai_evidence WHERE ai_id=?", (ai_id,)
            ))
            if not fact_ids or not evidence_ids:
                invalid_sections.add(section)
                continue
            existing_facts = {
                value[0] for value in store.connection.execute(
                    "SELECT fact_id FROM fact WHERE fact_id IN ({})".format(
                        ",".join("?" for _ in fact_ids)
                    ), fact_ids,
                )
            }
            existing_evidence = {
                value[0] for value in store.connection.execute(
                    "SELECT evidence_id FROM evidence WHERE evidence_id IN ({})".format(
                        ",".join("?" for _ in evidence_ids)
                    ), evidence_ids,
                )
            }
            fact_evidence: dict[str, list[str]] = {
                fact_id: sorted(value[0] for value in store.connection.execute(
                    "SELECT evidence_id FROM fact_evidence WHERE fact_id=?", (fact_id,)
                ))
                for fact_id in fact_ids
            }
            if (
                existing_facts != set(fact_ids)
                or existing_evidence != set(evidence_ids)
                or any(not values for values in fact_evidence.values())
            ):
                invalid_sections.add(section)
                continue
            claims.add(attrs["claim_id"])
            section_knowledge.append({
                "ai_id": ai_id,
                "subject_id": knowledge_row["subject_id"],
                "knowledge_type": knowledge_row["knowledge_type"],
                "section": section,
                "claim_id": attrs["claim_id"],
                "text": knowledge_row["text"],
                "review_status": "verified",
                "review_artifact_sha256": review_sha,
                "attrs": attrs,
                "support_fact_ids": fact_ids,
                "direct_evidence_ids": evidence_ids,
                "fact_evidence_ids": fact_evidence,
            })
        section_coverage_evidence = [value[0] for value in store.connection.execute(
            """
            SELECT evidence_id FROM knowledge_coverage_evidence
            WHERE subject_id=? AND section=? AND subtree_scope='direct'
            ORDER BY evidence_id
            """,
            (target_id, section),
        )]
        coverage_evidence_ids.update(section_coverage_evidence)
        required_claims = set(coverage_attrs.get("required_claim_ids", []))
        if not required_claims <= claims:
            invalid_sections.add(section)
        if section_knowledge:
            covered_sections.append(section)
            published.extend(section_knowledge)
        if row["status"] == "ready" and section not in invalid_sections:
            ready_sections.append(section)

    missing_sections = sorted(set(requested_sections) - set(ready_sections))
    if unknown_sections or missing_sections:
        reason_once("section-gap")
    if invalid_sections:
        reason_once("publication-invalid")
    if not published:
        reason_once("no-usable-knowledge")
        route = "BYPASS"
    else:
        route = "CONTEXT" if reasons else "DIRECT"
    requested_additional = sorted(set(additional_requirements))
    covered_additional: list[str] = []
    missing_additional: list[str] = []
    unresolved_additional: list[str] = []
    for requirement in requested_additional:
        states: list[str] = []
        for section in requested_sections:
            attrs = coverage_attrs_by_section.get(section)
            row = by_section.get(section)
            if (
                attrs is None
                or row is None
                or row["status"] != "ready"
                or section in invalid_sections
            ):
                states.append("unresolved")
                continue
            values = attrs.get("covered_additional_requirements")
            if not isinstance(values, list):
                states.append("unresolved")
            elif requirement in values:
                states.append("covered")
            else:
                states.append("missing")
        if states and all(state == "covered" for state in states):
            covered_additional.append(requirement)
        elif "missing" in states:
            missing_additional.append(requirement)
        else:
            unresolved_additional.append(requirement)

    section_child_sets = [
        child_ids_by_section[section]
        for section in requested_sections
        if (
            section in child_ids_by_section
            and section in by_section
            and by_section[section]["status"] == "ready"
            and section not in invalid_sections
        )
    ]
    all_sections_have_child_coverage = (
        bool(requested_sections) and len(section_child_sets) == len(requested_sections)
    )
    common_children = (
        set.intersection(*section_child_sets)
        if all_sections_have_child_coverage and section_child_sets
        else set()
    )
    union_children = set().union(*section_child_sets) if section_child_sets else set()
    covered_children = sorted(set(expected_children) & common_children)
    missing_children = sorted(set(expected_children) - common_children)
    unexpected_children = sorted(union_children - set(expected_children))

    coverage = {
        "requested_sections": requested_sections,
        "covered_sections": sorted(set(covered_sections)),
        "ready_sections": sorted(set(ready_sections)),
        "missing_sections": missing_sections,
        "required_child_instance_ids": expected_children,
        "covered_child_instance_ids": covered_children,
        "missing_child_instance_ids": missing_children,
        "unexpected_child_instance_ids": unexpected_children,
        "requested_additional_requirements": requested_additional,
        "covered_additional_requirements": covered_additional,
        "missing_additional_requirements": missing_additional,
        "unresolved_additional_requirements": unresolved_additional,
    }
    base = {
        "route": route,
        "reasons": reasons or ["complete-exact-coverage"],
        "candidate_accessed": True,
        "response_view": response_view,
        "truncated": False,
        "coverage": coverage,
    }
    if response_view == "answer":
        return _module_knowledge_answer_view(
            store, base=base, target=target, published=published,
            build_sha256=build_sha256, request_kind=request_kind,
            additional_requirements=additional_requirements,
        )
    if response_view == "audit":
        return _module_knowledge_audit_view(
            store, base=base, published=published,
            coverage_evidence_ids=coverage_evidence_ids,
            audit_refs=audit_refs or [],
        )
    return _module_knowledge_full_view(
        store, base=base, published=published,
        coverage_evidence_ids=coverage_evidence_ids,
    )


def _evidence_record(store: PrototypeStore, evidence_id: str) -> dict[str, Any] | None:
    row = store.connection.execute(
        "SELECT evidence_id, kind, locator, start_line, end_line, attrs_json "
        "FROM evidence WHERE evidence_id=?", (evidence_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "evidence_id": evidence_id, "kind": row["kind"],
        "locator": row["locator"], "start_line": row["start_line"],
        "end_line": row["end_line"], "attrs": json.loads(row["attrs_json"]),
    }


def _fact_record(
    store: PrototypeStore, fact_id: str, *, compact: bool
) -> dict[str, Any] | None:
    row = store.connection.execute(
        "SELECT fact_id, subject_id, predicate, object_id, producer, attrs_json "
        "FROM fact WHERE fact_id=?", (fact_id,),
    ).fetchone()
    if row is None:
        return None
    attrs = json.loads(row["attrs_json"])
    return {
        "fact_id": fact_id, "subject_id": row["subject_id"],
        "predicate": row["predicate"], "object_id": row["object_id"],
        "producer": row["producer"],
        "attrs": _compact_returned_fact_attrs(attrs) if compact else attrs,
        "evidence_ids": sorted(value[0] for value in store.connection.execute(
            "SELECT evidence_id FROM fact_evidence WHERE fact_id=?", (fact_id,)
        )),
    }


def _module_knowledge_full_view(
    store: PrototypeStore,
    *,
    base: dict[str, Any],
    published: list[dict[str, Any]],
    coverage_evidence_ids: set[str],
) -> dict[str, Any]:
    """Compatibility view with every bounded Fact and evidence record expanded."""
    facts: dict[str, dict[str, Any]] = {}
    evidence: dict[str, dict[str, Any]] = {}
    knowledge: list[dict[str, Any]] = []
    for item in published:
        knowledge.append({
            key: item[key] for key in (
                "ai_id", "subject_id", "knowledge_type", "section", "claim_id",
                "text", "review_status", "review_artifact_sha256",
                "support_fact_ids",
            )
        } | {"evidence_ids": item["direct_evidence_ids"]})
        for fact_id in item["support_fact_ids"]:
            record = _fact_record(store, fact_id, compact=True)
            if record is not None:
                facts[fact_id] = record
                for evidence_id in record["evidence_ids"]:
                    evidence_record = _evidence_record(store, evidence_id)
                    if evidence_record is not None:
                        evidence[evidence_id] = evidence_record
        for evidence_id in item["direct_evidence_ids"]:
            evidence_record = _evidence_record(store, evidence_id)
            if evidence_record is not None:
                evidence[evidence_id] = evidence_record
    for evidence_id in coverage_evidence_ids:
        evidence_record = _evidence_record(store, evidence_id)
        if evidence_record is not None:
            evidence[evidence_id] = evidence_record
    counts = {
        "knowledge": len(knowledge), "support_facts": len(facts),
        "evidence": len(evidence),
    }
    return {
        **base, "returned_items": sum(counts.values()), "returned_counts": counts,
        "knowledge": knowledge,
        "support_facts": [facts[key] for key in sorted(facts)],
        "evidence": [evidence[key] for key in sorted(evidence)],
    }


def _module_knowledge_answer_view(
    store: PrototypeStore,
    *,
    base: dict[str, Any],
    target: sqlite3.Row,
    published: list[dict[str, Any]],
    build_sha256: str,
    request_kind: str,
    additional_requirements: list[str],
) -> dict[str, Any]:
    """Serving-style view: answer-bearing content plus compact source anchors."""
    child_ids = base["coverage"]["required_child_instance_ids"]
    child_rows = []
    if child_ids:
        child_rows = list(store.connection.execute(
            "SELECT entity_id, kind, qualified_name, attrs_json FROM entity "
            "WHERE entity_id IN ({}) ORDER BY qualified_name".format(
                ",".join("?" for _ in child_ids)
            ), child_ids,
        ))
    instance_of = store.contract_id("facts", "INSTANCE_OF")
    definition_by_instance: dict[str, str] = {}
    definition_ids: set[str] = set()
    for child in child_rows:
        row = store.connection.execute(
            "SELECT object_id FROM fact WHERE subject_id=? AND predicate=? ORDER BY object_id",
            (child["entity_id"], instance_of),
        ).fetchone()
        if row is not None:
            definition_by_instance[child["entity_id"]] = row["object_id"]
            definition_ids.add(row["object_id"])
    definition_rows = []
    if definition_ids:
        definition_rows = list(store.connection.execute(
            "SELECT entity_id, kind, qualified_name, attrs_json FROM entity "
            "WHERE entity_id IN ({}) ORDER BY qualified_name".format(
                ",".join("?" for _ in definition_ids)
            ), sorted(definition_ids),
        ))

    entity_refs: dict[str, str] = {target["entity_id"]: "M0"}
    for index, row in enumerate(child_rows, 1):
        entity_refs[row["entity_id"]] = f"I{index}"
    for index, row in enumerate(definition_rows, 1):
        entity_refs[row["entity_id"]] = f"D{index}"
    entity_rows = [target, *child_rows, *definition_rows]
    entities = [{
        "ref": entity_refs[row["entity_id"]], "entity_id": row["entity_id"],
        "kind": row["kind"], "qualified_name": row["qualified_name"],
    } for row in entity_rows]

    # Project only deterministic bindings already supporting the reviewed
    # publication. This exposes instance differences without expanding every
    # Fact attribute or treating unreviewed candidate rows as answer material.
    binding_rows: list[dict[str, Any]] = []
    supported_fact_ids = sorted({
        fact_id for item in published for fact_id in item["support_fact_ids"]
    })
    if child_ids and supported_fact_ids:
        binding_rows = list(store.connection.execute(
            "SELECT owner.subject_id AS instance_id, port.qualified_name AS port_name, "
            "binding.fact_id, binding.attrs_json "
            "FROM fact AS owner "
            "JOIN entity AS port ON port.entity_id=owner.object_id "
            "JOIN fact AS binding ON binding.subject_id=port.entity_id "
            "WHERE owner.predicate=? AND port.kind=? AND binding.predicate=? "
            "AND owner.subject_id IN ({}) AND binding.fact_id IN ({}) "
            "ORDER BY owner.subject_id, port.qualified_name, binding.fact_id".format(
                ",".join("?" for _ in child_ids),
                ",".join("?" for _ in supported_fact_ids),
            ),
            [store.contract_id("facts", "CONTAINS"),
             store.contract_id("entities", "PORT"),
             store.contract_id("facts", "BINDS_TO"),
             *child_ids, *supported_fact_ids],
        ))

    evidence_records: dict[str, dict[str, Any]] = {}
    claim_evidence: dict[str, set[str]] = {}
    for item in published:
        ids = set(item["direct_evidence_ids"])
        for evidence_id in ids:
            record = _evidence_record(store, evidence_id)
            if record is not None and record["kind"] == "source":
                evidence_records[evidence_id] = record
                claim_evidence.setdefault(item["ai_id"], set()).add(evidence_id)
    binding_evidence: dict[str, list[str]] = {}
    for binding in binding_rows:
        ids = [row[0] for row in store.connection.execute(
            "SELECT evidence_id FROM fact_evidence WHERE fact_id=? ORDER BY evidence_id",
            (binding["fact_id"],),
        )]
        binding_evidence[binding["fact_id"]] = ids
        for evidence_id in ids:
            record = _evidence_record(store, evidence_id)
            if record is not None and record["kind"] == "source":
                evidence_records[evidence_id] = record
    ordered_evidence = sorted(
        evidence_records.values(),
        key=lambda item: (
            item["locator"], item["start_line"] or 0,
            item["end_line"] or 0, item["evidence_id"],
        ),
    )
    evidence_refs = {
        item["evidence_id"]: f"E{index}"
        for index, item in enumerate(ordered_evidence, 1)
    }
    paths = sorted({item["locator"] for item in ordered_evidence})
    file_refs = {path: f"S{index}" for index, path in enumerate(paths, 1)}
    source_files = []
    for path in paths:
        hashes = {
            item["attrs"].get("sha256") for item in ordered_evidence
            if item["locator"] == path and item["attrs"].get("sha256")
        }
        source_files.append({
            "ref": file_refs[path], "path": path,
            "sha256": sorted(hashes)[0] if len(hashes) == 1 else "UNAVAILABLE",
        })
    source_evidence = [{
        "ref": evidence_refs[item["evidence_id"]],
        "evidence_id": item["evidence_id"],
        "file_ref": file_refs[item["locator"]],
        "start_line": item["start_line"], "end_line": item["end_line"],
        "audit_ref": f"evidence:{item['evidence_id']}",
    } for item in ordered_evidence]

    knowledge = []
    claim_limitations: dict[str, list[Any]] = {}
    for index, item in enumerate(published, 1):
        limitations = item["attrs"].get("limitations", [])
        claim_limitations[item["claim_id"]] = limitations
        knowledge.append({
            "ref": f"K{index}", "ai_id": item["ai_id"],
            "claim_id": item["claim_id"], "section": item["section"],
            "subject_ref": entity_refs.get(item["subject_id"], item["subject_id"]),
            "text": item["text"], "review_status": item["review_status"],
            "review_artifact_sha256": item["review_artifact_sha256"],
            "limitations": limitations,
            "source_evidence_refs": sorted(
                evidence_refs[value] for value in claim_evidence.get(item["ai_id"], set())
            ),
            "support_fact_count": len(item["support_fact_ids"]),
            "audit_ref": f"knowledge:{item['ai_id']}",
        })

    bindings_by_instance: dict[str, list[dict[str, Any]]] = {
        row["entity_id"]: [] for row in child_rows
    }
    instance_names = {
        row["entity_id"]: row["qualified_name"] for row in child_rows
    }
    for binding_index, binding in enumerate(binding_rows, 1):
        instance_id = binding["instance_id"]
        prefix = instance_names[instance_id] + "."
        port_name = binding["port_name"]
        attrs = json.loads(binding["attrs_json"])
        actual = attrs.get("actual")
        actual = actual if isinstance(actual, dict) else {}
        binding_ref = f"F{binding_index}"
        bindings_by_instance[instance_id].append({
            "ref": binding_ref,
            "formal": port_name[len(prefix):] if port_name.startswith(prefix) else "UNAVAILABLE",
            **({"port_qualified_name": port_name} if not port_name.startswith(prefix) else {}),
            "actual_text": actual.get("text") if isinstance(actual.get("text"), str) else "UNAVAILABLE",
            "actual_kind": actual.get("kind", "UNAVAILABLE"),
            "actual_truncated": bool(actual.get("text_truncated") or actual.get("nodes_truncated")),
            "connection_status": attrs.get("connection_status", "UNAVAILABLE"),
            "audit_ref": f"fact:{binding['fact_id']}",
            "source_evidence_refs": sorted(
                evidence_refs[evidence_id]
                for evidence_id in binding_evidence[binding["fact_id"]]
                if evidence_id in evidence_refs
            ),
        })

    compact_coverage = {
        key: base["coverage"][key] for key in (
            "requested_sections", "covered_sections", "ready_sections", "missing_sections",
            "requested_additional_requirements", "covered_additional_requirements",
            "missing_additional_requirements", "unresolved_additional_requirements",
        )
    }
    compact_coverage["direct_children"] = {
        "required_count": len(child_ids),
        "covered_count": len(base["coverage"]["covered_child_instance_ids"]),
        "required_refs": [entity_refs[value] for value in child_ids if value in entity_refs],
        "missing_ids": base["coverage"]["missing_child_instance_ids"],
        "unexpected_ids": base["coverage"]["unexpected_child_instance_ids"],
    }
    counts = {
        "knowledge": len(knowledge), "direct_instances": len(child_rows),
        "definitions": len(definition_rows), "source_evidence": len(source_evidence),
        "bindings": len(binding_rows),
    }
    return {
        **{key: value for key, value in base.items() if key != "coverage"},
        "coverage": compact_coverage,
        "target": {
            "ref": "M0", "entity_id": target["entity_id"],
            "kind": target["kind"], "qualified_name": target["qualified_name"],
            "build_sha256": build_sha256, "subtree_scope": "direct",
            "request_kind": request_kind,
            "additional_requirements": sorted(set(additional_requirements)),
        },
        "entities": entities,
        "structure": {
            "subtree_scope": "direct",
            "direct_instances": [{
                "instance_ref": entity_refs[row["entity_id"]],
                "definition_ref": entity_refs.get(definition_by_instance.get(row["entity_id"])),
                "characteristics": {
                    key: value for key, value in json.loads(row["attrs_json"]).items()
                    if key in {"name", "definition", "effective_parameters", "parameters_truncated"}
                },
            } for row in child_rows],
            "definitions": [entity_refs[row["entity_id"]] for row in definition_rows],
            "instance_bindings": [{
                "instance_ref": entity_refs[row["entity_id"]],
                "bindings": bindings_by_instance[row["entity_id"]],
            } for row in child_rows],
            "binding_scope": "published immediate deterministic facts only; no bit equivalence",
            "collaboration_claim_refs": [
                item["ref"] for item in knowledge if item["section"] == "collaboration"
            ],
        },
        "knowledge": knowledge,
        "source_files": source_files,
        "source_evidence": source_evidence,
        "limitations": {
            "subtree_scope": "direct",
            "recursive_descendants_covered": False,
            "semantic_class": "reviewed-ai-with-deterministic-support",
            "human_spot_check": "not-represented-in-candidate",
            "claim_limitations": claim_limitations,
            "missing_sections": compact_coverage["missing_sections"],
            "missing_child_instance_ids": compact_coverage["direct_children"]["missing_ids"],
            "unexpected_child_instance_ids": compact_coverage["direct_children"]["unexpected_ids"],
            "missing_additional_requirements": compact_coverage[
                "missing_additional_requirements"
            ],
            "unresolved_additional_requirements": compact_coverage[
                "unresolved_additional_requirements"
            ],
        },
        "audit": {
            "expanded": False,
            "supported_refs": ["knowledge:<exact-id>", "fact:<exact-id>", "evidence:<exact-id>"],
            "instruction": "repeat the same validated request with response_view=audit and audit_refs",
        },
        "returned_counts": counts,
        "returned_items": sum(counts.values()),
    }


def render_module_knowledge_readable(
    result: dict[str, Any],
    *,
    result_ref: str | None = None,
    structured_ref: str | None = None,
) -> str:
    """Render one answer response for direct use without changing its facts.

    The structured response remains authoritative.  This deterministic view
    keeps answer-bearing text, instance differences, limitations, and source
    anchors together so a consumer need not query the candidate again merely
    to arrange the returned material.
    """

    def scalar(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return "UNAVAILABLE"
        return str(value).replace("|", "\\|").replace("\n", " ")

    source_files = {
        item.get("ref"): item for item in result.get("source_files", [])
        if isinstance(item, dict) and item.get("ref")
    }
    evidence = {
        item.get("ref"): item for item in result.get("source_evidence", [])
        if isinstance(item, dict) and item.get("ref")
    }
    entities = {
        item.get("ref"): item for item in result.get("entities", [])
        if isinstance(item, dict) and item.get("ref")
    }

    def source_anchor(ref: Any) -> str:
        item = evidence.get(ref)
        if item is None:
            return scalar(ref)
        file_item = source_files.get(item.get("file_ref"), {})
        path = file_item.get("path", item.get("file_ref", "UNAVAILABLE"))
        start = item.get("start_line")
        end = item.get("end_line")
        if start is None:
            return scalar(path)
        suffix = str(start) if end in {None, start} else f"{start}-{end}"
        return f"{scalar(path)}:{suffix}"

    route = result.get("route", "BYPASS")
    reasons = result.get("reasons", [])
    target = result.get("target", {}) if isinstance(result.get("target"), dict) else {}
    coverage = result.get("coverage", {}) if isinstance(result.get("coverage"), dict) else {}
    lines = [
        "# RTL module knowledge",
        "",
        f"- Route: {scalar(route)}",
        f"- Reason: {', '.join(map(scalar, reasons)) or 'UNAVAILABLE'}",
        f"- Object: {scalar(target.get('qualified_name', 'UNAVAILABLE'))}",
        f"- Build: {scalar(target.get('build_sha256', 'UNAVAILABLE'))}",
        f"- Scope: {scalar(target.get('subtree_scope', 'direct'))}",
        f"- Request kind: {scalar(target.get('request_kind', 'UNAVAILABLE'))}",
    ]
    requested = coverage.get("requested_sections", [])
    covered = coverage.get("covered_sections", [])
    missing = coverage.get("missing_sections")
    requested_additional = coverage.get("requested_additional_requirements", [])
    covered_additional = coverage.get("covered_additional_requirements", [])
    missing_additional = coverage.get("missing_additional_requirements", [])
    unresolved_additional = coverage.get("unresolved_additional_requirements", [])
    for name, value in (
        ("requested_sections", requested),
        ("covered_sections", covered),
        ("requested_additional_requirements", requested_additional),
        ("covered_additional_requirements", covered_additional),
        ("missing_additional_requirements", missing_additional),
        ("unresolved_additional_requirements", unresolved_additional),
    ):
        if not isinstance(value, list):
            raise ValueError(f"knowledge coverage {name} must be a list")
    if missing is not None and not isinstance(missing, list):
        raise ValueError("knowledge coverage missing_sections must be a list")
    classified_additional = {
        *covered_additional, *missing_additional, *unresolved_additional,
    }
    unresolved_additional = sorted({
        *unresolved_additional,
        *(item for item in requested_additional if item not in classified_additional),
    })
    direct_children = coverage.get("direct_children")
    if isinstance(direct_children, dict):
        required_child_count = direct_children.get("required_count", "UNRESOLVED")
        covered_child_count = direct_children.get("covered_count", "UNRESOLVED")
        missing_children = direct_children.get("missing_ids")
        unexpected_children = direct_children.get("unexpected_ids")
    else:
        required_children = coverage.get("required_child_instance_ids")
        covered_children = coverage.get("covered_child_instance_ids")
        required_child_count = (
            len(required_children) if isinstance(required_children, list) else "UNRESOLVED"
        )
        covered_child_count = (
            len(covered_children) if isinstance(covered_children, list) else "UNRESOLVED"
        )
        missing_children = coverage.get("missing_child_instance_ids")
        unexpected_children = coverage.get("unexpected_child_instance_ids")

    def rendered_list(value: Any, *, absent: str = "UNRESOLVED") -> str:
        if not isinstance(value, list):
            return absent
        return ", ".join(map(scalar, value)) or "NONE"

    lines.extend([
        f"- Requested sections: {rendered_list(requested)}",
        f"- Covered sections: {rendered_list(covered)}",
        f"- Missing sections: {rendered_list(missing)}",
        f"- Requested additional requirements: {rendered_list(requested_additional)}",
        f"- Covered additional requirements: {rendered_list(covered_additional)}",
        f"- Missing additional requirements: {rendered_list(missing_additional)}",
        f"- Unresolved additional requirements: {rendered_list(unresolved_additional)}",
        f"- Direct child coverage: {scalar(covered_child_count)}/{scalar(required_child_count)}",
        f"- Missing direct children: {rendered_list(missing_children)}",
        f"- Unexpected direct children: {rendered_list(unexpected_children)}",
    ])

    knowledge = result.get("knowledge", [])
    if knowledge:
        lines.extend(["", "## Reviewed knowledge"])
        for item in knowledge:
            refs = ", ".join(source_anchor(ref) for ref in item.get("source_evidence_refs", []))
            lines.extend([
                "",
                f"### {scalar(item.get('ref'))} · {scalar(item.get('section'))}",
                scalar(item.get("text", "")),
                f"Evidence: {refs or 'UNAVAILABLE'}",
                f"Audit: {scalar(item.get('audit_ref', 'UNAVAILABLE'))}",
            ])
            limitations = item.get("limitations", [])
            if limitations:
                lines.append("Limitations: " + "; ".join(map(scalar, limitations)))

    structure = result.get("structure", {}) if isinstance(result.get("structure"), dict) else {}
    instances = structure.get("direct_instances", [])
    if instances:
        lines.extend([
            "", "## Direct instances", "",
            "| Instance | Definition | Characteristics |", "|---|---|---|",
        ])
        for item in instances:
            instance = entities.get(item.get("instance_ref"), {})
            definition = entities.get(item.get("definition_ref"), {})
            characteristics = item.get("characteristics", {})
            lines.append(
                "| {} | {} | {} |".format(
                    scalar(instance.get("qualified_name", item.get("instance_ref"))),
                    scalar(definition.get("qualified_name", item.get("definition_ref"))),
                    scalar(json.dumps(characteristics, sort_keys=True, ensure_ascii=False)),
                )
            )

    binding_groups = structure.get("instance_bindings", [])
    if binding_groups:
        lines.extend(["", "## Published immediate bindings"])
        for group in binding_groups:
            instance = entities.get(group.get("instance_ref"), {})
            instance_name = instance.get("qualified_name", group.get("instance_ref"))
            group_evidence = sorted({
                ref for binding in group.get("bindings", [])
                for ref in binding.get("source_evidence_refs", [])
            })
            lines.extend([
                "",
                f"### {scalar(instance_name)}",
                "Evidence: " + (
                    ", ".join(f"{scalar(ref)}={source_anchor(ref)}" for ref in group_evidence)
                    or "UNAVAILABLE"
                ),
                "",
                "| Formal | Actual | Kind/status | Evidence | Fact |",
                "|---|---|---|---|---|",
            ])
            for binding in group.get("bindings", []):
                flags = "/".join(map(scalar, (
                    binding.get("actual_kind", "UNAVAILABLE"),
                    binding.get("connection_status", "UNAVAILABLE"),
                    "truncated" if binding.get("actual_truncated") else "complete",
                )))
                refs = ", ".join(map(scalar, binding.get("source_evidence_refs", [])))
                lines.append(
                    "| {} | {} | {} | {} | {} |".format(
                        scalar(binding.get("formal")),
                        scalar(binding.get("actual_text")), flags,
                        refs or "UNAVAILABLE", scalar(binding.get("ref")),
                    )
                )

    limitations = result.get("limitations", {})
    if isinstance(limitations, dict):
        lines.extend(["", "## Scope and limitations"])
        for key in (
            "subtree_scope", "recursive_descendants_covered", "semantic_class",
            "human_spot_check", "missing_sections", "missing_child_instance_ids",
            "unexpected_child_instance_ids", "missing_additional_requirements",
            "unresolved_additional_requirements",
        ):
            if key in limitations:
                value = limitations[key]
                if isinstance(value, list):
                    value = ", ".join(map(scalar, value)) or "NONE"
                lines.append(f"- {key}: {scalar(value)}")
        claim_limits = limitations.get("claim_limitations", {})
        if isinstance(claim_limits, dict):
            for claim, values in sorted(claim_limits.items()):
                if values:
                    lines.append(f"- {scalar(claim)}: {'; '.join(map(scalar, values))}")

    lines.extend(["", "## Result reuse"])
    lines.append(f"- Result ref: {scalar(result_ref or 'NOT_PERSISTED')}")
    lines.append(f"- Structured result: {scalar(structured_ref or 'NOT_PERSISTED')}")
    audit = result.get("audit", {})
    if isinstance(audit, dict):
        lines.append(f"- Audit: {scalar(audit.get('instruction', 'UNAVAILABLE'))}")
    lines.append("- This view reports returned material; it does not prove final-answer use.")
    return "\n".join(lines) + "\n"


def _module_knowledge_audit_view(
    store: PrototypeStore,
    *,
    base: dict[str, Any],
    published: list[dict[str, Any]],
    coverage_evidence_ids: set[str],
    audit_refs: list[str],
) -> dict[str, Any]:
    """Expand only exact references reachable from the validated publication set."""
    by_knowledge = {item["ai_id"]: item for item in published}
    fact_evidence = {
        fact_id: evidence_ids
        for item in published for fact_id, evidence_ids in item["fact_evidence_ids"].items()
    }
    allowed_facts = set(fact_evidence)
    allowed_evidence = set(coverage_evidence_ids)
    for item in published:
        allowed_evidence.update(item["direct_evidence_ids"])
        for values in item["fact_evidence_ids"].values():
            allowed_evidence.update(values)
    selected_knowledge: set[str] = set()
    selected_facts: set[str] = set()
    selected_evidence: set[str] = set()
    for reference in dict.fromkeys(audit_refs):
        kind, separator, identifier = reference.partition(":")
        valid = False
        if separator and kind == "knowledge" and identifier in by_knowledge:
            selected_knowledge.add(identifier)
            selected_facts.update(by_knowledge[identifier]["support_fact_ids"])
            selected_evidence.update(by_knowledge[identifier]["direct_evidence_ids"])
            valid = True
        elif separator and kind == "fact" and identifier in allowed_facts:
            selected_facts.add(identifier)
            valid = True
        elif separator and kind == "evidence" and identifier in allowed_evidence:
            selected_evidence.add(identifier)
            valid = True
        if not valid:
            raise StoreError("audit reference is not published request material")
    for fact_id in selected_facts:
        selected_evidence.update(fact_evidence.get(fact_id, []))
    knowledge_records = []
    for ai_id in sorted(selected_knowledge):
        item = by_knowledge[ai_id]
        knowledge_records.append({
            "ai_id": ai_id, "subject_id": item["subject_id"],
            "knowledge_type": item["knowledge_type"], "text": item["text"],
            "attrs": item["attrs"], "support_fact_ids": item["support_fact_ids"],
            "evidence_ids": item["direct_evidence_ids"],
        })
    facts = [
        record for fact_id in sorted(selected_facts)
        if (record := _fact_record(store, fact_id, compact=False)) is not None
    ]
    evidence = [
        record for evidence_id in sorted(selected_evidence)
        if (record := _evidence_record(store, evidence_id)) is not None
    ]
    counts = {
        "knowledge": len(knowledge_records), "support_facts": len(facts),
        "evidence": len(evidence),
    }
    return {
        **base, "requested_audit_refs": list(dict.fromkeys(audit_refs)),
        "audit_records": {
            "knowledge": knowledge_records, "facts": facts, "evidence": evidence,
        },
        "returned_counts": counts, "returned_items": sum(counts.values()),
    }


def resolve_module_knowledge_target(
    store: PrototypeStore, target_name: str
) -> dict[str, Any]:
    """Resolve an exact or unique-suffix module and its direct instance IDs."""
    if not isinstance(target_name, str) or not target_name or "\n" in target_name:
        raise StoreError("module knowledge target is invalid")
    module_kind = store.contract_id("entities", "MODULE")
    rows = list(store.connection.execute(
        """
        SELECT entity_id, qualified_name FROM entity
        WHERE kind=? AND (qualified_name=? OR qualified_name LIKE ?)
        ORDER BY qualified_name
        """,
        (module_kind, target_name, f"%.{target_name}"),
    ))
    exact = [row for row in rows if row["qualified_name"] == target_name]
    selected = exact if exact else rows
    if not selected:
        return {"status": "not_found", "candidates": []}
    if len(selected) != 1:
        return {
            "status": "ambiguous",
            "candidates": [
                {"entity_id": row["entity_id"], "qualified_name": row["qualified_name"]}
                for row in selected
            ],
        }
    target_id = selected[0]["entity_id"]
    instance_kind = store.contract_id("entities", "INSTANCE")
    contains = store.contract_id("facts", "CONTAINS")
    child_ids = sorted(row[0] for row in store.connection.execute(
        """
        SELECT f.object_id FROM fact f JOIN entity e ON e.entity_id=f.object_id
        WHERE f.subject_id=? AND f.predicate=? AND e.kind=?
        """,
        (target_id, contains, instance_kind),
    ))
    return {
        "status": "ok", "resolved_by": "exact" if exact else "suffix",
        "target_id": target_id, "target_name": selected[0]["qualified_name"],
        "direct_child_instance_ids": child_ids,
    }


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _symbol_name(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    symbol = value.get("symbol")
    if isinstance(symbol, str) and symbol:
        return symbol.split(" ", 1)[-1]
    for key in ("value", "left", "right", "expr", "child"):
        nested = _symbol_name(value.get(key))
        if nested:
            return nested
    return None


def _symbol_names(value: Any) -> list[str]:
    names: list[str] = []
    for node in _walk(value):
        if node.get("kind") == "NamedValue":
            name = _symbol_name(node)
            if name and name not in names:
                names.append(name)
    return names


def _source_location(
    node: dict[str, Any], source_by_name: dict[str, str]
) -> tuple[str | None, int | None, int | None]:
    raw = node.get("source_file") or node.get("source_file_start")
    if not isinstance(raw, str) or not raw:
        return None, None, None
    locator = source_by_name.get(Path(raw.split(":", 1)[0]).name)
    if locator is None:
        return None, None, None
    line = node.get("source_line") or node.get("source_line_start")
    end = node.get("source_line_end") or line
    return locator, line if isinstance(line, int) else None, end if isinstance(end, int) else None


def extract_slang_ast(
    store: PrototypeStore,
    ast: dict[str, Any],
    spec: dict[str, Any],
    *,
    source_hashes: dict[str, str],
) -> dict[str, Any]:
    """Populate the thin store from generic, source-backed slang JSON shapes."""
    def entity_kind(name: str) -> str:
        return store.contract_id("entities", name)

    def predicate(name: str) -> str:
        return store.contract_id("facts", name)

    files = spec.get("files")
    scopes = spec.get("scopes")
    top = spec.get("top")
    if (
        not isinstance(files, list)
        or not files
        or not isinstance(scopes, list)
        or not isinstance(top, str)
        or set(files) != set(source_hashes)
    ):
        raise StoreError("invalid source-only build spec")
    source_by_name = {Path(path).name: path for path in files}
    if len(source_by_name) != len(files):
        raise StoreError("source basenames must be unique in the Prototype")

    project_name = str(spec.get("name") or "project")
    provenance = store.add_evidence(
        kind="provenance",
        locator=f"build-spec:{project_name}",
        attrs={"top": top, "files": files},
    )
    project_id = store.add_entity(entity_kind("PROJECT"), project_name)
    config_id = store.add_entity(
        entity_kind("CONFIGURATION"), f"{project_name}@candidate", attrs={"top": top}
    )
    store.add_fact(
        project_id,
        predicate("HAS_CONFIGURATION"),
        config_id,
        producer="build-spec",
        evidence_ids=[provenance],
    )
    for path in files:
        file_id = store.add_entity(
            entity_kind("SOURCE_FILE"), path, attrs={"sha256": source_hashes[path]}
        )
        store.add_fact(
            config_id,
            predicate("USES_SOURCE"),
            file_id,
            producer="build-spec",
            evidence_ids=[provenance],
        )

    definitions = ast.get("definitions")
    if not isinstance(definitions, list):
        raise StoreError("slang AST definitions are missing")
    module_ids: dict[str, str] = {}
    module_by_source: dict[str, str] = {}
    module_evidence: dict[str, str] = {}
    for definition in definitions:
        if not isinstance(definition, dict) or definition.get("kind") != "Definition":
            continue
        name = definition.get("name")
        locator, line, end = _source_location(definition, source_by_name)
        if name not in scopes or locator is None or not isinstance(name, str):
            continue
        evidence = store.add_evidence(
            kind="source", locator=locator, start_line=line, end_line=end,
            attrs={"sha256": source_hashes[locator]},
        )
        module_id = store.add_entity(
            entity_kind("MODULE"), name, attrs={"source": locator, "line": line}
        )
        module_ids[name] = module_id
        module_by_source[locator] = name
        module_evidence[name] = evidence
        store.add_fact(
            project_id, predicate("CONTAINS"), module_id,
            producer="slang", evidence_ids=[evidence],
        )
    if top not in module_ids:
        raise StoreError("selected top definition is missing from slang AST")
    store.add_fact(
        config_id, predicate("TOP"), module_ids[top],
        producer="build-spec", evidence_ids=[provenance],
    )

    summary: dict[str, Any] = {
        "modules": len(module_ids),
        "instances": 0,
        "ports": 0,
        "signals": 0,
        "processes": 0,
        "assignments": 0,
        "expressions": 0,
        "conditions": 0,
        "unresolved": 0,
        "unsupported_shapes": [],
    }
    entity_cache: dict[tuple[str, str], str] = {}

    def source_evidence(node: dict[str, Any]) -> tuple[str, str, int | None, int | None] | None:
        locator, line, end = _source_location(node, source_by_name)
        if locator is None:
            return None
        evidence = store.add_evidence(
            kind="source", locator=locator, start_line=line, end_line=end,
            attrs={"sha256": source_hashes[locator]},
        )
        return evidence, locator, line, end

    def module_for(node: dict[str, Any]) -> str | None:
        locator, _, _ = _source_location(node, source_by_name)
        return None if locator is None else module_by_source.get(locator)

    def declared_entity(
        module: str, name: str, kind: str | None = None, **attrs: Any
    ) -> str:
        if kind is None:
            kind = entity_kind("SIGNAL")
        key = (kind, f"{module}.{name}")
        if key not in entity_cache:
            entity_cache[key] = store.add_entity(kind, key[1], attrs=attrs)
        return entity_cache[key]

    parameter_names: set[tuple[str, str]] = set()
    for node in _walk(ast.get("design", {})):
        if node.get("kind") != "Parameter" or not isinstance(node.get("name"), str):
            continue
        module = module_for(node)
        location = source_evidence(node)
        if module is None or location is None:
            continue
        parameter_names.add((module, node["name"]))
        entity = declared_entity(
            module, node["name"], entity_kind("PARAMETER"), value=node.get("value")
        )
        store.add_fact(
            module_ids[module], predicate("CONTAINS"), entity,
            producer="slang", evidence_ids=[location[0]],
        )

    def symbol_entity(module: str, name: str) -> str:
        kind = entity_kind("PARAMETER") if (module, name) in parameter_names else entity_kind("SIGNAL")
        return declared_entity(module, name, kind)

    for node in _walk(ast.get("design", {})):
        kind = node.get("kind")
        module = module_for(node)
        if kind in {"Port", "Net", "Variable"} and module and isinstance(node.get("name"), str):
            location = source_evidence(node)
            if location is None:
                continue
            declared_kind = entity_kind("PORT") if kind == "Port" else entity_kind("SIGNAL")
            entity = declared_entity(
                module, node["name"], declared_kind,
                type=node.get("type"), direction=node.get("direction"),
            )
            store.add_fact(
                module_ids[module], predicate("CONTAINS"), entity,
                producer="slang", evidence_ids=[location[0]],
            )
            summary["ports" if kind == "Port" else "signals"] += 1
        elif kind == "ProceduralBlock" and module:
            location = source_evidence(node)
            if location is None:
                continue
            name = f"{module}.@process:{location[2]}:{node.get('source_column', 0)}"
            process = store.add_entity(
                entity_kind("PROCESS"), name,
                attrs={"procedure_kind": node.get("procedureKind")},
            )
            store.add_fact(
                module_ids[module], predicate("CONTAINS"), process,
                producer="slang", evidence_ids=[location[0]],
            )
            edged = [
                event for event in _walk(node.get("body", {}))
                if event.get("kind") == "SignalEvent"
                and event.get("edge") in {"PosEdge", "NegEdge"}
                and _symbol_name(event.get("expr"))
            ]
            if edged:
                clock = symbol_entity(module, _symbol_name(edged[0]["expr"]) or "")
                store.add_fact(
                    process, predicate("CLOCKED_BY"), clock,
                    producer="slang", evidence_ids=[location[0]],
                    attrs={"rule": "first-edged-event", "edge": edged[0].get("edge")},
                )
                for event in edged[1:]:
                    reset = symbol_entity(module, _symbol_name(event["expr"]) or "")
                    store.add_fact(
                        process, predicate("RESET_BY"), reset,
                        producer="slang", evidence_ids=[location[0]],
                        attrs={"rule": "subsequent-edged-event", "edge": event.get("edge")},
                    )
            summary["processes"] += 1
        elif kind == "Conditional" and module:
            location = source_evidence(node)
            if location is None:
                continue
            name = f"{module}.@condition:{location[2]}:{node.get('source_column_start', 0)}"
            condition = store.add_entity(
                entity_kind("CONDITION"), name, attrs={"check": node.get("check")}
            )
            store.add_fact(
                module_ids[module], predicate("CONTAINS"), condition,
                producer="slang", evidence_ids=[location[0]],
            )
            for item in node.get("conditions", []):
                for symbol in _symbol_names(item.get("expr") if isinstance(item, dict) else None):
                    target = symbol_entity(module, symbol)
                    store.add_fact(
                        condition, predicate("READS"), target,
                        producer="slang", evidence_ids=[location[0]],
                    )
                    store.add_fact(
                        condition, predicate("CONTROL_DEPENDS_ON"), target,
                        producer="slang", evidence_ids=[location[0]],
                    )
            summary["conditions"] += 1
        elif kind == "Case" and "Case" not in summary["unsupported_shapes"]:
            summary["unsupported_shapes"].append("Case")

    for node in _walk(ast.get("design", {})):
        if node.get("kind") != "Assignment":
            continue
        module = module_for(node)
        location = source_evidence(node)
        left = _symbol_name(node.get("left"))
        if module is None or location is None or not left:
            continue
        assignment_name = f"{module}.@assignment:{location[2]}:{node.get('source_column_start', 0)}"
        assignment = store.add_entity(
            entity_kind("ASSIGNMENT"), assignment_name,
            attrs={"nonblocking": bool(node.get("isNonBlocking"))},
        )
        store.add_fact(
            module_ids[module], predicate("CONTAINS"), assignment,
            producer="slang", evidence_ids=[location[0]],
        )
        left_entity = symbol_entity(module, left)
        store.add_fact(
            assignment, predicate("WRITES"), left_entity,
            producer="slang", evidence_ids=[location[0]],
        )
        store.add_fact(
            assignment, predicate("DRIVES"), left_entity,
            producer="slang", evidence_ids=[location[0]],
        )
        right = node.get("right")
        right_symbols = _symbol_names(right)
        if isinstance(right, dict) and right.get("kind") not in {None, "NamedValue"}:
            expr_name = f"{module}.@expression:{location[2]}:{right.get('source_column_start', 0)}"
            expression = store.add_entity(
                entity_kind("EXPRESSION"), expr_name,
                attrs={"kind": right.get("kind"), "operator": right.get("op"), "type": right.get("type")},
            )
            store.add_fact(
                module_ids[module], predicate("CONTAINS"), expression,
                producer="slang", evidence_ids=[location[0]],
            )
            store.add_fact(
                assignment, predicate("ASSIGNED_FROM"), expression,
                producer="slang", evidence_ids=[location[0]],
            )
            for symbol in right_symbols:
                store.add_fact(
                    expression, predicate("READS"), symbol_entity(module, symbol),
                    producer="slang", evidence_ids=[location[0]],
                )
            summary["expressions"] += 1
        elif right_symbols:
            store.add_fact(
                assignment, predicate("ASSIGNED_FROM"), symbol_entity(module, right_symbols[0]),
                producer="slang", evidence_ids=[location[0]],
            )
        for symbol in right_symbols:
            target = symbol_entity(module, symbol)
            store.add_fact(
                assignment, predicate("READS"), target,
                producer="slang", evidence_ids=[location[0]],
            )
            store.add_fact(
                left_entity, predicate("DATA_DEPENDS_ON"), target,
                producer="slang", evidence_ids=[location[0]],
            )
        for selection in _walk(right):
            if selection.get("kind") not in {"RangeSelect", "ElementSelect"}:
                continue
            base = _symbol_name(selection.get("value"))
            if not base:
                continue
            selection_location = source_evidence(selection) or location
            selection_name = f"{module}.@selection:{selection_location[2]}:{selection.get('source_column_start', 0)}"
            selection_entity = store.add_entity(
                entity_kind("EXPRESSION"), selection_name,
                attrs={
                    "kind": selection.get("kind"),
                    "left": selection.get("left", {}).get("constant"),
                    "right": selection.get("right", {}).get("constant"),
                    "selector": selection.get("selector", {}).get("constant"),
                },
            )
            store.add_fact(
                selection_entity, predicate("SELECTS_BITS_FROM"), symbol_entity(module, base),
                producer="slang", evidence_ids=[selection_location[0]],
            )
        summary["assignments"] += 1

    root_instance = next(
        (
            node for node in _walk(ast.get("design", {}))
            if node.get("kind") == "Instance" and node.get("name") == top
        ),
        None,
    )
    if not isinstance(root_instance, dict):
        raise StoreError("slang AST has no elaborated top instance")
    for instance in _walk(root_instance.get("body", {}).get("members", [])):
        if instance.get("kind") != "Instance" or not isinstance(instance.get("name"), str):
            continue
        parent = module_for(instance)
        location = source_evidence(instance)
        body = instance.get("body")
        if isinstance(body, dict):
            target_module = body.get("name")
        elif isinstance(body, str) and body.strip():
            target_module = body.split(" ", 1)[-1]
        else:
            target_module = None
        if parent is None or location is None or target_module == parent:
            continue
        instance_name = f"{parent}.{instance['name']}"
        instance_id = store.add_entity(
            entity_kind("INSTANCE"), instance_name, attrs={"module": target_module}
        )
        store.add_fact(
            module_ids[parent], predicate("CONTAINS"), instance_id,
            producer="slang", evidence_ids=[location[0]],
        )
        target_id = module_ids.get(str(target_module))
        if target_id is None:
            target_id = store.add_entity(
                entity_kind("UNRESOLVED"), f"module:{target_module or 'unknown'}"
            )
        target_evidence = module_evidence.get(str(target_module), location[0])
        store.add_fact(
            instance_id, predicate("INSTANCE_OF"), target_id,
            producer="slang", evidence_ids=[location[0], target_evidence],
        )
        for connection in instance.get("connections", []):
            if not isinstance(connection, dict) or not isinstance(connection.get("port"), dict):
                continue
            port = connection["port"]
            port_name = port.get("name")
            signal_name = _symbol_name(connection.get("expr"))
            if not isinstance(port_name, str) or not signal_name:
                continue
            port_location = source_evidence(port) or location
            instance_port = store.add_entity(
                entity_kind("PORT"), f"{instance_name}.{port_name}",
                attrs={"direction": str(port.get("direction", "unknown")).lower()},
            )
            store.add_fact(
                instance_id, predicate("CONTAINS"), instance_port,
                producer="slang", evidence_ids=[location[0], port_location[0]],
            )
            store.add_fact(
                instance_port, predicate("BINDS_TO"), symbol_entity(parent, signal_name),
                producer="slang", evidence_ids=[location[0]],
                attrs={"direction": str(port.get("direction", "unknown")).lower()},
            )
        summary["instances"] += 1

    for unresolved in _walk(ast.get("design", {})):
        if unresolved.get("kind") != "UninstantiatedDef" or not isinstance(unresolved.get("name"), str):
            continue
        parent = module_for(unresolved)
        location = source_evidence(unresolved)
        definition_name = unresolved.get("definitionName")
        if parent is None or location is None or not isinstance(definition_name, str):
            continue
        instance_name = f"{parent}.{unresolved['name']}"
        instance_id = store.add_entity(
            entity_kind("INSTANCE"), instance_name,
            attrs={"module": definition_name, "resolution": "unresolved"},
        )
        boundary_id = store.add_entity(
            entity_kind("UNRESOLVED"), f"module:{definition_name}",
            attrs={"definition_name": definition_name},
        )
        store.add_fact(
            module_ids[parent], predicate("CONTAINS"), instance_id,
            producer="slang", evidence_ids=[location[0]],
        )
        store.add_fact(
            instance_id, predicate("INSTANCE_OF"), boundary_id,
            producer="slang", evidence_ids=[location[0]],
        )
        for port in unresolved.get("ports", []):
            if not isinstance(port, dict) or not isinstance(port.get("name"), str):
                continue
            signal_name = _symbol_name(port.get("expr"))
            if not signal_name:
                continue
            port_id = store.add_entity(
                entity_kind("PORT"), f"{instance_name}.{port['name']}",
                attrs={"resolution": "unresolved"},
            )
            store.add_fact(
                instance_id, predicate("CONTAINS"), port_id,
                producer="slang", evidence_ids=[location[0]],
            )
            store.add_fact(
                port_id, predicate("BINDS_TO"), symbol_entity(parent, signal_name),
                producer="slang", evidence_ids=[location[0]],
                attrs={"resolution": "unresolved"},
            )
        summary["unresolved"] += 1

    return summary


def apply_ai_overlay(store: PrototypeStore, overlay: dict[str, Any]) -> dict[str, Any]:
    """Insert a small overlay that already names physical Fact and evidence IDs."""
    if (
        not isinstance(overlay, dict)
        or set(overlay) != {"schema", "records"}
        or overlay.get("schema") != "rtl-analysis-agent/ai-overlay-v01-candidate"
        or not isinstance(overlay.get("records"), list)
    ):
        raise StoreError("AI overlay shape is invalid")
    expected = {
        "subject_id", "knowledge_type", "text", "support_fact_ids", "evidence_ids"
    }
    inserted: list[str] = []
    for record in overlay["records"]:
        if not isinstance(record, dict) or set(record) != expected:
            raise StoreError("AI overlay record shape is invalid; selectors are not supported")
        if not isinstance(record["support_fact_ids"], list) or not isinstance(record["evidence_ids"], list):
            raise StoreError("AI overlay support/evidence shape is invalid")
        inserted.append(
            store.add_ai_knowledge(
                record["subject_id"],
                record["knowledge_type"],
                record["text"],
                support_fact_ids=record["support_fact_ids"],
                evidence_ids=record["evidence_ids"],
            )
        )
    return {"inserted": len(inserted), "ai_ids": inserted}


def _evidence_rows(
    connection: sqlite3.Connection, join_table: str, owner_column: str, owner_id: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"""
        SELECT e.evidence_id, e.kind, e.locator, e.start_line, e.end_line, e.attrs_json
        FROM {join_table} j JOIN evidence e ON e.evidence_id = j.evidence_id
        WHERE j.{owner_column} = ?
        ORDER BY e.locator, e.start_line, e.end_line, e.evidence_id
        """,
        (owner_id,),
    )
    return [
        {
            "evidence_id": row["evidence_id"],
            "kind": row["kind"],
            "locator": row["locator"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "attrs": json.loads(row["attrs_json"]),
        }
        for row in rows
    ]


def _relation_rows(
    store: PrototypeStore,
    relation_name: str,
    entity_ids: set[str],
    direction: str,
) -> list[sqlite3.Row]:
    """Fetch one named relation in a fixed endpoint direction."""
    if not entity_ids:
        return []
    if direction not in {"incoming", "outgoing", "either"}:
        raise StoreError("invalid internal context relation direction")
    predicate = store.contract_id("facts", relation_name)
    endpoints = ",".join("?" for _ in entity_ids)
    if direction == "incoming":
        endpoint_clause = f"f.object_id IN ({endpoints})"
        params = [predicate, *sorted(entity_ids)]
    elif direction == "outgoing":
        endpoint_clause = f"f.subject_id IN ({endpoints})"
        params = [predicate, *sorted(entity_ids)]
    else:
        endpoint_clause = (
            f"(f.subject_id IN ({endpoints}) OR f.object_id IN ({endpoints}))"
        )
        params = [predicate, *sorted(entity_ids), *sorted(entity_ids)]
    return store.connection.execute(
        f"""
        SELECT f.*, s.kind subject_kind, s.qualified_name subject_name,
               o.kind object_kind, o.qualified_name object_name
        FROM fact f
        JOIN entity s ON s.entity_id = f.subject_id
        JOIN entity o ON o.entity_id = f.object_id
        WHERE f.predicate = ? AND {endpoint_clause}
        ORDER BY s.qualified_name, o.qualified_name, f.fact_id
        """,
        params,
    ).fetchall()


def _profile_context_rows(
    store: PrototypeStore,
    *,
    seed: sqlite3.Row,
    profile: str,
    limit: int,
) -> tuple[dict[str, sqlite3.Row], set[str], bool, dict[str, Any]]:
    """Apply four deliberately small retrieval profiles without a planner or DSL."""
    fact_rows: dict[str, sqlite3.Row] = {}
    visited = {str(seed["entity_id"])}
    truncated = False
    omitted: dict[str, int] = {}

    def add_rows(rows: list[sqlite3.Row], quota: int, omission_key: str) -> None:
        nonlocal truncated
        unique = [row for row in rows if row["fact_id"] not in fact_rows]
        policy_count = max(0, len(unique) - quota)
        if policy_count:
            omitted[omission_key] = omitted.get(omission_key, 0) + policy_count
        selected = unique[:quota]
        remaining = max(0, limit - len(fact_rows))
        if len(selected) > remaining:
            truncated = True
            selected = selected[:remaining]
        for row in selected:
            fact_rows[row["fact_id"]] = row
            visited.update((row["subject_id"], row["object_id"]))

    seed_id = str(seed["entity_id"])
    unavailable_names: list[str] = []
    notes: list[str] = []

    if profile in {"driver", "register-state"}:
        add_rows(_relation_rows(store, "DRIVES", {seed_id}, "incoming"), 4, "DRIVES")
        add_rows(_relation_rows(store, "WRITES", {seed_id}, "incoming"), 4, "WRITES")
        writers = {
            row["subject_id"]
            for row in fact_rows.values()
            if row["object_id"] == seed_id
            and row["predicate"] in {
                store.contract_id("facts", "DRIVES"),
                store.contract_id("facts", "WRITES"),
            }
        }
        add_rows(
            _relation_rows(store, "ASSIGNED_FROM", writers, "outgoing"),
            8,
            "ASSIGNED_FROM",
        )
        rhs = {
            row["object_id"]
            for row in fact_rows.values()
            if row["predicate"] == store.contract_id("facts", "ASSIGNED_FROM")
        }
        read_quota = 20 if profile == "driver" else 12
        add_rows(
            _relation_rows(store, "READS", rhs or writers, "outgoing"),
            read_quota,
            "READS",
        )
        add_rows(
            _relation_rows(store, "SELECTS_BITS_FROM", rhs, "outgoing"),
            8,
            "SELECTS_BITS_FROM",
        )
        add_rows(
            _relation_rows(store, "DATA_DEPENDS_ON", {seed_id} | writers, "outgoing"),
            8,
            "DATA_DEPENDS_ON",
        )
        if profile == "register-state":
            add_rows(
                _relation_rows(store, "CONTROL_DEPENDS_ON", writers, "outgoing"),
                8,
                "CONTROL_DEPENDS_ON",
            )
            unavailable_names = [
                name
                for name in (
                    "CLOCKED_BY",
                    "RESET_BY",
                    "RESET_VALUE",
                    "NEXT_VALUE_FROM",
                    "UPDATED_WHEN",
                    "HOLDS_WHEN",
                    "HAS_STATE",
                    "HAS_TRANSITION",
                )
                if store.contract_id("facts", name)
                not in {row["predicate"] for row in fact_rows.values()}
            ]
            notes.append(
                "No writer-to-process Fact is inferred; use writer evidence as the local Source entry point."
            )

    elif profile == "cross-module":
        add_rows(_relation_rows(store, "BINDS_TO", {seed_id}, "either"), 12, "BINDS_TO")
        port_kind = store.contract_id("entities", "PORT")
        first_ports = {
            endpoint
            for row in fact_rows.values()
            for endpoint, kind in (
                (row["subject_id"], row["subject_kind"]),
                (row["object_id"], row["object_kind"]),
            )
            if kind == port_kind
        }
        bound_non_ports = {
            endpoint
            for row in fact_rows.values()
            for endpoint, kind in (
                (row["subject_id"], row["subject_kind"]),
                (row["object_id"], row["object_kind"]),
            )
            if kind != port_kind
        }
        add_rows(
            _relation_rows(store, "BINDS_TO", bound_non_ports, "either"),
            12,
            "BINDS_TO",
        )
        ports = first_ports | {
            endpoint
            for row in fact_rows.values()
            for endpoint, kind in (
                (row["subject_id"], row["subject_kind"]),
                (row["object_id"], row["object_kind"]),
            )
            if kind == port_kind
        }
        add_rows(_relation_rows(store, "CONTAINS", ports, "incoming"), 12, "CONTAINS")
        instances = {
            row["subject_id"]
            for row in fact_rows.values()
            if row["predicate"] == store.contract_id("facts", "CONTAINS")
            and row["object_id"] in ports
        }
        add_rows(
            _relation_rows(store, "INSTANCE_OF", instances, "outgoing"),
            12,
            "INSTANCE_OF",
        )
        add_rows(_relation_rows(store, "CONTAINS", instances, "incoming"), 12, "CONTAINS")
        add_rows(_relation_rows(store, "DRIVES", {seed_id}, "incoming"), 4, "DRIVES")

    elif profile == "module-overview":
        module_kind = store.contract_id("entities", "MODULE")
        instance_kind = store.contract_id("entities", "INSTANCE")
        unresolved_kind = store.contract_id("entities", "UNRESOLVED")
        if seed["kind"] == module_kind:
            children = _relation_rows(store, "CONTAINS", {seed_id}, "outgoing")
            kinds_and_quotas = (
                ("INSTANCE", 4),
                ("PROCESS", 6),
                ("PORT", 8),
                ("SIGNAL", 6),
            )
            for kind_name, quota in kinds_and_quotas:
                kind_id = store.contract_id("entities", kind_name)
                candidates = [row for row in children if row["object_kind"] == kind_id]
                activity: dict[str, int] = {}
                if candidates:
                    ids = [row["object_id"] for row in candidates]
                    placeholders = ",".join("?" for _ in ids)
                    contains = store.contract_id("facts", "CONTAINS")
                    for entity_id, count in store.connection.execute(
                        f"""
                        SELECT endpoint, COUNT(*) FROM (
                          SELECT subject_id endpoint FROM fact
                          WHERE subject_id IN ({placeholders}) AND predicate != ?
                          UNION ALL
                          SELECT object_id endpoint FROM fact
                          WHERE object_id IN ({placeholders}) AND predicate != ?
                        ) GROUP BY endpoint
                        """,
                        [*ids, contains, *ids, contains],
                    ):
                        activity[str(entity_id)] = int(count)
                candidates.sort(
                    key=lambda row: (
                        0
                        if kind_name == "SIGNAL"
                        and re.search(r"(^|[_.$])(state|.*_state|.*reg.*)($|[_.$])", row["object_name"], re.I)
                        else 1,
                        -activity.get(row["object_id"], 0),
                        row["object_name"],
                        row["fact_id"],
                    )
                )
                add_rows(candidates, quota, kind_name)
            selected_instances = {
                row["object_id"]
                for row in fact_rows.values()
                if row["object_kind"] == instance_kind
            }
            add_rows(
                _relation_rows(store, "INSTANCE_OF", selected_instances, "outgoing"),
                6,
                "INSTANCE_OF",
            )
            selected_signals = {
                row["object_id"]
                for row in fact_rows.values()
                if row["object_kind"] == store.contract_id("entities", "SIGNAL")
            }
            driver_candidates = _relation_rows(
                store, "DRIVES", selected_signals, "incoming"
            )
            representative_drivers = []
            for signal_id in sorted(selected_signals):
                matching = [
                    row for row in driver_candidates if row["object_id"] == signal_id
                ]
                if matching:
                    representative_drivers.append(matching[0])
            if len(driver_candidates) > len(representative_drivers):
                omitted["DRIVES"] = omitted.get("DRIVES", 0) + (
                    len(driver_candidates) - len(representative_drivers)
                )
            add_rows(representative_drivers, 8, "DRIVES")
        elif seed["kind"] == instance_kind:
            add_rows(_relation_rows(store, "INSTANCE_OF", {seed_id}, "outgoing"), 1, "INSTANCE_OF")
            add_rows(_relation_rows(store, "CONTAINS", {seed_id}, "incoming"), 1, "CONTAINS")
            port_kind = store.contract_id("entities", "PORT")
            ports = [
                row
                for row in _relation_rows(store, "CONTAINS", {seed_id}, "outgoing")
                if row["object_kind"] == port_kind
            ]
            port_activity: dict[str, int] = {}
            for row in _relation_rows(
                store, "BINDS_TO", {item["object_id"] for item in ports}, "either"
            ):
                port_activity[row["subject_id"]] = port_activity.get(row["subject_id"], 0) + 1
                port_activity[row["object_id"]] = port_activity.get(row["object_id"], 0) + 1
            ports.sort(
                key=lambda row: (
                    -port_activity.get(row["object_id"], 0),
                    row["object_name"],
                    row["fact_id"],
                )
            )
            add_rows(ports, 8, "PORT")
            selected_ports = {
                row["object_id"]
                for row in fact_rows.values()
                if row["subject_id"] == seed_id and row["object_kind"] == port_kind
            }
            add_rows(
                _relation_rows(store, "BINDS_TO", selected_ports, "either"),
                8,
                "BINDS_TO",
            )
        elif seed["kind"] == unresolved_kind:
            add_rows(
                _relation_rows(store, "INSTANCE_OF", {seed_id}, "incoming"),
                12,
                "INSTANCE",
            )
        else:
            raise StoreError("module-overview profile requires a module, instance, or unresolved seed")

    included_names = sorted(
        {store.fact_contract[row["predicate"]]["name"] for row in fact_rows.values()}
    )
    unavailable = [
        {
            "name": name,
            "maturity": store.fact_contract[store.contract_id("facts", name)]["maturity"],
        }
        for name in unavailable_names
    ]
    diagnostics = {
        "included_relation_names": included_names,
        "omitted_by_policy": dict(sorted(omitted.items())),
        "unavailable_relations": unavailable,
        "notes": notes,
    }
    return fact_rows, visited, truncated, diagnostics


def query_context(
    store: PrototypeStore,
    *,
    seed_entity_id: str,
    roles: list[str],
    profile: str | None = None,
    max_depth: int = 2,
    limit: int = 100,
) -> dict[str, Any]:
    """Return a Contract-routed Context Pack with a fixed one-or-two-hop budget."""
    if not isinstance(max_depth, int) or max_depth not in {1, 2}:
        raise StoreError("context depth must be 1 or 2")
    if not isinstance(limit, int) or not 1 <= limit <= 200:
        raise StoreError("context limit must be between 1 and 200")
    if not isinstance(roles, list) or not roles or len(roles) != len(set(roles)):
        raise StoreError("context roles must be a non-empty unique list")
    known_roles = {
        role
        for group in ("facts", "ai_knowledge")
        for entry in store.contracts[group]
        for role in entry["query_roles"]
    }
    unknown = set(roles) - known_roles
    if unknown:
        raise StoreError(f"unknown context role: {sorted(unknown)[0]}")
    if profile is not None and profile not in CONTEXT_PROFILES:
        raise StoreError(f"unknown context profile: {profile}")
    seed = store.connection.execute(
        "SELECT entity_id, kind, qualified_name, attrs_json FROM entity WHERE entity_id = ?",
        (seed_entity_id,),
    ).fetchone()
    if seed is None:
        raise StoreError("exact context seed entity does not exist")

    allowed_predicates = sorted(
        entry["id"]
        for entry in store.contracts["facts"]
        if set(entry["query_roles"]) & set(roles)
    )
    allowed_ai_types = sorted(
        entry["id"]
        for entry in store.contracts["ai_knowledge"]
        if set(entry["query_roles"]) & set(roles)
    )
    retrieval_diagnostics: dict[str, Any] | None = None
    if profile is not None:
        fact_rows, visited, truncated, retrieval_diagnostics = _profile_context_rows(
            store, seed=seed, profile=profile, limit=limit
        )
    else:
        visited = {seed_entity_id}
        frontier = {seed_entity_id}
        fact_rows = {}
        truncated = False
        for _ in range(max_depth):
            if not frontier or not allowed_predicates or len(fact_rows) >= limit:
                break
            predicates = ",".join("?" for _ in allowed_predicates)
            endpoints = ",".join("?" for _ in frontier)
            params = [*allowed_predicates, *frontier, *frontier]
            rows = store.connection.execute(
                f"""
                SELECT f.*, s.kind subject_kind, s.qualified_name subject_name,
                       o.kind object_kind, o.qualified_name object_name
                FROM fact f
                JOIN entity s ON s.entity_id = f.subject_id
                JOIN entity o ON o.entity_id = f.object_id
                WHERE f.predicate IN ({predicates})
                  AND (f.subject_id IN ({endpoints}) OR f.object_id IN ({endpoints}))
                ORDER BY f.predicate, s.qualified_name, o.qualified_name, f.fact_id
                """,
                params,
            ).fetchall()
            next_frontier: set[str] = set()
            for row in rows:
                if row["fact_id"] not in fact_rows and len(fact_rows) >= limit:
                    truncated = True
                    break
                fact_rows[row["fact_id"]] = row
                next_frontier.update((row["subject_id"], row["object_id"]))
            next_frontier -= visited
            visited.update(next_frontier)
            frontier = next_frontier
            if truncated:
                break

    deterministic_facts = []
    source_anchors: dict[str, dict[str, Any]] = {}
    ordered_fact_ids = list(fact_rows) if profile is not None else sorted(fact_rows)
    for fact_id in ordered_fact_ids:
        row = fact_rows[fact_id]
        declaration = store.fact_contract[row["predicate"]]
        evidence = _evidence_rows(store.connection, "fact_evidence", "fact_id", fact_id)
        for item in evidence:
            source_anchors[item["evidence_id"]] = item
        deterministic_facts.append(
            {
                "fact_id": fact_id,
                "subject": {
                    "entity_id": row["subject_id"],
                    "kind": row["subject_kind"],
                    "qualified_name": row["subject_name"],
                },
                "predicate": row["predicate"],
                "object": {
                    "entity_id": row["object_id"],
                    "kind": row["object_kind"],
                    "qualified_name": row["object_name"],
                },
                "producer": row["producer"],
                "contract": {
                    "name": declaration["name"],
                    "category": declaration["category"],
                    "maturity": declaration["maturity"],
                },
                "attrs": json.loads(row["attrs_json"]),
                "evidence": evidence,
            }
        )

    ai_rows: list[sqlite3.Row] = []
    if allowed_ai_types and visited:
        types = ",".join("?" for _ in allowed_ai_types)
        subjects = ",".join("?" for _ in visited)
        ai_rows = store.connection.execute(
            f"""
            SELECT a.*, e.kind subject_kind, e.qualified_name subject_name
            FROM ai_knowledge a JOIN entity e ON e.entity_id = a.subject_id
            WHERE a.knowledge_type IN ({types}) AND a.subject_id IN ({subjects})
            ORDER BY a.knowledge_type, e.qualified_name, a.ai_id
            """,
            [*allowed_ai_types, *visited],
        ).fetchall()
    if len(ai_rows) > limit:
        truncated = True
        ai_rows = ai_rows[:limit]
    ai_knowledge = []
    for row in ai_rows:
        evidence = _evidence_rows(store.connection, "ai_evidence", "ai_id", row["ai_id"])
        for item in evidence:
            source_anchors[item["evidence_id"]] = item
        support = [
            item[0]
            for item in store.connection.execute(
                "SELECT fact_id FROM ai_fact_support WHERE ai_id = ? ORDER BY fact_id",
                (row["ai_id"],),
            )
        ]
        ai_knowledge.append(
            {
                "ai_id": row["ai_id"],
                "subject": {
                    "entity_id": row["subject_id"],
                    "kind": row["subject_kind"],
                    "qualified_name": row["subject_name"],
                },
                "knowledge_type": row["knowledge_type"],
                "text": row["text"],
                "support_fact_ids": support,
                "evidence": evidence,
            }
        )

    fact_budget = max(0, limit - len(ai_knowledge))
    if len(deterministic_facts) > fact_budget:
        deterministic_facts = deterministic_facts[:fact_budget]
        truncated = True
    if profile is not None:
        visited = {seed_entity_id}
        for record in deterministic_facts:
            visited.update(
                (record["subject"]["entity_id"], record["object"]["entity_id"])
            )
        visited.update(record["subject"]["entity_id"] for record in ai_knowledge)
    source_anchors = {}
    for record in [*deterministic_facts, *ai_knowledge]:
        for item in record["evidence"]:
            source_anchors[item["evidence_id"]] = item

    entity_rows = store.connection.execute(
        f"SELECT entity_id, kind, qualified_name, attrs_json FROM entity WHERE entity_id IN ({','.join('?' for _ in visited)}) ORDER BY kind, qualified_name, entity_id",
        sorted(visited),
    ).fetchall()
    entities = [
        {
            "entity_id": row["entity_id"],
            "kind": row["kind"],
            "qualified_name": row["qualified_name"],
            "attrs": json.loads(row["attrs_json"]),
        }
        for row in entity_rows
    ]
    unresolved_kind = store.contract_id("entities", "UNRESOLVED")
    boundary = [entity for entity in entities if entity["kind"] == unresolved_kind]
    result = {
        "schema": "rtl-analysis-agent/context-pack-v01-candidate",
        "request": {
            "seed_entity_id": seed_entity_id,
            "roles": roles,
            "profile": profile,
        },
        "seed": {
            "entity_id": seed["entity_id"],
            "kind": seed["kind"],
            "qualified_name": seed["qualified_name"],
            "attrs": json.loads(seed["attrs_json"]),
        },
        "entities": entities,
        "deterministic_facts": deterministic_facts,
        "ai_knowledge": ai_knowledge,
        "source_anchors": [source_anchors[key] for key in sorted(source_anchors)],
        "boundary_or_unsupported": boundary,
        "limits": {"max_depth": max_depth, "limit": limit},
        "truncated": truncated,
    }
    if retrieval_diagnostics is not None:
        result["retrieval_diagnostics"] = retrieval_diagnostics
    return result


def _file_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def build_source_candidate(
    *,
    project_name: str,
    source_revision: str,
    source_root: Path,
    spec_path: Path,
    database: Path,
    slang_executable: Path,
    slang_version: str,
    temporary_root: Path,
    ai_overlay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one source/slang-only build into a new, independent candidate."""
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise StoreError(f"source-only build spec is missing or invalid: {error}") from error
    required = {"name", "top", "files", "scopes", "slang"}
    if not isinstance(spec, dict) or not required <= set(spec):
        raise StoreError("source-only build spec shape is invalid")
    if not isinstance(spec["slang"], dict):
        raise StoreError("source-only slang configuration is invalid")
    extra_args = spec["slang"].get("extra_args", [])
    if not isinstance(extra_args, list) or not all(isinstance(item, str) for item in extra_args):
        raise StoreError("source-only slang extra arguments are invalid")
    source_root = source_root.resolve()
    source_hashes: dict[str, str] = {}
    for value in spec["files"]:
        if not isinstance(value, str) or not value or Path(value).is_absolute():
            raise StoreError("source-only file list contains an invalid path")
        path = (source_root / value).resolve()
        try:
            path.relative_to(source_root)
        except ValueError as error:
            raise StoreError("source-only file path escapes source root") from error
        if not path.is_file() or path.is_symlink():
            raise StoreError(f"source-only file is missing or unsafe: {value}")
        source_hashes[Path(value).as_posix()] = _file_hash(path)
    if not slang_executable.is_file():
        raise StoreError("locked slang executable is missing")
    temporary_root.mkdir(parents=True, exist_ok=True)
    database = database.resolve()
    if database.exists() or database.is_symlink():
        raise StoreError("candidate database already exists")

    created = False
    store: PrototypeStore | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="rtl-knowledge-prototype-", dir=temporary_root) as directory:
            ast_path = Path(directory) / "slang-ast.json"
            command = [
                str(slang_executable),
                "--quiet",
                "--top",
                str(spec["top"]),
                "--ast-json",
                str(ast_path),
                "--ast-json-source-info",
            ]
            if spec["slang"].get("allow_unknown_modules"):
                command.append("--ignore-unknown-modules")
            command.extend(extra_args)
            command.extend(str(Path(path)) for path in spec["files"])
            completed = subprocess.run(
                command,
                cwd=source_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if completed.returncode != 0 or not ast_path.is_file():
                detail = (completed.stderr or completed.stdout).strip().splitlines()[:3]
                raise StoreError("slang source-only build failed: " + " | ".join(detail))
            try:
                ast = json.loads(ast_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise StoreError("slang source-only AST is invalid") from error
            store = PrototypeStore.create(
                database,
                {
                    "project": project_name,
                    "source_revision": source_revision,
                    "spec_name": spec["name"],
                    "top": spec["top"],
                    "source_files": spec["files"],
                    "producer": {"name": "slang", "version": slang_version},
                },
            )
            created = True
            summary = extract_slang_ast(store, ast, spec, source_hashes=source_hashes)
            overlay_result = {"inserted": 0, "ai_ids": []}
            if ai_overlay is not None:
                overlay_result = apply_ai_overlay(store, ai_overlay)
            integrity = store.integrity()
            if integrity != {"integrity_check": "ok", "foreign_key_check": []}:
                raise StoreError("candidate SQLite integrity check failed")
            counts = {
                table: store.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("entity", "fact", "ai_knowledge", "ai_fact_support", "evidence")
            }
            store.close()
            store = None
        recorded_command = [
            "slang", "--top", str(spec["top"]), "--ast-json", "$TEMP/ast.json",
            "--ast-json-source-info",
        ]
        if spec["slang"].get("allow_unknown_modules"):
            recorded_command.append("--ignore-unknown-modules")
        recorded_command.extend([*extra_args, *spec["files"]])
        return {
            "schema": "rtl-analysis-agent/knowledge-prototype-build-v01-candidate",
            "producer": "slang",
            "producer_version": slang_version,
            "command": recorded_command,
            "summary": summary,
            "overlay": overlay_result,
            "counts": counts,
            "integrity": integrity,
        }
    except Exception:
        if store is not None:
            store.close()
        if created:
            database.unlink(missing_ok=True)
        raise
