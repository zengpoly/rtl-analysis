"""Product contract for the public Project Model skills and private atoms."""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "src/rtl-pack"
PUBLIC_SKILLS = {
    "rtl-knowledge-build",
    "rtl-build2-draft",
    "rtl-knowledge-manual",
    "rtl-query",
    "rtl-question-factory",
    "rtl-project-model-mvp-build",
    "rtl-project-model-query",
}
BUILD_ATOMS = {
    "plan-scope",
    "freeze-input",
    "run-slang",
    "run-yosys-netlist",
    "run-yosys-fsm",
    "seed-semantics",
    "validate-publish",
    "resume-build",
}
QUERY_ATOMS = {
    "route-question",
    "search-entities",
    "query-summary-entity",
    "query-path",
    "query-fsm",
    "query-clock-reset",
    "query-coverage",
    "query-semantics-evidence",
    "render-result",
}


class DomainSkillLayoutTests(unittest.TestCase):
    def test_runtime_and_question_factory_plus_two_legacy_skills_are_published(self) -> None:
        manifest = json.loads((PACK / "config/domain-pack.json").read_text())
        self.assertEqual(set(manifest["skills"]), PUBLIC_SKILLS)
        self.assertIn("config/rtl-knowledge-registry.json", manifest["files"])
        for required in (
            "config/knowledge-contract-v01-ai.json",
            "config/knowledge-contract-v01-entities.json",
            "config/knowledge-contract-v01-facts.json",
            "config/module-q2-publication-profile.v2.json",
            "lib/rtl_knowledge_prototype.py",
            "lib/rtl_manual_v6.py",
            "lib/rtl_module_knowledge.py",
            "templates/manual-v6",
        ):
            self.assertIn(required, manifest["files"])
        self.assertNotIn("internal-skills", manifest["files"])
        actual = {path.name for path in (PACK / "skills").iterdir() if path.is_dir()}
        self.assertEqual(actual, PUBLIC_SKILLS)
        self.assertFalse((PACK / "internal-skills").exists())
        runtime = (PACK / "skills/rtl-query/SKILL.md").read_text()
        self.assertIn("rtl query", runtime)
        self.assertIn("scope", runtime)
        self.assertIn("inspect", runtime)
        self.assertIn("bindings", runtime)
        self.assertIn("uses", runtime)
        self.assertNotIn("SQLite", runtime)
        factory = (PACK / "skills/rtl-question-factory/SKILL.md").read_text()
        self.assertIn("Space-driven", factory)
        self.assertIn("Seed-driven", factory)
        self.assertIn("不依赖独立模型 CLI", factory)
        build1 = (PACK / "skills/rtl-knowledge-build/SKILL.md").read_text()
        for contract in (
            "knowledge-build",
            "READY",
            "PARTIAL",
            "BLOCKED",
            "whole-design",
            "actual-root",
            "不要读取或修改 SQLite",
        ):
            self.assertIn(contract, build1)
        self.assertNotIn("OpenC910", build1)

        build2 = (PACK / "skills/rtl-build2-draft/SKILL.md").read_text()
        self.assertIn("references/bounded-reading.md#query-原件先落盘再读所需字段", build2)
        build2_reading = (PACK / "skills/rtl-build2-draft/references/bounded-reading.md").read_text()
        self.assertIn("--view context", build2_reading)
        self.assertIn("--role structure", build2_reading)
        self.assertIn("DRAFT_UNREVIEWED", build2)
        self.assertIn("scripts/state.py", build2)
        self.assertNotIn("Polly", build2)

        mapping = (PACK / "skills/rtl-knowledge-manual/SKILL.md").read_text()
        for contract in (
            "manual-build",
            "deterministic",
            "read-only",
            "Analysis Scope",
            "Compiler Fact",
            "Source Evidence",
            "NOT_BUILT",
            "STOP",
        ):
            self.assertIn(contract, mapping)
        for forbidden in ("SQLite schema", "Jinja", "OpenC910"):
            self.assertNotIn(forbidden, mapping)

    def test_runtime_query_v14_is_generic_and_bounded(self) -> None:
        runtime = (PACK / "skills/rtl-query/SKILL.md").read_text()
        for contract in (
            'version: "1.14"',
            "SOURCE_FAST_PATH",
            "RUNTIME_QUERY_PATH",
            "MIXED_PATH",
            "Deterministic obligations",
            "Semantic obligations",
            "CLOSED_TOOL",
            "CLOSED_KNOWLEDGE",
            "1..100",
            "一次 primary + 一次有证据的 corrected retry",
            "不得拼接 `parent.child` 猜路径",
            "不要再次打开同一 source 复核",
            "最小但完整",
        ):
            self.assertIn(contract, runtime)

        forbidden = (
            "P03",
            "P06",
            "P07",
            "P11",
            "OpenC910",
            "Ibex",
            "PLIC",
            "LSU",
            "AG",
            "DC",
            "DA",
            "RB",
            "LFB",
        )
        for token in forbidden:
            self.assertIsNone(
                re.search(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", runtime),
                token,
            )

    def test_internal_atoms_are_complete_and_not_public(self) -> None:
        for parent, group, expected in (
            ("rtl-project-model-mvp-build", "build", BUILD_ATOMS),
            ("rtl-project-model-query", "query", QUERY_ATOMS),
        ):
            root = PACK / "skills" / parent / "internal-skills"
            actual = {path.name for path in root.iterdir() if path.is_dir()}
            self.assertEqual(actual, expected)
            for name in expected:
                text = (root / name / "SKILL.md").read_text()
                self.assertIn(f"name: {group}-{name}", text)
                self.assertIn("visibility: internal", text)
        self.assertTrue(PUBLIC_SKILLS.isdisjoint(BUILD_ATOMS | QUERY_ATOMS))

    def test_public_skills_route_atoms_and_keep_logs_out_of_model(self) -> None:
        build = (PACK / "skills/rtl-project-model-mvp-build/SKILL.md").read_text()
        query = (PACK / "skills/rtl-project-model-query/SKILL.md").read_text()
        for atom in BUILD_ATOMS:
            self.assertIn(f"internal-skills/{atom}/SKILL.md", build)
        for atom in QUERY_ATOMS:
            self.assertIn(f"internal-skills/{atom}/SKILL.md", query)
        for text in (build, query):
            self.assertIn("analysis/model/rtl-project-model.sqlite", text)
            self.assertIn("analysis/tasks/<task-id>/", text)
            self.assertIn("request.json", text)
            self.assertIn("executions.jsonl", text)
            self.assertIn("artifacts/", text)
            self.assertIn("report.md", text)
            self.assertIn("运行日志", text)
            self.assertIn("优化反馈", text)
            self.assertNotIn("analysis/cache", text)
            self.assertNotIn("analysis/knowledge", text)
        self.assertIn("rtl model build", build)
        for view in (
            "summary",
            "search",
            "entity",
            "path",
            "fsm",
            "clock-reset",
            "coverage",
            "semantics",
            "evidence",
        ):
            self.assertIn(view, query)
        self.assertIn("templates/workspace/README.md", query)
        self.assertIn("Markdown", query)
        self.assertIn("Mermaid", query)

    def test_mvp_build_has_resumable_overall_and_scope_state(self) -> None:
        root = PACK / "skills/rtl-project-model-mvp-build"
        overall = json.loads((root / "templates/state.json").read_text())
        scope = json.loads((root / "templates/scope-state.json").read_text())
        self.assertEqual(overall["schema"], "rtl-project-model-mvp-build-state/v1")
        self.assertEqual(scope["schema"], "rtl-project-model-mvp-scope-state/v1")
        self.assertEqual(overall["goal"]["mode"], "bounded-mvp")
        self.assertIn("identity", overall)
        self.assertEqual(overall["identity"]["schema_version"], "rtlpm-v2")
        self.assertIn("progress", overall)
        self.assertIn("current_checkpoint", overall)
        self.assertIn("next_action", overall)
        self.assertIn("producer_status", scope)
        self.assertIn("next_action", scope)
        skill = (root / "SKILL.md").read_text()
        self.assertIn("state.json", skill)
        self.assertIn("scopes/<scope-id>/state.json", skill)
        self.assertIn("stale", skill)
        self.assertIn("只负责有界 MVP", skill)

    def test_task_template_is_log_only(self) -> None:
        task = PACK / "templates/task"
        self.assertEqual(
            {path.name for path in task.iterdir() if path.is_file()},
            {"request.json", "executions.jsonl", "report.md"},
        )
        request = json.loads((task / "request.json").read_text())
        self.assertEqual(request["schema"], "rtl-project-model-task/v1")
        self.assertEqual(request["model"], "analysis/model/rtl-project-model.sqlite")
        report = (task / "report.md").read_text()
        self.assertIn("运行日志摘要", report)
        self.assertIn("不是知识正本", report)
        self.assertIn("优化反馈", report)


if __name__ == "__main__":
    unittest.main()
