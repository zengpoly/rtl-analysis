"""Run-local knowledge result bundle tests for the Agent Runner."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/rtl-pack/lib"))
SERVER = ROOT / "tools/agent-runner/runtime_server.py"
SPEC = importlib.util.spec_from_file_location("agent_runner_runtime_server", SERVER)
assert SPEC is not None and SPEC.loader is not None
runtime_server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime_server)
ENTRY_SPEC = importlib.util.spec_from_file_location(
    "agent_runner_entry", ROOT / "tools/agent-runner/runner_entry.py"
)
assert ENTRY_SPEC is not None and ENTRY_SPEC.loader is not None
runner_entry = importlib.util.module_from_spec(ENTRY_SPEC)
ENTRY_SPEC.loader.exec_module(runner_entry)


class KnowledgeResultBundleTests(unittest.TestCase):
    def response(
        self,
        *,
        route: str = "DIRECT",
        reasons: list[str] | None = None,
        coverage: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "schema": "rtl-analysis-agent/runtime-query-result-v1",
            "request_id": "rtlq-request-1",
            "operation": "knowledge",
            "status": "ok",
            "route": route,
            "reasons": reasons or ["complete-exact-coverage"],
            "candidate_accessed": True,
            "target": {
                "qualified_name": "top",
                "build_sha256": "b" * 64,
                "subtree_scope": "direct",
                "request_kind": "module-understanding",
            },
            "coverage": coverage or {
                "requested_sections": ["function"],
                "covered_sections": ["function"],
                "missing_sections": [],
                "requested_additional_requirements": [],
                "covered_additional_requirements": [],
                "missing_additional_requirements": [],
                "unresolved_additional_requirements": [],
                "direct_children": {
                    "required_count": 0,
                    "covered_count": 0,
                    "required_refs": [],
                    "missing_ids": [],
                    "unexpected_ids": [],
                },
            },
            "knowledge": [],
            "entities": [],
            "source_files": [],
            "source_evidence": [],
            "structure": {"direct_instances": [], "instance_bindings": []},
            "limitations": {"recursive_descendants_covered": False},
            "returned_items": 0,
            "returned_counts": {},
        }

    def test_one_response_writes_two_bound_views_and_rereads_without_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            response = self.response()
            artifacts = runtime_server.persist_knowledge_result(
                root,
                run_id="run-1",
                request_id="rtlq-request-1",
                response=response,
                candidate_sha256="c" * 64,
            )
            self.assertEqual(artifacts["result_ref"], "rtlq-request-1")
            structured = runtime_server.read_knowledge_result(
                root, run_id="run-1", result_ref="rtlq-request-1", output_format="json"
            )
            readable = runtime_server.read_knowledge_result(
                root, run_id="run-1", result_ref="rtlq-request-1", output_format="readable"
            )
            parsed = json.loads(structured)
            self.assertEqual(parsed["result_identity"]["run_id"], "run-1")
            self.assertEqual(parsed["result_identity"]["candidate_sha256"], "c" * 64)
            self.assertEqual(len(parsed["result_identity"]["request_sha256"]), 64)
            self.assertEqual(parsed["target"]["build_sha256"], "b" * 64)
            self.assertIn("# RTL module knowledge", readable)
            self.assertEqual(len(readable.encode()), artifacts["readable"]["bytes"])
            self.assertEqual(len(structured.encode()), artifacts["structured"]["bytes"])
            self.assertEqual(
                stat.S_IMODE(Path(artifacts["structured"]["path"]).stat().st_mode), 0o400
            )
            self.assertEqual(
                stat.S_IMODE(Path(artifacts["readable"]["path"]).stat().st_mode), 0o400
            )

    def test_public_reread_preserves_direct_context_and_bypass_state(self) -> None:
        context_coverage = {
            "requested_sections": ["function"],
            "covered_sections": ["function"],
            "missing_sections": [],
            "requested_additional_requirements": ["timing-guarantee"],
            "covered_additional_requirements": [],
            "missing_additional_requirements": ["timing-guarantee"],
            "unresolved_additional_requirements": [],
            "direct_children": {
                "required_count": 0, "covered_count": 0,
                "required_refs": [], "missing_ids": [], "unexpected_ids": [],
            },
        }
        cases = (
            ("DIRECT", ["complete-exact-coverage"], self.response()),
            (
                "CONTEXT", ["additional-requirement-gap"],
                self.response(
                    route="CONTEXT", reasons=["additional-requirement-gap"],
                    coverage=context_coverage,
                ),
            ),
            (
                "BYPASS", ["no-usable-knowledge"],
                self.response(route="BYPASS", reasons=["no-usable-knowledge"]),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (route, reasons, original) in enumerate(cases, 1):
                result_ref = f"rtlq-case-{index}"
                original["request_id"] = result_ref
                runtime_server.persist_knowledge_result(
                    root,
                    run_id="run-1",
                    request_id=result_ref,
                    response=original,
                    candidate_sha256="c" * 64,
                )
                before = {
                    path.name: path.read_bytes()
                    for path in root.glob(f"{result_ref}.*")
                }
                json_response = runtime_server.serve_knowledge_result(
                    root,
                    knowledge_mode="on",
                    run_id="run-1",
                    request_id=f"read-json-{index}",
                    result_ref=result_ref,
                    output_format="json",
                )
                readable_response = runtime_server.serve_knowledge_result(
                    root,
                    knowledge_mode="on",
                    run_id="run-1",
                    request_id=f"read-readable-{index}",
                    result_ref=result_ref,
                    output_format="readable",
                )
                self.assertEqual(json_response["status"], "ok")
                self.assertEqual(json_response["route"], route)
                self.assertEqual(json_response["reasons"], reasons)
                self.assertEqual(readable_response["route"], route)
                self.assertEqual(readable_response["reasons"], reasons)
                self.assertEqual(json.loads(json_response["content"])["route"], route)
                self.assertIn(f"Route: {route}", readable_response["content"])
                if route == "CONTEXT":
                    self.assertIn(
                        "Missing additional requirements: timing-guarantee",
                        readable_response["content"],
                    )
                after = {
                    path.name: path.read_bytes()
                    for path in root.glob(f"{result_ref}.*")
                }
                self.assertEqual(before, after)

    def test_off_short_circuits_and_invalid_saved_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(
                runtime_server,
                "load_knowledge_result",
                side_effect=AssertionError("OFF accessed a saved ON result"),
            ):
                off = runtime_server.serve_knowledge_result(
                    root,
                    knowledge_mode="off",
                    run_id="run-1",
                    request_id="read-off",
                    result_ref="missing-result",
                    output_format="json",
                )
            self.assertEqual(off["route"], "BYPASS")
            self.assertEqual(off["reasons"], ["knowledge-disabled"])
            self.assertFalse(off["result_accessed"])

            with self.assertRaisesRegex(ValueError, "route"):
                runtime_server.persist_knowledge_result(
                    root,
                    run_id="run-1",
                    request_id="rtlq-invalid-state",
                    response=self.response(route="INVALID", reasons=["invalid"]),
                    candidate_sha256="c" * 64,
                )
            missing_reasons = self.response()
            del missing_reasons["reasons"]
            with self.assertRaisesRegex(ValueError, "reasons"):
                runtime_server.persist_knowledge_result(
                    root,
                    run_id="run-1",
                    request_id="rtlq-missing-reasons",
                    response=missing_reasons,
                    candidate_sha256="c" * 64,
                )
            artifacts = runtime_server.persist_knowledge_result(
                root,
                run_id="run-1",
                request_id="rtlq-tampered-state",
                response=self.response(),
                candidate_sha256="c" * 64,
            )
            structured_path = Path(artifacts["structured"]["path"])
            tampered = json.loads(structured_path.read_text(encoding="utf-8"))
            tampered["route"] = "INVALID"
            structured_path.chmod(0o600)
            structured_path.write_text(json.dumps(tampered), encoding="utf-8")
            structured_path.chmod(0o400)
            with self.assertRaisesRegex(ValueError, "route"):
                runtime_server.serve_knowledge_result(
                    root,
                    knowledge_mode="on",
                    run_id="run-1",
                    request_id="read-tampered",
                    result_ref="rtlq-tampered-state",
                    output_format="json",
                )
            with self.assertRaises(FileNotFoundError):
                runtime_server.serve_knowledge_result(
                    root,
                    knowledge_mode="on",
                    run_id="run-1",
                    request_id="read-missing",
                    result_ref="missing-result",
                    output_format="json",
                )

    def test_unclassified_additional_requirement_is_rendered_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            response = self.response(
                route="CONTEXT",
                reasons=["additional-requirement-gap"],
                coverage={
                    "requested_sections": ["function"],
                    "covered_sections": ["function"],
                    "missing_sections": [],
                    "requested_additional_requirements": ["unknown-property"],
                },
            )
            artifacts = runtime_server.persist_knowledge_result(
                root,
                run_id="run-1",
                request_id="rtlq-unresolved",
                response=response,
                candidate_sha256="c" * 64,
            )
            readable = Path(artifacts["readable"]["path"]).read_text(encoding="utf-8")
            self.assertIn(
                "Unresolved additional requirements: unknown-property", readable
            )

    def test_result_ref_cannot_escape_or_overwrite_run_local_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_server.persist_knowledge_result(
                root,
                run_id="run-1",
                request_id="rtlq-request-1",
                response=self.response(),
                candidate_sha256="c" * 64,
            )
            with self.assertRaises(FileExistsError):
                runtime_server.persist_knowledge_result(
                    root,
                    run_id="run-1",
                    request_id="rtlq-request-1",
                    response=self.response(),
                    candidate_sha256="c" * 64,
                )
            with self.assertRaises(ValueError):
                runtime_server.read_knowledge_result(
                    root, run_id="run-1", result_ref="../other", output_format="json"
                )
            with self.assertRaises(ValueError):
                runtime_server.read_knowledge_result(
                    root, run_id="other-run", result_ref="rtlq-request-1", output_format="json"
                )

    def test_runner_prompt_only_declares_visibility_not_analysis_policy(self) -> None:
        on = runner_entry.capability_contract("C", "on", "available")
        off = runner_entry.capability_contract("C", "off")
        self.assertIn("Reviewed module knowledge is visible", on)
        self.assertIn("--format readable", on)
        self.assertIn("Reviewed module knowledge is disabled", off)
        for prompt in (on, off):
            self.assertNotIn("DIRECT", prompt)
            self.assertNotIn("CONTEXT", prompt)
            self.assertNotIn("BYPASS", prompt)
            self.assertNotIn("STOP", prompt)

    def test_prefetch_delivery_builds_one_bounded_prompt_copy(self) -> None:
        material = (
            "# RTL module knowledge\n\n"
            "- Route: DIRECT\n"
            "- Result ref: rtlq-prefetch-1\n"
            "- Marker: UNIQUE_PREFETCH_MATERIAL\n"
        )
        self.assertEqual(
            runner_entry.resolve_knowledge_delivery("on", "", "top"), "prefetch"
        )
        self.assertEqual(
            runner_entry.resolve_knowledge_delivery("on", "", ""), "off"
        )
        self.assertEqual(
            runner_entry.resolve_knowledge_delivery("on", "available", ""), "available"
        )
        self.assertEqual(
            runner_entry.resolve_knowledge_delivery("on", "prefetch"), "prefetch"
        )
        self.assertEqual(
            runner_entry.resolve_knowledge_delivery("off", "", ""), "off"
        )
        with self.assertRaisesRegex(ValueError, "prefetch"):
            runner_entry.resolve_knowledge_delivery("off", "prefetch", "top")

        prompt = runner_entry.build_agent_prompt(
            treatment="C",
            knowledge_mode="on",
            knowledge_delivery="prefetch",
            question="ORIGINAL QUESTION",
            prefetched_knowledge=material,
        )
        self.assertEqual(
            prompt.count("----- BEGIN REVIEWED PREBUILT KNOWLEDGE -----"), 1
        )
        self.assertEqual(
            prompt.count("----- END REVIEWED PREBUILT KNOWLEDGE -----"), 1
        )
        self.assertEqual(prompt.count("UNIQUE_PREFETCH_MATERIAL"), 1)
        self.assertEqual(prompt.count("ORIGINAL QUESTION"), 1)
        self.assertIn("already been prefetched by the Controller", prompt)
        self.assertIn("do not repeat the same `rtlq knowledge` query", prompt)
        self.assertIn("Source and `rtlq` remain available", prompt)
        for consumer_policy in (
            "organize the answer from covered material",
            "use Source/Tool only for",
            "retain covered material and investigate only",
            "use the ordinary Source/Tool route",
            "must be copied verbatim",
        ):
            self.assertNotIn(consumer_policy, prompt)

        available = runner_entry.build_agent_prompt(
            treatment="C",
            knowledge_mode="on",
            knowledge_delivery="available",
            question="ORIGINAL QUESTION",
        )
        off = runner_entry.build_agent_prompt(
            treatment="C",
            knowledge_mode="off",
            knowledge_delivery="off",
            question="ORIGINAL QUESTION",
        )
        for old_mode in (available, off):
            self.assertNotIn("BEGIN REVIEWED PREBUILT KNOWLEDGE", old_mode)

    def test_no_target_skips_prefetch_without_claiming_knowledge_disabled(self) -> None:
        prompt = runner_entry.build_agent_prompt(
            treatment="C",
            knowledge_mode="off",
            knowledge_delivery="off",
            knowledge_requested_mode="on",
            no_prefetch_reason="target-unresolved",
            question="ORIGINAL QUESTION",
        )
        self.assertIn("no reliable exact target", prompt)
        self.assertIn("target-unresolved", prompt)
        self.assertNotIn("knowledge is disabled", prompt)
        self.assertNotIn("BEGIN REVIEWED PREBUILT KNOWLEDGE", prompt)

    def test_context_delivery_keeps_route_gaps_and_result_ref(self) -> None:
        material = (
            "# RTL module knowledge\n\n"
            "- Route: CONTEXT\n"
            "- Reason: additional-requirement-gap\n"
            "- Missing additional requirements: timing-guarantee\n"
            "- Result ref: rtlq-context-1\n"
        )
        prompt = runner_entry.build_agent_prompt(
            treatment="C", knowledge_mode="on", knowledge_delivery="prefetch",
            question="ORIGINAL QUESTION", prefetched_knowledge=material,
        )
        for expected in (
            "Route: CONTEXT", "additional-requirement-gap",
            "timing-guarantee", "rtlq-context-1",
        ):
            self.assertIn(expected, prompt)
        self.assertNotIn("only the explicit missing or unresolved obligations", prompt)
        self.assertNotIn("retain covered material", prompt)
        self.assertNotIn("CLOSED_KNOWLEDGE", prompt)

    def test_bypass_prefetch_delivers_only_short_route_reason(self) -> None:
        large = "X" * 28000
        readable = (
            "# RTL module knowledge\n\n"
            "- Route: BYPASS\n"
            "- Reason: scope-not-covered\n"
            "- Result ref: rtlq-bypass-1\n\n"
            f"## Internal payload\n{large}\n"
        )
        completed = runner_entry.subprocess.CompletedProcess(
            ["rtlq"], 0, stdout=readable, stderr=""
        )
        with mock.patch.object(
            runner_entry.subprocess, "run", return_value=completed
        ), mock.patch.object(runner_entry, "append_event"):
            result = runner_entry.controller_prefetch(run_id="run-1", target="top")
        self.assertEqual(result["route"], "BYPASS")
        self.assertEqual(result["reasons"], ["scope-not-covered"])
        self.assertNotIn(large, result["delivery_material"])
        self.assertLess(result["delivered_bytes"], 512)
        self.assertGreater(result["readable_bytes"], 28000)
        prompt = runner_entry.build_agent_prompt(
            treatment="C",
            knowledge_mode="on",
            knowledge_delivery="prefetch",
            question="ORIGINAL QUESTION",
            prefetched_knowledge=result["delivery_material"],
        )
        self.assertIn("Route: BYPASS", prompt)
        self.assertIn("scope-not-covered", prompt)
        self.assertNotIn("use the ordinary Source/Tool route", prompt)

    def test_skill_is_the_only_route_consumer_policy_owner(self) -> None:
        skill = (ROOT / "src/rtl-pack/skills/rtl-query/SKILL.md").read_text(
            encoding="utf-8"
        )
        prompts = [
            runner_entry.build_agent_prompt(
                treatment="C",
                knowledge_mode="on",
                knowledge_delivery="prefetch",
                question="ORIGINAL QUESTION",
                prefetched_knowledge=(
                    "# RTL module knowledge\n\n"
                    f"- Route: {route}\n"
                    f"- Reason: {reason}\n"
                ),
            )
            for route, reason in (
                ("DIRECT", "complete-exact-coverage"),
                ("CONTEXT", "section-gap"),
                ("BYPASS", "scope-not-covered"),
            )
        ]
        for prompt in prompts:
            for duplicate_policy in (
                "organize the answer from covered material",
                "retain covered material and investigate only",
                "use the ordinary Source/Tool route",
            ):
                self.assertNotIn(duplicate_policy, prompt)
        for skill_contract in (
            "`DIRECT`",
            "`CONTEXT`",
            "`BYPASS`",
            "`CLOSED_KNOWLEDGE`",
            "只为每个 missing obligation",
        ):
            self.assertIn(skill_contract, skill)
        self.assertIn("接收 Query Entry", skill)

    def test_exact_identity_guard_flags_p11_style_qualified_name_alteration(self) -> None:
        structured = {
            "entities": [{
                "ref": "I11",
                "qualified_name": (
                    "top.cache.x_ct_lsu_dcache_ld_data_bank7_array"
                ),
            }],
            "structure": {"direct_instances": [{
                "instance_ref": "I11",
                "characteristics": {
                    "name": "x_ct_lsu_dcache_ld_data_bank7_array",
                },
            }]},
        }
        altered = runner_entry.validate_exact_identities(
            "Instance x_ct_lsu_dcache_ld_data_bank7_array is "
            "top.cache.x_ct_lsu_dcache_bank7_array.",
            structured,
        )
        self.assertEqual(altered["status"], "ALTERED")
        self.assertEqual(altered["altered_count"], 1)
        self.assertFalse(altered["answer_modified"])

        correct = runner_entry.validate_exact_identities(
            "top.cache.x_ct_lsu_dcache_ld_data_bank7_array", structured
        )
        self.assertEqual(correct["status"], "PASS")

    def test_controller_prefetch_is_one_query_with_explicit_actor_events(self) -> None:
        readable = (
            "# RTL module knowledge\n\n"
            "- Route: DIRECT\n"
            "- Reason: complete-exact-coverage\n"
            "- Result ref: rtlq-prefetch-1\n"
        )
        events: list[tuple[str, dict[str, object]]] = []
        completed = runner_entry.subprocess.CompletedProcess(
            ["rtlq"], 0, stdout=readable, stderr=""
        )
        with mock.patch.object(
            runner_entry.subprocess, "run", return_value=completed
        ) as run, mock.patch.object(
            runner_entry,
            "append_event",
            side_effect=lambda event_type, **values: events.append((event_type, values)),
        ):
            result = runner_entry.controller_prefetch(
                run_id="run-1", target="ct_lsu_dcache_top"
            )
        self.assertEqual(run.call_count, 1)
        self.assertEqual(
            run.call_args.args[0],
            ["rtlq", "knowledge", "ct_lsu_dcache_top", "--format", "readable"],
        )
        self.assertEqual(
            run.call_args.kwargs["env"]["RTLQ_ACTOR"], "controller"
        )
        self.assertEqual(run.call_args.kwargs["env"]["RTLQ_PHASE"], "prefetch")
        self.assertEqual(result["result_ref"], "rtlq-prefetch-1")
        self.assertEqual(result["readable"], readable)
        self.assertEqual(result["bytes"], len(readable.encode()))
        self.assertEqual([item[0] for item in events], [
            "controller_prefetch_started", "controller_prefetch_finished"
        ])
        self.assertTrue(all(item[1]["actor"] == "controller" for item in events))
        self.assertTrue(all(item[1]["phase"] == "prefetch" for item in events))

    def test_request_projection_keeps_actor_and_phase(self) -> None:
        bounded = runtime_server.bounded_request({
            "id": "rtlq-prefetch-1",
            "op": "knowledge",
            "target": "top",
            "actor": "controller",
            "phase": "prefetch",
        })
        self.assertEqual(bounded["actor"], "controller")
        self.assertEqual(bounded["phase"], "prefetch")

    def test_public_runner_defaults_on_to_prefetch_only_with_reliable_target(self) -> None:
        run_script = ROOT / "tools/agent-runner/run"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "top.sv").write_text("module top; endmodule\n")
            question = root / "question.md"
            question.write_text("Explain top.\n")
            build = root / "build.json"
            build.write_text(json.dumps({
                "schema": "rtl-analysis-agent/runtime-query-build-v1",
                "tops": ["top"],
            }))
            base = [
                str(run_script), "--treatment", "B", "--source", str(source),
                "--prompt", str(question), "--build", str(build),
                "--output", str(root / "output"), "--probe", "--knowledge", "on",
                "--candidate", str(root / "missing.sqlite"),
            ]
            environment = {**os.environ, "AGENT_RUNNER_IMAGE_B": "missing:p12-test"}
            no_target = subprocess.run(
                base, capture_output=True, text=True, check=False, env=environment
            )
            self.assertEqual(no_target.returncode, 2)
            self.assertIn("Docker image 'missing:p12-test' is missing", no_target.stderr)
            self.assertNotIn("requires --candidate", no_target.stderr)

            with_target = subprocess.run(
                [*base, "--knowledge-target", "top"],
                capture_output=True, text=True, check=False, env=environment,
            )
            self.assertEqual(with_target.returncode, 2)
            self.assertIn("knowledge ON requires --candidate FILE", with_target.stderr)

    def test_public_runner_keeps_available_as_explicit_legacy_mode(self) -> None:
        run_script = ROOT / "tools/agent-runner/run"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "top.sv").write_text("module top; endmodule\n")
            question = root / "question.md"
            question.write_text("Explain top.\n")
            build = root / "build.json"
            build.write_text(json.dumps({
                "schema": "rtl-analysis-agent/runtime-query-build-v1",
                "tops": ["top"],
            }))
            completed = subprocess.run(
                [
                    str(run_script), "--treatment", "B", "--source", str(source),
                    "--prompt", str(question), "--build", str(build),
                    "--output", str(root / "output"), "--probe", "--knowledge", "on",
                    "--knowledge-delivery", "available",
                    "--candidate", str(root / "missing.sqlite"),
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("knowledge ON requires --candidate FILE", completed.stderr)


if __name__ == "__main__":
    unittest.main()
