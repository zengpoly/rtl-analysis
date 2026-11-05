from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/rtl-pack/lib"))

import rtl_project_model as model  # noqa: E402


class ProjectModelFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "model.sqlite"
        self.identity = model.make_identity(
            project_name="demo",
            source_revision="a" * 40,
            config={"top": "top", "files": [{"path": "rtl/top.sv", "sha256": "1"}]},
            toolchain={"slang": "11.0", "yosys": "0.67.0.0.post1190"},
            spec={"schema": "rtl-analysis-agent/project-model-spec-v1"},
        )
        self.store = model.ModelStore.create(self.database, self.identity)
        self.run_id = self.store.add_producer_run(
            producer="fixture",
            version="1",
            command=["fixture", "--json"],
            scope={"module": "top"},
            input_hash="2" * 64,
            status="complete",
            artifact_hash="3" * 64,
        )
        self.evidence_id = self.store.add_evidence(
            self.run_id,
            kind="source",
            locator="rtl/top.sv",
            start_line=1,
            end_line=1,
            content_hash="4" * 64,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def entity(self, kind: str, name: str) -> str:
        return self.store.add_entity(kind, name, display_name=name.rsplit(".", 1)[-1])


class SchemaAndQueryTests(ProjectModelFixture):
    def test_search_is_literal_ranked_filtered_and_bounded(self) -> None:
        exact = self.entity("rtl.sv/1:signal", "top.req")
        prefix = self.entity("rtl.sv/1:signal", "top.request")
        contains = self.entity("rtl.sv/1:port", "sub.top.req_ack")
        self.entity("rtl.sv/1:signal", "top.other")
        self.store.finalize()

        result = self.store.query("search", text="top.req", limit=20)
        self.assertEqual(
            [row["entity_id"] for row in result["entities"]],
            [exact, prefix, contains],
        )
        self.assertEqual(
            [row["match"] for row in result["entities"]],
            ["exact", "prefix", "contains"],
        )
        filtered = self.store.query(
            "search", text="req", kind="rtl.sv/1:port", limit=1
        )
        self.assertEqual([row["entity_id"] for row in filtered["entities"]], [contains])
        for value in (0, 101):
            with self.assertRaisesRegex(model.ModelError, "between 1 and 100"):
                self.store.query("search", text="req", limit=value)
        with self.assertRaisesRegex(model.ModelError, "non-empty"):
            self.store.query("search", text="  ")

    def test_assertion_without_evidence_is_rejected(self) -> None:
        subject = self.entity("rtl.sv/1:signal", "top.subject")
        target = self.entity("rtl.sv/1:signal", "top.target")
        with self.assertRaisesRegex(model.ModelError, "evidence"):
            self.store.add_assertion(
                subject_id=subject,
                predicate="rtl.connectivity/1:binds-to",
                object_id=target,
                dimension="connectivity",
                derivation="tool",
                confidence=1.0,
                status="verified",
                producer_run_id=self.run_id,
                evidence_ids=[],
            )

    def test_open_vocabulary_provenance_coverage_and_integrity(self) -> None:
        project = self.entity("rtl.project/1:project", "demo")
        extension = self.entity("vendor.future/1:protocol-endpoint", "top.endpoint")
        assertion = self.store.add_assertion(
            subject_id=project,
            predicate="vendor.future/1:connects",
            object_id=extension,
            dimension="engineering-semantic",
            derivation="human",
            confidence=0.8,
            status="conflicted",
            producer_run_id=self.run_id,
            evidence_ids=[self.evidence_id],
        )
        statuses = ["verified", "partial", "unsupported", "failed", "stale"]
        for index, status in enumerate(statuses):
            self.store.set_coverage(
                scope=f"scope-{index}",
                dimension="hierarchy",
                status=status,
                reason=f"fixture-{status}",
                producer_run_id=self.run_id,
            )
        self.store.finalize()

        self.assertEqual(
            self.store.integrity(), {"foreign_keys": [], "integrity": "ok"}
        )
        entity = self.store.query("entity", name="top.endpoint")
        self.assertEqual(
            entity["entities"][0]["kind"], "vendor.future/1:protocol-endpoint"
        )
        self.assertEqual(entity["incoming"][0]["assertion_id"], assertion)
        self.assertEqual(
            entity["incoming"][0]["subject"]["qualified_name"], "demo"
        )
        self.assertEqual(
            entity["incoming"][0]["object"]["qualified_name"], "top.endpoint"
        )
        self.assertEqual(
            entity["incoming"][0]["evidence"][0]["locator"], "rtl/top.sv"
        )
        evidence = self.store.query("evidence", assertion_id=assertion)
        self.assertEqual(evidence["evidence"][0]["locator"], "rtl/top.sv")
        semantics = self.store.query("semantics")
        self.assertEqual(
            semantics["assertions"][0]["subject"]["qualified_name"], "demo"
        )
        self.assertEqual(
            semantics["assertions"][0]["object"]["qualified_name"], "top.endpoint"
        )
        coverage = self.store.query("coverage")
        self.assertEqual({row["status"] for row in coverage["coverage"]}, set(statuses))

    def test_recursive_path_is_cycle_safe_and_depth_is_bounded(self) -> None:
        nodes = [self.entity("rtl.sv/1:signal", f"top.n{index}") for index in range(3)]
        for subject, obj in (
            (nodes[0], nodes[1]),
            (nodes[1], nodes[2]),
            (nodes[2], nodes[0]),
        ):
            self.store.add_assertion(
                subject_id=subject,
                predicate="rtl.connectivity/1:binds-to",
                object_id=obj,
                dimension="connectivity",
                derivation="tool",
                confidence=1.0,
                status="verified",
                producer_run_id=self.run_id,
                evidence_ids=[self.evidence_id],
            )
        self.store.finalize()

        result = self.store.query("path", start="top.n0", end="top.n2", max_depth=8)
        self.assertEqual(
            [node["qualified_name"] for node in result["path"]],
            ["top.n0", "top.n1", "top.n2"],
        )
        with self.assertRaisesRegex(model.ModelError, "32"):
            self.store.query("path", start="top.n0", end="top.n2", max_depth=33)

    def test_path_entity_ids_resolve_ambiguous_qualified_names(self) -> None:
        selected = self.entity("rtl.sv/1:port", "top.shared")
        self.entity("rtl.sv/1:signal", "top.shared")
        target = self.entity("rtl.sv/1:port", "top.target")
        self.store.add_assertion(
            subject_id=selected,
            predicate="rtl.connectivity/1:binds-to",
            object_id=target,
            dimension="connectivity",
            derivation="tool",
            confidence=1.0,
            status="verified",
            producer_run_id=self.run_id,
            evidence_ids=[self.evidence_id],
        )
        self.store.finalize()

        with self.assertRaisesRegex(model.ModelError, "resolves to 2"):
            self.store.query("path", start="top.shared", end="top.target")
        result = self.store.query("path", start_id=selected, end_id=target)
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["path"][0]["entity_id"], selected)
        with self.assertRaisesRegex(model.ModelError, "not both"):
            self.store.query(
                "path", start="top.shared", start_id=selected, end_id=target
            )

    def test_clock_reset_name_excludes_unrelated_clock_lineage(self) -> None:
        register = self.entity("rtl.temporal/1:register", "leaf.q@register:1")
        leaf_clock = self.entity("rtl.clock/1:clock", "leaf.gclk")
        leaf_source = self.entity("rtl.clock/1:clock", "leaf.clk")
        other_clock = self.entity("rtl.clock/1:clock", "other.gclk")
        other_source = self.entity("rtl.clock/1:clock", "other.clk")
        for subject, predicate, obj in (
            (register, "rtl.clock-reset/1:clocked-by", leaf_clock),
            (leaf_clock, "rtl.clock-reset/1:derived-from", leaf_source),
            (other_clock, "rtl.clock-reset/1:derived-from", other_source),
        ):
            self.store.add_assertion(
                subject_id=subject,
                predicate=predicate,
                object_id=obj,
                dimension="clock/reset",
                derivation="tool",
                confidence=1.0,
                status="verified",
                producer_run_id=self.run_id,
                evidence_ids=[self.evidence_id],
            )
        self.store.finalize()

        result = self.store.query("clock-reset", name="leaf")
        self.assertEqual(len(result["derived_clock_assertions"]), 1)
        lineage = result["derived_clock_assertions"][0]
        self.assertEqual(lineage["subject"]["qualified_name"], "leaf.gclk")
        self.assertEqual(lineage["object"]["qualified_name"], "leaf.clk")
        self.assertEqual(lineage["evidence"][0]["locator"], "rtl/top.sv")


class IdentityAndExchangeTests(ProjectModelFixture):
    def test_identity_is_stable_and_tracks_each_versioned_input(self) -> None:
        same = model.make_identity(
            project_name="demo",
            source_revision="a" * 40,
            config={"top": "top", "files": [{"path": "rtl/top.sv", "sha256": "1"}]},
            toolchain={"slang": "11.0", "yosys": "0.67.0.0.post1190"},
            spec={"schema": "rtl-analysis-agent/project-model-spec-v1"},
        )
        self.assertEqual(self.identity, same)
        changes = [
            {"source_revision": "b" * 40},
            {"config": {"top": "other"}},
            {"toolchain": {"slang": "next"}},
            {"schema_version": "rtlpm-v1"},
        ]
        base = {
            "project_name": "demo",
            "source_revision": "a" * 40,
            "config": {"top": "top", "files": [{"path": "rtl/top.sv", "sha256": "1"}]},
            "toolchain": {"slang": "11.0", "yosys": "0.67.0.0.post1190"},
            "spec": {"schema": "rtl-analysis-agent/project-model-spec-v1"},
        }
        for change in changes:
            with self.subTest(change=change):
                candidate = model.make_identity(**(base | change))
                self.assertNotEqual(candidate["model_id"], self.identity["model_id"])

    def test_jsonl_round_trip_preserves_canonical_rows(self) -> None:
        first = self.entity("rtl.sv/1:signal", "top.a")
        second = self.entity("rtl.sv/1:signal", "top.b")
        self.store.add_assertion(
            subject_id=first,
            predicate="rtl.behavior/1:data-depends-on",
            object_id=second,
            dimension="behavior/causality",
            derivation="tool",
            confidence=1.0,
            status="verified",
            producer_run_id=self.run_id,
            evidence_ids=[self.evidence_id],
        )
        self.store.finalize()
        exported = self.root / "model.jsonl"
        imported = self.root / "imported.sqlite"
        model.export_jsonl(self.database, exported)
        model.import_jsonl(exported, imported)
        second_export = self.root / "second.jsonl"
        model.export_jsonl(imported, second_export)
        self.assertEqual(exported.read_bytes(), second_export.read_bytes())
        with sqlite3.connect(imported) as connection:
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )


class ProducerFormatTests(unittest.TestCase):
    def test_kiss2_parser_preserves_reset_and_transitions(self) -> None:
        parsed = model.parse_kiss2(
            ".i 2\n.o 1\n.p 2\n.s 2\n.r 00\n0- 00 01 1\n1- 01 00 0\n"
        )
        self.assertEqual(parsed["input_count"], 2)
        self.assertEqual(parsed["reset_encoding"], "00")
        self.assertEqual(len(parsed["transitions"]), 2)
        self.assertEqual(parsed["transitions"][0]["next_state"], "01")

    def test_cli_exposes_nested_model_commands(self) -> None:
        import rtl_analysis_cli as cli

        parser = cli.build_parser()
        args = parser.parse_args(
            ["model", "query", "--project", "/tmp/project", "--view", "summary"]
        )
        self.assertEqual((args.operation, args.model_operation), ("model", "query"))
        search = parser.parse_args(
            [
                "model", "query", "--project", "/tmp/project", "--view", "search",
                "--text", "req", "--kind", "rtl.sv/1:signal", "--limit", "5",
            ]
        )
        self.assertEqual((search.text, search.kind, search.limit), ("req", "rtl.sv/1:signal", 5))
        path = parser.parse_args(
            [
                "model", "query", "--project", "/tmp/project", "--view", "path",
                "--start-id", "e:start", "--end-id", "e:end",
            ]
        )
        self.assertEqual((path.start_id, path.end_id), ("e:start", "e:end"))

    def test_project_path_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(model.ModelError, "Project-relative|escapes"):
                model._resolve_project_output(
                    Path(directory), "../outside.sqlite", ".sqlite"
                )

    def test_model_spec_is_limited_to_repository_or_project(self) -> None:
        import rtl_analysis_cli as cli

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            repository = parent / "repository"
            project = parent / "project"
            outside = parent / "outside.json"
            repository.mkdir()
            project.mkdir()
            project_spec = project / "model-spec.json"
            project_spec.write_text("{}")
            outside.write_text("{}")
            self.assertEqual(
                cli.resolve_model_spec(repository, project, str(project_spec)),
                project_spec,
            )
            with self.assertRaisesRegex(cli.AdapterError, "repository or Project"):
                cli.resolve_model_spec(repository, project, str(outside))

    def test_producer_failure_preserves_complete_model_and_removes_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            source = project / "source"
            (project / "analysis/model").mkdir(parents=True)
            source.mkdir()
            (source / "top.sv").write_text("module top; endmodule\n")
            spec = {
                "schema": "rtl-analysis-agent/project-model-spec-v2",
                "name": "failure-fixture",
                "top": "top",
                "files": ["top.sv"],
                "scopes": ["top"],
                "slang": {"allow_unknown_modules": False, "extra_args": []},
                "yosys": {"netlist_passes": ["proc"]},
                "fsms": [],
                "semantic_assertions": [],
                "expected_coverage": {
                    "configuration/elaboration": "verified",
                    "hierarchy": "verified",
                    "connectivity": "verified",
                    "behavior/causality": "partial",
                    "temporal/ordering": "partial",
                    "clock/reset": "partial",
                    "engineering-semantic": "partial",
                },
                "required_capabilities": {
                    "fixture": [],
                    "top": [],
                },
                "project_profile": {"predicates": [], "capabilities": []},
                "expectations": {},
            }
            spec_path = root / "spec.json"
            spec_path.write_text(json.dumps(spec))
            output = project / "analysis/model/rtl-project-model.sqlite"
            output.write_bytes(b"last-complete-model")
            with self.assertRaisesRegex(model.ModelError, "slang"):
                model.build_project_model(
                    project_dir=project,
                    target={"name": "fixture", "revision": "unversioned"},
                    source_root=source,
                    spec_path=spec_path,
                    output_relative="analysis/model/rtl-project-model.sqlite",
                    slang_executable=Path("/bin/false"),
                    slang_version="fixture",
                    yosys_executable=Path("/bin/false"),
                    yosys_version="fixture",
                    temporary_root=root,
                )
            self.assertEqual(output.read_bytes(), b"last-complete-model")
            self.assertEqual(list(output.parent.glob("*.candidate-*")), [])

    def test_v1_jsonl_is_not_imported_in_place_as_v2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "v1.jsonl"
            target = root / "candidate.sqlite"
            source.write_text(
                '{"format":"rtl-analysis-agent/project-model-jsonl-v1",'
                '"schema_version":"rtlpm-v1"}\n'
            )
            with self.assertRaises(model.ModelError):
                model.import_jsonl(source, target)
            self.assertFalse(target.exists())


class AdapterFixtureTests(unittest.TestCase):
    def test_slang_yosys_and_fsm_fixtures_form_one_graph(self) -> None:
        fixture = ROOT / "tests/fixtures/project-model"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "fixture.sqlite"
            identity = model.make_identity(
                project_name="fixture",
                source_revision="unversioned",
                config={"top": "top"},
                toolchain={"slang": "fixture", "yosys": "fixture"},
                spec={"schema": "fixture"},
            )
            store = model.ModelStore.create(database, identity)
            source_files = {
                "leaf_out.sv": "rtl/leaf_out.sv",
                "leaf_in.sv": "rtl/leaf_in.sv",
                "top.sv": "rtl/top.sv",
            }
            extractor = model._Extractor(
                store,
                fixture,
                source_files,
                {
                    value: str(index) * 64
                    for index, value in enumerate(source_files.values(), 1)
                },
            )
            slang_run = store.add_producer_run(
                producer="slang",
                version="fixture",
                command=["slang"],
                scope={},
                input_hash="1" * 64,
                status="complete",
                artifact_hash="2" * 64,
            )
            yosys_run = store.add_producer_run(
                producer="yosys",
                version="fixture",
                command=["yosys"],
                scope={},
                input_hash="1" * 64,
                status="complete",
                artifact_hash="3" * 64,
            )
            fsm_run = store.add_producer_run(
                producer="yosys-fsm",
                version="fixture",
                command=["fsm"],
                scope={},
                input_hash="1" * 64,
                status="complete",
                artifact_hash="4" * 64,
            )
            spec = {"top": "top", "scopes": ["leaf_out", "leaf_in", "top"]}
            spec_ev = extractor.evidence(
                slang_run, "fixture-spec.json", kind="configuration"
            )
            slang_summary = extractor.extract_slang(
                json.loads((fixture / "slang-ast.json").read_text()),
                spec,
                slang_run,
                spec_ev,
            )
            yosys_summary = extractor.extract_yosys(
                json.loads((fixture / "yosys-netlist.json").read_text()),
                {"scopes": ["leaf_in"]},
                yosys_run,
            )
            fsm_summary = extractor.extract_fsm(
                json.loads((fixture / "yosys-fsm.json").read_text()),
                model.parse_kiss2((fixture / "fsm.kiss2").read_text()),
                {
                    "module": "leaf_in",
                    "state_signal": "state",
                    "encoding_parameters": ["IDLE", "RUN"],
                },
                slang_summary["parameters"],
                fsm_run,
            )
            store.finalize()

            path = store.query("path", start="top.u_out.value", end="top.u_in.value")
            self.assertEqual(
                [item["qualified_name"] for item in path["path"]],
                ["top.u_out.value", "top.shared", "top.u_in.value"],
            )
            self.assertEqual(
                yosys_summary["registers"]["leaf_in"], {"cell_count": 1, "bit_count": 1}
            )
            clock = store.query("clock-reset")
            self.assertEqual(len(clock["registers"]), 1)
            self.assertEqual(len(clock["derived_clock_assertions"]), 1)
            self.assertEqual(clock["registers"][0]["attrs"]["clock_edge"], "posedge")
            self.assertEqual(clock["registers"][0]["attrs"]["reset_active"], "low")
            self.assertEqual(
                clock["registers"][0]["attrs"]["reset_mode"], "asynchronous"
            )
            self.assertEqual(
                clock["assertions"][0]["evidence"][0]["locator"], "rtl/leaf_in.sv"
            )
            self.assertEqual(fsm_summary["reset_state"], "IDLE")
            self.assertEqual(fsm_summary["transition_count"], 2)
            fsm = store.query("fsm", name="leaf_in.state@fsm")
            self.assertEqual(len(fsm["transitions"]), 2)
            self.assertEqual(len(fsm["transition_assertions"]), 4)
            self.assertTrue(all(item["evidence"] for item in fsm["transition_assertions"]))
            self.assertEqual(
                fsm["assertions"][0]["subject"]["qualified_name"],
                "leaf_in.state@fsm",
            )
            self.assertTrue(fsm["assertions"][0]["evidence"])
            self.assertEqual(
                [item["qualified_name"] for item in fsm["control_inputs"]],
                ["leaf_in.control"],
            )
            summary = store.query("summary")
            self.assertGreater(
                summary["predicates"]["rtl.behavior/1:control-depends-on"], 0
            )
            store.close()


if __name__ == "__main__":
    unittest.main()
