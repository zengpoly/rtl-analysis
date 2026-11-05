"""Optional integration regression against the approved, unchanged Q2 publication."""

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/rtl-pack/lib"))
from rtl_knowledge_prototype import PrototypeStore
from rtl_query_v02 import route_v02
CLI = ROOT / "src/rtl-pack/bin/rtl"
BUILD = "0994de23d7f61b3829504686d35a167ffc48bd8597fa2328fd4bbc8a30898866"
ROLE = "module-q2.module-role@2"


@unittest.skipUnless(os.environ.get("RTL_QUERY_Q2_PROJECT"), "approved Q2 Project not installed")
class ApprovedQ2QueryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = Path(os.environ["RTL_QUERY_Q2_PROJECT"])
        cls.database = cls.project / "analysis/model/rtl-knowledge-module-q2-v4-published.sqlite"
        if not cls.database.is_file():
            raise unittest.SkipTest("approved Q2 database not installed")

    def query(self, *extra, model=None, build=BUILD):
        command = [str(CLI), "model", "query", "--project", str(self.project),
                   "--model", str(model or self.database.relative_to(self.project)), "--view", "module-knowledge",
                   "--target-name", "ibex_controller", "--build-sha256", build, *extra]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_real_q2_is_context_with_explicit_partial_gap(self):
        result = self.query("--obligation", ROLE)
        self.assertEqual(result["route"], "CONTEXT")
        self.assertEqual(result["coverage"]["partial_obligations"], [ROLE])
        self.assertEqual(result["coverage"]["ready_obligations"], [])
        self.assertEqual(len(result["knowledge"]), 1)
        self.assertTrue(result["knowledge"][0]["structured_fields"])
        self.assertTrue(result["evidence"])
        self.assertTrue(all(item["path"].startswith("source/rtl/") for item in result["evidence"]))

    def test_deterministic_fact_is_direct(self):
        result = self.query("--request-kind", "deterministic-instance-of")
        self.assertEqual(result["route"], "DIRECT")
        self.assertEqual(result["support_facts"][0]["object_name"], "ibex_controller")
        self.assertTrue(result["evidence"])
        self.assertTrue(all(item["path"].startswith("source/rtl/") for item in result["evidence"]))

    def test_stage1_only_fact_is_direct_but_semantic_is_not_published(self):
        stage1 = Path("analysis/model/rtl-knowledge-module-q2-whole-stage1-v6.sqlite")
        self.assertEqual(self.query("--request-kind", "deterministic-instance-of", model=stage1)["route"], "DIRECT")
        semantic = self.query("--obligation", ROLE, model=stage1)
        self.assertEqual(semantic["route"], "BYPASS")
        self.assertEqual(semantic["knowledge"], [])

    def test_off_and_identity_fail_closed(self):
        off = self.query("--knowledge", "off", "--obligation", ROLE,
                         model=Path("analysis/model/no-such-candidate.sqlite"))
        self.assertFalse(off["candidate_accessed"])
        self.assertEqual(off["route"], "BYPASS")
        self.assertEqual(self.query("--obligation", ROLE, "--actual-id", "e:no-such-actual")["route"], "BYPASS")
        self.assertEqual(self.query("--obligation", "module-q2.no-such@2")["route"], "BYPASS")
        self.assertEqual(self.query("--obligation", ROLE, build="0" * 64)["reasons"], ["build-identity-mismatch"])

    def test_ambiguous_actual_requires_exact_identity(self):
        command = [str(CLI), "model", "query", "--project", str(self.project),
                   "--model", str(self.database.relative_to(self.project)), "--view", "module-knowledge",
                   "--target-name", "ibex_csr", "--build-sha256", BUILD,
                   "--obligation", ROLE]
        result = json.loads(subprocess.run(command, capture_output=True, text=True, check=True).stdout)
        self.assertEqual(result["route"], "BYPASS")
        self.assertEqual(result["reasons"], ["actual-required-or-ambiguous"])
        self.assertEqual(result["knowledge"], [])

    def test_missing_bundle_rejected_in_disposable_copy(self):
        self._mutated_copy("DELETE FROM model_metadata WHERE key='publication_bundle'")

    def _mutated_copy(self, statement, *, deterministic=False, stage1=False):
        with tempfile.TemporaryDirectory() as directory:
            duplicate = Path(directory) / "candidate.sqlite"
            source_database = (self.project / "analysis/model/rtl-knowledge-module-q2-whole-stage1-v6.sqlite"
                               if stage1 else self.database)
            shutil.copy2(source_database, duplicate)
            connection = sqlite3.connect(duplicate)
            connection.execute(statement)
            connection.commit()
            connection.close()
            original = PrototypeStore.open(source_database, readonly=True)
            try:
                target = original.connection.execute(
                    "SELECT entity_id FROM entity WHERE qualified_name='ibex_controller'"
                ).fetchone()[0]
            finally:
                original.close()
            store = PrototypeStore.open(duplicate, readonly=True)
            try:
                result = route_v02(store, database=duplicate, project_root=self.project,
                                   source_root=self.project / "source",
                                   target_id=target, build_sha256=BUILD, actual_id=None,
                                   obligations=[] if deterministic else [ROLE],
                                   request_kind="deterministic-instance-of" if deterministic else "module-understanding")
            finally:
                store.close()
            self.assertEqual(result["route"], "BYPASS")
            self.assertEqual(result["knowledge"], [])

    def test_missing_fact_evidence_and_claim_support_rejected(self):
        self._mutated_copy("DELETE FROM fact_evidence WHERE fact_id=(SELECT fact_id FROM ai_fact_support LIMIT 1)")
        self._mutated_copy("DELETE FROM ai_fact_support WHERE rowid=(SELECT rowid FROM ai_fact_support LIMIT 1)")

    def test_changed_review_and_coverage_rejected(self):
        self._mutated_copy("UPDATE evidence SET attrs_json='{}' WHERE evidence_id=(SELECT review_evidence_id FROM obligation_coverage LIMIT 1)")
        self._mutated_copy("UPDATE obligation_coverage SET closure_status='READY' WHERE coverage_id=(SELECT coverage_id FROM obligation_coverage LIMIT 1)")

    def test_review_status_is_bound_to_verified_provenance(self):
        result = self.query("--obligation", ROLE)
        self.assertEqual(result["route"], "CONTEXT")
        claim = result["knowledge"][0]
        self.assertEqual(claim["review"]["status"], "VERIFIED")
        self.assertEqual(len(claim["review"]["artifact_sha256"]), 64)
        self.assertEqual(claim["review"]["evidence_id"], claim["review_evidence_id"])
        self.assertEqual(claim["support_kind"], "scope-fact-and-source")
        self.assertEqual(result["coverage"]["partial_obligations"], [ROLE])
        self.assertTrue(result["coverage"]["gaps"])

    def test_wrong_review_binding_rejected(self):
        self._mutated_copy("UPDATE ai_knowledge SET review_evidence_id='ev:missing' WHERE ai_id=(SELECT ai_id FROM ai_knowledge LIMIT 1)")

    def test_deterministic_evidence_span_identity_rejected(self):
        self._mutated_copy(
            "UPDATE evidence SET start_line=start_line+1 WHERE evidence_id="
            "(SELECT fe.evidence_id FROM fact_evidence fe JOIN fact f ON f.fact_id=fe.fact_id "
            "WHERE f.fact_id='f:e931cf531df2d2b4eef0c393a15bf0f44d2b9fe0e9f1939c1f5b244fb2f71ea4' LIMIT 1)",
            deterministic=True,
        )

    def test_stage1_bad_fact_and_evidence_rejected(self):
        self._mutated_copy(
            "UPDATE fact SET producer='build-spec' WHERE fact_id="
            "'f:e931cf531df2d2b4eef0c393a15bf0f44d2b9fe0e9f1939c1f5b244fb2f71ea4'",
            deterministic=True, stage1=True,
        )
        self._mutated_copy(
            "DELETE FROM fact_evidence WHERE fact_id="
            "'f:e931cf531df2d2b4eef0c393a15bf0f44d2b9fe0e9f1939c1f5b244fb2f71ea4'",
            deterministic=True, stage1=True,
        )
        self._mutated_copy(
            "UPDATE evidence SET attrs_json='{}' WHERE evidence_id="
            "(SELECT evidence_id FROM fact_evidence WHERE fact_id="
            "'f:e931cf531df2d2b4eef0c393a15bf0f44d2b9fe0e9f1939c1f5b244fb2f71ea4' LIMIT 1)",
            deterministic=True, stage1=True,
        )

    def test_source_drift_rejected_in_disposable_source_copy(self):
        store = PrototypeStore.open(self.database, readonly=True)
        try:
            paths = store.analysis_scope()["boundary_policy"]["knowledge_source_paths"]
            target = store.connection.execute(
                "SELECT entity_id FROM entity WHERE qualified_name='ibex_controller'"
            ).fetchone()[0]
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory)
                for relative in paths:
                    destination = source / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(self.project / "source" / relative, destination)
                changed = source / paths[0]
                changed.write_bytes(changed.read_bytes() + b"\n// deliberate test drift\n")
                result = route_v02(store, database=self.database, project_root=source.parent,
                                   source_root=source,
                                   target_id=target, build_sha256=BUILD, actual_id=None,
                                   obligations=[ROLE], request_kind="module-understanding")
                self.assertEqual(result["route"], "BYPASS")
                self.assertEqual(result["knowledge"], [])
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
