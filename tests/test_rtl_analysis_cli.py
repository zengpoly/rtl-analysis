from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/rtl-pack/lib"))

import rtl_analysis_cli as cli  # noqa: E402


class DoctorTests(unittest.TestCase):
    def test_no_project_checks_tools_without_loading_default_target(self) -> None:
        tool = {"name": "pyslang", "version": "11.0.0",
                "version_command": ["bin/pyslang-version"], "version_contains": "11.0.0"}
        with mock.patch.object(cli, "target_context") as target, \
                mock.patch.object(cli, "load_json", return_value={"tools": [tool]}), \
                mock.patch.object(cli, "tool_context", return_value=(tool, ROOT)), \
                mock.patch.object(cli, "run_tool", return_value=subprocess.CompletedProcess(
                    [], 0, "11.0.0", "")) as run:
            code, result = cli.doctor_command(ROOT, None)
        target.assert_not_called()
        run.assert_called_once()
        self.assertEqual(code, 0)
        self.assertEqual(result["scope"], "toolchain")
        self.assertNotIn("target", result)
        self.assertEqual(result["toolchain"], [{"name": "pyslang", "version": "11.0.0"}])

    def test_explicit_project_still_requires_valid_source(self) -> None:
        with mock.patch.object(cli, "target_context", side_effect=cli.AdapterError(
                "project-missing", "missing source")) as target:
            with self.assertRaises(cli.AdapterError):
                cli.doctor_command(ROOT, None, "projects/missing")
        target.assert_called_once_with(ROOT, "openc910", "projects/missing")

    def test_bare_cli_does_not_supply_a_default_target_to_doctor(self) -> None:
        with mock.patch.object(cli, "doctor_command", return_value=(0, {})) as doctor, \
                mock.patch("sys.stdout"):
            self.assertEqual(cli.main(["doctor"]), 0)
        doctor.assert_called_once_with(ROOT, None, None)


class NormalizationTests(unittest.TestCase):
    def test_slang_text_diagnostic_is_stable(self) -> None:
        diagnostics, truncated = cli.normalize_slang_diagnostics(
            [], "rtl/top.sv:3:8: error: expected expression\nassign = ;\n       ^"
        )
        self.assertFalse(truncated)
        self.assertEqual(
            diagnostics,
            [
                {
                    "severity": "error",
                    "message": "expected expression",
                    "location": {"file": "rtl/top.sv", "line": 3, "column": 8},
                }
            ],
        )

    def test_verible_diagnostic_is_stable(self) -> None:
        diagnostics, truncated = cli.normalize_verible_diagnostics(
            "rtl/top.sv:12:4-9: Remove trailing spaces. [Style: trailing-spaces] [no-trailing-spaces]"
        )
        self.assertFalse(truncated)
        self.assertEqual(diagnostics[0]["location"]["line"], 12)
        self.assertEqual(diagnostics[0]["rule"], "no-trailing-spaces")
        self.assertEqual(diagnostics[0]["message"], "Remove trailing spaces.")

    def test_module_outline_handles_non_ansi_ports(self) -> None:
        tree = {
            "kind": "ModuleDeclaration",
            "header": {
                "name": {"kind": "Identifier", "text": "demo"},
                "ports": {
                    "ports": [
                        {
                            "kind": "ImplicitNonAnsiPort",
                            "expr": {"name": {"kind": "Identifier", "text": "clk"}},
                        }
                    ]
                },
            },
            "members": [{"kind": "ContinuousAssign"}],
        }
        self.assertEqual(
            cli.module_outline(tree),
            [{"name": "demo", "ports": ["clk"], "member_kinds": {"ContinuousAssign": 1}}],
        )

    def test_yosys_structure_and_connection_are_bounded(self) -> None:
        netlist = {
            "modules": {
                "demo": {
                    "ports": {
                        "result": {"direction": "output", "bits": [2]},
                    },
                    "cells": {
                        "$or$/work/input.sv:4$1": {
                            "type": "$or",
                            "port_directions": {"Y": "output"},
                            "connections": {"Y": [2]},
                        }
                    },
                    "netnames": {"result": {"bits": [2]}},
                }
            }
        }
        modules = cli.yosys_modules(netlist)
        self.assertEqual(modules[0]["cell_types"], {"$or": 1})
        module, signal = cli.yosys_signal_connections(netlist, "result", "rtl/demo.sv")
        self.assertEqual(module, "demo")
        self.assertEqual(signal["connections"][0]["cell"], "$or$rtl/demo.sv:4$1")
        self.assertEqual(signal["connections"][0]["direction"], "output")


class BoundaryTests(unittest.TestCase):
    def project(self, parent: Path, name: str = "portable-project") -> Path:
        project = parent / name
        (project / "source").mkdir(parents=True)
        (project / "analysis").mkdir()
        (project / "source/top.sv").write_text("module top; endmodule\n")
        (project / "rtl-project.json").write_text(
            json.dumps(
                {
                    "schema": cli.PROJECT_SCHEMA,
                    "name": "portable-project",
                    "source": "source",
                    "analysis": "analysis",
                    "revision": None,
                    "entry": "top.sv",
                }
            )
        )
        return project

    def test_absolute_target_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(cli.AdapterError, "relative"):
            cli.resolve_target_file(ROOT, "/tmp/outside.sv")

    def test_build1_composite_parser_has_one_scope_aware_entry(self) -> None:
        args = cli.build_parser().parse_args(
            [
                "model", "knowledge-build",
                "--project", "/tmp/project",
                "--scope", "whole-design",
                "--candidate", "analysis/model/build1.sqlite",
                "--manual", "analysis/manual-v6",
            ]
        )
        self.assertEqual(args.model_operation, "knowledge-build")
        self.assertEqual(args.build, "runtime-query-build.json")
        self.assertEqual(args.source_viewer, "locator")

    def test_yosys_cold_start_has_a_separate_bounded_budget(self) -> None:
        self.assertEqual(cli.TOOL_TIMEOUT_SECONDS, 30)
        self.assertEqual(cli.YOSYS_TIMEOUT_SECONDS, 90)

    def test_external_temporary_directory_is_explicit_and_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            selected = parent / "runtime-tmp"
            with mock.patch.dict("os.environ", {"RTL_ANALYSIS_TMPDIR": str(selected)}):
                self.assertEqual(cli.temporary_root(ROOT), selected.resolve())
            with mock.patch.dict("os.environ", {"RTL_ANALYSIS_TMPDIR": "relative/tmp"}):
                with self.assertRaisesRegex(cli.AdapterError, "must be absolute"):
                    cli.temporary_root(ROOT)
            link = parent / "linked-tmp"
            link.symlink_to(selected, target_is_directory=True)
            with mock.patch.dict("os.environ", {"RTL_ANALYSIS_TMPDIR": str(link)}):
                with self.assertRaisesRegex(cli.AdapterError, "symbolic link"):
                    cli.temporary_root(ROOT)

    def test_target_path_escape_is_rejected_with_json_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            # An existing out-of-source file must still be rejected, without a local RTL checkout.
            (project / "outside.sv").write_text("module outside; endmodule\n")
            result = subprocess.run(
                [str(ROOT / "src/rtl-pack/bin/rtl"), "--project", str(project),
                 "outline", "--file", "../outside.sv"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error"]["code"], "target-path")

    def test_unsupported_build1_scope_returns_machine_blocked_without_build_spec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            result = subprocess.run(
                [
                    str(ROOT / "src/rtl-pack/bin/rtl"),
                    "model", "knowledge-build",
                    "--project", str(project),
                    "--scope", "multi-root",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema"], "rtl-analysis-agent/build1-status-v1")
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertEqual(payload["reason_code"], "SCOPE_MODE_UNSUPPORTED")
        self.assertEqual(
            payload["heavy_actions"],
            {"compile_build_count": 0, "render_count": 0},
        )

    def test_build1_input_failure_keeps_machine_status_contract(self) -> None:
        result = subprocess.run(
            [
                str(ROOT / "src/rtl-pack/bin/rtl"),
                "model", "knowledge-build",
                "--project", "/definitely/missing/build1-project",
                "--scope", "whole-design",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema"], "rtl-analysis-agent/build1-status-v1")
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertEqual(payload["reason_code"], "INPUT_OR_ENVIRONMENT_INVALID")
        self.assertEqual(
            payload["heavy_actions"],
            {"compile_build_count": 0, "render_count": 0},
        )

    def test_manual_build_returns_public_mapping_identity_and_timing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            candidate = project / "analysis/candidate.sqlite"
            candidate.write_bytes(b"candidate")
            args = cli.build_parser().parse_args(
                [
                    "model", "manual-build",
                    "--project", str(project),
                    "--model", "analysis/candidate.sqlite",
                    "--output", "analysis/manual-v6",
                    "--source-viewer", "locator",
                ]
            )
            model = {
                "candidate_sha256": "c" * 64,
                "scope": {
                    "scope_id": "scope:test",
                    "scope_mode": "ACTUAL_INSTANCE_ROOT",
                    "build_identity": {"build_sha256": "b" * 64},
                    "limitations": ["Build1 semantic knowledge is not built."],
                },
                "scope_presentation": {"label": "Fixture / root_a"},
            }
            manifest = {
                "schema": "rtl-analysis-agent/manual-v6-build1-manifest-v1",
                "scope_id": "scope:test",
                "build_sha256": "b" * 64,
                "candidate_sha256": "c" * 64,
                "page_counts": {
                    "scope": 1, "instance": 2, "module_definition": 2,
                    "source": 0, "container": 0,
                },
                "resource_counts": {"source": 2},
                "structured_detail_schema": "rtl-analysis-agent/manual-v6-structured-detail-v1",
                "structured_detail_counts": {"instance": 2, "module_definition": 2},
                "structured_detail_map": {"entity:a": "structured/a.json"},
                "source_serving": "locator",
                "page_map": {"scope": "index.html", "entity:a": "instance/a.html"},
                "ai_count": 0,
                "behavior_count": 0,
            }
            with (
                mock.patch.object(cli.manual_v6, "load_backbone", return_value=model),
                mock.patch.object(cli.manual_v6, "build_site", return_value=manifest),
            ):
                code, result = cli.model_command(ROOT, args)

        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["candidate"]["sha256"], "c" * 64)
        self.assertEqual(result["candidate"]["path"], "analysis/candidate.sqlite")
        self.assertEqual(result["scope_id"], "scope:test")
        self.assertEqual(result["scope_mode"], "ACTUAL_INSTANCE_ROOT")
        self.assertEqual(result["analysis_scope"], "Fixture / root_a")
        self.assertEqual(result["manual"]["path"], "analysis/manual-v6")
        self.assertEqual(result["manual"]["page_counts"]["instance"], 2)
        self.assertEqual(result["manual"]["structured_detail_counts"]["instance"], 2)
        self.assertEqual(result["source_serving"], "locator")
        self.assertEqual(result["limitations"], ["Build1 semantic knowledge is not built."])
        self.assertEqual(
            set(result["timing"]),
            {"candidate_projection_wall_seconds", "render_wall_seconds", "total_wall_seconds"},
        )
        self.assertGreaterEqual(result["timing"]["total_wall_seconds"], 0)

    def test_project_contract_is_portable_and_unversioned_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            target, source = cli.project_context(str(project))
            self.assertEqual(target["name"], "portable-project")
            self.assertEqual(target["revision"], "unversioned")
            self.assertEqual(target["entry"], "top.sv")
            self.assertEqual(source, (project / "source").resolve())

    def test_project_file_cannot_read_a_neighbor_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            selected = self.project(parent, "selected")
            self.project(parent, "neighbor")
            result = subprocess.run(
                [
                    str(ROOT / "src/rtl-pack/bin/rtl"),
                    "--project",
                    str(selected),
                    "outline",
                    "--file",
                    "../../neighbor/source/top.sv",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["error"]["code"], "target-path")

    def test_project_rejects_hidden_layout_and_unverifiable_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            config_path = project / "rtl-project.json"
            config = json.loads(config_path.read_text())
            config["source"] = "../outside"
            config_path.write_text(json.dumps(config))
            with self.assertRaisesRegex(cli.AdapterError, "fixed relative"):
                cli.project_context(str(project))

            config["source"] = "source"
            config["revision"] = "a" * 40
            config_path.write_text(json.dumps(config))
            with self.assertRaisesRegex(cli.AdapterError, "not a Git worktree"):
                cli.project_context(str(project))


if __name__ == "__main__":
    unittest.main()
