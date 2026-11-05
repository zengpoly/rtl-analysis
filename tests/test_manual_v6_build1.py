#!/usr/bin/env python3
"""Scope-aware Build1 contract and Manual V6 product tests."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "src/rtl-pack/lib"
sys.path.insert(0, str(LIB))

from rtl_knowledge_prototype import PrototypeStore, StoreError, MODULE_Q2_PUBLICATION_PATH  # noqa: E402
import rtl_manual_v6 as manual_v6  # noqa: E402


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ManualV6Build1Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_root = self.root / "source"
        (self.source_root / "rtl").mkdir(parents=True)
        self.sources = {
            "rtl/root_a.sv": "module root_a; leaf u_leaf(); endmodule\n",
            "rtl/root_b.sv": "module root_b; leaf u_leaf(); endmodule\n",
            "rtl/leaf.sv": "module leaf #(parameter WIDTH=8); endmodule\n",
            "rtl/closure_only.sv": "module closure_only; endmodule\n",
        }
        for relative, text in self.sources.items():
            (self.source_root / relative).write_text(text, encoding="utf-8")
        self.database = self.root / "candidate.sqlite"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_full_source_render_rejects_change_after_backbone_load(self) -> None:
        store, ids = self._make_store()
        self._register(
            store, mode="ACTUAL_INSTANCE_ROOT",
            roots=[self._root(ids["root_a"], store.contract_id("entities", "INSTANCE"))],
            source_paths=["rtl/root_a.sv", "rtl/leaf.sv"],
        )
        store.close()
        model = manual_v6.load_backbone(self.database, self.source_root)
        (self.source_root / "rtl/root_a.sv").write_text("module root_a; changed u_leaf(); endmodule\n")
        output = self.root / "changed-source-site"
        with self.assertRaisesRegex(ValueError, "Source.*changed"):
            manual_v6.build_site(model, output, source_viewer="full")
        self.assertFalse(output.exists())

    def _make_store(
        self, database: Path | None = None, *, real_q2: bool = False,
        publication_contract: Path = MODULE_Q2_PUBLICATION_PATH,
    ) -> tuple[PrototypeStore, dict[str, str]]:
        database = database or self.database
        store = PrototypeStore.create(
            database,
            {
                "project": "fixture",
                "source_revision": "0123456789abcdef",
                "build_sha256": "a" * 64,
                "build_spec_sha256": "b" * 64,
                "source_closure_sha256": "c" * 64,
                "effective_input_count": 4,
                **({"candidate_id": "synthetic-real-q2-candidate"} if real_q2 else {}),
            },
            schema_version="v0.2" if real_q2 else "v0.1",
            publication_contract=publication_contract if real_q2 else None,
        )
        kind = lambda name: store.contract_id("entities", name)
        predicate = lambda name: store.contract_id("facts", name)
        evidence = {
            relative: store.add_evidence(
                kind="source",
                locator=relative,
                start_line=1,
                end_line=1,
                attrs={"sha256": _sha(self.source_root / relative)},
            )
            for relative in self.sources
        }
        provenance = store.add_evidence(
            kind="provenance", locator="fixture-build.json", attrs={"sha256": "b" * 64}
        )
        project = store.add_entity(kind("PROJECT"), "fixture")
        config = store.add_entity(kind("CONFIGURATION"), "fixture@" + "a" * 64)
        store.add_fact(
            project,
            predicate("HAS_CONFIGURATION"),
            config,
            producer="build-spec",
            evidence_ids=[provenance],
        )
        source_ids = {}
        for relative in self.sources:
            source_ids[relative] = store.add_entity(
                kind("SOURCE_FILE"), relative, attrs={"sha256": _sha(self.source_root / relative)}
            )
            store.add_fact(
                config,
                predicate("USES_SOURCE"),
                source_ids[relative],
                producer="build-spec",
                evidence_ids=[provenance],
            )

        modules = {
            "root_a": store.add_entity(kind("MODULE"), "root_a", attrs={"source": "rtl/root_a.sv"}),
            "root_b": store.add_entity(kind("MODULE"), "root_b", attrs={"source": "rtl/root_b.sv"}),
            "leaf": store.add_entity(kind("MODULE"), "leaf", attrs={"source": "rtl/leaf.sv"}),
            "closure": store.add_entity(
                kind("MODULE"), "closure_only", attrs={"source": "rtl/closure_only.sv"}
            ),
        }
        instances = {
            "root_a": store.add_entity(
                kind("INSTANCE"), "fixture.root_a", attrs={"name": "root_a", "effective_parameters": []}
            ),
            "root_b": store.add_entity(
                kind("INSTANCE"), "fixture.root_b", attrs={"name": "root_b", "effective_parameters": []}
            ),
            "leaf_a": store.add_entity(
                kind("INSTANCE"),
                "fixture.root_a.u_leaf",
                attrs={"name": "u_leaf", "effective_parameters": [{"name": "WIDTH", "value": "8"}]},
            ),
            "leaf_b": store.add_entity(
                kind("INSTANCE"),
                "fixture.root_b.u_leaf",
                attrs={"name": "u_leaf", "effective_parameters": [{"name": "WIDTH", "value": "16"}]},
            ),
        }
        for name in ("root_a", "root_b"):
            store.add_fact(
                project,
                predicate("CONTAINS"),
                instances[name],
                producer="slang",
                evidence_ids=[evidence[f"rtl/{name}.sv"]],
            )
            store.add_fact(
                instances[name],
                predicate("INSTANCE_OF"),
                modules[name],
                producer="slang",
                evidence_ids=[evidence[f"rtl/{name}.sv"]],
            )
        for root_name, leaf_name in (("root_a", "leaf_a"), ("root_b", "leaf_b")):
            store.add_fact(
                instances[root_name],
                predicate("CONTAINS"),
                instances[leaf_name],
                producer="slang",
                evidence_ids=[evidence[f"rtl/{root_name}.sv"]],
            )
            store.add_fact(
                instances[leaf_name],
                predicate("INSTANCE_OF"),
                modules["leaf"],
                producer="slang",
                evidence_ids=[evidence["rtl/leaf.sv"]],
            )
        port = store.add_entity(
            kind("PORT"), "fixture.root_a.u_leaf.in_data", attrs={"direction": "In"}
        )
        signal = store.add_entity(kind("SIGNAL"), "fixture.root_a.data")
        store.add_fact(
            instances["leaf_a"], predicate("CONTAINS"), port,
            producer="slang", evidence_ids=[evidence["rtl/root_a.sv"]],
        )
        store.add_fact(
            port,
            predicate("BINDS_TO"),
            signal,
            producer="slang",
            evidence_ids=[evidence["rtl/root_a.sv"]],
            attrs={"actual": "data"},
        )
        return store, {**modules, **instances, **source_ids}

    def _register(
        self,
        store: PrototypeStore,
        *,
        mode: str,
        roots: list[dict[str, str]],
        source_paths: list[str],
        limitations: list[str] | None = None,
        producer_capability: dict[str, str] | None = None,
    ) -> dict[str, object]:
        return store.register_analysis_scope(
            scope_mode=mode,
            roots=roots,
            boundary_policy={
                "kind": "SOURCE_PATH_SET" if not roots else "ROOTED_CONTAINS_CLOSURE",
                "max_depth": 1,
                "knowledge_source_paths": source_paths,
            },
            dependency_policy={"kind": "REFERENCED_DEFINITIONS_AND_EVIDENCE"},
            applicability={"schema": "rtl-analysis-agent/applicability-v1", "constraints": {}},
            profile=(store.metadata("semantic_publication_contract")["profile"]
                     if store.schema_version == "v0.2" else {"id": "rtl-analysis-agent/build1", "version": "1"}),
            vocabulary={"id": "rtl-analysis-agent/knowledge-contract", "version": "0.1-candidate"},
            producer_capability=producer_capability or {
                "actual_hierarchy": "DIRECT", "semantic": "NOT_BUILT"
            },
            completeness={"structure": "COMPLETE_WITHIN_BOUNDARY", "semantic": "NOT_BUILT"},
            limitations=limitations or [],
        )

    def _root(self, entity_id: str, kind: str, role: str = "PRIMARY") -> dict[str, str]:
        return {
            "entity_id": entity_id,
            "kind": kind,
            "role": role,
            "resolution_state": "RESOLVED",
        }

    def test_scope_metadata_is_content_addressed_not_an_entity(self) -> None:
        store, ids = self._make_store()
        before = store.connection.execute("SELECT count(*) FROM entity").fetchone()[0]
        scope = self._register(
            store,
            mode="ACTUAL_INSTANCE_ROOT",
            roots=[self._root(ids["root_a"], store.contract_id("entities", "INSTANCE"))],
            source_paths=["rtl/root_a.sv", "rtl/leaf.sv"],
        )
        after = store.connection.execute("SELECT count(*) FROM entity").fetchone()[0]
        self.assertEqual(before, after)
        self.assertTrue(str(scope["scope_id"]).startswith("scope:"))
        self.assertEqual(store.metadata("analysis_scope"), scope)
        tables = {row[0] for row in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("analysis_scope", tables)
        mismatched = json.loads(json.dumps(scope))
        mismatched["build_identity"]["build_sha256"] = "f" * 64
        with self.assertRaises(StoreError):
            store.validate_analysis_scope(mismatched)
        with self.assertRaises(StoreError):
            self._register(store, mode="SOURCE_ONLY", roots=[], source_paths=["rtl/leaf.sv"])
        store.close()

    def test_scope_rejects_wrong_root_cardinality_kind_and_source(self) -> None:
        store, ids = self._make_store()
        instance = store.contract_id("entities", "INSTANCE")
        module = store.contract_id("entities", "MODULE")
        with self.assertRaises(StoreError):
            self._register(store, mode="ACTUAL_INSTANCE_ROOT", roots=[], source_paths=["rtl/root_a.sv"])
        with self.assertRaises(StoreError):
            self._register(
                store,
                mode="ACTUAL_INSTANCE_ROOT",
                roots=[self._root(ids["root_a"], module)],
                source_paths=["rtl/root_a.sv"],
            )
        with self.assertRaises(StoreError):
            self._register(
                store,
                mode="MULTI_ROOT",
                roots=[self._root(ids["root_a"], instance)],
                source_paths=["rtl/root_a.sv"],
            )
        with self.assertRaises(StoreError):
            self._register(
                store,
                mode="MODULE_DEFINITION_ROOT",
                roots=[self._root(ids["leaf"], module)],
                source_paths=["../escape.sv"],
            )
        store.close()

    def test_scope_applicability_is_a_canonical_finite_set_contract(self) -> None:
        store, ids = self._make_store()
        scope = store.register_analysis_scope(
            scope_mode="ACTUAL_INSTANCE_ROOT",
            roots=[self._root(ids["root_a"], store.contract_id("entities", "INSTANCE"))],
            boundary_policy={
                "kind": "ROOTED_CONTAINS_CLOSURE",
                "max_depth": 1,
                "knowledge_source_paths": ["rtl/root_a.sv", "rtl/leaf.sv"],
            },
            dependency_policy={"kind": "REFERENCED_DEFINITIONS_AND_EVIDENCE"},
            applicability={
                "schema": "rtl-analysis-agent/applicability-v1",
                "constraints": {
                    "parameter.MODE": ["safe", "fast", "safe"],
                    "instance_id": [ids["leaf_a"], ids["root_a"]],
                },
            },
            profile={"id": "rtl-analysis-agent/build1", "version": "1"},
            vocabulary={"id": "rtl-analysis-agent/knowledge-contract", "version": "0.1-candidate"},
            producer_capability={"hierarchy": "DIRECT"},
            completeness={"structure": "COMPLETE_WITHIN_BOUNDARY"},
            limitations=[],
        )
        self.assertEqual(
            scope["applicability"]["constraints"]["parameter.MODE"],
            ["fast", "safe"],
        )
        self.assertEqual(
            scope["applicability"]["constraints"]["instance_id"],
            sorted([ids["leaf_a"], ids["root_a"]]),
        )
        malformed = json.loads(json.dumps(scope))
        malformed["applicability"]["constraints"] = {"parameter.MODE": "safe"}
        with self.assertRaises(StoreError):
            store.validate_analysis_scope(malformed)
        for constraints in (
            {"unknown": ["x"]},
            {"parameter.bad-name": ["x"]},
            {"parameter.MODE": []},
            {"parameter.MODE": ["x", "x"]},
            {"parameter.MODE": ["x\ny"]},
        ):
            malformed = json.loads(json.dumps(scope))
            malformed["applicability"]["constraints"] = constraints
            with self.assertRaises(StoreError):
                store.validate_analysis_scope(malformed)
        store.close()

    def test_reversed_roots_and_set_like_fields_have_same_scope_identity(self) -> None:
        left_database = self.root / "left.sqlite"
        left, ids = self._make_store(left_database)
        left.close()
        right_database = self.root / "right.sqlite"
        shutil.copy2(left_database, right_database)
        instance = PrototypeStore.open(left_database)
        reverse = PrototypeStore.open(right_database)
        kind = instance.contract_id("entities", "INSTANCE")
        common = {
            "scope_mode": "MULTI_ROOT",
            "boundary_policy": {
                "kind": "ROOTED_CONTAINS_CLOSURE",
                "max_depth": 1,
                "knowledge_source_paths": ["rtl/root_b.sv", "rtl/leaf.sv", "rtl/root_a.sv"],
            },
            "dependency_policy": {"kind": "REFERENCED_DEFINITIONS_AND_EVIDENCE"},
            "applicability": {
                "schema": "rtl-analysis-agent/applicability-v1",
                "constraints": {"parameter.MODE": ["two", "one"]},
            },
            "profile": {"id": "rtl-analysis-agent/build1", "version": "1"},
            "vocabulary": {"id": "rtl-analysis-agent/knowledge-contract", "version": "0.1-candidate"},
            "producer_capability": {"hierarchy": "FULL"},
            "completeness": {"structure": "COMPLETE"},
        }
        root_a = self._root(ids["root_a"], kind, "PRIMARY")
        root_b = self._root(ids["root_b"], kind, "PEER")
        first = instance.register_analysis_scope(
            roots=[root_a, root_b], limitations=["z", "a", "z"], **common
        )
        reversed_common = json.loads(json.dumps(common))
        reversed_common["boundary_policy"]["knowledge_source_paths"].reverse()
        reversed_common["applicability"]["constraints"]["parameter.MODE"].reverse()
        second = reverse.register_analysis_scope(
            roots=[root_b, root_a], limitations=["a", "z"], **reversed_common
        )
        self.assertEqual(first["scope_id"], second["scope_id"])
        self.assertEqual(first, second)
        instance.close()
        reverse.close()

    def test_scope_translation_preserves_name_and_machine_data(self) -> None:
        store, ids = self._make_store()
        self._register(store, mode="MODULE_DEFINITION_ROOT",
                       roots=[self._root(ids["leaf"], store.contract_id("entities", "MODULE"))],
                       source_paths=["rtl/leaf.sv"])
        store.close()
        model = manual_v6.load_backbone(self.database, self.source_root)
        model["metadata"]["project"] = "Demo Roots"
        model["scope_presentation"] = manual_v6._scope_presentation(
            model["scope"], model["metadata"], model["entities"])
        self.assertEqual(model["scope_presentation"]["label"], "Demo Roots / leaf · Module Definition")
        before = json.dumps(model["scope_presentation"], sort_keys=True)
        manifest = manual_v6.build_site(model, self.root / "name-probe")
        for relative in ("index.html", manifest["page_map"][ids["leaf"]]):
            html = (self.root / "name-probe" / relative).read_text()
            self.assertIn("Demo Roots / leaf · 模块定义", html)
            self.assertNotIn("Demo 个根节点", html)
        self.assertEqual(json.dumps(model["scope_presentation"], sort_keys=True), before)

    def test_case_b_module_root_has_definition_without_actual_page(self) -> None:
        store, ids = self._make_store()
        self._register(
            store,
            mode="MODULE_DEFINITION_ROOT",
            roots=[self._root(ids["leaf"], store.contract_id("entities", "MODULE"))],
            source_paths=["rtl/leaf.sv"],
            limitations=["No elaborated actual configuration is available."],
        )
        store.close()
        model = manual_v6.load_backbone(self.database, self.source_root)
        manifest = manual_v6.build_site(model, self.root / "site-b")
        self.assertEqual(manifest["page_counts"]["scope"], 1)
        self.assertEqual(manifest["page_counts"]["instance"], 0)
        self.assertEqual(manifest["page_counts"]["module_definition"], 1)
        module_page = self.root / "site-b" / manifest["page_map"][ids["leaf"]]
        text = module_page.read_text(encoding="utf-8")
        self.assertIn("MODULE_DEFINITION", text)
        self.assertIn("实际配置", text)
        self.assertIn("NOT_AVAILABLE", text)
        self.assertIn("语义尚未构建（NOT_BUILT）", text)
        self.assertNotIn("fixture.root_a.u_leaf", text)
        self.assertEqual(
            model["scope_presentation"]["label"],
            "Fixture / leaf · Module Definition",
        )
        self.assertIn("分析范围", text)

    def test_case_c_multi_root_forest_has_no_fake_parent_and_unique_urls(self) -> None:
        store, ids = self._make_store()
        instance_kind = store.contract_id("entities", "INSTANCE")
        self._register(
            store,
            mode="MULTI_ROOT",
            roots=[
                self._root(ids["root_a"], instance_kind, "PRIMARY"),
                self._root(ids["root_b"], instance_kind, "PEER"),
            ],
            source_paths=["rtl/root_a.sv", "rtl/root_b.sv", "rtl/leaf.sv"],
        )
        store.close()
        model = manual_v6.load_backbone(self.database, self.source_root)
        manifest = manual_v6.build_site(model, self.root / "site-c")
        tree = model["tree"]
        self.assertEqual(tree["root_ids"], [ids["root_a"], ids["root_b"]])
        by_id = {node["id"]: node for node in tree["nodes"]}
        self.assertIsNone(by_id[ids["root_a"]]["parent_id"])
        self.assertIsNone(by_id[ids["root_b"]]["parent_id"])
        self.assertEqual(by_id[ids["leaf_a"]]["parent_id"], ids["root_a"])
        self.assertEqual(by_id[ids["leaf_b"]]["parent_id"], ids["root_b"])
        leaf_urls = {manifest["page_map"][ids["leaf_a"]], manifest["page_map"][ids["leaf_b"]]}
        self.assertEqual(len(leaf_urls), 2)
        self.assertTrue(all(ids[item].split(":", 1)[1] in manifest["page_map"][ids[item]] for item in ("leaf_a", "leaf_b")))
        rendered_tree = json.loads((self.root / "site-c/assets/tree.json").read_text(encoding="utf-8"))
        self.assertEqual(rendered_tree["root_ids"], [ids["root_a"], ids["root_b"]])
        tree_node_ids = {node["id"] for node in rendered_tree["nodes"]}
        self.assertTrue(set(rendered_tree["groups"]["Sources"]) <= tree_node_ids)
        search = json.loads((self.root / "site-c/assets/search-index.json").read_text(encoding="utf-8"))
        self.assertTrue({ids["leaf_a"], ids["leaf_b"]} <= {item["id"] for item in search})
        self.assertNotIn("PROJECT", (self.root / "site-c/index.html").read_text(encoding="utf-8"))
        self.assertEqual(model["scope_presentation"]["label"], "Fixture · 2 Roots")
        self.assertIn("Fixture · 2 个根节点", (self.root / "site-c/index.html").read_text())

    def test_case_d_source_only_does_not_publish_build_closure_file(self) -> None:
        store, ids = self._make_store()
        self._register(
            store,
            mode="SOURCE_ONLY",
            roots=[],
            source_paths=["rtl/leaf.sv"],
            limitations=["No reliable elaborated root was resolved."],
        )
        store.close()
        model = manual_v6.load_backbone(self.database, self.source_root)
        manifest = manual_v6.build_site(model, self.root / "site-d")
        self.assertEqual(model["scope"]["roots"], [])
        self.assertEqual(manifest["page_counts"]["instance"], 0)
        self.assertEqual(manifest["page_counts"]["module_definition"], 1)
        self.assertEqual(manifest["page_counts"]["source"], 1)
        self.assertNotIn(ids["closure"], manifest["page_map"])
        self.assertNotIn(ids["rtl/closure_only.sv"], manifest["page_map"])
        index = (self.root / "site-d/index.html").read_text(encoding="utf-8")
        self.assertIn("未能解析出可确认的已展开根节点。", index)
        self.assertIn("没有可确认的知识根节点", index)
        self.assertIn("Fixture · 源码 / 定义范围", index)
        self.assertEqual(
            model["scope_presentation"]["label"],
            "Fixture · Source / Definition Scope",
        )

    def test_whole_scope_label_is_generic_and_global(self) -> None:
        store, ids = self._make_store()
        self._register(
            store,
            mode="ACTUAL_INSTANCE_ROOT",
            roots=[self._root(ids["root_a"], store.contract_id("entities", "INSTANCE"))],
            source_paths=["rtl/root_a.sv", "rtl/leaf.sv"],
            producer_capability={
                "actual_hierarchy": "FULL", "semantic": "NOT_BUILT"
            },
        )
        store.close()
        model = manual_v6.load_backbone(self.database, self.source_root)
        manifest = manual_v6.build_site(model, self.root / "whole")
        self.assertEqual(model["scope_presentation"]["label"], "Fixture · Whole Design")
        self.assertEqual(manifest["page_counts"]["source"], 2)
        for page in (self.root / "whole").rglob("*.html"):
            text = page.read_text(encoding="utf-8")
            self.assertIn("分析范围", text)
            self.assertIn("Fixture · 整体设计", text)

    def test_instance_definition_navigation_parameters_bindings_and_spans(self) -> None:
        store, ids = self._make_store()
        self._register(
            store,
            mode="ACTUAL_INSTANCE_ROOT",
            roots=[self._root(ids["root_a"], store.contract_id("entities", "INSTANCE"))],
            source_paths=["rtl/root_a.sv", "rtl/leaf.sv"],
        )
        store.close()
        model = manual_v6.load_backbone(self.database, self.source_root)
        self.assertEqual(model["scope_presentation"]["label"], "Fixture / root_a")
        manifest = manual_v6.build_site(model, self.root / "site-a")
        instance_page = self.root / "site-a" / manifest["page_map"][ids["leaf_a"]]
        module_page = self.root / "site-a" / manifest["page_map"][ids["leaf"]]
        instance_text = instance_page.read_text(encoding="utf-8")
        module_text = module_page.read_text(encoding="utf-8")
        self.assertIn(manifest["page_map"][ids["leaf"]].split("/")[-1], instance_text)
        self.assertIn(manifest["page_map"][ids["leaf_a"]].split("/")[-1], module_text)
        self.assertIn("WIDTH", instance_text)
        self.assertIn("fixture.root_a.data", instance_text)
        self.assertIn("#L1-L1", instance_text)
        source_page = self.root / "site-a" / manifest["page_map"][ids["rtl/leaf.sv"]]
        source_text = source_page.read_text(encoding="utf-8")
        self.assertIn('id="L1"', source_text)
        self.assertNotIn('id="L2"', source_text)
        self.assertIn("语义尚未构建（NOT_BUILT）", instance_text)
        self.assertIn("语义尚未构建（NOT_BUILT）", module_text)

    def test_each_preview_fact_has_its_own_source_grounding_without_structured_drift(self) -> None:
        store, ids = self._make_store()
        contains = store.contract_id("facts", "CONTAINS")
        instance_of = store.contract_id("facts", "INSTANCE_OF")
        closure_evidence = store.connection.execute(
            "SELECT evidence_id FROM evidence WHERE kind='source' AND locator=?",
            ("rtl/closure_only.sv",),
        ).fetchone()[0]
        store.add_fact(
            ids["root_a"], contains, ids["leaf_a"], producer="slang",
            evidence_ids=[closure_evidence],
        )
        self._register(
            store,
            mode="ACTUAL_INSTANCE_ROOT",
            roots=[self._root(ids["root_a"], store.contract_id("entities", "INSTANCE"))],
            source_paths=["rtl/root_a.sv", "rtl/leaf.sv", "rtl/closure_only.sv"],
        )
        store.close()

        model = manual_v6.load_backbone(self.database, self.source_root)
        locator_output = self.root / "fact-grounding-locator"
        full_output = self.root / "fact-grounding-full"
        locator_manifest = manual_v6.build_site(
            model, locator_output, source_viewer="locator"
        )
        full_manifest = manual_v6.build_site(model, full_output, source_viewer="full")

        root_locator = (
            locator_output / locator_manifest["page_map"][ids["root_a"]]
        ).read_text(encoding="utf-8")
        leaf_module_locator = (
            locator_output / locator_manifest["page_map"][ids["leaf"]]
        ).read_text(encoding="utf-8")
        root_full = (
            full_output / full_manifest["page_map"][ids["root_a"]]
        ).read_text(encoding="utf-8")
        leaf_module_full = (
            full_output / full_manifest["page_map"][ids["leaf"]]
        ).read_text(encoding="utf-8")

        def fact_row(html: str, predicate: str) -> str:
            matches = [
                row for row in re.findall(r"<tr>(.*?)</tr>", html, flags=re.DOTALL)
                if f"<code>{predicate}</code>" in row
            ]
            self.assertEqual(len(matches), 1)
            return matches[0]

        root_contains_locator = fact_row(root_locator, contains)
        root_instance_locator = fact_row(root_locator, instance_of)
        module_instance_locator = fact_row(leaf_module_locator, instance_of)
        self.assertIn("源码证据", root_locator)
        self.assertIn("rtl/root_a.sv:L1-L1", root_contains_locator)
        self.assertIn("rtl/closure_only.sv:L1-L1", root_contains_locator)
        self.assertIn("rtl/root_a.sv:L1-L1", root_instance_locator)
        self.assertNotIn("rtl/closure_only.sv", root_instance_locator)
        self.assertIn("rtl/leaf.sv:L1-L1", module_instance_locator)
        self.assertNotIn(".sv.html#L1-L1", root_locator)
        self.assertNotIn(".sv.html#L1-L1", leaf_module_locator)

        root_contains_full = fact_row(root_full, contains)
        module_instance_full = fact_row(leaf_module_full, instance_of)
        self.assertIn("../source/rtl/root_a.sv.html#L1-L1", root_contains_full)
        self.assertIn("../source/rtl/closure_only.sv.html#L1-L1", root_contains_full)
        self.assertIn("../source/rtl/leaf.sv.html#L1-L1", module_instance_full)

        root_structured = locator_manifest["structured_detail_map"][ids["root_a"]]
        module_structured = locator_manifest["structured_detail_map"][ids["leaf"]]
        for relative in (root_structured, module_structured):
            locator_bytes = (locator_output / relative).read_bytes()
            self.assertEqual(locator_bytes, (full_output / relative).read_bytes())
            detail = json.loads(locator_bytes)
            for fact in detail["deterministic_facts"]:
                self.assertEqual(
                    set(fact),
                    {
                        "attrs", "evidence_ids", "fact_id", "object_id",
                        "predicate", "producer", "subject_id",
                    },
                )
                self.assertTrue(fact["evidence_ids"])

    def test_fact_without_source_evidence_is_explicitly_not_available(self) -> None:
        store, ids = self._make_store()
        instance_of = store.contract_id("facts", "INSTANCE_OF")
        fact_id = store.connection.execute(
            "SELECT fact_id FROM fact WHERE subject_id=? AND predicate=?",
            (ids["root_a"], instance_of),
        ).fetchone()[0]
        provenance_id = store.connection.execute(
            "SELECT evidence_id FROM evidence WHERE kind='provenance'"
        ).fetchone()[0]
        store.connection.execute("DELETE FROM fact_evidence WHERE fact_id=?", (fact_id,))
        store.connection.execute(
            "INSERT INTO fact_evidence(fact_id, evidence_id) VALUES (?, ?)",
            (fact_id, provenance_id),
        )
        self._register(
            store,
            mode="ACTUAL_INSTANCE_ROOT",
            roots=[self._root(ids["root_a"], store.contract_id("entities", "INSTANCE"))],
            source_paths=["rtl/root_a.sv", "rtl/leaf.sv"],
        )
        store.close()
        model = manual_v6.load_backbone(self.database, self.source_root)
        output = self.root / "fact-grounding-unavailable"
        manifest = manual_v6.build_site(model, output, source_viewer="locator")
        html = (output / manifest["page_map"][ids["root_a"]]).read_text(encoding="utf-8")
        rows = [
            row for row in re.findall(r"<tr>(.*?)</tr>", html, flags=re.DOTALL)
            if f"<code>{instance_of}</code>" in row
        ]
        self.assertEqual(len(rows), 1)
        self.assertIn("NOT_AVAILABLE", rows[0])

    def test_condensed_html_and_structured_detail_share_complete_page_view(self) -> None:
        store, ids = self._make_store()
        parameters = [{"name": "WIDTH", "value": "8"}] + [
            {"name": f"P{index:02d}", "value": str(index), "overridden": index % 7 == 0}
            for index in range(39)
        ]
        store.connection.execute(
            "UPDATE entity SET attrs_json=? WHERE entity_id=?",
            (json.dumps({
                "name": "u_leaf",
                "effective_parameters": parameters,
                "parameters_truncated": False,
            }, sort_keys=True, separators=(",", ":")), ids["leaf_a"]),
        )
        self._register(
            store,
            mode="ACTUAL_INSTANCE_ROOT",
            roots=[self._root(ids["root_a"], store.contract_id("entities", "INSTANCE"))],
            source_paths=["rtl/root_a.sv", "rtl/leaf.sv"],
        )
        store.close()
        model = manual_v6.load_backbone(self.database, self.source_root)
        output = self.root / "structured"
        manifest = manual_v6.build_site(model, output, source_viewer="locator")
        self.assertEqual(manifest["structured_detail_counts"], {
            "instance": 2, "module_definition": 2,
        })
        structured_path = manifest["structured_detail_map"][ids["leaf_a"]]
        detail = json.loads((output / structured_path).read_text(encoding="utf-8"))
        self.assertEqual(detail["schema"], "rtl-analysis-agent/manual-v6-structured-detail-v1")
        self.assertEqual(len(detail["effective_parameters"]), 40)
        self.assertEqual(len(detail["bindings"]), 1)
        self.assertEqual(detail["bindings"][0]["formal"], "in_data")
        self.assertEqual(detail["bindings"][0]["evidence"]["path"], "rtl/root_a.sv")
        self.assertEqual(detail["bindings"][0]["evidence"]["sha256"], _sha(self.source_root / "rtl/root_a.sv"))
        self.assertEqual(detail["semantic"], "NOT_BUILT")
        page_path = manifest["page_map"][ids["leaf_a"]]
        html = (output / page_path).read_text(encoding="utf-8")
        self.assertIn("查看结构化数据", html)
        self.assertIn("40 项生效参数", html)
        self.assertNotIn(">P38<", html)
        self.assertIn("结构邻域", html)
        self.assertIn("仅展示直接编译器事实", html)
        self.assertIn(_sha(self.source_root / "rtl/root_a.sv")[:12], html)
        self.assertLess(len(html.encode("utf-8")), 80_000)

        module_detail_path = manifest["structured_detail_map"][ids["leaf"]]
        module_detail = json.loads(
            (output / module_detail_path).read_text(encoding="utf-8")
        )
        self.assertEqual(len(module_detail["known_instances"]), 1)
        self.assertEqual(module_detail["known_instances"][0]["entity_id"], ids["leaf_a"])

    def test_source_path_and_hash_are_fail_closed(self) -> None:
        store, ids = self._make_store()
        self._register(
            store,
            mode="MODULE_DEFINITION_ROOT",
            roots=[self._root(ids["leaf"], store.contract_id("entities", "MODULE"))],
            source_paths=["rtl/leaf.sv"],
        )
        store.close()
        (self.source_root / "rtl/leaf.sv").write_text("changed\n", encoding="utf-8")
        with self.assertRaises(manual_v6.SourceSafetyError):
            manual_v6.load_backbone(self.database, self.source_root)

    def test_candidate_database_symlink_is_rejected(self) -> None:
        store, ids = self._make_store()
        self._register(
            store,
            mode="MODULE_DEFINITION_ROOT",
            roots=[self._root(ids["leaf"], store.contract_id("entities", "MODULE"))],
            source_paths=["rtl/leaf.sv"],
        )
        store.close()
        alias = self.root / "candidate-alias.sqlite"
        alias.symlink_to(self.database)
        with self.assertRaises(ValueError):
            manual_v6.load_backbone(alias, self.source_root)

    def test_same_candidate_clean_build_is_byte_identical(self) -> None:
        store, ids = self._make_store()
        self._register(
            store,
            mode="MULTI_ROOT",
            roots=[
                self._root(ids["root_a"], store.contract_id("entities", "INSTANCE")),
                self._root(ids["root_b"], store.contract_id("entities", "INSTANCE")),
            ],
            source_paths=["rtl/root_a.sv", "rtl/root_b.sv", "rtl/leaf.sv"],
        )
        store.close()
        model = manual_v6.load_backbone(self.database, self.source_root)
        left, right = self.root / "left", self.root / "right"
        manual_v6.build_site(model, left)
        manual_v6.build_site(model, right)
        left_hashes = {
            path.relative_to(left).as_posix(): _sha(path)
            for path in left.rglob("*") if path.is_file()
        }
        right_hashes = {
            path.relative_to(right).as_posix(): _sha(path)
            for path in right.rglob("*") if path.is_file()
        }
        self.assertEqual(left_hashes, right_hashes)

    def test_locator_source_mode_keeps_resources_without_source_html(self) -> None:
        store, ids = self._make_store()
        self._register(
            store,
            mode="ACTUAL_INSTANCE_ROOT",
            roots=[self._root(ids["root_a"], store.contract_id("entities", "INSTANCE"))],
            source_paths=["rtl/root_a.sv", "rtl/leaf.sv"],
        )
        store.close()
        model = manual_v6.load_backbone(self.database, self.source_root)
        output = self.root / "locator-site"
        manifest = manual_v6.build_site(model, output, source_viewer="locator")
        self.assertEqual(manifest["page_counts"]["source"], 0)
        self.assertEqual(manifest["resource_counts"]["source"], 2)
        self.assertFalse((output / "source").exists())
        page = (output / manifest["page_map"][ids["leaf_a"]]).read_text(encoding="utf-8")
        self.assertIn("rtl/root_a.sv:L1-L1", page)
        tree = json.loads((output / "assets/tree.json").read_text(encoding="utf-8"))
        source_nodes = [item for item in tree["nodes"] if item["kind"] == "SOURCE_FILE"]
        self.assertEqual(len(source_nodes), 2)
        self.assertTrue(all(item["path"] is None for item in source_nodes))
        self.assertTrue((output / "assets/tree-data.js").is_file())
        self.assertTrue((output / "assets/search-data.js").is_file())
        self.assertIn("data-presentation-group=\"RTL Structure\"", page)

    def test_optional_draft_brand_does_not_change_default_brand(self) -> None:
        store, ids = self._make_store()
        self._register(
            store,
            mode="ACTUAL_INSTANCE_ROOT",
            roots=[self._root(ids["root_a"], store.contract_id("entities", "INSTANCE"))],
            source_paths=["rtl/root_a.sv", "rtl/leaf.sv"],
        )
        store.close()
        model = manual_v6.load_backbone(self.database, self.source_root)
        normal = self.root / "normal-brand"
        draft = self.root / "draft-brand"
        manual_v6.build_site(model, normal)
        manual_v6.build_site(model, draft, manual_brand="RTL Build2 Draft Manual")
        self.assertIn("RTL Build1 结构手册", (normal / "index.html").read_text())
        self.assertIn("RTL Build2 草稿手册（未审核）", (draft / "index.html").read_text())
        self.assertIn("RTL Build2 草稿手册（未审核）", (draft / model["modules"][0]["output_path"]).read_text())
        self.assertNotIn("RTL Build2 草稿手册（未审核）", (normal / "index.html").read_text())

    def test_mapping_template_family_provenance_and_sidebar_contract(self) -> None:
        store, ids = self._make_store()
        self._register(
            store,
            mode="ACTUAL_INSTANCE_ROOT",
            roots=[self._root(ids["root_a"], store.contract_id("entities", "INSTANCE"))],
            source_paths=["rtl/root_a.sv", "rtl/leaf.sv"],
        )
        store.close()
        model = manual_v6.load_backbone(self.database, self.source_root)
        output = self.root / "mapping-contract"
        manifest = manual_v6.build_site(model, output, source_viewer="full")

        root_actual = (output / manifest["page_map"][ids["root_a"]]).read_text()
        deep_actual = (output / manifest["page_map"][ids["leaf_a"]]).read_text()
        definition = (output / manifest["page_map"][ids["leaf"]]).read_text()
        source = (output / manifest["page_map"][ids["rtl/leaf.sv"]]).read_text()
        scope = (output / "index.html").read_text()

        self.assertIn('data-page-template="instance"', root_actual)
        self.assertIn('data-page-template="instance"', deep_actual)
        self.assertIn('data-page-template="module-definition"', definition)
        self.assertIn('data-page-template="source-file"', source)
        self.assertIn('data-page-template="scope-overview"', scope)
        for page in (scope, root_actual, deep_actual, definition, source):
            self.assertIn('lang="zh-CN"', page)
            self.assertIn('charset="utf-8"', page)
            self.assertIn("查找对象", page)
        for page in (root_actual, deep_actual, definition):
            self.assertIn('data-provenance="compiler-fact"', page)
            self.assertIn("Slang / 编译器事实", page)
            self.assertIn('data-provenance="source-evidence"', page)
            self.assertIn("RTL 源码 / 源码证据", page)
            self.assertIn('data-provenance="ai-reviewed-semantic"', page)
            self.assertIn("AI / 经审核语义", page)
            self.assertIn("NOT_BUILT", page)
            self.assertIn("编译器事实或源码证据", page)
        self.assertIn('data-provenance="source-evidence"', source)

        css = (ROOT / "src/rtl-pack/templates/manual-v6/assets/manual.css").read_text()
        for token in (
            "--manual-sidebar-width",
            "--provenance-ai",
            "--provenance-fact",
            "--provenance-source",
            "grid-template-columns:var(--manual-sidebar-width)",
        ):
            self.assertIn(token, css)
        self.assertNotIn("grid-template-columns:310px", css)

    def _semantic_validation_payload(
        self, definition: str = "leaf", fact_id: str | None = None
    ) -> dict[str, object]:
        return {
            "schema": "rtl-analysis-agent/manual-v2-module-validation-v1",
            "publication_status": "DRAFT_UNREVIEWED",
            "subject": {"definition": definition},
            "overview": [{
                "id": "OV-1", "text": "这是一个有源码支持的模块概览。",
                "status": "DRAFT_UNREVIEWED", "support_type": "SOURCE_ONLY",
                "support": ["SRC-1"],
            }],
            "responsibilities": [{
                "id": "R-1", "text": "保留输入并产生模块输出。",
                "status": "DRAFT_UNREVIEWED",
                "support_type": "COMPILER_FACT_AND_SOURCE",
                "support": ["FACT-1", "SRC-1"],
            }],
            "key_interfaces": [{
                "group": "关键输入", "why_key": "定义模块的主要输入边界。",
                "signals": ["in_data"], "status": "DRAFT_UNREVIEWED",
                "support_type": "SOURCE_ONLY", "support": ["SRC-1"],
            }],
            "important_flows": [],
            "key_data_structures": [],
            "key_rtl": [{
                "path": "rtl/leaf.sv", "fragment": "L1-L1",
                "why": "先确认模块声明与参数边界。",
            }],
            "supports": {
                "FACT-1": {
                    "kind": "COMPILER_FACT", "fact_id": fact_id,
                    "detail": "Build1 definition identity",
                    "source": "rtl/leaf.sv:L1-L1",
                },
                "SRC-1": {
                    "kind": "RTL_SOURCE", "path": "rtl/leaf.sv", "fragment": "L1-L1",
                },
            },
        }

    def test_module_page_v2_progressive_semantic_and_template_reference(self) -> None:
        store, ids = self._make_store()
        self._register(
            store,
            mode="MULTI_ROOT",
            roots=[
                self._root(ids["root_a"], store.contract_id("entities", "INSTANCE")),
                self._root(ids["root_b"], store.contract_id("entities", "INSTANCE")),
            ],
            source_paths=["rtl/root_a.sv", "rtl/root_b.sv", "rtl/leaf.sv"],
        )
        store.close()
        model = manual_v6.load_backbone(self.database, self.source_root)
        leaf = next(item for item in model["modules"] if item["qualified_name"] == "leaf")
        root = next(item for item in model["modules"] if item["qualified_name"] == "root_a")
        structured_before = json.loads(json.dumps(leaf["structured_detail"]))

        manual_v6.apply_module_page_v2_validation(
            model, self._semantic_validation_payload(fact_id=leaf["facts"][0]["fact_id"])
        )
        output = self.root / "module-v2"
        manifest = manual_v6.build_site(model, output, source_viewer="full")
        leaf_html = (output / manifest["page_map"][leaf["entity_id"]]).read_text()
        root_html = (output / manifest["page_map"][root["entity_id"]]).read_text()
        sidebar_html = (output / "index.html").read_text()

        self.assertIn('data-page-template="module-definition"', leaf_html)
        self.assertIn('data-page-template="module-definition"', root_html)
        self.assertIn("模块定义 V2", leaf_html)
        self.assertIn("模块概览", leaf_html)
        self.assertIn("核心职责", leaf_html)
        self.assertIn("关键接口", leaf_html)
        self.assertIn("推荐优先阅读的 RTL", leaf_html)
        self.assertNotIn("关键控制结构", leaf_html)
        self.assertNotIn("重要操作流程与协作", leaf_html)
        self.assertIn("AI 草稿未审核（UNREVIEWED）", leaf_html)
        self.assertIn("RTL 源码证据", leaf_html)
        self.assertIn("Slang 事实与 RTL 源码", leaf_html)
        self.assertIn("rtl/leaf.sv:L1-L1", leaf_html)
        self.assertNotIn("模块概览", root_html)
        self.assertNotIn("核心职责", root_html)
        self.assertIn("语义尚未构建（NOT_BUILT）", root_html)
        self.assertIn('data-presentation-group="Page Types"', sidebar_html)
        self.assertIn("模块定义 V2", sidebar_html)
        self.assertEqual(leaf["structured_detail"], structured_before)
        persisted = json.loads((output / leaf["structured_path"]).read_text())
        self.assertEqual(persisted, structured_before)
        locator_output = self.root / "module-v2-locator"
        locator_manifest = manual_v6.build_site(
            model, locator_output, source_viewer="locator"
        )
        locator_html = (
            locator_output / locator_manifest["page_map"][leaf["entity_id"]]
        ).read_text()
        self.assertIn("rtl/leaf.sv:L1-L1", locator_html)
        self.assertNotIn("rtl/leaf.sv.html#L1-L1", locator_html)
        template = (ROOT / "src/rtl-pack/templates/manual-v6/module-definition.html").read_text()
        self.assertNotIn("ct_mmu_tlboper", template)
        self.assertNotIn("OpenC910", template)

    def test_module_page_v2_rejects_unsupported_factual_semantic(self) -> None:
        store, ids = self._make_store()
        self._register(
            store,
            mode="ACTUAL_INSTANCE_ROOT",
            roots=[self._root(ids["root_a"], store.contract_id("entities", "INSTANCE"))],
            source_paths=["rtl/root_a.sv", "rtl/leaf.sv"],
        )
        store.close()
        model = manual_v6.load_backbone(self.database, self.source_root)
        leaf = next(item for item in model["modules"] if item["qualified_name"] == "leaf")
        payload = self._semantic_validation_payload(fact_id=leaf["facts"][0]["fact_id"])
        payload["overview"][0]["support"] = []
        with self.assertRaisesRegex(ValueError, "support"):
            manual_v6.apply_module_page_v2_validation(model, payload)

    def test_module_page_v2_static_svg_is_safe_relative_and_deterministic(self) -> None:
        store, ids = self._make_store()
        self._register(
            store,
            mode="ACTUAL_INSTANCE_ROOT",
            roots=[self._root(ids["root_a"], store.contract_id("entities", "INSTANCE"))],
            source_paths=["rtl/root_a.sv", "rtl/leaf.sv"],
        )
        store.close()
        model = manual_v6.load_backbone(self.database, self.source_root)
        svg = self.root / "leaf-architecture.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><title>leaf</title></svg>\n')
        architecture = {
            "model": {
                "schema": "rtl-analysis-agent/diagram-view-model-prototype-v1",
                "subject": "leaf", "status": "DRAFT_UNREVIEWED",
                "nodes": [{"id": "leaf", "label": "leaf", "kind": "SUBJECT"}],
                "edges": [],
                "legend": {"STRUCTURAL": "compiler-grounded"},
            },
            "svg_path": svg,
            "sha256": _sha(svg),
            "alt": "leaf architecture validation diagram",
        }
        leaf = next(item for item in model["modules"] if item["qualified_name"] == "leaf")
        manual_v6.apply_module_page_v2_validation(
            model,
            self._semantic_validation_payload(fact_id=leaf["facts"][0]["fact_id"]),
            architecture=architecture,
        )
        left, right = self.root / "module-v2-left", self.root / "module-v2-right"
        left_manifest = manual_v6.build_site(model, left, source_viewer="full")
        right_manifest = manual_v6.build_site(model, right, source_viewer="full")
        html = (left / left_manifest["page_map"][leaf["entity_id"]]).read_text()
        self.assertIn("设计位置与关键协作关系", html)
        match = re.search(r'<img[^>]+src="([^"]+\.svg)"', html)
        self.assertIsNotNone(match)
        svg_target = (left / left_manifest["page_map"][leaf["entity_id"]]).parent / match.group(1)
        self.assertEqual(_sha(svg_target.resolve()), _sha(svg))
        left_hashes = {
            path.relative_to(left).as_posix(): _sha(path)
            for path in left.rglob("*") if path.is_file()
        }
        right_hashes = {
            path.relative_to(right).as_posix(): _sha(path)
            for path in right.rglob("*") if path.is_file()
        }
        self.assertEqual(left_hashes, right_hashes)
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><title>changed</title></svg>\n')
        rejected = self.root / "module-v2-rejected"
        with self.assertRaisesRegex(ValueError, "changed or is unsafe"):
            manual_v6.build_site(model, rejected, source_viewer="full")
        self.assertFalse(rejected.exists())


if __name__ == "__main__":
    unittest.main()
