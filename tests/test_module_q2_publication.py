"""Isolated, fake-review tests of the real Profile's mechanical gates."""

from __future__ import annotations

import hashlib
import json
import copy
import sqlite3
import shutil
import subprocess
import threading
from unittest import mock
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/rtl-pack/lib"))
import rtl_knowledge_prototype as p  # noqa: E402
import rtl_manual_v6 as manual  # noqa: E402
from tests.test_manual_v6_build1 import ManualV6Build1Test  # noqa: E402


def digest(value: object) -> str:
    return hashlib.sha256(p._canonical(value).encode()).hexdigest()


def artifact(path: Path, value: dict) -> str:
    path.write_text(p._canonical(value) + "\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ModuleQ2PublicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ManualV6Build1Test("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_real_profile_has_five_bounded_shapes_and_no_source_only_ready(self) -> None:
        contract, raw_sha, purpose = p._load_publication_contract(p.MODULE_Q2_PUBLICATION_PATH)
        self.assertEqual(raw_sha, p.MODULE_Q2_PUBLICATION_SHA256)
        self.assertEqual(purpose, "REAL_MODULE_Q2")
        self.assertEqual(set(contract["claim_shapes"]), {
            "module-q2.module-role@2", "module-q2.module-function@2",
            "module-q2.module-interface@2", "module-q2.fsm-boundary@2",
            "module-q2.flow-step@2",
        })
        declared = {item["id"] for item in p.load_contracts()["ai_knowledge"]}
        for shape_id, shape in contract["claim_shapes"].items():
            obligation = contract["obligations"][shape_id]
            self.assertEqual(obligation["claim_shapes"], [shape_id])
            self.assertEqual(obligation["target"], shape["target"])
            self.assertTrue(shape["source_only_publication"])
            self.assertFalse(obligation["source_only_ready"])
            self.assertIn(shape["knowledge_type"], declared)
            self.assertTrue(set(obligation["required_fields"]) <= set(shape["fields"]))

    def test_real_profile_requires_decomposed_fields_and_port_references(self) -> None:
        contract, _, _ = p._load_publication_contract(p.MODULE_Q2_PUBLICATION_PATH)
        shapes = contract["claim_shapes"]
        self.assertTrue({"location", "role"} <= set(shapes["module-q2.module-role@2"]["fields"]))
        self.assertTrue({"function", "condition", "effect", "exceptions"} <=
                        set(shapes["module-q2.module-function@2"]["fields"]))
        signals = shapes["module-q2.module-interface@2"]["fields"]["signal_refs"]
        self.assertEqual(signals["item_kind"], "entity_ref")
        self.assertTrue({"group", "why_key"} <= set(shapes["module-q2.module-interface@2"]["fields"]))
        self.assertTrue({"state_names", "reset_path", "scope_limit"} <=
                        set(shapes["module-q2.fsm-boundary@2"]["fields"]))
        self.assertTrue({"entry", "step", "condition", "effect", "boundary"} <=
                        set(shapes["module-q2.flow-step@2"]["fields"]))

    def test_formal_role_populates_existing_overview_column(self) -> None:
        store, overlay, root, approved, ai_id = self._prepared()
        store.close()
        store = p.PrototypeStore.open_publication(self.fixture.database,
            publication_contract=p.MODULE_Q2_PUBLICATION_PATH)
        store.publish_reviewed_semantic_overlay(
            overlay, source_root=self.fixture.source_root, review_root=root,
            publication_contract=p.MODULE_Q2_PUBLICATION_PATH,
            approval_file="A.json", approved_sha256=approved,
        )
        store.close()
        model = manual.load_formal_q2(self.fixture.database, self.fixture.source_root)
        module = next(item for item in model["modules"] if item["qualified_name"] == "root_a")
        self.assertEqual(module["module_page_v2"]["overview"][0]["ai_id"], ai_id)
        self.assertEqual(module["module_page_v2"]["overview"],
                         module["formal_q2"]["sections"]["overview"])
        manual.build_site(model, self.fixture.root / "readable")
        html = (self.fixture.root / "readable" / module["output_path"]).read_text()
        self.assertIn('id="overview"', html)
        self.assertNotIn("<pre>", html)

    def test_scope_fact_does_not_make_source_only_ready(self) -> None:
        store, overlay, root, approved, ai_id = self._prepared()
        fact_id = store.connection.execute(
            "SELECT fact_id FROM fact WHERE predicate=? AND object_id=? LIMIT 1",
            (store.contract_id("facts", "INSTANCE_OF"),
             overlay["claims"][0]["target_subject_id"]),
        ).fetchone()[0]
        overlay["claims"][0]["fact_support"] = [{
            "fact_id": fact_id, "quality": "SCOPE_ONLY", "fields_proven": [],
        }]
        overlay["coverage"][0]["closure_status"] = "READY"
        overlay["coverage"][0]["limitations"] = []
        batch = {key: value for key, value in overlay.items() if key != "review"}
        review = overlay["review"]
        review["input_sha256"] = digest(batch)
        review["evidence_id"] = p._identity(
            "ev", "provenance", review["locator"], None, None,
            p._canonical(p._publication_review_attrs(batch, review)),
        )
        store.close()
        store = p.PrototypeStore.open_publication(self.fixture.database,
            publication_contract=p.MODULE_Q2_PUBLICATION_PATH)
        with self.assertRaisesRegex(p.StoreError, "Source-only READY"):
            store.publish_reviewed_semantic_overlay(
                overlay, source_root=self.fixture.source_root, review_root=root,
                publication_contract=p.MODULE_Q2_PUBLICATION_PATH,
                approval_file="A.json", approved_sha256=approved,
            )
        store.close()

    def _prepared(self):
        f = self.fixture
        store, ids = f._make_store(real_q2=True)
        scope = f._register(
            store, mode="ACTUAL_INSTANCE_ROOT",
            roots=[f._root(ids["root_a"], store.contract_id("entities", "INSTANCE"))],
            source_paths=["rtl/root_a.sv", "rtl/leaf.sv"],
        )
        contract, _, _ = p._load_publication_contract(p.MODULE_Q2_PUBLICATION_PATH)
        candidate, publication = store._publication_review_context(scope, contract)
        module_id = store.connection.execute(
            "SELECT entity_id FROM entity WHERE kind=? AND qualified_name='root_a'",
            (store.contract_id("entities", "MODULE"),),
        ).fetchone()[0]
        source_path = "rtl/root_a.sv"
        raw = (f.source_root / source_path).read_bytes()
        span_hash = hashlib.sha256(raw.decode().splitlines()[0].encode()).hexdigest()
        attrs = {"sha256": span_hash, "file_sha256": hashlib.sha256(raw).hexdigest(),
                 "hash_kind": "utf8-line-span-v1"}
        source = {
            "evidence_id": p._identity("ev", "source", source_path, 1, 1, p._canonical(attrs)),
            "path": source_path, "span": "L1", "content_sha256": span_hash,
            "file_sha256": attrs["file_sha256"], "current": True,
        }
        gap = {
            "schema": "rtl-analysis-agent/fact-gap-v1", "gap_kind": "COMPILER_FACT_ABSENT",
            "missing_fact_content": ["module role and inference basis"],
            "affected_fields": ["inference_basis", "role"],
            "affected_obligations": ["module-q2.module-role@2"],
            "direct_source_rationale": "The one-line synthetic module declaration bounds this role.",
            "remaining_limitations": ["Synthetic fixture has no design intent assessment."],
        }
        claim = {
            "target_kind": "SUBJECT", "behavior_key": None,
            "target_subject_id": module_id, "claim_key": "root-role",
            "knowledge_type": "rtl.ai/1:module-role",
            "text": "Synthetic root module instantiates a leaf.",
            "structured_schema_id": "module-q2.module-role@2",
            "structured_fields": {"location": "fixture.root_a", "role": "synthetic root wrapper", "inference_basis": "module declaration and leaf instance"},
            "applicability": {"instance_id": [ids["root_a"]]}, "limitations": [gap],
        }
        ai_id = "ai:" + digest(claim)
        claim.update(ai_id=ai_id, fact_support=[], direct_source_evidence=[source])
        coverage = {
            "target_kind": "SUBJECT", "target_behavior_key": None,
            "target_subject_id": module_id, "obligation_id": "module-q2.module-role@2",
            "profile": contract["profile"], "vocabulary": contract["vocabulary"],
            "claim_members": [ai_id], "claim_set_digest": digest([ai_id]),
            "applicability": {"instance_id": [ids["root_a"]]}, "closure_status": "PARTIAL",
            "limitations": ["Synthetic fixture does not assess complete role."],
            "conflicts": [], "unresolved": [],
        }
        batch = {"schema": p.PUBLICATION_REVIEW_SCHEMA, "candidate": candidate,
                 "contract": publication, "behavior": None, "claims": [claim], "coverage": [coverage]}
        batch_sha = digest(batch)
        review_root = f.root / "review"
        review_root.mkdir()
        whole = {
            "schema": "rtl-analysis-agent/module-q2-complete-input-v1", "candidate": candidate,
            "contract": publication, "profile_sha256": p.MODULE_Q2_PUBLICATION_SHA256,
            "intake": [source], "batches": [batch],
            "dispositions": [{"goal_id": goal, "status": "COVERED", "claim_ids": [ai_id],
                              "reason": "mechanical synthetic fixture"} for goal in p.MODULE_Q2_GOALS],
        }
        whole_sha = artifact(review_root / "I.json", whole)
        independent = {
            "schema": "rtl-analysis-agent/module-q2-independent-review-v1",
            "decision": "VERIFIED", "input_sha256": whole_sha, "batch_sha256": [batch_sha],
            "reviewer": "fake-independent-reviewer", "generator": "fixture-generator", "findings": [],
        }
        review_sha = artifact(review_root / "R.json", independent)
        approval = {
            "schema": "rtl-analysis-agent/module-q2-approval-v1", "decision": "APPROVED",
            "input_path": "I.json", "input_sha256": whole_sha,
            "review_path": "R.json", "review_sha256": review_sha,
            "profile_sha256": p.MODULE_Q2_PUBLICATION_SHA256,
            "candidate": candidate, "intake_sha256": digest([source]),
            "batch_sha256": [batch_sha], "goal_ids": list(p.MODULE_Q2_GOALS),
        }
        approval_sha = artifact(review_root / "A.json", approval)
        review = {"status": "VERIFIED", "input_sha256": batch_sha,
                  "artifact_sha256": review_sha, "reviewer": independent["reviewer"],
                  "generator": independent["generator"], "locator": "R.json"}
        review["evidence_id"] = p._identity(
            "ev", "provenance", "R.json", None, None,
            p._canonical(p._publication_review_attrs(batch, review)),
        )
        return store, {**batch, "review": review}, review_root, approval_sha, ai_id

    def _reapprove_one(self, overlay: dict, root: Path) -> str:
        """Create only synthetic fixture approval for a changed reviewed batch."""
        claim = overlay["claims"][0]
        claim["ai_id"] = "ai:" + digest({key: value for key, value in claim.items()
            if key not in {"ai_id", "fact_support", "direct_source_evidence"}})
        coverage = overlay["coverage"][0]
        coverage["claim_members"] = [claim["ai_id"]]
        coverage["claim_set_digest"] = digest(coverage["claim_members"])
        batch = {key: value for key, value in overlay.items() if key != "review"}
        I = json.loads((root / "I.json").read_text())
        I["batches"] = [batch]
        for item in I["dispositions"]:
            item["claim_ids"] = [claim["ai_id"]]
        whole_sha = artifact(root / "I.json", I)
        R = json.loads((root / "R.json").read_text())
        R.update(input_sha256=whole_sha, batch_sha256=[digest(batch)])
        review_sha = artifact(root / "R.json", R)
        A = json.loads((root / "A.json").read_text())
        A.update(input_sha256=whole_sha, review_sha256=review_sha,
                 batch_sha256=[digest(batch)])
        approved = artifact(root / "A.json", A)
        review = overlay["review"]
        review.update(input_sha256=digest(batch), artifact_sha256=review_sha)
        review["evidence_id"] = p._identity(
            "ev", "provenance", review["locator"], None, None,
            p._canonical(p._publication_review_attrs(batch, review)),
        )
        return approved

    def _synthetic_reviewed_batches(self, root: Path, batches: list[dict]) -> tuple[str, list[dict]]:
        """Bind self-owned synthetic batches; never use this for a real review."""
        batches = sorted(batches, key=digest)
        whole = json.loads((root / "I.json").read_text())
        whole["batches"] = batches
        all_ids = sorted({claim["ai_id"] for batch in batches for claim in batch["claims"]})
        whole["dispositions"][0]["claim_ids"] = all_ids
        input_sha = artifact(root / "I.json", whole)
        independent = json.loads((root / "R.json").read_text())
        independent.update(input_sha256=input_sha, batch_sha256=[digest(batch) for batch in batches])
        review_sha = artifact(root / "R.json", independent)
        approval = json.loads((root / "A.json").read_text())
        approval.update(input_sha256=input_sha, review_sha256=review_sha,
                        batch_sha256=independent["batch_sha256"])
        approved = artifact(root / "A.json", approval)
        reviewed = []
        for batch in batches:
            review = {"status": "VERIFIED", "input_sha256": digest(batch),
                      "artifact_sha256": review_sha, "reviewer": independent["reviewer"],
                      "generator": independent["generator"], "locator": "R.json"}
            review["evidence_id"] = p._identity(
                "ev", "provenance", "R.json", None, None,
                p._canonical(p._publication_review_attrs(batch, review)),
            )
            reviewed.append({**batch, "review": review})
        return approved, reviewed

    def test_complete_input_rejects_duplicate_current_slice_before_first_write(self) -> None:
        store, overlay, root, _, _ = self._prepared()
        first = {key: value for key, value in overlay.items() if key != "review"}
        second = copy.deepcopy(first)
        claim = second["claims"][0]
        claim["claim_key"] = "other-role"
        claim["text"] = "Other bounded role of the same synthetic module."
        claim["ai_id"] = "ai:" + digest({key: value for key, value in claim.items()
            if key not in {"ai_id", "fact_support", "direct_source_evidence"}})
        coverage = second["coverage"][0]
        coverage["claim_members"] = [claim["ai_id"]]
        coverage["claim_set_digest"] = digest(coverage["claim_members"])
        approved, reviewed = self._synthetic_reviewed_batches(root, [first, second])
        baseline_evidence = store.connection.execute("SELECT count(*) FROM evidence").fetchone()[0]
        store.close()
        writer = p.PrototypeStore.open_publication(self.fixture.database,
            publication_contract=p.MODULE_Q2_PUBLICATION_PATH)
        try:
            with self.assertRaisesRegex(p.StoreError, "duplicate CURRENT coverage slice"):
                writer.publish_reviewed_semantic_overlay(
                    reviewed[0], source_root=self.fixture.source_root, review_root=root,
                    publication_contract=p.MODULE_Q2_PUBLICATION_PATH,
                    approval_file="A.json", approved_sha256=approved,
                )
            for table in ("ai_knowledge", "ai_evidence", "behavior_object",
                          "obligation_coverage", "model_metadata"):
                if table == "model_metadata":
                    self.assertIsNone(writer.connection.execute(
                        "SELECT 1 FROM model_metadata WHERE key='publication_bundle'").fetchone())
                else:
                    self.assertEqual(writer.connection.execute(
                        f"SELECT count(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(writer.connection.execute(
                "SELECT count(*) FROM evidence").fetchone()[0], baseline_evidence)
        finally:
            writer.close()

    def test_complete_input_allows_same_target_in_distinct_instance_slices(self) -> None:
        store, overlay, root, _, _ = self._prepared()
        first = {key: value for key, value in overlay.items() if key != "review"}
        module_id = first["claims"][0]["target_subject_id"]
        actual = first["claims"][0]["applicability"]["instance_id"][0]
        evidence_id = store.connection.execute(
            "SELECT evidence_id FROM fact_evidence LIMIT 1").fetchone()[0]
        second_actual = store.add_entity(store.contract_id("entities", "INSTANCE"),
                                         "fixture.root_a.second", attrs={"name": "second"})
        store.add_fact(actual, store.contract_id("facts", "CONTAINS"), second_actual,
                       producer="slang", evidence_ids=[evidence_id])
        store.add_fact(second_actual, store.contract_id("facts", "INSTANCE_OF"), module_id,
                       producer="slang", evidence_ids=[evidence_id])
        second = copy.deepcopy(first)
        claim = second["claims"][0]
        claim["claim_key"] = "role-second-instance"
        claim["applicability"] = {"instance_id": [second_actual]}
        claim["ai_id"] = "ai:" + digest({key: value for key, value in claim.items()
            if key not in {"ai_id", "fact_support", "direct_source_evidence"}})
        coverage = second["coverage"][0]
        coverage["applicability"] = {"instance_id": [second_actual]}
        coverage["claim_members"] = [claim["ai_id"]]
        coverage["claim_set_digest"] = digest(coverage["claim_members"])
        approved, reviewed = self._synthetic_reviewed_batches(root, [first, second])
        store.close()
        writer = p.PrototypeStore.open_publication(self.fixture.database,
            publication_contract=p.MODULE_Q2_PUBLICATION_PATH)
        try:
            for batch in reviewed:
                writer.publish_reviewed_semantic_overlay(
                    batch, source_root=self.fixture.source_root, review_root=root,
                    publication_contract=p.MODULE_Q2_PUBLICATION_PATH,
                    approval_file="A.json", approved_sha256=approved,
                )
            rows = writer.connection.execute(
                "SELECT applicability_json FROM obligation_coverage WHERE validity='CURRENT' ORDER BY applicability_json"
            ).fetchall()
            self.assertEqual([json.loads(row[0]) for row in rows],
                             sorted([first["coverage"][0]["applicability"],
                                     second["coverage"][0]["applicability"]], key=p._canonical))
        finally:
            writer.close()

    def test_full_synthetic_rebatched_package_public_cli_and_mapping(self) -> None:
        store, overlay, root, _, _ = self._prepared()
        contract, _, _ = p._load_publication_contract(p.MODULE_Q2_PUBLICATION_PATH)
        role = overlay["claims"][0]
        module_id = role["target_subject_id"]
        actual = role["applicability"]["instance_id"][0]
        source = role["direct_source_evidence"]
        evidence_id = store.connection.execute(
            "SELECT evidence_id FROM fact_evidence LIMIT 1").fetchone()[0]
        port = store.add_entity(store.contract_id("entities", "PORT"),
                                "fixture.root_a.ready_o", attrs={"direction": "Out"})
        store.add_fact(actual, store.contract_id("facts", "CONTAINS"), port,
                       producer="slang", evidence_ids=[evidence_id])

        def make_claim(name: str, fields: dict, behavior_key: str | None = None) -> dict:
            shape_id = f"module-q2.{name}@2"
            shape = contract["claim_shapes"][shape_id]
            gap = {"schema": "rtl-analysis-agent/fact-gap-v1",
                   "gap_kind": "COMPILER_FACT_ABSENT",
                   "missing_fact_content": [f"synthetic {name} content Fact absent"],
                   "affected_fields": sorted(shape["fact_content"]["required_fields"]),
                   "affected_obligations": [shape_id],
                   "direct_source_rationale": "bounded synthetic source line",
                   "remaining_limitations": [f"bounded {name} claim"]}
            claim = {"target_kind": "SUBJECT" if behavior_key is None else "BEHAVIOR",
                     "behavior_key": behavior_key, "claim_key": f"synthetic-{name}-{fields.get('function', fields.get('why_key', behavior_key))}",
                     "knowledge_type": shape["knowledge_type"], "text": f"Synthetic {name}: {fields}",
                     "structured_schema_id": shape_id, "structured_fields": fields,
                     "applicability": {"instance_id": [actual]}, "limitations": [gap]}
            if behavior_key is None:
                claim["target_subject_id"] = module_id
            claim["ai_id"] = "ai:" + digest(claim)
            claim.update(fact_support=[], direct_source_evidence=source)
            return claim

        def make_coverage(claims: list[dict], name: str, behavior_key: str | None = None) -> dict:
            members = sorted(claim["ai_id"] for claim in claims)
            return {"target_kind": "SUBJECT" if behavior_key is None else "BEHAVIOR",
                    "target_behavior_key": behavior_key,
                    "target_subject_id": module_id if behavior_key is None else None,
                    "obligation_id": f"module-q2.{name}@2", "profile": contract["profile"],
                    "vocabulary": contract["vocabulary"], "claim_members": members,
                    "claim_set_digest": digest(members), "applicability": {"instance_id": [actual]},
                    "closure_status": "PARTIAL", "limitations": [f"bounded {name} claim"],
                    "conflicts": [], "unresolved": []}

        functions = [make_claim("module-function", {
            "function": f"bounded function {n}", "condition": f"condition {n}",
            "effect": f"effect {n}", "exceptions": [f"exception {n}"],
            "inference_basis": "synthetic source line"}) for n in range(5)]
        interfaces = [make_claim("module-interface", {
            "group": "fixture group", "why_key": f"interface reason {n}",
            "signal_refs": [port], "inference_basis": "synthetic source line"}) for n in range(3)]
        subject_claims = sorted([role, *functions, *interfaces], key=lambda claim: claim["ai_id"])
        subject_coverage = [make_coverage([role], "module-role"),
                            make_coverage(functions, "module-function"),
                            make_coverage(interfaces, "module-interface")]
        subject_batch = {key: value for key, value in overlay.items() if key != "review"}
        subject_batch["claims"] = subject_claims
        subject_batch["coverage"] = sorted(subject_coverage, key=lambda row: row["obligation_id"])
        behavior_batches = []
        for name, key, kind, fields in (
            ("fsm-boundary", "synthetic-c01", "FSM",
             {"state_names": ["S0", "S1"], "reset_path": "reset to S0",
              "scope_limit": "fixture only", "inference_basis": "synthetic source line",
              "participants": [], "related_subject_ids": [module_id]}),
            ("flow-step", "synthetic-f01", "FLOW",
             {"entry": "entry 1", "step": "step 1", "condition": "condition 1",
              "effect": "effect 1", "boundary": "fixture only", "inference_basis": "synthetic source line",
              "participants": [actual], "related_subject_ids": [module_id]}),
            ("flow-step", "synthetic-f02", "FLOW",
             {"entry": "entry 2", "step": "step 2", "condition": "condition 2",
              "effect": "effect 2", "boundary": "fixture only", "inference_basis": "synthetic source line",
              "participants": [actual], "related_subject_ids": [module_id]}),
        ):
            claim = make_claim(name, fields, key)
            behavior = {"behavior_type": kind, "behavior_key": key,
                        "scope_id": store.analysis_scope()["scope_id"],
                        "ownership_mode": "SUBJECT", "owner_subject_id": module_id,
                        "applicability": {"instance_id": [actual]},
                        "participants": fields["participants"],
                        "related_subject_ids": fields["related_subject_ids"]}
            behavior_batches.append({**{field: subject_batch[field]
                                        for field in ("schema", "candidate", "contract")},
                                     "behavior": behavior, "claims": [claim],
                                     "coverage": [make_coverage([claim], name, key)]})
        baseline_evidence = store.connection.execute("SELECT count(*) FROM evidence").fetchone()[0]
        store.close()
        f = self.fixture
        (f.root / "analysis").mkdir()
        (f.root / "rtl-project.json").write_text(json.dumps({
            "schema": "rtl-analysis-agent/project-v1", "name": "synthetic-multi-claim",
            "source": "source", "analysis": "analysis", "revision": None,
            "entry": "rtl/root_a.sv",
        }))
        command = [sys.executable, str(ROOT / "src/rtl-pack/bin/rtl"), "model"]
        old_grouping = []
        for claim in subject_claims:
            name = claim["structured_schema_id"].removeprefix("module-q2.").removesuffix("@2")
            old_grouping.append({**{field: subject_batch[field]
                                    for field in ("schema", "candidate", "contract", "behavior")},
                                 "claims": [claim], "coverage": [make_coverage([claim], name)]})
        bad_approval, bad_reviewed = self._synthetic_reviewed_batches(
            root, [*old_grouping, *behavior_batches])
        failed = subprocess.run(command + [
            "q2-publish", "--project", str(f.root), "--model", "candidate.sqlite",
            "--approval-root", "review", "--approval-file", "A.json",
            "--approved-sha256", bad_approval,
            "--batch-sha256", bad_reviewed[0]["review"]["input_sha256"],
        ], cwd=ROOT, text=True, capture_output=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("duplicate CURRENT coverage slice", failed.stderr + failed.stdout)
        with sqlite3.connect(f"file:{f.database}?mode=ro", uri=True) as unchanged:
            for table in ("ai_knowledge", "ai_evidence", "behavior_object", "obligation_coverage"):
                self.assertEqual(unchanged.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(unchanged.execute(
                "SELECT count(*) FROM evidence").fetchone()[0], baseline_evidence)
            self.assertIsNone(unchanged.execute(
                "SELECT 1 FROM model_metadata WHERE key='publication_bundle'").fetchone())
        approved, reviewed = self._synthetic_reviewed_batches(
            root, [subject_batch, *behavior_batches])
        def publish(batch: dict) -> dict:
            result = subprocess.run(command + [
                "q2-publish", "--project", str(f.root), "--model", "candidate.sqlite",
                "--approval-root", "review", "--approval-file", "A.json",
                "--approved-sha256", approved,
                "--batch-sha256", batch["review"]["input_sha256"],
            ], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            return json.loads(result.stdout)["publication"]
        self.assertEqual(len(reviewed), 4)
        self.assertEqual(sum(len(batch["claims"]) for batch in reviewed), 12)
        self.assertEqual(sum(len(batch["coverage"]) for batch in reviewed), 6)
        self.assertFalse(publish(reviewed[0])["idempotent"])
        with self.assertRaisesRegex(p.StoreError, "missing a batch"):
            manual.load_formal_q2(f.database, f.source_root)
        for batch in reviewed[1:]:
            self.assertFalse(publish(batch)["idempotent"])
        self.assertTrue(publish(reviewed[0])["idempotent"])
        reader = sqlite3.connect(f"file:{f.database}?mode=ro", uri=True)
        try:
            self.assertEqual(reader.execute("SELECT count(*) FROM ai_knowledge").fetchone()[0], 12)
            self.assertEqual(reader.execute("SELECT count(*) FROM behavior_object").fetchone()[0], 3)
            self.assertEqual(reader.execute("SELECT count(*) FROM obligation_coverage").fetchone()[0], 6)
            for row in subject_coverage:
                found = reader.execute(
                    "SELECT coverage_id FROM obligation_coverage WHERE target_kind='SUBJECT' AND obligation_id=?",
                    (row["obligation_id"],)).fetchone()
                self.assertIsNotNone(found)
                members = [item[0] for item in reader.execute(
                    "SELECT ai_id FROM obligation_coverage_claim WHERE coverage_id=? ORDER BY ai_id", (found[0],))]
                self.assertEqual(members, row["claim_members"])
        finally:
            reader.close()
        model = manual.load_formal_q2(f.database, f.source_root)
        module = next(item for item in model["modules"] if item["qualified_name"] == "root_a")
        self.assertEqual(len(module["formal_q2"]["claims"]), 12)
        mapped = subprocess.run(command + [
            "manual-build", "--project", str(f.root), "--model", "candidate.sqlite",
            "--output", "analysis/formal-multi", "--source-viewer", "full", "--formal-q2",
        ], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(mapped.returncode, 0, mapped.stderr + mapped.stdout)
        manifest = json.loads((f.root / "analysis/formal-multi/manifest.json").read_text())
        self.assertEqual(manifest["formal_q2"]["approval_sha256"], approved)

    def test_interface_refs_reject_missing_wrong_kind_actual_and_scope(self) -> None:
        store, overlay, root, _, _ = self._prepared()
        actual = overlay["claims"][0]["applicability"]["instance_id"][0]
        members = store._publication_members(store.analysis_scope())
        source_evidence = store.connection.execute(
            "SELECT evidence_id FROM evidence WHERE kind='source' LIMIT 1"
        ).fetchone()[0]
        valid = store.add_entity(store.contract_id("entities", "PORT"),
                                 "fixture.root_a.valid_o", attrs={"direction": "Out"})
        store.add_fact(actual, store.contract_id("facts", "CONTAINS"), valid,
                       producer="slang", evidence_ids=[source_evidence])
        wrong_actual = store.connection.execute(
            "SELECT entity_id FROM entity WHERE qualified_name='fixture.root_a.u_leaf.in_data'"
        ).fetchone()[0]
        wrong_kind = overlay["claims"][0]["target_subject_id"]
        out_of_scope = store.connection.execute(
            "SELECT entity_id FROM entity WHERE qualified_name='fixture.root_b'"
        ).fetchone()[0]
        for ref in ("e:" + "0" * 64, wrong_kind, wrong_actual, out_of_scope):
            with self.subTest(ref=ref), self.assertRaisesRegex(p.StoreError, "PORT|actual|interface"):
                store._publication_interface_ports({"signal_refs": [ref]}, [actual],
                                                   store._publication_members(store.analysis_scope()))
        self.assertEqual(store._publication_interface_ports(
            {"signal_refs": [valid]}, [actual], store._publication_members(store.analysis_scope())
        ), [{"entity_id": valid, "name": "valid_o", "direction": "Out"}])
        claim = overlay["claims"][0]
        claim.update(structured_schema_id="module-q2.module-interface@2",
                     structured_fields={"group": "synthetic output", "why_key": "valid handshake",
                                        "signal_refs": [valid], "inference_basis": "source"})
        claim["limitations"][0].update(
            affected_fields=["group", "inference_basis", "why_key"],
            affected_obligations=["module-q2.module-interface@2"],
        )
        coverage = overlay["coverage"][0]
        coverage["obligation_id"] = "module-q2.module-interface@2"
        store.close()
        writer = p.PrototypeStore.open_publication(self.fixture.database,
            publication_contract=p.MODULE_Q2_PUBLICATION_PATH)
        try:
            for ref in ("e:" + "0" * 64, wrong_kind, wrong_actual, out_of_scope):
                invalid = copy.deepcopy(overlay)
                invalid["claims"][0]["structured_fields"]["signal_refs"] = [ref]
                invalid_approval = self._reapprove_one(invalid, root)
                with self.subTest(writer_ref=ref), self.assertRaisesRegex(
                    p.StoreError, "relation field|interface signal"
                ):
                    writer.publish_reviewed_semantic_overlay(
                        invalid, source_root=self.fixture.source_root, review_root=root,
                        publication_contract=p.MODULE_Q2_PUBLICATION_PATH,
                        approval_file="A.json", approved_sha256=invalid_approval,
                    )
            approved = self._reapprove_one(overlay, root)
            writer.publish_reviewed_semantic_overlay(
                overlay, source_root=self.fixture.source_root, review_root=root,
                publication_contract=p.MODULE_Q2_PUBLICATION_PATH,
                approval_file="A.json", approved_sha256=approved,
            )
        finally:
            writer.close()
        model = manual.load_formal_q2(self.fixture.database, self.fixture.source_root)
        module = next(item for item in model["modules"] if item["qualified_name"] == "root_a")
        self.assertEqual(module["module_page_v2"]["key_interfaces"][0]["signals"][0]["name"], "valid_o")

    def test_required_condition_effect_exception_fields_cannot_disappear(self) -> None:
        store, overlay, root, _, _ = self._prepared()
        store.close()
        writer = p.PrototypeStore.open_publication(self.fixture.database,
            publication_contract=p.MODULE_Q2_PUBLICATION_PATH)
        try:
            for omitted in ("condition", "effect", "exceptions"):
                invalid = copy.deepcopy(overlay)
                claim = invalid["claims"][0]
                claim["structured_schema_id"] = "module-q2.module-function@2"
                contract, _, _ = p._load_publication_contract(p.MODULE_Q2_PUBLICATION_PATH)
                claim["knowledge_type"] = contract["claim_shapes"][claim["structured_schema_id"]]["knowledge_type"]
                claim["structured_fields"] = {
                    "function": "bounded function", "condition": "C", "effect": "E",
                    "exceptions": ["X"], "inference_basis": "source line",
                }
                del claim["structured_fields"][omitted]
                invalid["review"]["input_sha256"] = digest({
                    key: value for key, value in invalid.items() if key != "review"
                })
                with self.subTest(omitted=omitted), self.assertRaisesRegex(
                    p.StoreError, "required field is missing"
                ):
                    writer.publish_reviewed_semantic_overlay(
                        invalid, source_root=self.fixture.source_root, review_root=root,
                        publication_contract=p.MODULE_Q2_PUBLICATION_PATH,
                    )
        finally:
            writer.close()

    def test_five_shapes_fill_original_columns_from_reviewed_fields(self) -> None:
        store, original, root, _, role_id = self._prepared()
        module_id = original["claims"][0]["target_subject_id"]
        actual = original["claims"][0]["applicability"]["instance_id"][0]
        source = original["claims"][0]["direct_source_evidence"][0]
        evidence = store.connection.execute(
            "SELECT evidence_id FROM evidence WHERE kind='source' LIMIT 1"
        ).fetchone()[0]
        port = store.add_entity(store.contract_id("entities", "PORT"),
                                "fixture.root_a.ready_o", attrs={"direction": "Out"})
        store.add_fact(actual, store.contract_id("facts", "CONTAINS"), port,
                       producer="slang", evidence_ids=[evidence])
        contract, _, _ = p._load_publication_contract(p.MODULE_Q2_PUBLICATION_PATH)
        variants = [
            ("module-function", {"function": "A bounded synthetic function explains a deliberately long control description across the available reading column without implying complete timing, progress, or priority guarantees for the rest of the design.", "condition": "COND_SENTINEL",
                                 "effect": "EFFECT_SENTINEL", "exceptions": ["EXCEPTION_SENTINEL"],
                                 "inference_basis": "source line"}, None),
            ("module-interface", {"group": "fixture group", "why_key": "WHY_SENTINEL",
                                  "signal_refs": [port], "inference_basis": "source line"}, None),
            ("fsm-boundary", {"state_names": ["S0", "S1"], "reset_path": "RESET_PATH_SENTINEL",
                              "scope_limit": "FSM_LIMIT_SENTINEL", "inference_basis": "source line",
                              "participants": [], "related_subject_ids": [module_id]}, "FSM"),
            ("flow-step", {"entry": "ENTRY_SENTINEL", "step": "STEP_SENTINEL",
                           "condition": "FLOW_COND_SENTINEL", "effect": "FLOW_EFFECT_SENTINEL",
                           "boundary": "FLOW_BOUNDARY_SENTINEL", "inference_basis": "source line",
                           "participants": [actual], "related_subject_ids": [module_id]}, "FLOW"),
        ]
        batches = [{key: value for key, value in original.items() if key != "review"}]
        ids = {"module-role": role_id}
        for name, fields, behavior_type in variants:
            shape_id = f"module-q2.{name}@2"
            shape = contract["claim_shapes"][shape_id]
            behavior_key = f"synthetic-{name}" if behavior_type else None
            behavior = ({"behavior_type": behavior_type, "behavior_key": behavior_key,
                         "scope_id": store.analysis_scope()["scope_id"],
                         "ownership_mode": "SUBJECT", "owner_subject_id": module_id,
                         "applicability": {"instance_id": [actual]},
                         "participants": fields["participants"],
                         "related_subject_ids": fields["related_subject_ids"]}
                        if behavior_type else None)
            claim = {"target_kind": "BEHAVIOR" if behavior_type else "SUBJECT",
                     "behavior_key": behavior_key, "claim_key": name,
                     "knowledge_type": shape["knowledge_type"], "text": name,
                     "structured_schema_id": shape_id, "structured_fields": fields,
                     "applicability": {"instance_id": [actual]},
                     "limitations": [{"schema": "rtl-analysis-agent/fact-gap-v1",
                                      "gap_kind": "COMPILER_FACT_ABSENT",
                                      "missing_fact_content": ["synthetic content Fact absent"],
                                      "affected_fields": sorted(shape["fact_content"]["required_fields"]),
                                      "affected_obligations": [shape_id],
                                      "direct_source_rationale": "source line",
                                      "remaining_limitations": ["bounded synthetic claim"]}]}
            if not behavior_type:
                claim["target_subject_id"] = module_id
            ai_id = "ai:" + digest(claim)
            ids[name] = ai_id
            claim.update(ai_id=ai_id, fact_support=[], direct_source_evidence=[source])
            coverage = {"target_kind": claim["target_kind"],
                        "target_behavior_key": behavior_key,
                        "target_subject_id": module_id if not behavior_type else None,
                        "obligation_id": shape_id, "profile": contract["profile"],
                        "vocabulary": contract["vocabulary"], "claim_members": [ai_id],
                        "claim_set_digest": digest([ai_id]), "applicability": {"instance_id": [actual]},
                        "closure_status": "PARTIAL", "limitations": ["bounded synthetic claim"],
                        "conflicts": [], "unresolved": []}
            batches.append({"schema": p.PUBLICATION_REVIEW_SCHEMA,
                            "candidate": original["candidate"], "contract": original["contract"],
                            "behavior": behavior, "claims": [claim], "coverage": [coverage]})
        batches.sort(key=digest)
        I = json.loads((root / "I.json").read_text())
        I["batches"] = batches
        goal_shape = {"OV01": "module-role", "OV02": "module-function",
                      **{f"R{i:02d}": "module-function" for i in range(1, 5)},
                      **{f"I{i:02d}": "module-interface" for i in range(1, 4)},
                      "C01": "fsm-boundary", "F01": "flow-step", "F02": "flow-step"}
        for item in I["dispositions"]:
            item["claim_ids"] = [ids[goal_shape[item["goal_id"]]]]
        whole_sha = artifact(root / "I.json", I)
        batch_sha = [digest(batch) for batch in batches]
        R = json.loads((root / "R.json").read_text())
        R.update(input_sha256=whole_sha, batch_sha256=batch_sha)
        review_sha = artifact(root / "R.json", R)
        A = json.loads((root / "A.json").read_text())
        A.update(input_sha256=whole_sha, review_sha256=review_sha,
                 batch_sha256=batch_sha)
        approved = artifact(root / "A.json", A)
        store.close()
        writer = p.PrototypeStore.open_publication(self.fixture.database,
            publication_contract=p.MODULE_Q2_PUBLICATION_PATH)
        try:
            for batch in batches:
                review = {"status": "VERIFIED", "input_sha256": digest(batch),
                          "artifact_sha256": review_sha, "reviewer": R["reviewer"],
                          "generator": R["generator"], "locator": "R.json"}
                review["evidence_id"] = p._identity(
                    "ev", "provenance", "R.json", None, None,
                    p._canonical(p._publication_review_attrs(batch, review)),
                )
                writer.publish_reviewed_semantic_overlay(
                    {**batch, "review": review}, source_root=self.fixture.source_root,
                    review_root=root, publication_contract=p.MODULE_Q2_PUBLICATION_PATH,
                    approval_file="A.json", approved_sha256=approved,
                )
        finally:
            writer.close()
        model = manual.load_formal_q2(self.fixture.database, self.fixture.source_root)
        module = next(item for item in model["modules"] if item["qualified_name"] == "root_a")
        projection = module["formal_q2"]
        sections = projection["sections"]
        self.assertEqual(module["structured_detail"]["semantic"], projection)
        self.assertEqual([len(items) for items in sections.values()], [1, 1, 1, 1, 1])
        self.assertEqual(sections["responsibilities"][0]["condition"], "COND_SENTINEL")
        self.assertEqual(sections["responsibilities"][0]["effect"], "EFFECT_SENTINEL")
        self.assertEqual(sections["responsibilities"][0]["exceptions"], ["EXCEPTION_SENTINEL"])
        self.assertEqual(sections["key_interfaces"][0]["signals"],
                         [{"entity_id": port, "name": "ready_o", "direction": "Out"}])
        self.assertEqual(sections["control_summary"][0]["reset_path"], "RESET_PATH_SENTINEL")
        self.assertEqual(sections["important_flows"][0]["effect"], "FLOW_EFFECT_SENTINEL")
        self.assertEqual(sections["control_summary"][0]["related_subjects"], ["root_a"])
        self.assertEqual(sections["important_flows"][0]["participants"], ["fixture.root_a"])
        manifest = manual.build_site(model, self.fixture.root / "five-shape-site", source_viewer="full")
        self.assertEqual(manifest["formal_q2"]["modules"][module["entity_id"]], projection)
        html = (self.fixture.root / "five-shape-site" / module["output_path"]).read_text()
        for expected in ("COND_SENTINEL", "EFFECT_SENTINEL", "EXCEPTION_SENTINEL",
                         "WHY_SENTINEL", "ready_o", "RESET_PATH_SENTINEL",
                         "FLOW_COND_SENTINEL", "FLOW_EFFECT_SENTINEL", "PUBLISHED / PARTIAL",
                         "参与实体：</strong>fixture.root_a", "关联实体：</strong>root_a"):
            self.assertIn(expected, html)
        for anchor in ('id="overview"', 'id="responsibilities"', 'id="key-interfaces"',
                       'id="control"', 'id="flows"'):
            self.assertIn(anchor, html)
        self.assertIn("fixture.root_a ·", html)
        self.assertIn("#L1", html)
        source_page = next((self.fixture.root / "five-shape-site/source").rglob("root_a.sv.html"))
        self.assertIn('id="L1"', source_page.read_text())
        # The public Mapping consumes only the approved SQLite and locked Source.
        shutil.rmtree(root)
        (self.fixture.root / "analysis").mkdir()
        (self.fixture.root / "rtl-project.json").write_text(json.dumps({
            "schema": "rtl-analysis-agent/project-v1", "name": "synthetic-five-shape",
            "source": "source", "analysis": "analysis", "revision": None,
            "entry": "rtl/root_a.sv",
        }))
        command = [sys.executable, str(ROOT / "src/rtl-pack/bin/rtl"), "model"]
        result = subprocess.run(command + [
            "manual-build", "--project", str(self.fixture.root),
            "--model", "candidate.sqlite", "--output", "analysis/formal-five",
            "--source-viewer", "full", "--formal-q2",
        ], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        cli_site = self.fixture.root / "analysis/formal-five"
        self.assertEqual(json.loads((cli_site / "manifest.json").read_text())["formal_q2"]["modules"][module["entity_id"]], projection)
        self.assertIn("RTL Q2 知识手册", (cli_site / module["output_path"]).read_text())

    def test_real_profile_requires_external_approval_and_projects_bundle(self) -> None:
        store, overlay, root, approved, ai_id = self._prepared()
        store.close()
        store = p.PrototypeStore.open_publication(self.fixture.database,
            publication_contract=p.MODULE_Q2_PUBLICATION_PATH)
        with self.assertRaisesRegex(p.StoreError, "Controller-pinned approval"):
            store.publish_reviewed_semantic_overlay(
                overlay, source_root=self.fixture.source_root, review_root=root,
                publication_contract=p.MODULE_Q2_PUBLICATION_PATH,
            )
        with self.assertRaisesRegex(p.StoreError, "approval anchor"):
            store.publish_reviewed_semantic_overlay(
                overlay, source_root=self.fixture.source_root, review_root=root,
                publication_contract=p.MODULE_Q2_PUBLICATION_PATH,
                approval_file="A.json", approved_sha256="0" * 64,
            )
        result = store.publish_reviewed_semantic_overlay(
            overlay, source_root=self.fixture.source_root, review_root=root,
            publication_contract=p.MODULE_Q2_PUBLICATION_PATH,
            approval_file="A.json", approved_sha256=approved,
        )
        self.assertFalse(result["idempotent"])
        self.assertTrue(store.publish_reviewed_semantic_overlay(
            overlay, source_root=self.fixture.source_root, review_root=root,
            publication_contract=p.MODULE_Q2_PUBLICATION_PATH,
            approval_file="A.json", approved_sha256=approved,
        )["idempotent"])
        store.close()
        model = manual.load_formal_q2(self.fixture.database, self.fixture.source_root)
        module = next(item for item in model["modules"] if item["qualified_name"] == "root_a")
        self.assertEqual(module["formal_q2"]["claims"][0]["ai_id"], ai_id)
        self.assertEqual(module["module_page_v2"]["semantic_maturity"], "PUBLISHED_PARTIAL")
        self.assertEqual(module["structured_detail"]["semantic"], module["formal_q2"])
        manifest = manual.build_site(model, self.fixture.root / "formal", source_viewer="full")
        self.assertEqual(manifest["formal_q2"]["modules"][module["entity_id"]], module["formal_q2"])
        html = (self.fixture.root / "formal" / module["output_path"]).read_text()
        self.assertIn(ai_id, html)
        self.assertIn("PUBLISHED_PARTIAL", html)
        self.assertIn("#L1", html)
        self.assertIn("rtl/root_a.sv:L1-L1", html)
        module["formal_q2"]["sections"]["overview"][0]["role"] = "<script>alert(1)</script>"
        manual.build_site(model, self.fixture.root / "escaped", source_viewer="locator")
        escaped = (self.fixture.root / "escaped" / module["output_path"]).read_text()
        self.assertNotIn("<script>alert(1)</script>", escaped)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", escaped)
        source_path = self.fixture.source_root / "rtl/root_a.sv"
        source_path.write_bytes(source_path.read_bytes() + b"// changed after load\n")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            manual.build_site(model, self.fixture.root / "stale-locator", source_viewer="locator")

    def test_two_module_bundle_blocks_mapping_until_second_batch(self) -> None:
        store, first, root, _, first_id = self._prepared()
        f = self.fixture
        leaf_module = store.connection.execute(
            "SELECT entity_id FROM entity WHERE kind=? AND qualified_name='leaf'",
            (store.contract_id("entities", "MODULE"),),
        ).fetchone()[0]
        leaf_actual = store.connection.execute(
            "SELECT entity_id FROM entity WHERE kind=? AND qualified_name='fixture.root_a.u_leaf'",
            (store.contract_id("entities", "INSTANCE"),),
        ).fetchone()[0]
        second = copy.deepcopy(first)
        second.pop("review")
        claim = second["claims"][0]
        claim["target_subject_id"] = leaf_module
        claim["claim_key"] = "leaf-role"
        claim["text"] = "Synthetic leaf has one width parameter."
        claim["structured_fields"]["role"] = "synthetic parameterized leaf"
        claim["applicability"] = {"instance_id": [leaf_actual]}
        raw = (f.source_root / "rtl/leaf.sv").read_bytes()
        span_hash = hashlib.sha256(raw.decode().splitlines()[0].encode()).hexdigest()
        attrs = {"sha256": span_hash, "file_sha256": hashlib.sha256(raw).hexdigest(),
                 "hash_kind": "utf8-line-span-v1"}
        claim["direct_source_evidence"] = [{
            "evidence_id": p._identity("ev", "source", "rtl/leaf.sv", 1, 1, p._canonical(attrs)),
            "path": "rtl/leaf.sv", "span": "L1", "content_sha256": span_hash,
            "file_sha256": attrs["file_sha256"], "current": True,
        }]
        claim["ai_id"] = "ai:" + digest({key: value for key, value in claim.items()
            if key not in {"ai_id", "fact_support", "direct_source_evidence"}})
        coverage = second["coverage"][0]
        coverage["target_subject_id"] = leaf_module
        coverage["applicability"] = {"instance_id": [leaf_actual]}
        coverage["claim_members"] = [claim["ai_id"]]
        coverage["claim_set_digest"] = digest([claim["ai_id"]])
        I = json.loads((root / "I.json").read_text())
        I["batches"] = sorted([I["batches"][0], second], key=digest)
        I["intake"] = sorted([I["intake"][0], claim["direct_source_evidence"][0]],
                             key=lambda item: item["evidence_id"])
        for item in I["dispositions"][6:]:
            item["claim_ids"] = [claim["ai_id"]]
        whole_sha = artifact(root / "I.json", I)
        batch_sha = [digest(batch) for batch in I["batches"]]
        R = json.loads((root / "R.json").read_text())
        R["input_sha256"] = whole_sha
        R["batch_sha256"] = batch_sha
        review_sha = artifact(root / "R.json", R)
        A = json.loads((root / "A.json").read_text())
        A.update(input_sha256=whole_sha, review_sha256=review_sha,
                 intake_sha256=digest(I["intake"]), batch_sha256=batch_sha)
        approved = artifact(root / "A.json", A)

        def reviewed(batch: dict) -> dict:
            review = {"status": "VERIFIED", "input_sha256": digest(batch),
                      "artifact_sha256": review_sha, "reviewer": R["reviewer"],
                      "generator": R["generator"], "locator": "R.json"}
            review["evidence_id"] = p._identity(
                "ev", "provenance", "R.json", None, None,
                p._canonical(p._publication_review_attrs(batch, review)),
            )
            return {**batch, "review": review}

        store.close()
        writer = p.PrototypeStore.open_publication(f.database,
            publication_contract=p.MODULE_Q2_PUBLICATION_PATH)
        try:
            writer.publish_reviewed_semantic_overlay(
                reviewed(I["batches"][0]), source_root=f.source_root, review_root=root,
                publication_contract=p.MODULE_Q2_PUBLICATION_PATH,
                approval_file="A.json", approved_sha256=approved,
            )
            with self.assertRaisesRegex(p.StoreError, "missing a batch"):
                manual.load_formal_q2(f.database, f.source_root)
            writer.publish_reviewed_semantic_overlay(
                reviewed(I["batches"][1]), source_root=f.source_root, review_root=root,
                publication_contract=p.MODULE_Q2_PUBLICATION_PATH,
                approval_file="A.json", approved_sha256=approved,
            )
        finally:
            writer.close()
        model = manual.load_formal_q2(f.database, f.source_root)
        projected = {item["qualified_name"]: item["formal_q2"] for item in model["modules"]}
        self.assertEqual({item["ai_id"] for item in projected["root_a"]["claims"]}, {first_id})
        self.assertEqual({item["ai_id"] for item in projected["leaf"]["claims"]}, {claim["ai_id"]})

    def test_same_span_on_changed_file_cannot_rebind_source_revision(self) -> None:
        store, overlay, root, approved, ai_id = self._prepared()
        source = overlay["claims"][0]["direct_source_evidence"][0]
        path = self.fixture.source_root / source["path"]
        path.write_bytes(path.read_bytes() + b"// changed outside reviewed span\n")
        source["file_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        attrs = {"sha256": source["content_sha256"],
                 "file_sha256": source["file_sha256"], "hash_kind": "utf8-line-span-v1"}
        source["evidence_id"] = p._identity("ev", "source", source["path"], 1, 1, p._canonical(attrs))
        with self.assertRaisesRegex(p.StoreError, "frozen Stage1 file"):
            store._publication_source(source, self.fixture.source_root, store.analysis_scope(), allow_missing=True)
        store.close()

    def test_public_cli_writer_to_formal_mapping_on_synthetic_project(self) -> None:
        store, overlay, root, approved, ai_id = self._prepared()
        store.close()
        f = self.fixture
        (f.root / "analysis").mkdir()
        (f.root / "rtl-project.json").write_text(json.dumps({
            "schema": "rtl-analysis-agent/project-v1", "name": "synthetic-alt-module",
            "source": "source", "analysis": "analysis", "revision": None,
            "entry": "rtl/root_a.sv",
        }))
        command = [sys.executable, str(ROOT / "src/rtl-pack/bin/rtl"), "model"]
        published = subprocess.run(command + [
            "q2-publish", "--project", str(f.root), "--model", "candidate.sqlite",
            "--approval-root", "review", "--approval-file", "A.json",
            "--approved-sha256", approved, "--batch-sha256", overlay["review"]["input_sha256"],
        ], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(published.returncode, 0, published.stderr + published.stdout)
        self.assertEqual(json.loads(published.stdout)["publication"]["ai_ids"], [ai_id])
        mapped = subprocess.run(command + [
            "manual-build", "--project", str(f.root), "--model", "candidate.sqlite",
            "--output", "analysis/formal-q2", "--source-viewer", "full", "--formal-q2",
        ], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(mapped.returncode, 0, mapped.stderr + mapped.stdout)
        manifest = json.loads((f.root / "analysis/formal-q2/manifest.json").read_text())
        self.assertEqual(manifest["formal_q2"]["approval_sha256"], approved)
        self.assertIn(ai_id, (f.root / "analysis/formal-q2" /
                              manifest["page_map"][overlay["claims"][0]["target_subject_id"]]).read_text())

    def test_changed_complete_input_and_unapproved_extra_row_are_rejected(self) -> None:
        store, overlay, root, approved, ai_id = self._prepared()
        store.close()
        writer = p.PrototypeStore.open_publication(self.fixture.database,
            publication_contract=p.MODULE_Q2_PUBLICATION_PATH)
        input_path = root / "I.json"
        original = input_path.read_bytes()
        changed = json.loads(original)
        changed["dispositions"][0]["reason"] = "changed after review"
        artifact(input_path, changed)
        with self.assertRaisesRegex(p.StoreError, "complete input identity"):
            writer.publish_reviewed_semantic_overlay(
                overlay, source_root=self.fixture.source_root, review_root=root,
                publication_contract=p.MODULE_Q2_PUBLICATION_PATH,
                approval_file="A.json", approved_sha256=approved,
            )
        input_path.write_bytes(original)
        writer.publish_reviewed_semantic_overlay(
            overlay, source_root=self.fixture.source_root, review_root=root,
            publication_contract=p.MODULE_Q2_PUBLICATION_PATH,
            approval_file="A.json", approved_sha256=approved,
        )
        row = writer.connection.execute("SELECT * FROM ai_knowledge WHERE ai_id=?", (ai_id,)).fetchone()
        extra = list(row)
        extra[0] = "ai:unapproved-extra"
        extra[5] = "Unapproved extra semantic row"
        extra[3] = "unapproved-extra"
        writer.connection.execute(
            "INSERT INTO ai_knowledge VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", extra,
        )
        writer.connection.commit()
        writer.close()
        with self.assertRaisesRegex(p.StoreError, "extra semantic rows"):
            manual.load_formal_q2(self.fixture.database, self.fixture.source_root)

    def test_formal_mapping_holds_one_snapshot_across_concurrent_publish(self) -> None:
        store, overlay, root, approved, ai_id = self._prepared()
        store.close()
        paused = threading.Event()
        release = threading.Event()
        writer_started = threading.Event()
        mapping_error: list[BaseException] = []
        writer_error: list[BaseException] = []
        original_load = manual.load_backbone

        def pause_after_backbone(*args, **kwargs):
            model = original_load(*args, **kwargs)
            paused.set()
            self.assertTrue(release.wait(5))
            return model

        def map_once():
            try:
                manual.load_formal_q2(self.fixture.database, self.fixture.source_root)
            except BaseException as error:
                mapping_error.append(error)

        def publish_once():
            writer_started.set()
            try:
                writer = p.PrototypeStore.open_publication(self.fixture.database,
                    publication_contract=p.MODULE_Q2_PUBLICATION_PATH)
                try:
                    writer.publish_reviewed_semantic_overlay(
                        overlay, source_root=self.fixture.source_root, review_root=root,
                        publication_contract=p.MODULE_Q2_PUBLICATION_PATH,
                        approval_file="A.json", approved_sha256=approved,
                    )
                finally:
                    writer.close()
            except BaseException as error:
                writer_error.append(error)

        # Set the durable mode before the reader takes its snapshot.
        writer = p.PrototypeStore.open_publication(self.fixture.database,
            publication_contract=p.MODULE_Q2_PUBLICATION_PATH)
        writer.close()
        with mock.patch.object(manual, "load_backbone", side_effect=pause_after_backbone):
            reader_thread = threading.Thread(target=map_once)
            reader_thread.start()
            self.assertTrue(paused.wait(5))
            writer_thread = threading.Thread(target=publish_once)
            writer_thread.start()
            self.assertTrue(writer_started.wait(5))
            release.set()
            reader_thread.join(5)
            writer_thread.join(5)
        self.assertFalse(reader_thread.is_alive())
        self.assertFalse(writer_thread.is_alive())
        self.assertEqual(writer_error, [])
        self.assertEqual(len(mapping_error), 1)
        self.assertIsInstance(mapping_error[0], p.StoreError)
        self.assertIn("publication_bundle", str(mapping_error[0]))
        current = manual.load_formal_q2(self.fixture.database, self.fixture.source_root)
        self.assertEqual(current["ai_count"], 1)


if __name__ == "__main__":
    unittest.main()
