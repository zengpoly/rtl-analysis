"""Prototype contract, persistence, extraction, and context tests for T911."""

from __future__ import annotations

import copy
import json
import hashlib
import os
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "src/rtl-pack"
PYSLANG_SITE = Path(os.environ.get(
    "RTL_ANALYSIS_PYSLANG_SITE",
    ROOT / ".local/tools/pyslang/11.0.0/site-packages",
))
sys.path.insert(0, str(PACK / "lib"))

import rtl_knowledge_prototype as prototype  # noqa: E402
import rtl_module_knowledge as module_knowledge  # noqa: E402


class KnowledgeContractTests(unittest.TestCase):
    def test_candidate_contracts_load_with_core_starting_points(self) -> None:
        contracts = prototype.load_contracts()
        self.assertEqual(contracts["version"], "0.1-candidate")
        self.assertEqual(len(contracts["entities"]), 18)
        self.assertEqual(len(contracts["facts"]), 26)
        self.assertEqual(len(contracts["ai_knowledge"]), 10)
        self.assertEqual(
            sum(item["category"] == "analysis-core" for item in contracts["entities"]),
            15,
        )
        self.assertEqual(
            sum(item["category"] == "analysis-core" for item in contracts["facts"]),
            22,
        )

    def test_extensions_record_four_part_rationale(self) -> None:
        contracts = prototype.load_contracts()
        required = {"real_scenario", "expression_gap", "producer", "query_consumer"}
        extensions = [
            item
            for group in ("entities", "facts")
            for item in contracts[group]
            if item["category"] != "analysis-core"
        ]
        self.assertEqual({item["name"] for item in extensions}, {
            "PROJECT", "CONFIGURATION", "SOURCE_FILE",
            "HAS_CONFIGURATION", "USES_SOURCE", "TOP", "DERIVED_FROM",
        })
        for item in extensions:
            self.assertEqual(set(item["extension_rationale"]), required)
            self.assertTrue(all(item["extension_rationale"].values()))

    def test_new_contract_chain_is_source_only_and_project_neutral(self) -> None:
        serialized = json.dumps(prototype.load_contracts(), ensure_ascii=False).casefold()
        for forbidden in ("yosys", "kiss2", "netlist_cell", "q01", "atomic_id"):
            self.assertNotIn(forbidden, serialized)

    def test_prototype_code_does_not_duplicate_vocabulary_ids(self) -> None:
        source = (ROOT / "src/rtl-pack/lib/rtl_knowledge_prototype.py").read_text()
        contracts = prototype.load_contracts()
        for group in ("entities", "facts", "ai_knowledge"):
            for entry in contracts[group]:
                self.assertNotIn(f'"{entry["id"]}"', source)

    def test_duplicate_and_invalid_endpoint_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in prototype.CONTRACT_PATHS.values():
                shutil.copyfile(path, root / path.name)
            entities = root / prototype.CONTRACT_PATHS["entities"].name
            payload = json.loads(entities.read_text())
            payload["entries"].append(dict(payload["entries"][0]))
            entities.write_text(json.dumps(payload))
            with self.assertRaisesRegex(prototype.ContractError, "duplicate"):
                prototype.load_contracts(root)

            shutil.copyfile(prototype.CONTRACT_PATHS["entities"], entities)
            facts = root / prototype.CONTRACT_PATHS["facts"].name
            payload = json.loads(facts.read_text())
            payload["entries"][0]["subject_kinds"] = ["rtl.unknown/1:thing"]
            facts.write_text(json.dumps(payload))
            with self.assertRaisesRegex(prototype.ContractError, "endpoint"):
                prototype.load_contracts(root)

    def test_ai_contract_requires_fact_support_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in prototype.CONTRACT_PATHS.values():
                shutil.copyfile(path, root / path.name)
            ai_path = root / prototype.CONTRACT_PATHS["ai_knowledge"].name
            payload = json.loads(ai_path.read_text())
            payload["entries"][0]["requires_fact_support"] = False
            ai_path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(prototype.ContractError, "support"):
                prototype.load_contracts(root)


class PrototypeStoreTests(unittest.TestCase):
    def test_store_batch_transaction_rolls_back_rows_and_identity_caches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "rollback.sqlite"
            store = prototype.PrototypeStore.create(database, {"project": "rollback"})
            module_kind = store.contract_id("entities", "MODULE")
            with self.assertRaisesRegex(RuntimeError, "abort"):
                with store.transaction():
                    entity_id = store.add_entity(module_kind, "discarded")
                    raise RuntimeError("abort")
            self.assertEqual(
                store.connection.execute(
                    "SELECT count(*) FROM entity WHERE entity_id=?", (entity_id,)
                ).fetchone()[0],
                0,
            )
            restored_id = store.add_entity(module_kind, "discarded")
            self.assertEqual(restored_id, entity_id)
            self.assertEqual(store._entity_kind(restored_id), module_kind)
            store.close()

    @staticmethod
    def _complete_parameter_snapshot():
        fixture = ROOT / "tests/fixtures/runtime_module_candidate"
        build_identity = {
            "build_sha256": "b" * 64,
            "build_spec_sha256": "c" * 64,
            "source_closure_sha256": "d" * 64,
        }

        def evidence(path: str, line: int):
            return {
                "path": path,
                "sha256": module_knowledge.sha256_file(fixture / path),
                "line_start": line,
                "line_end": line,
                "configuration_identity": build_identity["build_sha256"],
            }

        parameter = {
            "name": "WIDTH", "kind": "Parameter", "value": "8",
            "type": {"text": "int", "kind": "IntegerType"},
            "overridden": True,
            "evidence": evidence("leaf.sv", 2),
            "declaration_evidence": evidence("leaf.sv", 2),
            "default": {
                "text": "4", "text_truncated": False,
                "evidence": evidence("leaf.sv", 2),
            },
            "override": {
                "binding_kind": "named", "formal_name": "WIDTH",
                "expression_text": "8", "text_truncated": False,
                "evidence": evidence("top.sv", 7),
            },
            "provenance_complete": True,
        }
        target = {
            "kind": "Instance", "name": "u_low", "path": "fixture_top.u_low",
            "definition": "fixture_leaf", "evidence": evidence("top.sv", 7),
            "definition_evidence": evidence("leaf.sv", 1),
            "parameters": [parameter], "parameters_truncated": False,
        }
        snapshot = {
            "target": target,
            "children": [],
            "validation": {
                "target_status": "ok", "target_inspect_completeness": "native-local",
                "target_parameters_complete": True,
            },
            "traversal": {
                "status": "complete", "semantic_child_paths": [],
                "scope_paths": [target["path"]], "per_scope_limit": 100,
                "scope_budget": 1000,
            },
        }
        return fixture, build_identity, snapshot

    @staticmethod
    def _generate_scope_session(*, incomplete_path: str | None = None):
        target = {
            "kind": "Instance", "name": "fixture_top", "path": "fixture_top",
            "definition": "fixture_top", "evidence": {"path": "top.sv"},
            "definition_evidence": {"path": "top.sv"}, "parameters": [],
            "parameters_truncated": False,
        }
        ordinary = {
            "kind": "Instance", "name": "u_plain", "path": "fixture_top.u_plain",
            "definition": "plain_leaf", "evidence": {"path": "top.sv"},
            "definition_evidence": {"path": "leaf.sv"}, "active": True,
        }
        generated = {
            "kind": "Instance", "name": "u_generated",
            "path": "fixture_top.g_array[0].g_nested.u_generated",
            "definition": "generated_leaf", "evidence": {"path": "top.sv"},
            "definition_evidence": {"path": "leaf.sv"}, "active": True,
        }
        scopes = {
            "fixture_top": [
                ordinary,
                {"kind": "GenerateBlockArray", "name": "g_array",
                 "path": "fixture_top.g_array", "active": True},
                {"kind": "GenerateBlock", "name": "g_inactive",
                 "path": "fixture_top.g_inactive", "active": False},
            ],
            "fixture_top.g_array": [
                {"kind": "GenerateBlock", "name": "g_array[0]",
                 "path": "fixture_top.g_array[0]", "active": True},
            ],
            "fixture_top.g_array[0]": [
                {"kind": "GenerateBlock", "name": "g_nested",
                 "path": "fixture_top.g_array[0].g_nested", "active": True},
            ],
            "fixture_top.g_array[0].g_nested": [generated, dict(generated)],
        }

        class NativeInstance:
            kind = "Instance"
            isUninstantiated = False

            def __init__(self, record):
                self.hierarchicalPath = record["path"]
                self.body = type("Body", (), {"parameters": []})()
                self.portConnections = []
                self.record = record

        native = {
            record["path"]: NativeInstance(record)
            for record in (target, ordinary, generated)
        }

        class FakeSession:
            def __init__(self):
                self.scope_requests: list[tuple[str, str]] = []

            def _lookup_exact(self, path):
                return native.get(path)

            def _symbol_record(self, instance, *, register_handle):
                if register_handle:
                    raise AssertionError("native Build1 projection registered a handle")
                return dict(instance.record)

            def _parameter_override_map(self, instance):
                return {}, False

            def _connection_syntax_map(self, instance, connections):
                return {}, False

            def query(self, request):
                operation = request["op"]
                path = request.get("path") or request.get("instance")
                if operation == "inspect" and path == "fixture_top":
                    return {"status": "ok", "record": target}
                if operation == "scope":
                    if path in {ordinary["path"], generated["path"]}:
                        raise AssertionError("child Instance body must be terminal")
                    kind = request.get("kind")
                    if kind is None:
                        return {
                            "status": "ok", "completeness": "truncated",
                            "truncated": True, "records": scopes[path][:1],
                        }
                    if kind not in ("Instance", "GenerateBlockArray", "GenerateBlock"):
                        raise AssertionError(f"unexpected scope kind: {kind}")
                    self.scope_requests.append((path, kind))
                    complete = not (
                        path == incomplete_path and kind == "GenerateBlock"
                    )
                    return {
                        "status": "ok" if complete else "partial",
                        "completeness": "complete" if complete else "truncated",
                        "truncated": not complete,
                        "records": [item for item in scopes[path] if item["kind"] == kind],
                    }
                if operation == "inspect":
                    child = ordinary if path == ordinary["path"] else generated
                    return {
                        "status": "ok",
                        "record": {**child, "parameters": [], "parameters_truncated": False},
                    }
                if operation == "bindings":
                    return {"status": "ok", "completeness": "complete", "records": []}
                raise AssertionError(request)

        return FakeSession(), ordinary, generated

    def test_collect_module_scope_transparently_walks_generate_containers(self) -> None:
        session, ordinary, generated = self._generate_scope_session()
        snapshot = module_knowledge.collect_module_scope(session, "fixture_top")

        self.assertEqual(
            [item["scope"]["path"] for item in snapshot["children"]],
            sorted([ordinary["path"], generated["path"]]),
        )
        self.assertEqual(
            session.scope_requests,
            [
                (path, kind)
                for path in (
                    "fixture_top",
                    "fixture_top.g_array",
                    "fixture_top.g_array[0]",
                    "fixture_top.g_array[0].g_nested",
                )
                for kind in ("Instance", "GenerateBlockArray", "GenerateBlock")
            ],
        )
        self.assertEqual(snapshot["traversal"]["status"], "complete")
        self.assertEqual(
            snapshot["traversal"]["semantic_child_paths"],
            sorted([ordinary["path"], generated["path"]]),
        )

    def test_collect_module_scope_fails_closed_on_incomplete_or_budgeted_walk(self) -> None:
        session, _, _ = self._generate_scope_session(incomplete_path="fixture_top.g_array")
        with self.assertRaisesRegex(RuntimeError, "transparent scope is incomplete"):
            module_knowledge.collect_module_scope(session, "fixture_top")

        session, _, _ = self._generate_scope_session()
        with mock.patch.object(module_knowledge, "TRANSPARENT_SCOPE_BUDGET", 1):
            with self.assertRaisesRegex(RuntimeError, "scope budget"):
                module_knowledge.collect_module_scope(session, "fixture_top")

    def test_cycle1_target_obligations_allow_bounded_child_parameters(self) -> None:
        target = {
            "kind": "Instance", "path": "fixture_top", "parameters": [],
            "parameters_truncated": False,
        }
        child_path = "fixture_top.u_large_child"
        snapshot = {
            "target": target,
            "children": [{
                "scope": {"path": child_path},
                "inspection": {"parameters": [{}] * 32, "parameters_truncated": True},
            }],
            "validation": {
                "target_status": "ok", "target_parameters_complete": True,
            },
            "traversal": {
                "status": "complete", "semantic_child_paths": [child_path],
            },
        }

        observed_target, child_paths, parameter_names = (
            module_knowledge._validated_snapshot_obligations(snapshot)
        )

        self.assertEqual(observed_target, target)
        self.assertEqual(child_paths, [child_path])
        self.assertEqual(parameter_names, [])

    def test_fact_and_ai_are_physically_separate_and_integral(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.sqlite"
            store = prototype.PrototypeStore.create(
                database,
                {"project": "neutral", "source_revision": "a" * 40},
            )
            module = store.add_entity("rtl.sv/1:module", "top")
            signal = store.add_entity("rtl.sv/1:signal", "top.in")
            evidence = store.add_evidence(
                kind="source", locator="rtl/top.sv", start_line=3, end_line=3
            )
            fact = store.add_fact(
                module,
                "rtl.hierarchy/1:contains",
                signal,
                producer="slang",
                evidence_ids=[evidence],
            )
            knowledge = store.add_ai_knowledge(
                module,
                "rtl.ai/1:module-role",
                "Top-level integration module in this selected scope.",
                support_fact_ids=[fact],
                evidence_ids=[evidence],
            )
            self.assertTrue(fact.startswith("f:"))
            self.assertTrue(knowledge.startswith("ai:"))
            self.assertEqual(store.integrity(), {"integrity_check": "ok", "foreign_key_check": []})
            tables = {
                row[0]
                for row in store.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("fact", tables)
            self.assertIn("ai_knowledge", tables)
            self.assertIn("ai_fact_support", tables)
            store.close()

    def test_real_runtime_preserves_instance_parameters_and_connection_expressions(self) -> None:
        pyslang_site = os.environ.get("RTL_ANALYSIS_PYSLANG_SITE")
        if not pyslang_site or not Path(pyslang_site).is_dir():
            self.skipTest("locked PySlang site was not supplied for the real Runtime fixture")
        import rtl_query_runtime as runtime

        fixture = ROOT / "tests/fixtures/runtime_module_candidate"
        with runtime.RuntimeQuerySession.open(
            source_root=fixture, build_path=fixture / "build.json",
            project_name="runtime-module-candidate", source_revision="fixture-v1",
            pyslang_site=Path(pyslang_site),
        ) as session, tempfile.TemporaryDirectory() as directory:
            snapshot = module_knowledge.collect_module_scope(session, "fixture_top")
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite",
                {
                    "project": "runtime-module-candidate",
                    "build_sha256": session.ready_result()["compilation_identity"]["build_sha256"],
                },
            )
            summary = module_knowledge.populate_module_scope(
                store, source_root=fixture, project_name="runtime-module-candidate",
                compilation_identity=session.ready_result()["compilation_identity"],
                snapshot=snapshot,
            )
            self.assertEqual(len(summary["direct_instance_ids"]), 2)
            self.assertEqual(summary["definitions"], ["fixture_leaf", "fixture_top"])
            instance_attrs = [
                json.loads(row[0]) for row in store.connection.execute(
                    "SELECT attrs_json FROM entity WHERE kind='rtl.sv/1:instance' ORDER BY qualified_name"
                )
            ]
            widths = {
                next(item for item in attrs["effective_parameters"] if item["name"] == "WIDTH")["value"]
                for attrs in instance_attrs
                if any(item["name"] == "WIDTH" for item in attrs["effective_parameters"])
            }
            self.assertEqual(widths, {"4", "8"})
            binding_attrs = [
                json.loads(row[0]) for row in store.connection.execute(
                    "SELECT attrs_json FROM fact WHERE predicate='rtl.connectivity/1:binds-to'"
                )
            ]
            texts = {item["actual"]["text"] for item in binding_attrs if item["actual"]}
            self.assertTrue(any("bus_i[7:0]" in text for text in texts))
            self.assertTrue(any("{bus_i[15:14], bus_i[9:8]}" in text for text in texts))
            self.assertTrue(any("1'b1" in text for text in texts))
            expected_references = {
                "bus_i[7:0]": {"fixture_top.bus_i"},
                "{bus_i[15:14], bus_i[9:8]}": {"fixture_top.bus_i"},
                "select_i": {"fixture_top.select_i"},
                "low_o": {"fixture_top.low_o"},
                "high_o": {"fixture_top.high_o"},
                "1'b1": set(),
            }
            self.assertEqual(texts, set(expected_references))
            for item in binding_attrs:
                actual = item["actual"]
                # Compact storage omits empty references for literal constants.
                references = {ref["path"] for ref in actual.get("references", [])}
                self.assertEqual(references, expected_references[actual["text"]])
                self.assertFalse(item["complete_connection_semantics"])
                self.assertIn("no expression or bit-equivalence inference", item["semantic_boundary"])
            self.assertGreaterEqual(summary["expression_boundary_count"], 3)
            self.assertEqual(store.integrity(), {"integrity_check": "ok", "foreign_key_check": []})
            store.close()

    def test_populate_module_scope_uses_actual_root_instance(self) -> None:
        fixture = ROOT / "tests/fixtures/runtime_module_candidate"
        build_identity = {
            "build_sha256": "b" * 64,
            "build_spec_sha256": "c" * 64,
            "source_closure_sha256": "d" * 64,
        }

        def evidence(path: str, line: int):
            return {
                "path": path,
                "sha256": module_knowledge.sha256_file(fixture / path),
                "line_start": line,
                "line_end": line,
                "configuration_identity": build_identity["build_sha256"],
            }

        target = {
            "kind": "Instance", "name": "u_root", "path": "fixture_top.u_root",
            "definition": "fixture_leaf", "evidence": evidence("top.sv", 7),
            "definition_evidence": evidence("leaf.sv", 1),
            "parameters": [], "parameters_truncated": False,
        }
        child = {
            "kind": "Instance", "name": "u_child",
            "path": "fixture_top.u_root.u_child", "definition": "fixture_leaf",
            "evidence": evidence("top.sv", 13),
            "definition_evidence": evidence("leaf.sv", 1), "active": True,
        }
        snapshot = {
            "target": target,
            "children": [{
                "scope": child,
                "inspection": {**child, "parameters": [], "parameters_truncated": False},
                "bindings": [],
            }],
            "traversal": {
                "status": "complete", "semantic_child_paths": [child["path"]],
                "scope_paths": [target["path"]], "per_scope_limit": 100,
                "scope_budget": 1000,
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite",
                {"project": "runtime-module-candidate", **build_identity},
            )
            summary = module_knowledge.populate_module_scope(
                store, source_root=fixture, project_name="runtime-module-candidate",
                compilation_identity=build_identity, snapshot=snapshot,
            )

            self.assertNotIn("parent_entity_id", summary)
            root_id = summary["target_instance_entity_id"]
            module_id = summary["target_module_entity_id"]
            root = store.connection.execute(
                "SELECT kind, qualified_name, attrs_json FROM entity WHERE entity_id=?",
                (root_id,),
            ).fetchone()
            self.assertEqual(root["kind"], store.contract_id("entities", "INSTANCE"))
            self.assertEqual(root["qualified_name"], target["path"])
            self.assertEqual(json.loads(root["attrs_json"])["definition"], "fixture_leaf")
            module = store.connection.execute(
                "SELECT kind, qualified_name, attrs_json FROM entity WHERE entity_id=?",
                (module_id,),
            ).fetchone()
            self.assertEqual(module["kind"], store.contract_id("entities", "MODULE"))
            self.assertEqual(module["qualified_name"], "fixture_leaf")
            self.assertNotIn("elaborated_instance_path", json.loads(module["attrs_json"]))
            self.assertNotIn("effective_parameters", json.loads(module["attrs_json"]))

            project_id = store.connection.execute(
                "SELECT entity_id FROM entity WHERE kind=?",
                (store.contract_id("entities", "PROJECT"),),
            ).fetchone()[0]
            contains = store.connection.execute(
                "SELECT fact_id FROM fact WHERE subject_id=? AND predicate=? AND object_id=?",
                (project_id, store.contract_id("facts", "CONTAINS"), root_id),
            ).fetchone()
            self.assertIsNotNone(contains)
            contains_locators = {
                row[0] for row in store.connection.execute(
                    "SELECT e.locator FROM fact_evidence fe JOIN evidence e USING(evidence_id) "
                    "WHERE fe.fact_id=?", (contains[0],)
                )
            }
            self.assertEqual(contains_locators, {"top.sv"})
            instance_of = store.connection.execute(
                "SELECT fact_id FROM fact WHERE subject_id=? AND predicate=? AND object_id=?",
                (root_id, store.contract_id("facts", "INSTANCE_OF"), module_id),
            ).fetchone()
            self.assertIsNotNone(instance_of)
            child_id = summary["direct_instance_ids"][0]
            self.assertIsNotNone(store.connection.execute(
                "SELECT 1 FROM fact WHERE subject_id=? AND predicate=? AND object_id=?",
                (root_id, store.contract_id("facts", "CONTAINS"), child_id),
            ).fetchone())
            shared_modules = store.connection.execute(
                "SELECT count(*) FROM entity WHERE kind=? AND qualified_name='fixture_leaf'",
                (store.contract_id("entities", "MODULE"),),
            ).fetchone()[0]
            self.assertEqual(shared_modules, 1)
            store.close()

    def test_populate_module_scope_publishes_all_target_bindings(self) -> None:
        fixture = ROOT / "tests/fixtures/runtime_module_candidate"
        build_identity = {
            "build_sha256": "b" * 64,
            "build_spec_sha256": "c" * 64,
            "source_closure_sha256": "d" * 64,
        }

        def evidence(path: str, line: int):
            return {
                "path": path,
                "sha256": module_knowledge.sha256_file(fixture / path),
                "line_start": line,
                "line_end": line,
                "configuration_identity": build_identity["build_sha256"],
            }

        target_path = "fixture_top.u_root"
        target = {
            "kind": "Instance", "name": "u_root", "path": target_path,
            "definition": "fixture_leaf", "evidence": evidence("top.sv", 7),
            "definition_evidence": evidence("leaf.sv", 1),
            "parameters": [], "parameters_truncated": False,
        }
        bindings = []
        for index in range(128):
            if index == 127:
                actual = {
                    "kind": "BinaryExpression", "text": "bus_a | bus_b",
                    "text_truncated": False, "nodes_truncated": False,
                    "references": [
                        {"kind": "Net", "path": "fixture_top.bus_a"},
                        {"kind": "Net", "path": "fixture_top.bus_b"},
                    ],
                }
            else:
                actual = {
                    "kind": "NamedValue", "text": f"signal_{index}",
                    "text_truncated": False, "nodes_truncated": False,
                    "references": [{
                        "kind": "Net", "path": f"fixture_top.signal_{index}"
                    }],
                }
            bindings.append({
                "relation": "port_binding", "instance": target_path,
                "formal": f"port_{index}", "direction": "In",
                "formal_type": None, "internal_endpoint": None,
                "internal_expression": None,
                "connection_status": "connected", "connection_origin": "explicit",
                "actual": actual, "connection_evidence": evidence("top.sv", 7),
                "default_evidence": None, "formal_evidence": evidence("leaf.sv", 2),
            })
        snapshot = {
            "target": target,
            "target_bindings": bindings,
            "children": [],
            "traversal": {
                "status": "complete", "semantic_child_paths": [],
                "scope_paths": [target_path], "per_scope_limit": 100,
                "scope_budget": 1000,
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite",
                {"project": "runtime-module-candidate", **build_identity},
            )
            summary = module_knowledge.populate_module_scope(
                store, source_root=fixture, project_name="runtime-module-candidate",
                compilation_identity=build_identity, snapshot=snapshot,
            )
            root_id = summary["target_instance_entity_id"]
            port_kind = store.contract_id("entities", "PORT")
            unresolved_kind = store.contract_id("entities", "UNRESOLVED")
            signal_kind = store.contract_id("entities", "SIGNAL")
            contains = store.contract_id("facts", "CONTAINS")
            binds_to = store.contract_id("facts", "BINDS_TO")
            self.assertEqual(summary["binding_count"], 128)
            self.assertEqual(summary["expression_boundary_count"], 1)
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM fact f JOIN entity p ON p.entity_id=f.object_id "
                "WHERE f.subject_id=? AND f.predicate=? AND p.kind=?",
                (root_id, contains, port_kind),
            ).fetchone()[0], 128)
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM fact f JOIN entity p ON p.entity_id=f.subject_id "
                "WHERE f.predicate=? AND p.kind=?",
                (binds_to, port_kind),
            ).fetchone()[0], 128)
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM entity WHERE kind=?", (signal_kind,)
            ).fetchone()[0], 127)
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM entity WHERE kind=?", (unresolved_kind,)
            ).fetchone()[0], 1)
            self.assertEqual(
                store.integrity(), {"integrity_check": "ok", "foreign_key_check": []}
            )
            store.close()

    def test_parameter_evidence_normalization_uses_existing_store_evidence(self) -> None:
        fixture, build_identity, snapshot = self._complete_parameter_snapshot()
        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite",
                {"project": "runtime-module-candidate", **build_identity},
            )
            summary = module_knowledge.populate_module_scope(
                store, source_root=fixture, project_name="runtime-module-candidate",
                compilation_identity=build_identity, snapshot=snapshot,
            )
            attrs = json.loads(store.connection.execute(
                "SELECT attrs_json FROM entity WHERE entity_id=?",
                (summary["target_instance_entity_id"],),
            ).fetchone()[0])
            width = attrs["effective_parameters"][0]

            self.assertNotIn("evidence", width)
            self.assertEqual(width["name"], "WIDTH")
            self.assertEqual(width["value"], "8")
            self.assertTrue(width["provenance_complete"])
            self.assertEqual(width["default"]["text"], "4")
            self.assertEqual(width["override"]["binding_kind"], "named")
            self.assertEqual(width["override"]["formal_name"], "WIDTH")
            self.assertEqual(width["override"]["expression_text"], "8")
            evidence_rows = {
                row["evidence_id"]: row
                for row in store.connection.execute("SELECT * FROM evidence")
            }
            declaration = evidence_rows[width["declaration_evidence_id"]]
            default = evidence_rows[width["default"]["evidence_id"]]
            override = evidence_rows[width["override"]["evidence_id"]]
            elaboration = evidence_rows[width["elaboration_evidence_id"]]
            self.assertEqual((declaration["locator"], declaration["start_line"]), ("leaf.sv", 2))
            self.assertEqual((default["locator"], default["start_line"]), ("leaf.sv", 2))
            self.assertEqual((override["locator"], override["start_line"]), ("top.sv", 7))
            self.assertEqual(elaboration["kind"], "provenance")
            self.assertEqual(elaboration["locator"], "runtime-query-build:runtime-module-candidate")
            store.close()

    def test_cycle1_conformance_predicate_accepts_complete_grounding(self) -> None:
        fixture, build_identity, snapshot = self._complete_parameter_snapshot()
        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite",
                {"project": "runtime-module-candidate", **build_identity},
            )
            module_knowledge.populate_module_scope(
                store, source_root=fixture, project_name="runtime-module-candidate",
                compilation_identity=build_identity, snapshot=snapshot,
            )
            result = module_knowledge.evaluate_cycle1_conformance(
                store, snapshot=snapshot, compilation_identity=build_identity
            )

            self.assertEqual(result["status"], "READY")
            self.assertTrue(all(result["checks"].values()))
            self.assertEqual(result["expected_child_paths"], [])
            self.assertEqual(result["required_parameter_names"], ["WIDTH"])
            self.assertEqual(
                store.metadata("module_core_v1_cycle1_conformance"), result
            )
            store.close()

    def test_cycle1_conformance_predicate_rejects_four_grounding_gaps(self) -> None:
        fixture, build_identity, complete_snapshot = self._complete_parameter_snapshot()

        def normalized_store(directory: str, snapshot):
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite",
                {"project": "runtime-module-candidate", **build_identity},
            )
            summary = module_knowledge.populate_module_scope(
                store, source_root=fixture, project_name="runtime-module-candidate",
                compilation_identity=build_identity, snapshot=snapshot,
            )
            return store, summary

        with self.subTest("missing override Evidence"), tempfile.TemporaryDirectory() as directory:
            normalization_input = copy.deepcopy(complete_snapshot)
            normalization_input["target"]["parameters"][0]["override"]["evidence"] = None
            store, _ = normalized_store(directory, normalization_input)
            result = module_knowledge.evaluate_cycle1_conformance(
                store, snapshot=complete_snapshot, compilation_identity=build_identity
            )
            self.assertEqual(result["status"], "PARTIAL")
            self.assertFalse(result["checks"]["parameter_grounding_complete"])
            self.assertIn("parameter-grounding-incomplete", result["reasons"])
            store.close()

        with self.subTest("wrong but existing Evidence"), tempfile.TemporaryDirectory() as directory:
            snapshot = copy.deepcopy(complete_snapshot)
            store, summary = normalized_store(directory, snapshot)
            root_id = summary["target_instance_entity_id"]
            attrs = json.loads(store.connection.execute(
                "SELECT attrs_json FROM entity WHERE entity_id=?", (root_id,)
            ).fetchone()[0])
            width = attrs["effective_parameters"][0]
            width["override"]["evidence_id"] = width["default"]["evidence_id"]
            with store.connection:
                store.connection.execute(
                    "UPDATE entity SET attrs_json=? WHERE entity_id=?",
                    (json.dumps(attrs, sort_keys=True, separators=(",", ":")), root_id),
                )
            self.assertEqual(store.integrity()["integrity_check"], "ok")
            result = module_knowledge.evaluate_cycle1_conformance(
                store, snapshot=snapshot, compilation_identity=build_identity
            )
            self.assertEqual(result["status"], "PARTIAL")
            self.assertFalse(result["checks"]["parameter_grounding_complete"])
            self.assertIn("parameter-evidence-correspondence-mismatch", result["reasons"])
            store.close()

        with self.subTest("missing PROJECT contains target"), tempfile.TemporaryDirectory() as directory:
            snapshot = copy.deepcopy(complete_snapshot)
            store, summary = normalized_store(directory, snapshot)
            root_id = summary["target_instance_entity_id"]
            contains = store.contract_id("facts", "CONTAINS")
            project = store.connection.execute(
                "SELECT entity_id FROM entity WHERE kind=?",
                (store.contract_id("entities", "PROJECT"),),
            ).fetchone()[0]
            with store.connection:
                store.connection.execute(
                    "DELETE FROM fact WHERE subject_id=? AND predicate=? AND object_id=?",
                    (project, contains, root_id),
                )
            result = module_knowledge.evaluate_cycle1_conformance(
                store, snapshot=snapshot, compilation_identity=build_identity
            )
            self.assertEqual(result["status"], "PARTIAL")
            self.assertFalse(result["checks"]["project_contains_target"])
            self.assertIn("project-containment-missing", result["reasons"])
            store.close()

        with self.subTest("missing snapshot required parameter"), tempfile.TemporaryDirectory() as directory:
            snapshot = copy.deepcopy(complete_snapshot)
            store, summary = normalized_store(directory, snapshot)
            root_id = summary["target_instance_entity_id"]
            attrs = json.loads(store.connection.execute(
                "SELECT attrs_json FROM entity WHERE entity_id=?", (root_id,)
            ).fetchone()[0])
            attrs["effective_parameters"] = []
            with store.connection:
                store.connection.execute(
                    "UPDATE entity SET attrs_json=? WHERE entity_id=?",
                    (json.dumps(attrs, sort_keys=True, separators=(",", ":")), root_id),
                )
            result = module_knowledge.evaluate_cycle1_conformance(
                store, snapshot=snapshot, compilation_identity=build_identity
            )
            self.assertEqual(result["status"], "PARTIAL")
            self.assertFalse(result["checks"]["parameter_grounding_complete"])
            self.assertEqual(result["required_parameter_names"], ["WIDTH"])
            self.assertIn("parameter-grounding-incomplete", result["reasons"])
            store.close()

    @unittest.skipUnless(PYSLANG_SITE.is_dir(), "locked PySlang is not installed")
    def test_runtime_module_build_report_v2_returns_cycle1_ready(self) -> None:
        fixture = ROOT / "tests/fixtures/runtime_module_candidate"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "candidate.sqlite"
            report = module_knowledge.build_runtime_module_candidate(
                source_root=fixture,
                build_path=fixture / "build.json",
                pyslang_site=PYSLANG_SITE,
                project_name="runtime-module-candidate",
                source_revision="fixture-v1",
                target_path="fixture_top.u_low",
                output=output,
            )
            command = {"status": "pass", **report}

            self.assertEqual(
                command["schema"], "rtl-analysis-agent/runtime-module-candidate-build-v2"
            )
            self.assertEqual(command["status"], "pass")
            self.assertNotIn("parent_entity_id", command["scope"])
            self.assertEqual(
                command["target_instance_entity_id"],
                command["scope"]["target_instance_entity_id"],
            )
            self.assertEqual(
                command["target_module_entity_id"],
                command["scope"]["target_module_entity_id"],
            )
            self.assertEqual(command["cycle1_conformance"]["status"], "READY")
            self.assertTrue(all(command["cycle1_conformance"]["checks"].values()))
            self.assertEqual(command["candidate"]["integrity"]["integrity_check"], "ok")
            store = prototype.PrototypeStore.open(output, readonly=True)
            self.assertEqual(
                store.metadata("module_core_v1_cycle1_conformance"),
                command["cycle1_conformance"],
            )
            store.close()

    @unittest.skipUnless(PYSLANG_SITE.is_dir(), "locked PySlang is not installed")
    def test_runtime_design_build_publishes_complete_fixture_hierarchy(self) -> None:
        fixture = ROOT / "tests/fixtures/runtime_module_candidate"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "whole-design.sqlite"
            report = module_knowledge.build_runtime_design_candidate(
                source_root=fixture,
                build_path=fixture / "build.json",
                pyslang_site=PYSLANG_SITE,
                project_name="runtime-design-candidate",
                source_revision="fixture-v1",
                output=output,
            )

            self.assertEqual(
                report["schema"],
                "rtl-analysis-agent/runtime-design-candidate-build-v1",
            )
            self.assertEqual(report["scope"]["actual_instance_count"], 3)
            self.assertEqual(report["scope"]["module_definition_count"], 2)
            self.assertEqual(report["scope"]["max_hierarchy_depth"], 1)
            self.assertEqual(report["analysis_scope"]["boundary_policy"]["max_depth"], 2)
            self.assertEqual(
                report["analysis_scope"]["scope_mode"], "ACTUAL_INSTANCE_ROOT"
            )
            self.assertEqual(
                report["analysis_scope"]["producer_capability"]["actual_hierarchy"],
                "FULL",
            )
            store = prototype.PrototypeStore.open(output, readonly=True)
            try:
                instance_kind = store.contract_id("entities", "INSTANCE")
                module_kind = store.contract_id("entities", "MODULE")
                self.assertEqual(
                    store.connection.execute(
                        "SELECT count(*) FROM entity WHERE kind=?", (instance_kind,)
                    ).fetchone()[0],
                    3,
                )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT count(*) FROM entity WHERE kind=?", (module_kind,)
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT count(*) FROM ai_knowledge"
                    ).fetchone()[0],
                    0,
                )
                binding_rows = list(store.connection.execute(
                    "SELECT p.attrs_json, f.attrs_json, target.attrs_json "
                    "FROM fact owner JOIN entity p ON p.entity_id=owner.object_id "
                    "JOIN fact f ON f.subject_id=p.entity_id "
                    "JOIN entity target ON target.entity_id=f.object_id "
                    "WHERE owner.predicate=? AND f.predicate=?",
                    (
                        store.contract_id("facts", "CONTAINS"),
                        store.contract_id("facts", "BINDS_TO"),
                    ),
                ))
                self.assertTrue(binding_rows)
                members = store._publication_members(report["analysis_scope"])
                deepest_ports = {
                    row["entity_id"] for row in store.connection.execute(
                        "SELECT entity_id FROM entity WHERE kind=? AND qualified_name LIKE ?",
                        (store.contract_id("entities", "PORT"), "fixture_top.u_low.%"),
                    )
                }
                self.assertTrue(deepest_ports)
                self.assertLessEqual(deepest_ports, members)
                shallow = copy.deepcopy(report["analysis_scope"])
                shallow["boundary_policy"]["max_depth"] = 1
                self.assertFalse(deepest_ports & store._publication_members(shallow))
                endpoint_ids = {
                    row[0] for row in store.connection.execute(
                        "SELECT object_id FROM fact WHERE predicate=?",
                        (store.contract_id("facts", "BINDS_TO"),),
                    )
                }
                self.assertTrue(endpoint_ids - members)
                for port_json, fact_json, target_json in binding_rows:
                    port_attrs = json.loads(port_json)
                    fact_attrs = json.loads(fact_json)
                    target_attrs = json.loads(target_json)
                    self.assertIn(port_attrs["direction"], {"In", "Out", "InOut"})
                    self.assertNotIn("actual", fact_attrs)
                    self.assertNotIn("internal_expression", fact_attrs)
                    expression = target_attrs.get("expression")
                    if expression:
                        self.assertNotIn("nodes", expression)
                        self.assertNotIn("evidence", expression)
                self.assertEqual(store.integrity()["integrity_check"], "ok")
            finally:
                store.close()

    @unittest.skipUnless(PYSLANG_SITE.is_dir(), "locked PySlang is not installed")
    def test_runtime_design_scope_depth_handles_multiple_roots_and_no_ports(self) -> None:
        fixture = ROOT / "tests/fixtures/runtime_module_candidate"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            multi = root / "multi"
            shutil.copytree(fixture, multi)
            spec = json.loads((multi / "build.json").read_text())
            spec["tops"] = ["fixture_top", "fixture_leaf"]
            (multi / "build.json").write_text(json.dumps(spec))
            multi_report = module_knowledge.build_runtime_design_candidate(
                source_root=multi, build_path=multi / "build.json",
                pyslang_site=PYSLANG_SITE, project_name="multi-root",
                source_revision="fixture-v1", output=root / "multi.sqlite",
            )
            self.assertEqual(multi_report["analysis_scope"]["scope_mode"], "MULTI_ROOT")
            self.assertEqual(multi_report["analysis_scope"]["boundary_policy"]["max_depth"], 2)
            store = prototype.PrototypeStore.open(root / "multi.sqlite", readonly=True)
            try:
                members = store._publication_members(store.analysis_scope())
                port_kind = store.contract_id("entities", "PORT")
                ports = {row[0] for row in store.connection.execute(
                    "SELECT entity_id FROM entity WHERE kind=?", (port_kind,),
                )}
                self.assertTrue(ports)
                self.assertLessEqual(ports, members)
            finally:
                store.close()

            empty = root / "empty"
            empty.mkdir()
            (empty / "empty.sv").write_text("module empty_top; endmodule\n")
            spec.update(files=["empty.sv"], tops=["empty_top"])
            (empty / "build.json").write_text(json.dumps(spec))
            empty_report = module_knowledge.build_runtime_design_candidate(
                source_root=empty, build_path=empty / "build.json",
                pyslang_site=PYSLANG_SITE, project_name="no-port",
                source_revision="fixture-v1", output=root / "empty.sqlite",
            )
            self.assertEqual(empty_report["scope"]["max_hierarchy_depth"], 0)
            self.assertEqual(empty_report["analysis_scope"]["boundary_policy"]["max_depth"], 0)

    @unittest.skipUnless(PYSLANG_SITE.is_dir(), "locked PySlang is not installed")
    def test_runtime_module_build_report_keeps_command_pass_for_four_partial_variants(self) -> None:
        fixture = ROOT / "tests/fixtures/runtime_module_candidate"
        original_populate = module_knowledge.populate_module_scope

        def mutate_missing_override(store, **kwargs):
            altered = copy.deepcopy(kwargs["snapshot"])
            width = next(
                item for item in altered["target"]["parameters"] if item["name"] == "WIDTH"
            )
            width["override"]["evidence"] = None
            return original_populate(store, **{**kwargs, "snapshot": altered})

        def mutate_wrong_evidence(store, **kwargs):
            summary = original_populate(store, **kwargs)
            root_id = summary["target_instance_entity_id"]
            attrs = json.loads(store.connection.execute(
                "SELECT attrs_json FROM entity WHERE entity_id=?", (root_id,)
            ).fetchone()[0])
            width = next(item for item in attrs["effective_parameters"] if item["name"] == "WIDTH")
            width["override"]["evidence_id"] = width["default"]["evidence_id"]
            with store.connection:
                store.connection.execute(
                    "UPDATE entity SET attrs_json=? WHERE entity_id=?",
                    (json.dumps(attrs, sort_keys=True, separators=(",", ":")), root_id),
                )
            return summary

        def mutate_missing_containment(store, **kwargs):
            summary = original_populate(store, **kwargs)
            project = store.connection.execute(
                "SELECT entity_id FROM entity WHERE kind=?",
                (store.contract_id("entities", "PROJECT"),),
            ).fetchone()[0]
            with store.connection:
                store.connection.execute(
                    "DELETE FROM fact WHERE subject_id=? AND predicate=? AND object_id=?",
                    (
                        project,
                        store.contract_id("facts", "CONTAINS"),
                        summary["target_instance_entity_id"],
                    ),
                )
            return summary

        def mutate_missing_parameter(store, **kwargs):
            summary = original_populate(store, **kwargs)
            root_id = summary["target_instance_entity_id"]
            attrs = json.loads(store.connection.execute(
                "SELECT attrs_json FROM entity WHERE entity_id=?", (root_id,)
            ).fetchone()[0])
            attrs["effective_parameters"] = [
                item for item in attrs["effective_parameters"] if item["name"] != "WIDTH"
            ]
            with store.connection:
                store.connection.execute(
                    "UPDATE entity SET attrs_json=? WHERE entity_id=?",
                    (json.dumps(attrs, sort_keys=True, separators=(",", ":")), root_id),
                )
            return summary

        variants = {
            "missing-override": (mutate_missing_override, "parameter-grounding-incomplete"),
            "wrong-evidence": (
                mutate_wrong_evidence, "parameter-evidence-correspondence-mismatch"
            ),
            "missing-containment": (
                mutate_missing_containment, "project-containment-missing"
            ),
            "missing-parameter": (mutate_missing_parameter, "parameter-grounding-incomplete"),
        }
        with tempfile.TemporaryDirectory() as directory:
            for name, (mutator, reason) in variants.items():
                with self.subTest(name):
                    output = Path(directory) / f"{name}.sqlite"
                    with mock.patch.object(
                        module_knowledge, "populate_module_scope", side_effect=mutator
                    ):
                        report = module_knowledge.build_runtime_module_candidate(
                            source_root=fixture,
                            build_path=fixture / "build.json",
                            pyslang_site=PYSLANG_SITE,
                            project_name="runtime-module-candidate",
                            source_revision="fixture-v1",
                            target_path="fixture_top.u_low",
                            output=output,
                        )
                    command = {"status": "pass", **report}
                    self.assertEqual(command["status"], "pass")
                    self.assertEqual(command["cycle1_conformance"]["status"], "PARTIAL")
                    self.assertIn(reason, command["cycle1_conformance"]["reasons"])
                    self.assertEqual(
                        command["candidate"]["integrity"]["integrity_check"], "ok"
                    )

    def test_fact_requires_valid_producer_endpoint_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite", {"project": "neutral"}
            )
            module = store.add_entity("rtl.sv/1:module", "top")
            signal = store.add_entity("rtl.sv/1:signal", "top.in")
            evidence = store.add_evidence(kind="source", locator="rtl/top.sv")
            with self.assertRaisesRegex(prototype.StoreError, "evidence"):
                store.add_fact(module, "rtl.hierarchy/1:contains", signal, producer="slang", evidence_ids=[])
            with self.assertRaisesRegex(prototype.StoreError, "producer"):
                store.add_fact(module, "rtl.hierarchy/1:contains", signal, producer="agent", evidence_ids=[evidence])
            with self.assertRaisesRegex(prototype.StoreError, "endpoint"):
                store.add_fact(signal, "rtl.hierarchy/1:contains", module, producer="slang", evidence_ids=[evidence])
            store.close()

    def test_ai_requires_existing_fact_support_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite", {"project": "neutral"}
            )
            module = store.add_entity("rtl.sv/1:module", "top")
            evidence = store.add_evidence(kind="source", locator="rtl/top.sv")
            with self.assertRaisesRegex(prototype.StoreError, "support"):
                store.add_ai_knowledge(
                    module, "rtl.ai/1:module-role", "Role.",
                    support_fact_ids=[], evidence_ids=[evidence],
                )
            with self.assertRaisesRegex(prototype.StoreError, "support"):
                store.add_ai_knowledge(
                    module, "rtl.ai/1:module-role", "Role.",
                    support_fact_ids=["f:missing"], evidence_ids=[evidence],
                )
            store.close()

    def test_candidate_create_does_not_overwrite_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.sqlite"
            database.write_bytes(b"current")
            with self.assertRaisesRegex(prototype.StoreError, "exists"):
                prototype.PrototypeStore.create(database, {"project": "neutral"})
            self.assertEqual(database.read_bytes(), b"current")

    def test_metadata_supported_write_path_is_insert_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite",
                {"project": "neutral", "build_sha256": "b" * 64},
            )
            audit = {"schema": "cycle1-v1", "status": "READY"}
            store.add_metadata_once("module_core_v1_cycle1_conformance", audit)
            self.assertEqual(
                store.metadata("module_core_v1_cycle1_conformance"), audit
            )
            with self.assertRaises(sqlite3.IntegrityError):
                store.add_metadata_once(
                    "module_core_v1_cycle1_conformance", {"status": "PARTIAL"}
                )
            self.assertEqual(
                store.metadata("module_core_v1_cycle1_conformance"), audit
            )
            with self.assertRaises(sqlite3.IntegrityError):
                store.add_metadata_once("build_sha256", "c" * 64)
            self.assertEqual(store.metadata("build_sha256"), "b" * 64)
            store.close()

    def test_sqlite_foreign_keys_reject_dangling_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite", {"project": "neutral"}
            )
            with self.assertRaises(sqlite3.IntegrityError):
                store.connection.execute(
                    "INSERT INTO ai_fact_support(ai_id, fact_id) VALUES (?, ?)",
                    ("ai:missing", "f:missing"),
                )
            store.close()

    def test_instance_identity_is_not_collapsed_into_shared_definition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite",
                {"project": "neutral", "build_sha256": "b" * 64},
            )
            parent = store.add_entity("rtl.sv/1:module", "top")
            definition = store.add_entity("rtl.sv/1:module", "bank")
            evidence = store.add_evidence(kind="source", locator="rtl/top.sv", start_line=2)
            instances = [
                store.add_entity(
                    "rtl.sv/1:instance", f"top.u_bank{index}",
                    attrs={"effective_parameters": {"BANK": index}},
                )
                for index in range(2)
            ]
            for instance in instances:
                store.add_fact(
                    parent, "rtl.hierarchy/1:contains", instance,
                    producer="slang", evidence_ids=[evidence],
                )
                store.add_fact(
                    instance, "rtl.hierarchy/1:instance-of", definition,
                    producer="slang", evidence_ids=[evidence],
                )
            self.assertEqual(
                store.connection.execute(
                    "SELECT count(*) FROM entity WHERE kind = 'rtl.sv/1:instance'"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                store.connection.execute(
                    "SELECT count(*) FROM entity WHERE kind = 'rtl.sv/1:module' AND qualified_name = 'bank'"
                ).fetchone()[0],
                1,
            )
            attrs = [
                json.loads(row[0]) for row in store.connection.execute(
                    "SELECT attrs_json FROM entity WHERE kind = 'rtl.sv/1:instance' ORDER BY qualified_name"
                )
            ]
            self.assertEqual(attrs, [
                {"effective_parameters": {"BANK": 0}},
                {"effective_parameters": {"BANK": 1}},
            ])
            store.close()

    def test_module_coverage_routes_only_exact_complete_request_direct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite",
                {"project": "neutral", "build_sha256": "b" * 64},
            )
            module = store.add_entity("rtl.sv/1:module", "top")
            evidence = store.add_evidence(
                kind="source", locator="rtl/top.sv", start_line=1,
                attrs={"sha256": "a" * 64},
            )
            review = store.add_evidence(
                kind="provenance", locator="review/reviewer.json",
                attrs={"sha256": "d" * 64, "role": "independent-review"},
            )
            children = [
                store.add_entity("rtl.sv/1:instance", f"top.u{index}")
                for index in range(2)
            ]
            definition = store.add_entity("rtl.sv/1:module", "bank")
            support = []
            for child in children:
                support.append(store.add_fact(
                    module, "rtl.hierarchy/1:contains", child,
                    producer="slang", evidence_ids=[evidence],
                ))
                support.append(store.add_fact(
                    child, "rtl.hierarchy/1:instance-of", definition,
                    producer="slang", evidence_ids=[evidence],
                ))
            for index, child in enumerate(children):
                port = store.add_entity(
                    "rtl.sv/1:port", f"top.u{index}.data_i",
                )
                binding_evidence = store.add_evidence(
                    kind="source", locator="rtl/top.sv", start_line=10 + index,
                    end_line=10 + index, attrs={"sha256": "a" * 64},
                )
                store.add_fact(
                    child, "rtl.hierarchy/1:contains", port,
                    producer="slang", evidence_ids=[binding_evidence],
                )
                support.append(store.add_fact(
                    port, "rtl.connectivity/1:binds-to",
                    store.add_entity("rtl.sv/1:signal", f"top.bus{index}"),
                    producer="slang", evidence_ids=[binding_evidence],
                    attrs={
                        "connection_status": "connected",
                        "actual": {"kind": "NamedValue", "text": f"bus{index}",
                                   "text_truncated": False},
                        "complete_connection_semantics": False,
                    },
                ))
            unpublished_port = store.add_entity("rtl.sv/1:port", "top.u0.secret_i")
            store.add_fact(
                children[0], "rtl.hierarchy/1:contains", unpublished_port,
                producer="slang", evidence_ids=[evidence],
            )
            store.add_fact(
                unpublished_port, "rtl.connectivity/1:binds-to",
                store.add_entity("rtl.sv/1:signal", "top.secret"),
                producer="slang", evidence_ids=[evidence],
                attrs={"actual": {"kind": "NamedValue", "text": "secret"}},
            )
            for section in prototype.MODULE_KNOWLEDGE_SECTIONS:
                claim = f"claim-{section}"
                knowledge = store.add_ai_knowledge(
                    module, "rtl.ai/1:module-role", f"Verified {section}.",
                    support_fact_ids=support, evidence_ids=[evidence],
                    attrs={
                        "section": section, "claim_id": claim,
                        "review_status": "verified",
                        "review_artifact_sha256": "d" * 64,
                    },
                )
                store.add_knowledge_coverage(
                    module,
                    section=section,
                    subtree_scope="direct",
                    status="ready",
                    child_instance_ids=children,
                    request_kinds=["module-understanding"],
                    evidence_ids=[evidence, review],
                    attrs={
                        "knowledge_ids": [knowledge],
                        "knowledge_digest": prototype.knowledge_id_digest([knowledge]),
                        "review_artifact_sha256": "d" * 64,
                        "required_claim_ids": [claim],
                        "covered_additional_requirements": [],
                    },
                )
            draft = store.add_ai_knowledge(
                module, "rtl.ai/1:module-role", "Unreviewed draft must not leak.",
                support_fact_ids=support, evidence_ids=[evidence],
                attrs={"section": "function", "claim_id": "draft",
                       "review_status": "draft"},
            )
            direct = prototype.route_module_knowledge(
                store,
                enabled=True,
                target_id=module,
                build_sha256="b" * 64,
                requested_sections=list(prototype.MODULE_KNOWLEDGE_SECTIONS),
                required_child_instance_ids=children,
                request_kind="module-understanding",
                additional_requirements=[],
                response_view="full",
            )
            self.assertEqual(direct["route"], "DIRECT")
            self.assertTrue(direct["candidate_accessed"])
            self.assertNotIn(draft, {item["ai_id"] for item in direct["knowledge"]})
            self.assertEqual(direct["coverage"]["missing_sections"], [])
            self.assertEqual(direct["coverage"]["covered_sections"], sorted(
                prototype.MODULE_KNOWLEDGE_SECTIONS
            ))
            self.assertTrue(direct["support_facts"])
            self.assertEqual(direct["returned_items"], sum(direct["returned_counts"].values()))
            self.assertEqual(direct["returned_counts"]["knowledge"], 4)
            evidence_by_locator = {item["locator"]: item for item in direct["evidence"]}
            self.assertEqual(evidence_by_locator["rtl/top.sv"]["attrs"]["sha256"], "a" * 64)
            self.assertEqual(
                evidence_by_locator["review/reviewer.json"]["attrs"]["role"],
                "independent-review",
            )
            self.assertFalse(direct["truncated"])

            with mock.patch.object(
                prototype, "_compact_returned_fact_attrs",
                side_effect=AssertionError("answer view expanded full Fact attributes"),
            ):
                answer = prototype.route_module_knowledge(
                    store,
                    enabled=True,
                    target_id=module,
                    build_sha256="b" * 64,
                    requested_sections=list(prototype.MODULE_KNOWLEDGE_SECTIONS),
                    required_child_instance_ids=children,
                    request_kind="module-understanding",
                    additional_requirements=[],
                    response_view="answer",
                )
            self.assertEqual(answer["route"], "DIRECT")
            self.assertEqual(answer["response_view"], "answer")
            self.assertEqual(
                {item["text"] for item in answer["knowledge"]},
                {f"Verified {section}." for section in prototype.MODULE_KNOWLEDGE_SECTIONS},
            )
            self.assertEqual(len(answer["structure"]["direct_instances"]), 2)
            self.assertEqual(len(answer["structure"]["definitions"]), 1)
            self.assertEqual(len(answer["source_evidence"]), 3)
            bindings = answer["structure"]["instance_bindings"]
            self.assertEqual(len(bindings), 2)
            self.assertEqual(
                [item["bindings"][0]["ref"] for item in bindings], ["F1", "F2"]
            )
            self.assertEqual(
                [(item["instance_ref"], item["bindings"][0]["formal"],
                  item["bindings"][0]["actual_text"]) for item in bindings],
                [("I1", "data_i", "bus0"), ("I2", "data_i", "bus1")],
            )
            self.assertTrue(all(item["bindings"][0]["source_evidence_refs"] for item in bindings))
            self.assertTrue(all(item["bindings"][0]["audit_ref"].startswith("fact:") for item in bindings))
            self.assertNotIn("secret_i", json.dumps(bindings))
            project = Path(directory)
            (project / "source").mkdir()
            (project / "analysis").mkdir()
            (project / "source/top.sv").write_text("module top; endmodule\n")
            (project / "rtl-project.json").write_text(json.dumps({
                "schema": "rtl-analysis-agent/project-v1", "name": "neutral",
                "source": "source", "analysis": "analysis", "revision": None,
                "entry": "top.sv",
            }))
            public = subprocess.run([
                str(ROOT / "src/rtl-pack/bin/rtl"), "model", "query",
                "--project", str(project), "--model", "candidate.sqlite",
                "--view", "module-knowledge", "--target-name", "top",
                "--build-sha256", "b" * 64,
            ], cwd=ROOT, capture_output=True, text=True, check=False)
            self.assertEqual(public.returncode, 0, public.stdout + public.stderr)
            public_payload = json.loads(public.stdout)
            self.assertEqual(public_payload["route"], "DIRECT")
            self.assertEqual(public_payload["returned_counts"]["bindings"], 2)
            self.assertEqual(public_payload["execution_summary"]["actual_supplement"], "UNAVAILABLE")
            self.assertTrue(all(item["source_evidence_refs"] for item in answer["knowledge"]))
            self.assertNotIn("support_facts", answer)
            self.assertFalse(answer["limitations"]["recursive_descendants_covered"])
            self.assertEqual(answer["coverage"]["missing_sections"], [])
            readable = prototype.render_module_knowledge_readable(
                answer,
                result_ref="rtlq-result-fixture",
                structured_ref="state/results/rtlq-result-fixture.json",
            )
            self.assertIn("# RTL module knowledge", readable)
            self.assertIn("Route: DIRECT", readable)
            self.assertIn("Verified function.", readable)
            self.assertIn("top.u0", readable)
            self.assertIn("data_i", readable)
            self.assertIn("bus0", readable)
            self.assertIn("rtl/top.sv:10", readable)
            self.assertIn("recursive_descendants_covered: false", readable)
            self.assertIn("rtlq-result-fixture", readable)
            self.assertIn("state/results/rtlq-result-fixture.json", readable)
            self.assertLess(
                len(json.dumps(answer, ensure_ascii=False, sort_keys=True).encode()),
                len(json.dumps(direct, ensure_ascii=False, sort_keys=True).encode()),
            )
            knowledge_ref = answer["knowledge"][0]["audit_ref"]
            audit = prototype.route_module_knowledge(
                store,
                enabled=True,
                target_id=module,
                build_sha256="b" * 64,
                requested_sections=list(prototype.MODULE_KNOWLEDGE_SECTIONS),
                required_child_instance_ids=children,
                request_kind="module-understanding",
                additional_requirements=[],
                response_view="audit",
                audit_refs=[knowledge_ref],
            )
            self.assertEqual(audit["response_view"], "audit")
            self.assertEqual(len(audit["audit_records"]["knowledge"]), 1)
            self.assertTrue(audit["audit_records"]["facts"])
            self.assertTrue(audit["audit_records"]["evidence"])
            fact_id = audit["audit_records"]["facts"][0]["fact_id"]
            evidence_id = audit["audit_records"]["evidence"][0]["evidence_id"]
            fact_audit = prototype.route_module_knowledge(
                store,
                enabled=True,
                target_id=module,
                build_sha256="b" * 64,
                requested_sections=list(prototype.MODULE_KNOWLEDGE_SECTIONS),
                required_child_instance_ids=children,
                request_kind="module-understanding",
                additional_requirements=[],
                response_view="audit",
                audit_refs=[f"fact:{fact_id}", f"evidence:{evidence_id}"],
            )
            self.assertEqual(len(fact_audit["audit_records"]["facts"]), 1)
            self.assertIn(
                evidence_id,
                {item["evidence_id"] for item in fact_audit["audit_records"]["evidence"]},
            )
            with self.assertRaisesRegex(prototype.StoreError, "published request material"):
                prototype.route_module_knowledge(
                    store,
                    enabled=True,
                    target_id=module,
                    build_sha256="b" * 64,
                    requested_sections=list(prototype.MODULE_KNOWLEDGE_SECTIONS),
                    required_child_instance_ids=children,
                    request_kind="module-understanding",
                    additional_requirements=[],
                    response_view="audit",
                    audit_refs=[f"knowledge:{draft}"],
                )

            for request, reason in (
                ({"requested_sections": ["function", "timing-guarantee"]}, "section-gap"),
                ({"required_child_instance_ids": children[:1]}, "child-identity-mismatch"),
                ({"additional_requirements": ["timing-guarantee"]}, "additional-requirement-gap"),
            ):
                arguments = {
                    "enabled": True,
                    "target_id": module,
                    "build_sha256": "b" * 64,
                    "requested_sections": list(prototype.MODULE_KNOWLEDGE_SECTIONS),
                    "required_child_instance_ids": children,
                    "request_kind": "module-understanding",
                    "additional_requirements": [],
                    **request,
                }
                result = prototype.route_module_knowledge(store, **arguments)
                self.assertEqual(result["route"], "CONTEXT")
                self.assertIn(reason, result["reasons"])
                context_readable = prototype.render_module_knowledge_readable(result)
                self.assertIn("Route: CONTEXT", context_readable)
                self.assertIn("Verified function.", context_readable)

            additional_gap = prototype.route_module_knowledge(
                store,
                enabled=True,
                target_id=module,
                build_sha256="b" * 64,
                requested_sections=list(prototype.MODULE_KNOWLEDGE_SECTIONS),
                required_child_instance_ids=children,
                request_kind="module-understanding",
                additional_requirements=["timing-guarantee"],
            )
            self.assertEqual(additional_gap["route"], "CONTEXT")
            self.assertEqual(additional_gap["coverage"]["missing_sections"], [])
            self.assertEqual(
                additional_gap["coverage"]["requested_additional_requirements"],
                ["timing-guarantee"],
            )
            self.assertEqual(
                additional_gap["coverage"]["covered_additional_requirements"], []
            )
            self.assertEqual(
                additional_gap["coverage"]["missing_additional_requirements"],
                ["timing-guarantee"],
            )
            self.assertEqual(
                additional_gap["coverage"]["unresolved_additional_requirements"], []
            )
            additional_readable = prototype.render_module_knowledge_readable(additional_gap)
            self.assertIn("Missing sections: NONE", additional_readable)
            self.assertIn(
                "Missing additional requirements: timing-guarantee", additional_readable
            )
            self.assertNotIn("- Missing: NONE", additional_readable)

            child_gap = prototype.route_module_knowledge(
                store,
                enabled=True,
                target_id=module,
                build_sha256="b" * 64,
                requested_sections=list(prototype.MODULE_KNOWLEDGE_SECTIONS),
                required_child_instance_ids=[children[0], "missing-child"],
                request_kind="module-understanding",
                additional_requirements=[],
            )
            self.assertEqual(child_gap["route"], "CONTEXT")
            child_readable = prototype.render_module_knowledge_readable(child_gap)
            self.assertIn("Missing direct children: missing-child", child_readable)

            stale = prototype.route_module_knowledge(
                store, enabled=True, target_id=module, build_sha256="c" * 64,
                requested_sections=["function"], required_child_instance_ids=children,
                request_kind="module-understanding", additional_requirements=[],
            )
            self.assertEqual(stale["route"], "BYPASS")
            self.assertEqual(stale["reasons"], ["build-identity-mismatch"])

            before = hashlib.sha256(store.database.read_bytes()).hexdigest()
            disabled = prototype.route_module_knowledge(
                store, enabled=False, target_id=module, build_sha256="b" * 64,
                requested_sections=["function"], required_child_instance_ids=children,
                request_kind="module-understanding", additional_requirements=[],
            )
            after = hashlib.sha256(store.database.read_bytes()).hexdigest()
            self.assertEqual(disabled["route"], "BYPASS")
            self.assertEqual(disabled["reasons"], ["knowledge-disabled"])
            self.assertFalse(disabled["candidate_accessed"])
            self.assertEqual(disabled["returned_items"], 0)
            self.assertEqual(disabled["coverage"]["missing_sections"], ["function"])
            self.assertEqual(before, after)
            store.close()

    def test_returned_fact_expression_is_compact_but_keeps_meaning(self) -> None:
        compact = prototype._compact_returned_fact_attrs({
            "connection_status": "connected",
            "actual": {
                "kind": "Concatenation", "text": "{bus[3:2], 2'b01}",
                "nodes": [{"large": "payload"}], "text_truncated": False,
                "references": [{
                    "kind": "Value", "name": "bus", "path": "top.bus",
                    "evidence": {"large": "payload"},
                }],
            },
        })
        self.assertEqual(compact["actual"]["text"], "{bus[3:2], 2'b01}")
        self.assertEqual(compact["actual"]["references"], [
            {"kind": "Value", "name": "bus", "path": "top.bus"}
        ])
        self.assertNotIn("nodes", compact["actual"])
        self.assertEqual(compact["connection_status"], "connected")

    def test_empty_or_unpublished_knowledge_bypasses_instead_of_empty_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite",
                {"project": "neutral", "build_sha256": "b" * 64},
            )
            module = store.add_entity("rtl.sv/1:module", "top")
            result = prototype.route_module_knowledge(
                store, enabled=True, target_id=module, build_sha256="b" * 64,
                requested_sections=["function"], required_child_instance_ids=[],
                request_kind="module-understanding", additional_requirements=[],
            )
            self.assertEqual(result["route"], "BYPASS")
            self.assertIn("no-usable-knowledge", result["reasons"])
            self.assertEqual(result["returned_items"], 0)
            self.assertEqual(result["knowledge"], [])

            readable = prototype.render_module_knowledge_readable(result)
            self.assertIn("Route: BYPASS", readable)
            self.assertIn("no-usable-knowledge", readable)
            self.assertIn("Missing sections: function", readable)
            store.close()

    def test_ready_coverage_rejects_missing_claim_and_fake_context_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite",
                {"project": "neutral", "build_sha256": "b" * 64},
            )
            module = store.add_entity("rtl.sv/1:module", "top")
            child = store.add_entity("rtl.sv/1:instance", "top.u0")
            source = store.add_evidence(
                kind="source", locator="rtl/top.sv", start_line=1,
                attrs={"sha256": "a" * 64},
            )
            review = store.add_evidence(
                kind="provenance", locator="review/reviewer.json",
                attrs={"sha256": "d" * 64, "role": "independent-review"},
            )
            fact = store.add_fact(
                module, "rtl.hierarchy/1:contains", child,
                producer="slang", evidence_ids=[source],
            )
            knowledge = store.add_ai_knowledge(
                module, "rtl.ai/1:module-role", "Verified function.",
                support_fact_ids=[fact], evidence_ids=[source],
                attrs={
                    "section": "function", "claim_id": "claim-a",
                    "review_status": "verified",
                    "review_artifact_sha256": "d" * 64,
                },
            )
            draft = store.add_ai_knowledge(
                module, "rtl.ai/1:module-role", "Draft function.",
                support_fact_ids=[fact], evidence_ids=[source],
                attrs={
                    "section": "function", "claim_id": "claim-draft",
                    "review_status": "draft",
                },
            )
            with self.assertRaisesRegex(prototype.StoreError, "required claim"):
                store.add_knowledge_coverage(
                    module, section="function", subtree_scope="direct",
                    status="ready", child_instance_ids=[child],
                    request_kinds=["module-understanding"],
                    evidence_ids=[source, review],
                    attrs={
                        "knowledge_ids": [knowledge],
                        "knowledge_digest": prototype.knowledge_id_digest([knowledge]),
                        "review_artifact_sha256": "d" * 64,
                        "required_claim_ids": ["claim-a", "claim-missing"],
                        "covered_additional_requirements": [],
                        "producer_context_id": "generator",
                        "verifier_context_id": "reviewer",
                    },
                )
            with self.assertRaisesRegex(prototype.StoreError, "unverified"):
                store.add_knowledge_coverage(
                    module, section="function", subtree_scope="direct",
                    status="ready", child_instance_ids=[child],
                    request_kinds=["module-understanding"],
                    evidence_ids=[source, review],
                    attrs={
                        "knowledge_ids": [knowledge, draft],
                        "knowledge_digest": prototype.knowledge_id_digest([knowledge, draft]),
                        "review_artifact_sha256": "d" * 64,
                        "required_claim_ids": ["claim-a"],
                        "covered_additional_requirements": [],
                    },
                )
            store.close()


class SlangExtractionTests(unittest.TestCase):
    def test_generic_fixture_produces_structure_binding_and_data_facts(self) -> None:
        ast = json.loads((ROOT / "tests/fixtures/project-model/slang-ast.json").read_text())
        ast["design"]["members"][0]["body"]["members"].append({
            "kind": "Instance", "name": "u_in_reused",
            "source_file": "rtl/top.sv", "source_line": 30,
            "connections": [], "body": "123 leaf_in",
        })
        spec = {
            "name": "neutral-scope",
            "top": "top",
            "files": ["rtl/top.sv", "rtl/leaf_out.sv", "rtl/leaf_in.sv"],
            "scopes": ["top", "leaf_out", "leaf_in"],
        }
        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite", {"project": "neutral"}
            )
            summary = prototype.extract_slang_ast(
                store, ast, spec, source_hashes={path: path for path in spec["files"]}
            )
            self.assertEqual(summary["modules"], 3)
            self.assertEqual(summary["instances"], 3)
            self.assertGreaterEqual(summary["assignments"], 1)
            predicates = {
                row[0] for row in store.connection.execute(
                    "SELECT DISTINCT predicate FROM fact"
                )
            }
            self.assertTrue({
                "rtl.config/1:uses-source",
                "rtl.config/1:top",
                "rtl.hierarchy/1:instance-of",
                "rtl.connectivity/1:binds-to",
                "rtl.behavior/1:reads",
                "rtl.behavior/1:writes",
                "rtl.behavior/1:data-depends-on",
            } <= predicates)
            self.assertEqual(store.integrity()["integrity_check"], "ok")
            self.assertEqual(
                store.connection.execute(
                    "SELECT count(*) FROM entity WHERE kind = ?",
                    ("rtl.boundary/1:unresolved",),
                ).fetchone()[0],
                0,
            )
            store.close()

    def test_expression_selection_and_complex_boundary_are_generic(self) -> None:
        assignment = {
            "kind": "Assignment",
            "source_file_start": "rtl/root.sv",
            "source_line_start": 5,
            "source_line_end": 5,
            "left": {"kind": "NamedValue", "symbol": "1 result"},
            "right": {
                "kind": "RangeSelect",
                "source_file_start": "rtl/root.sv",
                "source_line_start": 5,
                "source_line_end": 5,
                "value": {"kind": "NamedValue", "symbol": "2 input_bus"},
                "left": {"kind": "IntegerLiteral", "value": "3", "constant": "3"},
                "right": {"kind": "IntegerLiteral", "value": "0", "constant": "0"},
            },
        }
        ast = {
            "definitions": [{
                "kind": "Definition", "name": "root",
                "source_file": "rtl/root.sv", "source_line": 1,
            }],
            "design": {"kind": "Root", "members": [{
                "kind": "Instance", "name": "root",
                "source_file": "rtl/root.sv", "source_line": 1, "connections": [],
                "body": {"kind": "InstanceBody", "name": "root", "members": [
                    {"kind": "ContinuousAssign", "source_file": "rtl/root.sv", "source_line": 5, "assignment": assignment},
                    {"kind": "Case", "source_file_start": "rtl/root.sv", "source_line_start": 7, "source_line_end": 9},
                    {"kind": "UninstantiatedDef", "name": "u_external", "definitionName": "external_cell", "source_file": "rtl/root.sv", "source_line": 11, "ports": []},
                ]},
            }]},
        }
        spec = {
            "name": "neutral", "top": "root",
            "files": ["rtl/root.sv"], "scopes": ["root"],
        }
        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite", {"project": "neutral"}
            )
            summary = prototype.extract_slang_ast(
                store, ast, spec, source_hashes={"rtl/root.sv": "a"}
            )
            predicates = {
                row[0] for row in store.connection.execute(
                    "SELECT DISTINCT predicate FROM fact"
                )
            }
            self.assertIn("rtl.connectivity/1:selects-bits-from", predicates)
            self.assertIn("Case", summary["unsupported_shapes"])
            self.assertNotIn("rtl.behavior/1:priority-before", predicates)
            self.assertEqual(summary["unresolved"], 1)
            self.assertEqual(
                store.connection.execute(
                    "SELECT qualified_name FROM entity WHERE kind = 'rtl.boundary/1:unresolved'"
                ).fetchone()[0],
                "module:external_cell",
            )
            store.close()


class AiOverlayTests(unittest.TestCase):
    def test_overlay_uses_physical_fact_and_evidence_ids_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite", {"project": "neutral"}
            )
            module = store.add_entity("rtl.sv/1:module", "top")
            signal = store.add_entity("rtl.sv/1:signal", "top.value")
            evidence = store.add_evidence(kind="source", locator="rtl/top.sv", start_line=3)
            fact = store.add_fact(
                module, "rtl.hierarchy/1:contains", signal,
                producer="slang", evidence_ids=[evidence],
            )
            result = prototype.apply_ai_overlay(store, {
                "schema": "rtl-analysis-agent/ai-overlay-v01-candidate",
                "records": [{
                    "subject_id": module,
                    "knowledge_type": "rtl.ai/1:module-role",
                    "text": "Contains the source-backed value signal.",
                    "support_fact_ids": [fact],
                    "evidence_ids": [evidence],
                }],
            })
            self.assertEqual(result["inserted"], 1)
            self.assertEqual(store.connection.execute("SELECT count(*) FROM ai_knowledge").fetchone()[0], 1)
            store.close()


class ContextQueryTests(unittest.TestCase):
    def facade_project(
        self, directory: str
    ) -> tuple[Path, prototype.PrototypeStore]:
        project = Path(directory) / "project"
        (project / "source").mkdir(parents=True)
        (project / "analysis/model").mkdir(parents=True)
        (project / "source/top.sv").write_text("module top; endmodule\n")
        (project / "rtl-project.json").write_text(json.dumps({
            "schema": "rtl-analysis-agent/project-v1",
            "name": "neutral",
            "source": "source",
            "analysis": "analysis",
            "revision": None,
            "entry": "top.sv",
        }))
        store = prototype.PrototypeStore.create(
            project / "analysis/model/rtl-knowledge-v01-candidate.sqlite",
            {"project": "neutral"},
        )
        return project, store

    def populated_store(self, directory: str) -> tuple[prototype.PrototypeStore, str]:
        store = prototype.PrototypeStore.create(
            Path(directory) / "candidate.sqlite", {"project": "neutral"}
        )
        module = store.add_entity("rtl.sv/1:module", "top")
        output = store.add_entity("rtl.sv/1:signal", "top.output")
        source = store.add_entity("rtl.sv/1:signal", "top.source")
        evidence = store.add_evidence(
            kind="source", locator="rtl/top.sv", start_line=5, end_line=5
        )
        contains = store.add_fact(
            module, "rtl.hierarchy/1:contains", output,
            producer="slang", evidence_ids=[evidence],
        )
        store.add_fact(
            output, "rtl.behavior/1:data-depends-on", source,
            producer="slang", evidence_ids=[evidence],
        )
        store.add_ai_knowledge(
            module, "rtl.ai/1:module-role", "Contains source-backed output logic.",
            support_fact_ids=[contains], evidence_ids=[evidence],
        )
        return store, module

    def test_context_is_contract_role_bounded_and_includes_support(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, module = self.populated_store(directory)
            result = prototype.query_context(
                store, seed_entity_id=module,
                roles=["structure", "data-flow", "semantics"],
                max_depth=2, limit=20,
            )
            self.assertEqual(result["seed"]["entity_id"], module)
            self.assertEqual({row["predicate"] for row in result["deterministic_facts"]}, {
                "rtl.hierarchy/1:contains", "rtl.behavior/1:data-depends-on",
            })
            self.assertEqual(len(result["ai_knowledge"]), 1)
            self.assertEqual(len(result["ai_knowledge"][0]["support_fact_ids"]), 1)
            self.assertEqual(result["limits"]["max_depth"], 2)
            self.assertFalse(result["truncated"])
            tight = prototype.query_context(
                store, seed_entity_id=module,
                roles=["structure", "data-flow", "semantics"],
                max_depth=2, limit=2,
            )
            self.assertEqual(len(tight["ai_knowledge"]), 1)
            self.assertEqual(len(tight["deterministic_facts"]), 1)
            self.assertTrue(tight["truncated"])
            store.close()

    def test_context_rejects_unknown_role_depth_and_seed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, module = self.populated_store(directory)
            with self.assertRaisesRegex(prototype.StoreError, "role"):
                prototype.query_context(store, seed_entity_id=module, roles=["arbitrary"], max_depth=1, limit=10)
            with self.assertRaisesRegex(prototype.StoreError, "depth"):
                prototype.query_context(store, seed_entity_id=module, roles=["structure"], max_depth=3, limit=10)
            with self.assertRaisesRegex(prototype.StoreError, "seed"):
                prototype.query_context(store, seed_entity_id="e:missing", roles=["structure"], max_depth=1, limit=10)
            with self.assertRaisesRegex(prototype.StoreError, "profile"):
                prototype.query_context(
                    store, seed_entity_id=module, roles=["structure"],
                    profile="generic-ranking", max_depth=1, limit=10,
                )
            store.close()

    def test_driver_profile_follows_only_the_rhs_direction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite", {"project": "neutral"}
            )
            module = store.add_entity("rtl.sv/1:module", "top")
            target = store.add_entity("rtl.sv/1:signal", "top.target")
            left = store.add_entity("rtl.sv/1:signal", "top.left")
            right = store.add_entity("rtl.sv/1:signal", "top.right")
            assignment = store.add_entity("rtl.sv/1:assignment", "top.@assignment:10:1")
            expression = store.add_entity("rtl.sv/1:expression", "top.@expression:10:10")
            consumer = store.add_entity("rtl.sv/1:expression", "top.@expression:20:10")
            evidence = store.add_evidence(
                kind="source", locator="rtl/top.sv", start_line=10, end_line=10
            )
            for entity in (target, left, right, assignment, expression, consumer):
                store.add_fact(
                    module, "rtl.hierarchy/1:contains", entity,
                    producer="slang", evidence_ids=[evidence],
                )
            for predicate in ("rtl.connectivity/1:drives", "rtl.behavior/1:writes"):
                store.add_fact(
                    assignment, predicate, target,
                    producer="slang", evidence_ids=[evidence],
                )
            store.add_fact(
                assignment, "rtl.behavior/1:assigned-from", expression,
                producer="slang", evidence_ids=[evidence],
            )
            for operand in (left, right):
                store.add_fact(
                    expression, "rtl.behavior/1:reads", operand,
                    producer="slang", evidence_ids=[evidence],
                )
            store.add_fact(
                consumer, "rtl.behavior/1:reads", target,
                producer="slang", evidence_ids=[evidence],
            )
            result = prototype.query_context(
                store, seed_entity_id=target, roles=["data-flow"],
                profile="driver", max_depth=2, limit=20,
            )
            facts = result["deterministic_facts"]
            self.assertEqual(result["request"]["profile"], "driver")
            self.assertEqual({row["predicate"] for row in facts}, {
                "rtl.connectivity/1:drives", "rtl.behavior/1:writes",
                "rtl.behavior/1:assigned-from", "rtl.behavior/1:reads",
            })
            self.assertNotIn(consumer, {
                endpoint["entity_id"]
                for row in facts for endpoint in (row["subject"], row["object"])
            })
            self.assertTrue(all(row["contract"]["maturity"] == "supported" for row in facts))
            self.assertFalse(result["truncated"])
            store.close()

    def test_register_profile_prioritizes_writers_over_readers_and_reports_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite", {"project": "neutral"}
            )
            state = store.add_entity("rtl.sv/1:signal", "top.state", attrs={"type": "reg[1:0]"})
            source = store.add_entity("rtl.sv/1:signal", "top.next_state")
            assignment = store.add_entity(
                "rtl.sv/1:assignment", "top.@assignment:12:1", attrs={"nonblocking": True}
            )
            expression = store.add_entity("rtl.sv/1:expression", "top.@expression:12:10")
            evidence = store.add_evidence(
                kind="source", locator="rtl/top.sv", start_line=12, end_line=12
            )
            for predicate in ("rtl.connectivity/1:drives", "rtl.behavior/1:writes"):
                store.add_fact(
                    assignment, predicate, state,
                    producer="slang", evidence_ids=[evidence],
                )
            store.add_fact(
                assignment, "rtl.behavior/1:assigned-from", expression,
                producer="slang", evidence_ids=[evidence],
            )
            store.add_fact(
                expression, "rtl.behavior/1:reads", source,
                producer="slang", evidence_ids=[evidence],
            )
            for index in range(30):
                reader = store.add_entity("rtl.sv/1:expression", f"top.@reader:{index}")
                store.add_fact(
                    reader, "rtl.behavior/1:reads", state,
                    producer="slang", evidence_ids=[evidence],
                )
            result = prototype.query_context(
                store, seed_entity_id=state,
                roles=["state", "control", "clock-reset"],
                profile="register-state", max_depth=2, limit=20,
            )
            facts = result["deterministic_facts"]
            self.assertEqual([row["predicate"].rsplit(":", 1)[-1] for row in facts[:2]], [
                "drives", "writes",
            ])
            self.assertNotIn(state, {
                row["object"]["entity_id"]
                for row in facts if row["predicate"] == "rtl.behavior/1:reads"
            })
            missing = {row["name"] for row in result["retrieval_diagnostics"]["unavailable_relations"]}
            self.assertTrue({"RESET_VALUE", "NEXT_VALUE_FROM", "UPDATED_WHEN", "HOLDS_WHEN"} <= missing)
            self.assertFalse(result["truncated"])
            store.close()

    def test_module_overview_is_typed_and_does_not_dump_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite", {"project": "neutral"}
            )
            module = store.add_entity("rtl.sv/1:module", "top")
            leaf = store.add_entity("rtl.sv/1:module", "leaf")
            instance = store.add_entity("rtl.sv/1:instance", "top.u_leaf")
            evidence = store.add_evidence(
                kind="source", locator="rtl/top.sv", start_line=1, end_line=40
            )
            store.add_fact(
                module, "rtl.hierarchy/1:contains", instance,
                producer="slang", evidence_ids=[evidence],
            )
            store.add_fact(
                instance, "rtl.hierarchy/1:instance-of", leaf,
                producer="slang", evidence_ids=[evidence],
            )
            assignments = []
            for index in range(30):
                assignment = store.add_entity("rtl.sv/1:assignment", f"top.@assignment:{index}:1")
                assignments.append(assignment)
                store.add_fact(
                    module, "rtl.hierarchy/1:contains", assignment,
                    producer="slang", evidence_ids=[evidence],
                )
            signal = store.add_entity("rtl.sv/1:signal", "top.bus")
            store.add_fact(
                module, "rtl.hierarchy/1:contains", signal,
                producer="slang", evidence_ids=[evidence],
            )
            for assignment in assignments[:4]:
                store.add_fact(
                    assignment, "rtl.connectivity/1:drives", signal,
                    producer="slang", evidence_ids=[evidence],
                )
            for index in range(16):
                port = store.add_entity("rtl.sv/1:port", f"top.port_{index}")
                store.add_fact(
                    module, "rtl.hierarchy/1:contains", port,
                    producer="slang", evidence_ids=[evidence],
                )
            process = store.add_entity("rtl.sv/1:process", "top.@process:20:1")
            store.add_fact(
                module, "rtl.hierarchy/1:contains", process,
                producer="slang", evidence_ids=[evidence],
            )
            result = prototype.query_context(
                store, seed_entity_id=module, roles=["structure", "connectivity"],
                profile="module-overview", max_depth=2, limit=30,
            )
            self.assertFalse(result["truncated"])
            self.assertIn("INSTANCE_OF", result["retrieval_diagnostics"]["included_relation_names"])
            self.assertEqual(
                sum(row["kind"] == "rtl.sv/1:assignment" for row in result["entities"]),
                1,
            )
            self.assertGreater(result["retrieval_diagnostics"]["omitted_by_policy"].get("PORT", 0), 0)
            store.close()

    def test_cross_module_profile_preserves_binding_and_instance_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite", {"project": "neutral"}
            )
            parent = store.add_entity("rtl.sv/1:module", "top")
            leaf = store.add_entity("rtl.sv/1:module", "leaf")
            instance = store.add_entity("rtl.sv/1:instance", "top.u_leaf")
            port = store.add_entity("rtl.sv/1:port", "top.u_leaf.value")
            signal = store.add_entity("rtl.sv/1:signal", "top.value")
            evidence = store.add_evidence(
                kind="source", locator="rtl/top.sv", start_line=5, end_line=5
            )
            for subject, predicate, obj in (
                (parent, "rtl.hierarchy/1:contains", instance),
                (instance, "rtl.hierarchy/1:contains", port),
                (instance, "rtl.hierarchy/1:instance-of", leaf),
                (port, "rtl.connectivity/1:binds-to", signal),
            ):
                store.add_fact(subject, predicate, obj, producer="slang", evidence_ids=[evidence])
            result = prototype.query_context(
                store, seed_entity_id=signal, roles=["connectivity", "structure"],
                profile="cross-module", max_depth=2, limit=20,
            )
            predicates = {row["predicate"] for row in result["deterministic_facts"]}
            self.assertEqual(predicates, {
                "rtl.connectivity/1:binds-to", "rtl.hierarchy/1:contains",
                "rtl.hierarchy/1:instance-of",
            })
            self.assertFalse(result["truncated"])
            store.close()

    def test_readonly_context_does_not_modify_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, module = self.populated_store(directory)
            database = store.database
            store.close()
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            readonly = prototype.PrototypeStore.open(database, readonly=True)
            prototype.query_context(
                readonly, seed_entity_id=module, roles=["structure"], max_depth=1, limit=10
            )
            readonly.close()
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)

    def test_public_cli_context_view_uses_exact_entity_id_and_roles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            (project / "source").mkdir(parents=True)
            (project / "analysis").mkdir()
            (project / "source/top.sv").write_text("module top; endmodule\n")
            (project / "rtl-project.json").write_text(json.dumps({
                "schema": "rtl-analysis-agent/project-v1",
                "name": "neutral",
                "source": "source",
                "analysis": "analysis",
                "revision": None,
                "entry": "top.sv",
            }))
            database = project / "analysis/candidate.sqlite"
            store = prototype.PrototypeStore.create(database, {"project": "neutral"})
            module = store.add_entity("rtl.sv/1:module", "top")
            signal = store.add_entity("rtl.sv/1:signal", "top.value")
            evidence = store.add_evidence(kind="source", locator="top.sv", start_line=1)
            store.add_fact(
                module, "rtl.hierarchy/1:contains", signal,
                producer="slang", evidence_ids=[evidence],
            )
            store.close()
            result = subprocess.run([
                str(ROOT / "src/rtl-pack/bin/rtl"),
                "model", "query", "--project", str(project),
                "--model", "analysis/candidate.sqlite",
                "--view", "context",
                "--entity-id", module,
                "--role", "structure",
                "--profile", "module-overview",
                "--max-depth", "1",
                "--limit", "10",
            ], cwd=ROOT, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["view"], "context")
            self.assertEqual(payload["seed"]["entity_id"], module)
            self.assertEqual(payload["request"]["profile"], "module-overview")

    def test_public_cli_module_knowledge_off_uses_same_readonly_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            (project / "source").mkdir(parents=True)
            (project / "analysis").mkdir()
            (project / "source/top.sv").write_text("module top; endmodule\n")
            (project / "rtl-project.json").write_text(json.dumps({
                "schema": "rtl-analysis-agent/project-v1", "name": "neutral",
                "source": "source", "analysis": "analysis", "revision": None,
                "entry": "top.sv",
            }))
            database = project / "analysis/candidate.sqlite"
            store = prototype.PrototypeStore.create(
                database, {"project": "neutral", "build_sha256": "b" * 64}
            )
            module = store.add_entity("rtl.sv/1:module", "top")
            store.close()
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            result = subprocess.run([
                str(ROOT / "src/rtl-pack/bin/rtl"), "model", "query",
                "--project", str(project), "--model", "analysis/candidate.sqlite",
                "--view", "module-knowledge", "--entity-id", module,
                "--build-sha256", "b" * 64, "--knowledge", "off",
            ], cwd=ROOT, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["route"], "BYPASS")
            self.assertEqual(payload["response_view"], "answer")
            self.assertFalse(payload["candidate_accessed"])
            self.assertEqual(payload["execution_summary"]["returned_route"], "BYPASS")
            self.assertEqual(payload["execution_summary"]["actual_supplement"], "UNAVAILABLE")
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)

            natural = subprocess.run([
                str(ROOT / "src/rtl-pack/bin/rtl"), "model", "query",
                "--project", str(project), "--model", "analysis/candidate.sqlite",
                "--view", "module-knowledge", "--target-name", "top",
                "--build-sha256", "b" * 64, "--knowledge", "on",
            ], cwd=ROOT, capture_output=True, text=True, check=False)
            self.assertEqual(natural.returncode, 0, natural.stdout + natural.stderr)
            natural_payload = json.loads(natural.stdout)
            self.assertEqual(natural_payload["route"], "BYPASS")
            self.assertEqual(natural_payload["response_view"], "answer")
            self.assertEqual(natural_payload["target_resolution"]["resolved_by"], "exact")
            self.assertEqual(natural_payload["target_resolution"]["direct_child_instance_ids"], [])

            absent = subprocess.run([
                str(ROOT / "src/rtl-pack/bin/rtl"), "model", "query",
                "--project", str(project), "--model", "analysis/not-mounted.sqlite",
                "--view", "module-knowledge", "--knowledge", "off",
            ], cwd=ROOT, capture_output=True, text=True, check=False)
            self.assertEqual(absent.returncode, 0, absent.stdout + absent.stderr)
            absent_payload = json.loads(absent.stdout)
            self.assertEqual(absent_payload["route"], "BYPASS")
            self.assertFalse(absent_payload["candidate_accessed"])
            self.assertFalse((project / "analysis/not-mounted.sqlite").exists())

    def test_context_facade_resolves_unique_symbols_and_uses_fixed_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, store = self.facade_project(directory)
            store.add_entity("rtl.sv/1:module", "top")
            store.add_entity("rtl.sv/1:signal", "top.driver_signal")
            store.add_entity("rtl.sv/1:signal", "top.binding_signal")
            store.add_entity("rtl.sv/1:signal", "top.register_signal")
            store.close()
            expected = {
                "driver": ("driver_signal", "driver", "suffix"),
                "binding": ("binding_signal", "cross-module", "suffix"),
                "register": ("register_signal", "register-state", "suffix"),
                "module": ("top", "module-overview", "exact"),
            }
            for action, (symbol, profile, resolution) in expected.items():
                result = subprocess.run(
                    [str(ROOT / "src/rtl-pack/bin/rtl"), "context", action, symbol],
                    cwd=project, capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["status"], "pass")
                self.assertEqual(payload["context_tool"]["action"], action)
                self.assertEqual(payload["context_tool"]["resolved_by"], resolution)
                self.assertEqual(payload["request"]["profile"], profile)
                if action == "module":
                    self.assertIn("localizer", payload["context_tool"]["notice"])

    def test_context_facade_returns_stable_candidates_without_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, store = self.facade_project(directory)
            store.add_entity("rtl.sv/1:signal", "a.shared")
            store.add_entity("rtl.sv/1:port", "b.shared")
            store.close()
            result = subprocess.run(
                [str(ROOT / "src/rtl-pack/bin/rtl"), "context", "binding", "shared"],
                cwd=project, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "ambiguous")
            self.assertEqual(payload["candidates"], [
                {"kind": "rtl.sv/1:signal", "qualified_name": "a.shared"},
                {"kind": "rtl.sv/1:port", "qualified_name": "b.shared"},
            ])
            self.assertNotIn("seed", payload)

    def test_context_facade_reports_missing_symbol_without_fuzzy_matching(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, store = self.facade_project(directory)
            store.add_entity("rtl.sv/1:signal", "top.actual_name")
            store.close()
            result = subprocess.run(
                [str(ROOT / "src/rtl-pack/bin/rtl"), "context", "driver", "actual"],
                cwd=project, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "not-found")
            self.assertEqual(payload["candidates"], [])

    def test_selector_like_overlay_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = prototype.PrototypeStore.create(
                Path(directory) / "candidate.sqlite", {"project": "neutral"}
            )
            with self.assertRaisesRegex(prototype.StoreError, "shape"):
                prototype.apply_ai_overlay(store, {
                    "schema": "rtl-analysis-agent/ai-overlay-v01-candidate",
                    "records": [{"subject_selector": {"name": "top"}}],
                })
            store.close()

class SourceBuildTests(unittest.TestCase):
    def test_build_runs_only_slang_and_creates_independent_candidate(self) -> None:
        ast = json.loads((ROOT / "tests/fixtures/project-model/slang-ast.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            for name in ("top.sv", "leaf_out.sv", "leaf_in.sv"):
                (source / name).write_text(f"module {name[:-3]}; endmodule\n")
            ast_path = root / "fixture-ast.json"
            ast_path.write_text(json.dumps(ast))
            fake_slang = root / "slang"
            fake_slang.write_text(
                "#!/usr/bin/env python3\n"
                "import os, shutil, sys\n"
                "out = sys.argv[sys.argv.index('--ast-json') + 1]\n"
                "shutil.copyfile(os.environ['FAKE_SLANG_AST'], out)\n"
            )
            fake_slang.chmod(0o755)
            spec = root / "spec.json"
            spec.write_text(json.dumps({
                "name": "neutral",
                "top": "top",
                "files": ["top.sv", "leaf_out.sv", "leaf_in.sv"],
                "scopes": ["top", "leaf_out", "leaf_in"],
                "slang": {"allow_unknown_modules": True, "extra_args": []},
            }))
            output = root / "candidate.sqlite"
            previous = os.environ.get("FAKE_SLANG_AST")
            os.environ["FAKE_SLANG_AST"] = str(ast_path)
            try:
                result = prototype.build_source_candidate(
                    project_name="neutral",
                    source_revision="a" * 40,
                    source_root=source,
                    spec_path=spec,
                    database=output,
                    slang_executable=fake_slang,
                    slang_version="fixture",
                    temporary_root=root,
                )
            finally:
                if previous is None:
                    os.environ.pop("FAKE_SLANG_AST", None)
                else:
                    os.environ["FAKE_SLANG_AST"] = previous
            self.assertTrue(output.is_file())
            self.assertEqual(result["producer"], "slang")
            self.assertIn("--ignore-unknown-modules", result["command"])
            self.assertNotIn("yosys", " ".join(result["command"]).lower())
            self.assertEqual(result["summary"]["modules"], 3)
            self.assertEqual(result["integrity"]["integrity_check"], "ok")

if __name__ == "__main__":
    unittest.main()
