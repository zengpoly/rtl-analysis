"""Bounded serving projection for the approved v0.2 MODULE Q2 publication."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from rtl_knowledge_prototype import (
    MODULE_Q2_PUBLICATION_PATH, Q2_FIFO_V2_PUBLICATION_PATH, PrototypeStore, StoreError,
    _canonical, _identity, _load_publication_contract,
)
from rtl_manual_v6 import SourceSafetyError, load_backbone
from rtl_source_path import source_path


def _empty(reason: str, obligations: list[str], *, accessed: bool = True) -> dict[str, Any]:
    return {
        "route": "BYPASS", "reasons": [reason], "candidate_accessed": accessed,
        "response_view": "answer", "returned_items": 0,
        "returned_counts": {"knowledge": 0, "support_facts": 0, "evidence": 0},
        "knowledge": [], "support_facts": [], "evidence": [], "truncated": False,
        "coverage": {"requested_obligations": obligations, "ready_obligations": [],
                     "partial_obligations": [], "missing_obligations": obligations},
    }


def _source_anchor(item: dict[str, Any], sources: dict[str, dict[str, Any]],
                   project_root: Path, source_root: Path) -> dict[str, Any]:
    source = sources.get(item["locator"])
    attrs = item["attrs"]
    first, last = item["start_line"], item["end_line"]
    if (source is None or not source["valid"] or not isinstance(first, int)
        or not isinstance(last, int) or first < 1 or last < first):
        raise SourceSafetyError("query Source Evidence is stale or invalid")
    lines = source["validated_bytes"].decode("utf-8").splitlines()
    span_hash = hashlib.sha256("\n".join(lines[first - 1:last]).encode()).hexdigest()
    expected_hash = span_hash if attrs.get("hash_kind") == "utf8-line-span-v1" else source["expected_sha256"]
    if (last > len(lines) or attrs.get("sha256") != expected_hash):
        raise SourceSafetyError("query Source Evidence span is stale")
    path, path_root = source_path(project_root, source_root, item["locator"])
    return {"evidence_id": item["evidence_id"], "path": path, "path_root": path_root,
            "locator": item["locator"],
            "start_line": first, "end_line": last, "sha256": attrs["sha256"]}


def route_v02(
    store: PrototypeStore, *, database: Path, project_root: Path, source_root: Path, target_id: str,
    build_sha256: str, actual_id: str | None, obligations: list[str],
    request_kind: str,
) -> dict[str, Any]:
    """Use the Mapping trust chain within one readonly snapshot; never publish or repair."""
    requested = list(dict.fromkeys(obligations))
    if store.schema_version != "v0.2":
        return _empty("unsupported-schema", requested)
    if store.metadata("build_sha256") != build_sha256:
        return _empty("build-identity-mismatch", requested)
    if not requested and request_kind != "deterministic-instance-of":
        return _empty("obligation-required", requested)
    if request_kind not in {"module-understanding", "deterministic-instance-of"}:
        return _empty("unsupported-request-kind", requested)
    semantic = store.metadata("semantic_publication_contract")
    path = (Q2_FIFO_V2_PUBLICATION_PATH if semantic.get("purpose") == "REAL_Q2_FIFO"
            else MODULE_Q2_PUBLICATION_PATH)
    contract, _, purpose = _load_publication_contract(path)
    if request_kind == "module-understanding" and (
        purpose not in {"REAL_MODULE_Q2", "REAL_Q2_FIFO"}
        or any(item not in contract["obligations"] for item in requested)
    ):
        return _empty("unsupported-obligation", requested)
    if store.connection.in_transaction:
        raise StoreError("query requires an idle readonly Store")
    store.connection.execute("BEGIN")
    try:
        model = load_backbone(database, source_root, _store=store)
        # Stage1 deterministic facts have their own Build/Source/Evidence trust
        # chain. A semantic publication is required only for semantic requests.
        bundle = (store.audit_real_publication_bundle(contract)
                  if request_kind == "module-understanding" else None)
        modules = {item["entity_id"] for item in model["modules"]}
        if target_id not in modules:
            return _empty("target-not-covered", requested)
        actuals = [item for item in model["actuals"] if item["definition_id"] == target_id]
        if actual_id is None:
            if len(actuals) != 1:
                return _empty("actual-required-or-ambiguous", requested)
            actual = actuals[0]
        else:
            matches = [item for item in actuals if item["entity_id"] == actual_id]
            if len(matches) != 1:
                return _empty("actual-target-mismatch", requested)
            actual = matches[0]
        sources = {item["qualified_name"]: item for item in model["sources"]}
        if request_kind == "deterministic-instance-of":
            if requested:
                return _empty("unsupported-obligation", requested)
            facts = [item for item in model["facts"] if item["subject_id"] == actual["entity_id"]
                     and item["object_id"] == target_id
                     and item["predicate"] == store.contract_id("facts", "INSTANCE_OF")]
            if len(facts) != 1:
                return _empty("deterministic-fact-missing", requested)
            fact = facts[0]
            if (fact["producer"] != "slang" or fact["fact_id"] != _identity(
                "f", fact["subject_id"], fact["predicate"], fact["object_id"],
                _canonical(fact["attrs"]),
            )):
                return _empty("deterministic-fact-invalid", requested)
            rows = store.connection.execute(
                "SELECT e.* FROM fact_evidence fe JOIN evidence e ON e.evidence_id=fe.evidence_id "
                "WHERE fe.fact_id=? ORDER BY e.evidence_id", (fact["fact_id"],),
            ).fetchall()
            if not rows or any(row["kind"] != "source" for row in rows):
                return _empty("deterministic-evidence-missing", requested)
            if any(row["attrs_json"] != _canonical(json.loads(row["attrs_json"]))
                   or row["evidence_id"] != _identity(
                       "ev", row["kind"], row["locator"], row["start_line"],
                       row["end_line"], row["attrs_json"])
                   or json.loads(row["attrs_json"]).get("configuration_identity") != build_sha256
                   for row in rows):
                return _empty("deterministic-evidence-invalid", requested)
            anchors = [_source_anchor({"evidence_id": row["evidence_id"], "locator": row["locator"],
                                       "start_line": row["start_line"], "end_line": row["end_line"],
                                       "attrs": json.loads(row["attrs_json"])}, sources,
                                      project_root, source_root) for row in rows]
            return {"route": "DIRECT", "reasons": ["deterministic-fact-supported"],
                    "candidate_accessed": True, "response_view": "answer", "returned_items": 1,
                    "returned_counts": {"knowledge": 0, "support_facts": 1, "evidence": len(anchors)},
                    "knowledge": [], "support_facts": [{"fact_id": fact["fact_id"],
                        "predicate": fact["predicate"], "subject_id": actual["entity_id"],
                        "subject_name": actual["qualified_name"], "object_id": target_id,
                        "object_name": next(x["qualified_name"] for x in model["modules"] if x["entity_id"] == target_id)}],
                    "evidence": anchors, "truncated": False,
                    "coverage": {"requested_obligations": [], "ready_obligations": [],
                                 "partial_obligations": [], "missing_obligations": []},
                    "actual_id": actual["entity_id"]}
        selected = store._read_published_semantic_snapshot({"instance_id": actual["entity_id"]}, contract)
        coverage = [item for item in selected["coverage"] if item["obligation_id"] in requested
                    and (item.get("target_subject_id") == target_id or
                         item.get("owner_subject_id") in {None, target_id, actual["entity_id"]})]
        used_ids = {ai_id for item in coverage for ai_id in item["claim_ids"]}
        claims = [item for item in selected["claims"] if item["ai_id"] in used_ids]
        anchors = {item["evidence_id"]: _source_anchor(item, sources, project_root, source_root)
                   for claim in claims for item in claim["source_evidence"]}
        returned = []
        for claim in claims:
            review_row = store.connection.execute(
                "SELECT kind,attrs_json FROM evidence WHERE evidence_id=?",
                (claim["review_evidence_id"],),
            ).fetchone()
            if review_row is None or review_row["kind"] != "provenance":
                raise StoreError("claim review provenance is missing")
            review = json.loads(review_row["attrs_json"])
            if (review.get("status") != "VERIFIED"
                or review.get("input_sha256") not in bundle["batch_sha256"]
                or review.get("artifact_sha256") != bundle["review_sha256"]):
                raise StoreError("claim review does not match the approved batch")
            returned.append({key: claim[key] for key in (
                "ai_id", "claim_key", "text", "structured_schema_id", "structured_fields",
                "limitations", "fact_support", "relations", "review_evidence_id",
            )} | {"source_evidence_ids": [item["evidence_id"] for item in claim["source_evidence"]],
                  "review": {"status": "VERIFIED", "evidence_id": claim["review_evidence_id"],
                             "input_sha256": review["input_sha256"],
                             "artifact_sha256": review["artifact_sha256"]},
                  "support_kind": ("content-fact-and-source" if any(
                      item["support_quality"] in {"EXACT", "PARTIAL"} and item["fields_proven"]
                      for item in claim["fact_support"]) else
                      "scope-fact-and-source" if claim["fact_support"] else "source-only")})
        by_obligation = {ob: [item for item in coverage if item["obligation_id"] == ob] for ob in requested}
        ready = [ob for ob, rows in by_obligation.items() if rows and all(row["closure_status"] == "READY" for row in rows)]
        partial = [ob for ob, rows in by_obligation.items() if rows and ob not in ready]
        missing = [ob for ob, rows in by_obligation.items() if not rows]
        gaps = [{"obligation_id": ob, "status": "PARTIAL", "limitations": [
            gap for row in by_obligation[ob] for gap in row["limitations"]],
            "conflicts": [gap for row in by_obligation[ob] for gap in row["conflicts"]],
            "unresolved": [gap for row in by_obligation[ob] for gap in row["unresolved"]]}
            for ob in partial] + [{"obligation_id": ob, "status": "MISSING"} for ob in missing]
        route = "DIRECT" if ready and len(ready) == len(requested) else "CONTEXT" if returned else "BYPASS"
        return {"route": route, "reasons": ["obligations-ready" if route == "DIRECT" else
                "partial-coverage" if route == "CONTEXT" else "no-applicable-coverage"],
                "candidate_accessed": True, "response_view": "answer", "returned_items": len(returned),
                "returned_counts": {"knowledge": len(returned), "support_facts": 0, "evidence": len(anchors)},
                "knowledge": returned, "support_facts": [], "evidence": list(anchors.values()),
                "truncated": False, "coverage": {"requested_obligations": requested,
                 "ready_obligations": ready, "partial_obligations": partial,
                 "missing_obligations": missing, "gaps": gaps,
                 "records": [{key: item[key] for key in ("coverage_id", "obligation_id",
                             "closure_status", "claim_ids", "review_evidence_id", "applicability")}
                             for item in coverage]},
                "actual_id": actual["entity_id"]}
    except (StoreError, SourceSafetyError, ValueError, KeyError, TypeError):
        return _empty("candidate-invalid-or-source-stale", requested)
    finally:
        store.connection.rollback()
