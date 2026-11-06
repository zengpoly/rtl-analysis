from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "src/rtl-pack/lib"
FIXTURE = ROOT / "tests/fixtures/runtime_query"
MODULE_FIXTURE = ROOT / "tests/fixtures/runtime_module_candidate"
PYSLANG_SITE = (
    ROOT
    / ".local/tools/pyslang/11.0.0/site-packages"
)
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(PYSLANG_SITE))


try:
    import rtl_query_runtime as runtime  # noqa: E402
except ImportError:
    runtime = None


@unittest.skipUnless(PYSLANG_SITE.is_dir(), "locked PySlang is not installed")
class RuntimeQueryTests(unittest.TestCase):
    def session(self):
        assert runtime is not None
        return runtime.RuntimeQuerySession.open(
            source_root=FIXTURE,
            build_path=FIXTURE / "build.json",
            project_name="runtime-query-fixture",
            source_revision="fixture-v1",
        )

    def test_symbol_type_and_source_evidence(self) -> None:
        with self.session() as session:
            result = session.query(
                {"id": "inspect-status", "op": "inspect", "path": "query_top.status"}
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["record"]["kind"], "Variable")
        self.assertIn("payload_t", result["record"]["type"])
        evidence = result["record"]["evidence"]
        self.assertEqual(evidence["path"], "design.sv")
        self.assertGreater(evidence["line_start"], 0)
        self.assertRegex(evidence["sha256"], r"^[0-9a-f]{64}$")

    def test_python_native_ast_and_state_reuse(self) -> None:
        with self.session() as session:
            self.assertIn("python", session.ready_result()["operations"])
            first = session.query({
                "id": "native-node", "op": "python",
                "code": "leaf = root.lookupName('query_top.u_leaf')\n"
                        "result = {'definition': leaf.definition.name, 'evidence': evidence(leaf)}",
            })
            second = session.query({
                "id": "native-statements", "op": "python",
                "code": "assignments = []\n"
                        "def collect(node):\n"
                        "    if isinstance(node, pyslang.ast.AssignmentExpression):\n"
                        "        assignments.append({'nba': node.isNonBlocking, 'evidence': evidence(node)})\n"
                        "leaf.body.visit(collect)\n"
                        "result = assignments",
            })
            self.assertEqual(first["status"], "ok", first)
            self.assertEqual(first["result"]["definition"], "query_leaf")
            self.assertEqual(first["result"]["evidence"]["path"], "design.sv")
            self.assertEqual(second["status"], "ok", second)
            self.assertEqual(len(second["result"]), 2)
            self.assertTrue(all(x["nba"] for x in second["result"]))
            self.assertTrue(all(x["evidence"]["line_start"] > 0 for x in second["result"]))
            self.assertEqual(second["telemetry"]["compilation_count"], 1)
            self.assertEqual(second["telemetry"]["query_count"], 2)
            self.assertTrue(second["telemetry"]["session_reused"])
        self.assertEqual(session.python_globals, {})
        with self.session() as fresh:
            result = fresh.query({"id": "fresh", "op": "python", "code": "result = 'leaf' in globals()"})
            self.assertIs(result["result"], False)

    def test_python_errors_and_output_do_not_break_jsonl(self) -> None:
        requests = [
            {"id": "print", "op": "python", "code": "import sys\nprint('hello')\nprint('note', file=sys.stderr)\nresult = 3"},
            {"id": "syntax", "op": "python", "code": "result = ("},
            {"id": "object", "op": "python", "code": "result = root"},
            {"id": "exit", "op": "python", "code": "raise SystemExit(7)"},
            {"id": "next", "op": "python", "code": "print(pyslang.__version__)"},
            {"id": "legacy", "op": "scope", "path": "query_top", "name": "u_leaf"},
        ]
        with self.session() as session:
            output = io.StringIO()
            runtime.serve(session, io.StringIO("".join(json.dumps(x) + "\n" for x in requests)), output)
        ready, *results = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(results), len(requests))
        self.assertEqual(results[0]["result"], 3)
        self.assertEqual(results[0]["stdout"], "hello\n")
        self.assertEqual(results[0]["stderr"], "note\n")
        for result, error in zip(results[1:4], ["SyntaxError", "TypeError", "SystemExit"]):
            self.assertEqual(result["status"], "query_error", result)
            self.assertEqual(result["error_type"], error)
            self.assertNotIn("result", result)
        self.assertIsNone(results[4]["result"])
        self.assertEqual(results[4]["stdout"], "11.0.0\n")
        self.assertEqual(results[5]["records"][0]["definition"], "query_leaf")
        self.assertEqual(results[5]["telemetry"]["compilation_count"], 1)

    def test_python_output_bounds_and_timeout(self) -> None:
        from unittest.mock import patch

        with self.session() as session:
            printed = session.query({"id": "print-limit", "op": "python", "code": "print('中' * 100000)\nresult = 1"})
            large = session.query({"id": "result-limit", "op": "python", "code": "result = 'x' * 600000"})
            with patch.object(runtime, "PYTHON_QUERY_TIMEOUT_SECONDS", 0.02):
                timeout = session.query({"id": "timeout", "op": "python", "code": "while True:\n    pass"})
            recovered = session.query({"id": "recovered", "op": "python", "code": "result = compilation is root.compilation"})
        self.assertEqual(printed["status"], "partial")
        self.assertTrue(printed["stdout_truncated"])
        self.assertEqual(printed["result"], 1)
        self.assertLessEqual(len(printed["stdout"].encode()), runtime.MAX_PYTHON_LOG_BYTES)
        self.assertEqual(large["status"], "partial")
        self.assertEqual(large["completeness"], "truncated")
        self.assertNotIn("result", large)
        self.assertEqual(timeout["status"], "query_error")
        self.assertEqual(timeout["error_type"], "TimeoutError")
        self.assertEqual(recovered["status"], "ok", recovered)
        self.assertTrue(recovered["result"])

    def test_python_validates_requests_before_execution(self) -> None:
        with self.session() as session:
            for code in [None, "", 42, "x" * (runtime.MAX_REQUEST_BYTES + 1)]:
                with self.subTest(code_type=type(code).__name__):
                    result = session.query({"id": "invalid-code", "op": "python", "code": code})
                    self.assertEqual(result["status"], "unsupported")
            session.invalidated = True
            stale = session.query({"id": "stale-python", "op": "python", "code": "raise RuntimeError('must not execute')"})
            self.assertEqual(stale["status"], "stale_input")

    def test_python_source_change_invalidates_result_and_later_queries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            shutil.copyfile(FIXTURE / "design.sv", source / "design.sv")
            shutil.copyfile(FIXTURE / "build.json", source / "build.json")
            with runtime.RuntimeQuerySession.open(
                source_root=source, build_path=source / "build.json",
                project_name="python-input-drift", source_revision="fixture-v1",
            ) as session:
                target = repr(str(source / "design.sv"))
                result = session.query({
                    "id": "changed", "op": "python",
                    "code": f"from pathlib import Path\np = Path({target})\np.write_text(p.read_text() + '\\n')\nresult = 42",
                })
                later = session.query({"id": "later", "op": "python", "code": "result = 7"})
        self.assertEqual(result["status"], "stale_input")
        self.assertNotIn("result", result)
        self.assertEqual(later["status"], "stale_input")

    def test_python_native_analysis_and_api_discovery(self) -> None:
        with self.session() as session:
            result = session.query({
                "id": "analysis", "op": "python",
                "code": "manager = get_analysis()\n"
                        "signal = root.lookupName('query_top.status')\n"
                        "result = {'shared': manager is get_analysis(),\n"
                        "          'drivers': [str(d.source) for d in manager.getDrivers(signal)],\n"
                        "          'assignment_api': dir(pyslang.ast.AssignmentExpression)}",
            })
        self.assertEqual(result["status"], "ok", result)
        self.assertTrue(result["result"]["shared"])
        self.assertTrue(any("AlwaysFF" in d for d in result["result"]["drivers"]))
        self.assertIn("isNonBlocking", result["result"]["assignment_api"])
        self.assertIn("analysis_init_ms", result["telemetry"])

    def test_scope_reports_active_generate_and_parameter_specialization(self) -> None:
        with self.session() as session:
            root = session.query({"id": "root", "op": "scope", "path": "query_top"})
            leaf = session.query(
                {"id": "leaf", "op": "inspect", "path": "query_top.u_leaf"}
            )
        self.assertEqual(root["status"], "ok")
        self.assertTrue(
            any(record["name"] == "g_extra" for record in root["records"])
        )
        self.assertTrue(
            next(record for record in root["records"] if record["name"] == "g_extra")[
                "active"
            ]
        )
        self.assertEqual(leaf["record"]["definition"], "query_leaf")
        self.assertEqual(leaf["record"]["parameters"][0]["value"], "4")

    def test_instance_parameter_projection_separates_default_override_and_effective(self) -> None:
        assert runtime is not None
        with runtime.RuntimeQuerySession.open(
            source_root=MODULE_FIXTURE,
            build_path=MODULE_FIXTURE / "build.json",
            project_name="runtime-module-candidate",
            source_revision="fixture-v1",
        ) as session:
            result = session.query(
                {"id": "parameter-provenance", "op": "inspect", "path": "fixture_top.u_low"}
            )

        self.assertEqual(result["status"], "ok")
        width = next(
            item for item in result["record"]["parameters"] if item["name"] == "WIDTH"
        )
        self.assertEqual(width["kind"], "Parameter")
        self.assertEqual(width["value"], "8")
        self.assertTrue(width["overridden"])
        self.assertEqual(width["declaration_evidence"]["path"], "leaf.sv")
        self.assertEqual(width["declaration_evidence"]["line_start"], 2)
        self.assertEqual(width["default"]["text"], "4")
        self.assertFalse(width["default"]["text_truncated"])
        self.assertEqual(width["default"]["evidence"]["path"], "leaf.sv")
        self.assertEqual(width["default"]["evidence"]["line_start"], 2)
        self.assertEqual(width["override"]["binding_kind"], "named")
        self.assertEqual(width["override"]["formal_name"], "WIDTH")
        self.assertEqual(width["override"]["expression_text"], "8")
        self.assertFalse(width["override"]["text_truncated"])
        self.assertEqual(width["override"]["evidence"]["path"], "top.sv")
        self.assertEqual(width["override"]["evidence"]["line_start"], 7)
        self.assertTrue(width["provenance_complete"])

    def test_top_parameter_changes_active_elaboration(self) -> None:
        assert runtime is not None
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory) / "build.json"
            spec = json.loads((FIXTURE / "build.json").read_text())
            spec["top_parameters"] = {"MAKE_EXTRA": 0}
            build.write_text(json.dumps(spec))
            with runtime.RuntimeQuerySession.open(
                source_root=FIXTURE,
                build_path=build,
                project_name="runtime-query-fixture",
                source_revision="fixture-v1",
            ) as session:
                root = session.query(
                    {"id": "root", "op": "scope", "path": "query_top"}
                )
                top = session.query(
                    {"id": "top", "op": "inspect", "path": "query_top"}
                )
        self.assertEqual(root["status"], "ok")
        generated = next(
            record for record in root["records"] if record["name"] == "g_extra"
        )
        self.assertFalse(generated["active"])
        parameter = next(
            item for item in top["record"]["parameters"] if item["name"] == "MAKE_EXTRA"
        )
        self.assertEqual(parameter["value"], "1'b0")

    def test_binding_preserves_expression_and_referenced_symbols(self) -> None:
        with self.session() as session:
            result = session.query(
                {
                    "id": "binding",
                    "op": "bindings",
                    "instance": "query_top.u_leaf",
                    "port": "data_i",
                }
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["records"]), 1)
        record = result["records"][0]
        self.assertEqual(record["formal"], "data_i")
        self.assertIn("{left_i[1:0], right_i[1:0]}", record["actual"]["text"])
        self.assertEqual(
            [item["path"] for item in record["actual"]["references"]],
            ["query_top.left_i", "query_top.right_i"],
        )

    def test_binding_distinguishes_default_open_and_missing(self) -> None:
        with self.session() as session:
            result = session.query(
                {
                    "id": "connection-kinds",
                    "op": "bindings",
                    "instance": "query_top.u_connections",
                }
            )
        by_port = {record["formal"]: record for record in result["records"]}
        self.assertEqual(by_port["defaulted_i"]["connection_status"], "defaulted")
        self.assertEqual(by_port["defaulted_i"]["connection_origin"], "formal_default")
        self.assertIsNone(by_port["defaulted_i"]["connection_evidence"])
        self.assertIsNotNone(by_port["defaulted_i"]["default_evidence"])
        self.assertEqual(by_port["open_o"]["connection_status"], "explicit_open")
        self.assertEqual(by_port["open_o"]["connection_origin"], "named_open")
        self.assertIsNotNone(by_port["open_o"]["connection_evidence"])
        self.assertEqual(by_port["missing_o"]["connection_status"], "missing")
        self.assertEqual(by_port["missing_o"]["connection_origin"], "omitted")
        self.assertIsNone(by_port["missing_o"]["connection_evidence"])

    def test_binding_preserves_wildcard_provenance(self) -> None:
        with self.session() as session:
            result = session.query(
                {
                    "id": "wildcard",
                    "op": "bindings",
                    "instance": "query_top.u_wildcard",
                }
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            {record["connection_status"] for record in result["records"]},
            {"connected"},
        )
        self.assertEqual(
            {record["connection_origin"] for record in result["records"]},
            {"wildcard"},
        )
        self.assertTrue(
            all(record["connection_evidence"] for record in result["records"])
        )

    def test_output_limit_includes_truncation_disclosure(self) -> None:
        with self.session() as session:
            result = session._base("payload", "scope")
            result["telemetry"] = {}
            result["records"] = [{"text": ""}]
            empty_record_size = len(
                json.dumps(result, sort_keys=True).encode("utf-8")
            )
            result["records"] = [
                {
                    "text": "x"
                    * (runtime.MAX_OUTPUT_BYTES - empty_record_size - 50)
                },
                {"text": "y" * 1_000},
            ]
            bounded = session._bound_payload(result)

        encoded = json.dumps(bounded, sort_keys=True).encode("utf-8")
        self.assertLessEqual(len(encoded), runtime.MAX_OUTPUT_BYTES)
        self.assertEqual(bounded["status"], "partial")
        self.assertTrue(bounded["truncated"])
        self.assertEqual(bounded["omitted_count"], 2)
        self.assertEqual(bounded["records"], [])

    def test_native_drivers_expose_procedure_and_timing_evidence(self) -> None:
        with self.session() as session:
            result = session.query(
                {
                    "id": "drivers",
                    "op": "uses",
                    "path": "query_top.status",
                    "mode": "drivers",
                }
            )
        self.assertIn(result["status"], {"ok", "partial"})
        self.assertTrue(result["records"])
        self.assertTrue(any(record["source"] == "AlwaysFF" for record in result["records"]))
        self.assertTrue(any(record["evidence"]["path"] == "design.sv" for record in result["records"]))
        sequential = next(record for record in result["records"] if record["source"] == "AlwaysFF")
        self.assertEqual(
            [event["edge"] for event in sequential["timing"]["events"]],
            ["PosEdge", "NegEdge"],
        )
        self.assertIn("always_ff", sequential["construct_evidence"]["excerpt"])

    def test_native_procedure_reads_are_scope_local(self) -> None:
        with self.session() as session:
            result = session.query(
                {
                    "id": "reads",
                    "op": "uses",
                    "path": "query_top",
                    "mode": "procedure_reads",
                }
            )
        self.assertEqual(result["status"], "ok")
        sequential = next(
            record for record in result["records"] if record["procedure_kind"] == "AlwaysFF"
        )
        paths = {item["path"] for item in sequential["reads"]}
        self.assertIn("query_top.enable", paths)
        self.assertIn("query_top.leaf_q", paths)
        self.assertEqual(sequential["sensitivity_kind"], "Explicit")
        self.assertIn("temporal proof", result["boundary"])

    def test_uses_limit_discloses_omitted_procedures(self) -> None:
        with self.session() as session:
            full = session.query(
                {
                    "id": "all-procedures",
                    "op": "uses",
                    "path": "query_top",
                    "mode": "procedure_reads",
                    "limit": 100,
                }
            )
            limited = session.query(
                {
                    "id": "one-procedure",
                    "op": "uses",
                    "path": "query_top",
                    "mode": "procedure_reads",
                    "limit": 1,
                }
            )
        self.assertGreater(len(full["records"]), 1)
        self.assertEqual(len(limited["records"]), 1)
        self.assertTrue(limited["truncated"])
        self.assertEqual(limited["completeness"], "truncated")
        self.assertEqual(limited["omitted_count"], len(full["records"]) - 1)

    def test_ambiguity_not_found_and_unsupported_are_distinct(self) -> None:
        with self.session() as session:
            ambiguous = session.query(
                {"id": "ambiguous", "op": "inspect", "path": "duplicate"}
            )
            missing = session.query(
                {"id": "missing", "op": "inspect", "path": "does_not_exist"}
            )
            unsupported = session.query(
                {"id": "unsupported", "op": "uses", "path": "query_top.status", "mode": "fanout"}
            )
        self.assertEqual(ambiguous["status"], "ambiguous")
        self.assertGreaterEqual(len(ambiguous["candidates"]), 2)
        self.assertEqual(missing["status"], "not_found")
        self.assertEqual(unsupported["status"], "unsupported")

    def test_suffix_search_budget_never_proves_absence_or_uniqueness(self) -> None:
        assert runtime is not None
        previous = runtime.MAX_VISITED_NODES
        try:
            runtime.MAX_VISITED_NODES = 1
            with self.session() as session:
                result = session.query(
                    {"id": "budget", "op": "inspect", "path": "duplicate"}
                )
        finally:
            runtime.MAX_VISITED_NODES = previous
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["completeness"], "truncated")

    def test_scope_work_and_inspect_parameters_are_bounded(self) -> None:
        assert runtime is not None
        old_scan = runtime.MAX_COLLECTION_SCAN
        old_nested = runtime.MAX_NESTED_RESULTS
        try:
            with self.session() as session:
                runtime.MAX_COLLECTION_SCAN = 1
                runtime.MAX_NESTED_RESULTS = 1
                scope = session.query(
                    {"id": "scope-bound", "op": "scope", "path": "query_top"}
                )
                inspect = session.query(
                    {"id": "parameter-bound", "op": "inspect", "path": "query_top.u_leaf"}
                )
        finally:
            runtime.MAX_COLLECTION_SCAN = old_scan
            runtime.MAX_NESTED_RESULTS = old_nested
        self.assertEqual(scope["status"], "partial")
        self.assertTrue(scope["truncated"])
        self.assertIsNone(scope["omitted_count"])
        self.assertEqual(len(inspect["record"]["parameters"]), 1)
        self.assertTrue(inspect["record"]["parameters_truncated"])

    def test_utf8_offsets_and_line_directives_keep_physical_evidence_honest(self) -> None:
        assert runtime is not None
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "design.sv").write_text(
                "// 中文注释\n"
                "`line 700 \"virtual.sv\" 0\n"
                "module child(input logic p); endmodule\n"
                "module utf8_top(input logic a, b); child u(.p(b)); endmodule\n"
            )
            (source / "build.json").write_text(
                json.dumps(
                    {
                        "schema": runtime.BUILD_SCHEMA,
                        "files": ["design.sv"],
                        "filelists": [],
                        "include_dirs": [],
                        "defines": [],
                        "tops": ["utf8_top"],
                        "single_unit": True,
                        "environment": {},
                    }
                )
            )
            with runtime.RuntimeQuerySession.open(
                source_root=source,
                build_path=source / "build.json",
                project_name="utf8",
                source_revision="fixture-v1",
            ) as session:
                result = session.query(
                    {"id": "binding", "op": "bindings", "instance": "utf8_top.u"}
                )
        actual = result["records"][0]["actual"]
        self.assertEqual(actual["text"], "b")
        self.assertEqual(actual["evidence"]["path"], "design.sv")
        self.assertEqual(actual["evidence"]["line_start"], 4)
        self.assertEqual(actual["evidence"]["logical_location"]["path"], "virtual.sv")

    def test_effective_input_change_invalidates_the_whole_session(self) -> None:
        assert runtime is not None
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            include = source / "width.svh"
            include.write_text("`define WIDTH 2\n")
            (source / "design.sv").write_text(
                '`include "width.svh"\nmodule stale_top; logic [`WIDTH-1:0] q; endmodule\n'
            )
            (source / "build.json").write_text(
                json.dumps(
                    {
                        "schema": runtime.BUILD_SCHEMA,
                        "files": ["design.sv"],
                        "filelists": [],
                        "include_dirs": ["."],
                        "defines": [],
                        "tops": ["stale_top"],
                        "single_unit": True,
                        "environment": {},
                    }
                )
            )
            with runtime.RuntimeQuerySession.open(
                source_root=source,
                build_path=source / "build.json",
                project_name="stale",
                source_revision="fixture-v1",
            ) as session:
                include.write_text("`define WIDTH 3\n")
                stale = session.query(
                    {"id": "stale", "op": "inspect", "path": "stale_top.q"}
                )
                later = session.query(
                    {"id": "later", "op": "scope", "path": "stale_top"}
                )
        self.assertEqual(stale["status"], "stale_input")
        self.assertEqual(later["status"], "stale_input")
        self.assertEqual(later["completeness"], "invalidated")

    def test_native_analysis_diagnostics_and_type_parameters_survive_projection(self) -> None:
        assert runtime is not None
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "design.sv").write_text(
                "module leaf #(parameter type T = logic)(); T q; endmodule\n"
                "module diag_top(input logic a, b);\n"
                "  logic q; leaf #(.T(logic [3:0])) u();\n"
                "  always_comb q = a; always_comb q = b;\n"
                "endmodule\n"
            )
            (source / "build.json").write_text(
                json.dumps(
                    {
                        "schema": runtime.BUILD_SCHEMA,
                        "files": ["design.sv"],
                        "filelists": [],
                        "include_dirs": [],
                        "defines": [],
                        "tops": ["diag_top"],
                        "single_unit": True,
                        "environment": {},
                    }
                )
            )
            with runtime.RuntimeQuerySession.open(
                source_root=source,
                build_path=source / "build.json",
                project_name="diagnostic",
                source_revision="fixture-v1",
            ) as session:
                parameter = session.query(
                    {"id": "parameter", "op": "inspect", "path": "diag_top.u"}
                )
                drivers = session.query(
                    {"id": "drivers", "op": "uses", "path": "diag_top.q", "mode": "drivers"}
                )
                one_driver = session.query(
                    {
                        "id": "one-driver",
                        "op": "uses",
                        "path": "diag_top.q",
                        "mode": "drivers",
                        "limit": 1,
                    }
                )
        self.assertEqual(parameter["status"], "ok")
        type_parameter = parameter["record"]["parameters"][0]
        self.assertEqual(type_parameter["kind"], "TypeParameter")
        self.assertIn("logic[3:0]", type_parameter["target_type"]["text"].replace(" ", ""))
        self.assertGreater(drivers["analysis_diagnostic_summary"]["total"], 0)
        self.assertTrue(
            any("MultipleAlwaysAssigns" in item["code"] for item in drivers["analysis_diagnostics"])
        )
        self.assertEqual(len(one_driver["records"]), 1)
        self.assertTrue(one_driver["truncated"])
        self.assertEqual(one_driver["omitted_count"], len(drivers["records"]) - 1)

    def test_compile_once_query_many_session_protocol(self) -> None:
        assert runtime is not None
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join((str(PYSLANG_SITE), str(LIB)))
        process = subprocess.Popen(
            [
                sys.executable,
                str(LIB / "rtl_query_runtime.py"),
                "--source-root",
                str(FIXTURE),
                "--build",
                str(FIXTURE / "build.json"),
                "--project-name",
                "fixture",
                "--source-revision",
                "fixture-v1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        ready = json.loads(process.stdout.readline())
        self.assertEqual(ready["status"], "ready")
        process.stdin.write(json.dumps({"id": "one", "op": "scope", "path": "query_top"}) + "\n")
        process.stdin.write(json.dumps({"id": "two", "op": "inspect", "path": "query_top.status"}) + "\n")
        process.stdin.close()
        first = json.loads(process.stdout.readline())
        second = json.loads(process.stdout.readline())
        self.assertEqual(first["telemetry"]["compilation_count"], 1)
        self.assertEqual(second["telemetry"]["compilation_count"], 1)
        self.assertEqual(second["telemetry"]["query_count"], 2)
        self.assertEqual(process.wait(timeout=5), 0)
        process.stdout.close()
        assert process.stderr is not None
        process.stderr.close()

    def test_request_and_result_payloads_are_bounded(self) -> None:
        assert runtime is not None
        with self.session() as session:
            direct = session.query(
                {
                    "id": "x" * (runtime.MAX_REQUEST_ID_CHARS + 1),
                    "op": "inspect",
                    "path": "query_top",
                }
            )
            input_stream = io.StringIO(
                json.dumps(
                    {
                        "id": "large-line",
                        "op": "inspect",
                        "path": "x" * runtime.MAX_REQUEST_BYTES,
                    }
                )
                + "\n"
            )
            output_stream = io.StringIO()
            runtime.serve(session, input_stream, output_stream)
        self.assertEqual(direct["status"], "unsupported")
        self.assertLessEqual(
            len(json.dumps(direct, sort_keys=True).encode("utf-8")),
            runtime.MAX_OUTPUT_BYTES,
        )
        rows = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual(rows[1]["status"], "unsupported")
        self.assertIn("input limit", rows[1]["message"])

    def test_public_cli_starts_runtime_session_without_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            source = project / "source"
            source.mkdir(parents=True)
            (project / "analysis").mkdir()
            shutil.copyfile(FIXTURE / "design.sv", source / "design.sv")
            shutil.copyfile(FIXTURE / "build.json", project / "build.json")
            (project / "rtl-project.json").write_text(
                json.dumps(
                    {
                        "schema": "rtl-analysis-agent/project-v1",
                        "name": "runtime-query-cli",
                        "source": "source",
                        "analysis": "analysis",
                        "revision": None,
                        "entry": "design.sv",
                    }
                )
            )
            completed = subprocess.run(
                [
                    str(ROOT / "src/rtl-pack/bin/rtl"),
                    "--project",
                    str(project),
                    "query",
                    "--build",
                    str(project / "build.json"),
                ],
                input=json.dumps(
                    {"id": "native", "op": "python", "code": "node = root.lookupName('query_top.u_leaf')\nresult = {'module': node.definition.name, 'evidence': evidence(node)}"}
                )
                + "\n" + json.dumps(
                    {"id": "binding", "op": "bindings", "instance": "query_top.u_leaf"}
                ) + "\n",
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        ready, native, result = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(native["status"], "ok", native)
        self.assertEqual(native["result"]["module"], "query_leaf")
        self.assertEqual(native["result"]["evidence"]["path"], "source/design.sv")
        self.assertEqual(native["result"]["evidence"]["locator"], "design.sv")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["telemetry"]["compilation_count"], 1)
        self.assertEqual(result["telemetry"]["query_count"], 2)

    def test_public_cli_preserves_compile_failure_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            source = project / "source"
            source.mkdir(parents=True)
            (project / "analysis").mkdir()
            shutil.copyfile(FIXTURE / "design.sv", source / "design.sv")
            spec = json.loads((FIXTURE / "build.json").read_text())
            spec["tops"] = ["missing_top"]
            (project / "build.json").write_text(json.dumps(spec))
            (project / "rtl-project.json").write_text(
                json.dumps(
                    {
                        "schema": "rtl-analysis-agent/project-v1",
                        "name": "runtime-query-cli-error",
                        "source": "source",
                        "analysis": "analysis",
                        "revision": None,
                        "entry": "design.sv",
                    }
                )
            )
            completed = subprocess.run(
                [
                    str(ROOT / "src/rtl-pack/bin/rtl"),
                    "--project",
                    str(project),
                    "query",
                    "--build",
                    str(project / "build.json"),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 2)
        result = json.loads(completed.stdout)
        self.assertEqual(result["error"]["code"], "compile_failed")
        self.assertIn("diagnostics", result["error"]["details"])

    def test_build_spec_rejects_paths_outside_source_root(self) -> None:
        assert runtime is not None
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory) / "build.json"
            build.write_text(
                json.dumps(
                    {
                        "schema": "rtl-analysis-agent/runtime-query-build-v1",
                        "files": ["../outside.sv"],
                        "filelists": [],
                        "include_dirs": [],
                        "defines": [],
                        "tops": ["top"],
                        "single_unit": True,
                        "environment": {},
                    }
                )
            )
            with self.assertRaisesRegex(runtime.QueryRuntimeError, "escapes source root"):
                runtime.RuntimeQuerySession.open(
                    source_root=FIXTURE,
                    build_path=build,
                    project_name="fixture",
                    source_revision="fixture-v1",
                )


if __name__ == "__main__":
    unittest.main()
