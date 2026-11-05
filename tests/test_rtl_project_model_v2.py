"""Contract tests for the small, project-neutral rtlpm-v2 extension."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "src/rtl-pack"
sys.path.insert(0, str(PACK / "lib"))

import rtl_project_model as model  # noqa: E402


DIMENSIONS = {
    "configuration/elaboration",
    "hierarchy",
    "connectivity",
    "behavior/causality",
    "temporal/ordering",
    "clock/reset",
    "engineering-semantic",
}
FORBIDDEN_CORE_TERMS = ("c910", "ifu", "btb", "bht", "ras", "sfp")


def identity(name: str) -> dict[str, str]:
    return model.make_identity(
        project_name=name,
        source_revision="a" * 40,
        config={"top": "top", "files": [{"path": "rtl/top.sv", "sha256": "1"}]},
        toolchain={"slang": "fixture", "yosys": "fixture"},
        spec={"schema": "rtl-analysis-agent/project-model-spec-v2"},
    )


class RtlpmV2ContractTests(unittest.TestCase):
    def test_registry_is_seven_dimensional_versioned_and_project_neutral(self) -> None:
        registry_path = PACK / "config/rtl-knowledge-registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))

        self.assertEqual(registry["schema"], "rtl-analysis-agent/knowledge-registry-v1")
        self.assertEqual({entry["id"] for entry in registry["dimensions"]}, DIMENSIONS)
        temporal = next(
            entry for entry in registry["dimensions"]
            if entry["id"] == "temporal/ordering"
        )
        self.assertEqual(temporal["label"], "State / Temporal / Ordering")
        for group in ("dimensions", "predicates", "capabilities"):
            for entry in registry[group]:
                self.assertTrue(entry["description"].strip())
                self.assertGreaterEqual(entry["version"], 1)
                if group != "dimensions":
                    self.assertIn(entry["dimension"], DIMENSIONS)
        serialized = json.dumps(registry, ensure_ascii=False).casefold()
        for term in FORBIDDEN_CORE_TERMS:
            self.assertNotIn(term, serialized)

    def test_dimensions_and_predicate_primary_dimension_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = model.ModelStore.create(Path(directory) / "model.sqlite", identity("neutral"))
            run = store.add_producer_run(
                producer="fixture", version="1", command=["fixture"], scope={},
                input_hash="1" * 64, status="complete",
            )
            evidence = store.add_evidence(run, kind="source", locator="rtl/top.sv")
            first = store.add_entity("rtl.sv/1:signal", "top.first")
            second = store.add_entity("rtl.sv/1:signal", "top.second")
            kwargs = {
                "subject_id": first,
                "object_id": second,
                "derivation": "tool",
                "confidence": 1.0,
                "status": "verified",
                "producer_run_id": run,
                "evidence_ids": [evidence],
            }
            with self.assertRaisesRegex(model.ModelError, "dimension"):
                store.add_assertion(
                    predicate="rtl.connectivity/1:drives",
                    dimension="connectivty",
                    **kwargs,
                )
            with self.assertRaisesRegex(model.ModelError, "primary dimension"):
                store.add_assertion(
                    predicate="rtl.connectivity/1:drives",
                    dimension="behavior/causality",
                    **kwargs,
                )
            store.add_assertion(
                predicate="vendor.neutral/1:relates",
                dimension="behavior/causality",
                **kwargs,
            )
            with self.assertRaisesRegex(model.ModelError, "primary dimension"):
                store.add_assertion(
                    predicate="vendor.neutral/1:relates",
                    dimension="connectivity",
                    **kwargs,
                )
            with self.assertRaisesRegex(model.ModelError, "dimension"):
                store.set_coverage(
                    scope="top", dimension="state", status="partial",
                    reason="invalid spelling", producer_run_id=run,
                )
            store.close()

    def test_capability_rows_roll_up_missing_and_mixed_status_honestly(self) -> None:
        statuses = ("verified", "partial", "unsupported", "failed", "stale")
        required = {
            "top": [
                "rtl.behavior.data-dependency.v1",
                "rtl.behavior.control-dependency.v1",
            ],
            "leaf": ["rtl.hierarchy.instances.v1"],
            **{
                f"{status}-scope": ["rtl.connectivity.driver-load.v1"]
                for status in statuses
            },
        }
        contract = {
            "required_capabilities": required,
            "project_profile": {"predicates": [], "capabilities": []},
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "model.sqlite"
            store = model.ModelStore.create(
                database, identity("neutral"), contract=contract
            )
            run = store.add_producer_run(
                producer="fixture", version="1", command=["fixture"], scope={},
                input_hash="1" * 64, status="complete",
            )
            store.set_capability_coverage(
                scope="top",
                dimension="behavior/causality",
                capability="rtl.behavior.data-dependency.v1",
                status="verified",
                reason="fixture evidence",
                producer_run_id=run,
            )
            store.set_capability_coverage(
                scope="leaf",
                dimension="hierarchy",
                capability="rtl.hierarchy.instances.v1",
                status="verified",
                reason="fixture evidence",
                producer_run_id=run,
            )
            for status in statuses:
                store.set_capability_coverage(
                    scope=f"{status}-scope",
                    dimension="connectivity",
                    capability="rtl.connectivity.driver-load.v1",
                    status=status,
                    reason=f"fixture {status}",
                    producer_run_id=run,
                )
            store.finalize()

            result = store.query("coverage")
            top = next(row for row in result["coverage"] if row["scope"] == "top")
            self.assertEqual(top["status"], "partial")
            missing = next(
                row for row in result["capability_coverage"]
                if row["capability"] == "rtl.behavior.control-dependency.v1"
            )
            self.assertEqual(missing["status"], "partial")
            self.assertIn("not produced", missing["reason"])
            leaf = next(row for row in result["coverage"] if row["scope"] == "leaf")
            self.assertEqual(leaf["status"], "verified")
            rolled_up = {
                row["scope"]: row["status"] for row in result["coverage"]
                if row["scope"].endswith("-scope")
            }
            self.assertEqual(
                rolled_up, {f"{status}-scope": status for status in statuses}
            )

            with sqlite3.connect(database) as connection:
                columns = {
                    row[1]: row
                    for row in connection.execute(
                        "PRAGMA table_info(capability_coverage)"
                    )
                }
            self.assertEqual(
                list(columns),
                [
                    "model_id", "scope", "dimension", "capability", "status",
                    "reason", "producer_run_id", "checked_at",
                ],
            )
            self.assertEqual(
                {name for name, row in columns.items() if row[5] > 0},
                {"model_id", "scope", "dimension", "capability"},
            )
            store.close()

    def test_project_profile_publication_guard_is_conservative_across_scopes(self) -> None:
        declaration = {
            "id": "project.example/1:implements-mode",
            "dimension": "engineering-semantic",
            "description": "Project-local name for a documented operating mode.",
            "requires": ["rtl.behavior.control-dependency.v1"],
        }
        contract = {
            "required_capabilities": {
                "top": ["rtl.behavior.control-dependency.v1"],
                "aux": [
                    "rtl.hierarchy.modules.v1",
                    "rtl.behavior.control-dependency.v1",
                ],
            },
            "project_profile": {"predicates": [declaration], "capabilities": []},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_path = root / "first.sqlite"
            second_path = root / "second.sqlite"
            first = model.ModelStore.create(
                first_path, identity("first"), contract=contract
            )
            second = model.ModelStore.create(second_path, identity("second"))
            self.assertEqual(
                first.connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall(),
                second.connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall(),
            )
            run = first.add_producer_run(
                producer="fixture", version="1", command=["fixture"], scope={},
                input_hash="1" * 64, status="complete",
            )
            evidence = first.add_evidence(run, kind="source", locator="rtl/top.sv")
            subject = first.add_entity("rtl.sv/1:module", "top")
            concept = first.add_entity("rtl.semantic/1:concept", "mode")
            assertion = first.add_assertion(
                subject_id=subject,
                predicate=declaration["id"],
                object_id=concept,
                dimension=declaration["dimension"],
                derivation="human",
                confidence=0.8,
                status="verified",
                producer_run_id=run,
                evidence_ids=[evidence],
            )
            first.set_capability_coverage(
                scope="top",
                dimension="behavior/causality",
                capability="rtl.behavior.control-dependency.v1",
                status="partial",
                reason="fixture is intentionally incomplete",
                producer_run_id=run,
            )
            first.set_capability_coverage(
                scope="aux",
                dimension="hierarchy",
                capability="rtl.hierarchy.modules.v1",
                status="verified",
                reason="fixture evidence",
                producer_run_id=run,
            )
            first.set_capability_coverage(
                scope="aux",
                dimension="behavior/causality",
                capability="rtl.behavior.control-dependency.v1",
                status="verified",
                reason="fixture evidence",
                producer_run_id=run,
            )
            with self.assertRaisesRegex(model.ModelError, "incomplete capability"):
                first.finalize()
            first.add_assertion(
                subject_id=subject,
                predicate=declaration["id"],
                object_id=concept,
                dimension=declaration["dimension"],
                derivation="human",
                confidence=0.8,
                status="partial",
                producer_run_id=run,
                evidence_ids=[evidence],
            )
            first.finalize()
            stored = first.connection.execute(
                "SELECT status,attrs_json FROM assertion WHERE assertion_id=?", (assertion,)
            ).fetchone()
            self.assertEqual(stored["status"], "partial")
            self.assertEqual(
                json.loads(stored["attrs_json"])["requires"],
                declaration["requires"],
            )
            self.assertEqual(
                {row["scope"] for row in first.query("coverage")["coverage"]},
                {"aux", "top"},
            )
            self.assertEqual(
                second.connection.execute("SELECT count(*) FROM entity").fetchone()[0],
                0,
            )
            first.close()
            second.close()

    def test_v2_jsonl_round_trip_includes_contract_and_capability_coverage(self) -> None:
        contract = {
            "required_capabilities": {"top": ["rtl.connectivity.driver-load.v1"]},
            "project_profile": {"predicates": [], "capabilities": []},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "model.sqlite"
            store = model.ModelStore.create(database, identity("neutral"), contract=contract)
            run = store.add_producer_run(
                producer="fixture", version="1", command=["fixture"], scope={},
                input_hash="1" * 64, status="complete",
            )
            store.set_capability_coverage(
                scope="top", dimension="connectivity",
                capability="rtl.connectivity.driver-load.v1", status="verified",
                reason="fixture evidence", producer_run_id=run,
            )
            store.finalize()
            store.close()

            exported = root / "model.jsonl"
            imported = root / "imported.sqlite"
            model.export_jsonl(database, exported)
            lines = exported.read_text(encoding="utf-8").splitlines()
            manifest = json.loads(lines[0])
            self.assertEqual(
                manifest,
                {"format": "rtl-analysis-agent/project-model-jsonl-v2", "schema_version": "rtlpm-v2"},
            )
            tampered = root / "tampered.jsonl"
            for index, line in enumerate(lines[1:], 1):
                item = json.loads(line)
                if item["table"] == "capability_coverage":
                    item["row"]["dimension"] = "connectivty"
                    lines[index] = model.canonical_json(item)
                    break
            tampered.write_text("\n".join(lines) + "\n", encoding="utf-8")
            rejected = root / "rejected.sqlite"
            with self.assertRaises(model.ModelError):
                model.import_jsonl(tampered, rejected)
            self.assertFalse(rejected.exists())

            model.import_jsonl(exported, imported)
            second = root / "second.jsonl"
            model.export_jsonl(imported, second)
            self.assertEqual(exported.read_bytes(), second.read_bytes())
            imported_store = model.ModelStore.open(imported, readonly=True)
            try:
                self.assertEqual(
                    imported_store.query("coverage")["capability_coverage"][0]["status"],
                    "verified",
                )
            finally:
                imported_store.close()


if __name__ == "__main__":
    unittest.main()
