"""Public v2 region contract with disposable, nonsemantic Build1 map fixtures."""

from __future__ import annotations

import copy
import contextlib
import importlib.util
import io
import json
import re
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests import test_build2_draft_state as draft


class RegionStateTests(unittest.TestCase):
    # Reuse the real public CLI fixture without inheriting its v1 test methods.
    setUp = draft.DraftStateTests.setUp
    call = draft.DraftStateTests.call

    def ref(self, path: Path) -> dict:
        return {"base": "project", "path": path.relative_to(self.project).as_posix(), "sha256": draft.sha(path)}

    def map_files(self) -> dict[str, dict]:
        root = self.project / "analysis/manual/build1"
        root.mkdir(parents=True)
        details = {}
        for name in ("a", "b"):
            path = root / f"{name}.json"
            path.write_text(json.dumps({
                "schema": "rtl-analysis-agent/manual-v6-structured-detail-v1",
                "page_kind": "ACTUAL_INSTANCE", "scope_id": "scope:test",
                "object": {"entity_id": f"actual:{name}"}, "semantic": "NOT_BUILT",
                "parent": None, "children": [], "effective_parameters": [],
                "parameters_truncated": False,
            }))
            details[name] = path
        receipt = json.loads((self.task / "build1-status.json").read_text())
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({
            "schema": "rtl-analysis-agent/manual-v6-build1-manifest-v1",
            "scope_id": receipt["scope_id"],
            "build_sha256": receipt["build_identity"]["build_sha256"],
            "candidate_sha256": receipt["candidate"]["sha256"],
            "structured_detail_map": {f"actual:{name}": f"{name}.json" for name in details},
        }))
        return {"manifest": manifest, **details}

    def plan(self, files: dict[str, Path]) -> dict:
        def block(name: str, covers: list[str], depends: list[str] | None = None) -> dict:
            return {
                "id": name, "goal": f"bounded {name} question", "object": f"actual:{name[0]}",
                "configuration": "fixture configuration", "boundary": f"{name} only",
                "covers": covers, "depends_on": depends or [],
                "map_anchor": {"manifest": self.ref(files["manifest"]),
                               "detail": self.ref(files[name[0]])},
            }
        return {
            "schema": "rtl-analysis-agent/build2-plan-v2", "maturity": "DRAFT_UNREVIEWED",
            "target": "bounded fixture region", "object": "actual:a and adjacent actual:b",
            "configuration": "fixture configuration", "boundary": "two exact objects only",
            "uncovered": [],
            "targets": [{"id": "a-entry", "question": "entry in A"},
                        {"id": "a-exit", "question": "exit in A"},
                        {"id": "b-link", "question": "link to B"}],
            "blocks": [block("a-main", ["a-entry", "a-exit"])],
        }

    def start_v2(self, plan: dict | None = None, files: dict[str, Path] | None = None,
                 *, ok: bool = True, task_path: str = "region") -> dict:
        files = files or self.map_files()
        plan = plan or self.plan(files)
        original = self.task / "build1-status.json"
        self.task = self.project / "analysis/tasks" / task_path
        self.task.mkdir(parents=True, exist_ok=True)
        (self.task / "build1-status.json").write_bytes(original.read_bytes())
        source = self.task / "input-plan.json"
        source.write_text(json.dumps(plan))
        return self.call("start", "--project", str(self.project),
                         "--receipt", str(self.task / "build1-status.json"),
                         "--plan", str(source), ok=ok)

    def stage(self, block: str, name: str, *, checks: dict | None = None,
              ok: bool = True, project_ref: bool = False) -> dict:
        if name == "evidence":
            references = [self.ref(self.candidate)]
        else:
            prior = "evidence" if name == "draft" else "draft"
            path = self.task / f"blocks/{block}/{prior}.json"
            references = [{"base": "task", "path": f"blocks/{block}/{prior}.json", "sha256": draft.sha(path)}]
        if project_ref:
            references.append(self.ref(self.candidate))
        payload = {"stage": name, "maturity": "DRAFT_UNREVIEWED", "references": references,
                   "unknowns": [], "next_action": "continue bounded fixture",
                   {"evidence": "observations", "draft": "claims", "review": "checks"}[name]: ["fixture only"]}
        if name == "review":
            payload["target_checks"] = checks if checks is not None else {
                target: {"status": "self_checked", "note": "fixture citation checked"}
                for target in self.block(block)["covers"]}
        source = self.task / f"input-{block}-{name}.json"
        source.write_text(json.dumps(payload))
        return self.call("save", "--block", block, "--stage", name, "--input", str(source), ok=ok)

    def block(self, name: str) -> dict:
        state = json.loads((self.task / "skill.state").read_text())
        plan_path = self.task / state.get("plan_path", "plan.json")
        return next(item for item in json.loads(plan_path.read_text())["blocks"] if item["id"] == name)

    def complete(self, block: str, checks: dict | None = None) -> None:
        for stage in ("evidence", "draft", "review"):
            self.stage(block, stage, checks=checks)

    def extend(self, plan: dict, *, reason: str = "next bounded region", ok: bool = True) -> dict:
        source = self.task / "complete-new-plan.json"
        source.write_text(json.dumps(plan))
        return self.call("extend-plan", "--plan", str(source), "--reason", reason, ok=ok)

    def current_plan(self) -> dict:
        state = json.loads((self.task / "skill.state").read_text())
        return json.loads((self.task / state.get("plan_path", "plan.json")).read_text())

    def expanded(self, files: dict[str, Path]) -> dict:
        plan = self.current_plan()
        plan["targets"].append({"id": "a-priority", "question": "priority in A"})
        plan["blocks"].append({
            "id": "a-priority-work", "goal": "reuse A facts for priority", "object": "actual:a",
            "configuration": "fixture configuration", "boundary": "priority only",
            "covers": ["a-priority"], "depends_on": [],
            "map_anchor": {"manifest": self.ref(files["manifest"]), "detail": self.ref(files["a"])},
        })
        plan["blocks"].append({
            "id": "b-relation", "goal": "boundary relation", "object": "actual:b",
            "configuration": "fixture configuration", "boundary": "B link only",
            "covers": ["b-link"], "depends_on": ["a-main"],
            "map_anchor": {"manifest": self.ref(files["manifest"]), "detail": self.ref(files["b"])},
        })
        return plan

    def test_null_revision_snapshot_start_save_resume_and_identity_rejection(self) -> None:
        # Model the public Build1 receipt normalization for a template snapshot.
        shutil.rmtree(self.source / ".git")
        config = json.loads((self.project / "rtl-project.json").read_text())
        config["revision"] = None
        (self.project / "rtl-project.json").write_text(json.dumps(config))
        receipt = json.loads((self.task / "build1-status.json").read_text())
        receipt["build_identity"]["source_revision"] = "unversioned"
        (self.task / "build1-status.json").write_text(json.dumps(receipt))
        self.start_v2(task_path="snapshot/nested")
        self.stage("a-main", "evidence")
        before = draft.sha(self.task / "skill.state")
        self.assertEqual(self.call("resume")["status"], "ready")
        self.assertEqual(draft.sha(self.task / "skill.state"), before)
        state = json.loads((self.task / "skill.state").read_text())
        self.assertEqual(state["identity"]["source"]["revision"], "unversioned")
        self.assertEqual(state["identity"]["source"]["mode"], "snapshot-sha256")
        original_project, relative = self.project, self.task.relative_to(self.project)
        for name, target in (("source", "source/unit.sv"), ("config", "runtime-query-build.json")):
            with self.subTest(identity=name):
                changed = Path(self.temp.name) / name
                shutil.copytree(original_project, changed)
                path = changed / target
                path.write_bytes(path.read_bytes() + b" ")
                self.project, self.task = changed, changed / relative
                self.assertEqual(self.call("resume")["status"], "stale")
                self.assertIn("identity", self.stage("a-main", "draft", ok=False)["reason"])
                self.assertEqual(draft.sha(self.task / "skill.state"), before)

    def test_v2_map_anchor_and_pending_targets_are_public(self) -> None:
        result = self.start_v2()
        self.assertEqual(result["targets"]["b-link"], {"status": "pending", "owners": []})
        self.assertEqual(result["targets"]["a-entry"]["status"], "open")
        self.assertEqual(result["completion"], "open")

    def test_v2_map_and_stage_survive_project_move(self) -> None:
        self.start_v2()
        self.stage("a-main", "evidence")
        stage_sha = draft.sha(self.task / "blocks/a-main/evidence.json")
        moved = Path(self.temp.name) / "new parent" / "ibex copy"
        moved.parent.mkdir()
        shutil.move(str(self.project), str(moved))
        self.project = moved
        self.source = moved / "source"
        self.candidate = moved / "analysis/model/build1.sqlite"
        self.task = moved / "analysis/tasks/region"
        result = self.call("resume")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["blocks"]["a-main"]["valid_stages"], ["evidence"])
        self.assertEqual(draft.sha(self.task / "blocks/a-main/evidence.json"), stage_sha)
        self.stage("a-main", "draft")

    def test_v2_nested_paths_move_and_keep_identity_guards(self) -> None:
        for relative in ("direct", "group/trial", "group/analysis/tasks/trial"):
            with self.subTest(task=relative):
                fixture = RegionStateTests()
                fixture.setUp()
                try:
                    fixture.start_v2(task_path=relative)
                    fixture.stage("a-main", "evidence")
                    old = fixture.project
                    moved = Path(fixture.temp.name) / "other parent" / "project B"
                    moved.parent.mkdir()
                    task_relative = fixture.task.relative_to(old)
                    state_sha = draft.sha(fixture.task / "skill.state")
                    stage_sha = draft.sha(fixture.task / "blocks/a-main/evidence.json")
                    shutil.move(str(old), str(moved))
                    self.assertFalse(old.exists())
                    fixture.project = moved
                    fixture.task = moved / task_relative
                    fixture.candidate = moved / "analysis/model/build1.sqlite"
                    self.assertEqual(fixture.call("resume")["status"], "ready")
                    self.assertEqual(draft.sha(fixture.task / "skill.state"), state_sha)
                    fixture.stage("a-main", "draft", project_ref=True)
                    self.assertEqual(draft.sha(fixture.task / "blocks/a-main/evidence.json"), stage_sha)
                    bad = moved.parent / "changed identity"
                    shutil.copytree(moved, bad)
                    fixture.project = bad
                    fixture.task = bad / task_relative
                    fixture.candidate = bad / "analysis/model/build1.sqlite"
                    fixture.candidate.write_bytes(b"changed candidate")
                    before = draft.sha(fixture.task / "skill.state")
                    self.assertEqual(fixture.call("resume")["status"], "stale")
                    self.assertIn("identity", fixture.stage("a-main", "review", ok=False)["reason"])
                    self.assertEqual(draft.sha(fixture.task / "skill.state"), before)
                finally:
                    fixture.doCleanups()

    def test_v2_outside_task_rejected_before_plan_or_state(self) -> None:
        files = self.map_files()
        plan = self.plan(files)
        receipt = (self.task / "build1-status.json").read_bytes()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        alias = self.project / "analysis/tasks/escape"
        alias.symlink_to(outside, target_is_directory=True)
        for path in (outside, alias, self.project / "analysis/tasks"):
            with self.subTest(task=str(path)):
                self.task = path
                (path / "build1-status.json").write_bytes(receipt)
                source = path / "input-plan.json"
                source.write_text(json.dumps(plan))
                before = sorted(str(p) for p in path.rglob("*"))
                self.call("start", "--project", str(self.project), "--receipt",
                          str(path / "build1-status.json"), "--plan", str(source), ok=False)
                self.assertEqual(sorted(str(p) for p in path.rglob("*")), before)
                self.assertFalse((path / "skill.state").exists())
                self.assertFalse((path / "plans").exists())

    def test_documented_v2_plan_example_starts_with_disposable_public_map(self) -> None:
        files = self.map_files()
        doc = (draft.STAGES_DOC).read_text()
        match = re.search(r"<!-- example:plan-v2 -->\s*```json\s*(.*?)\s*```", doc, re.S)
        self.assertIsNotNone(match)
        example = json.loads(match.group(1))
        replacements = {
            "__EXACT_A__": "actual:a", "__EXACT_B__": "actual:b",
            "__MANIFEST_RELATIVE_TO_PROJECT__": files["manifest"].relative_to(self.project).as_posix(),
            "__DETAIL_A_RELATIVE_TO_PROJECT__": files["a"].relative_to(self.project).as_posix(),
            "__MANIFEST_SHA256__": draft.sha(files["manifest"]),
            "__DETAIL_A_SHA256__": draft.sha(files["a"]),
        }
        text = json.dumps(example)
        for placeholder, value in replacements.items():
            text = text.replace(placeholder, value)
        result = self.start_v2(json.loads(text), files)
        self.assertEqual(result["targets"]["b-link"]["status"], "pending")

    def test_v2_each_covered_target_has_independent_review_status(self) -> None:
        self.start_v2()
        self.stage("a-main", "evidence")
        self.stage("a-main", "draft")
        good = {"a-entry": {"status": "self_checked", "note": "cited fixture"},
                "a-exit": {"status": "open", "note": "source gap"}}
        state_before = draft.sha(self.task / "skill.state")
        for bad in ({"a-entry": good["a-entry"]},
                    {**good, "b-link": good["a-entry"]},
                    {**good, "a-exit": {"status": "pass", "note": "invalid"}},
                    {**good, "a-exit": {"status": "open", "note": ""}}):
            self.stage("a-main", "review", checks=bad, ok=False)
            self.assertEqual(draft.sha(self.task / "skill.state"), state_before)
        result = self.stage("a-main", "review", checks=good)
        self.assertEqual(result["targets"]["a-entry"]["status"], "self_checked")
        self.assertEqual(result["targets"]["a-exit"]["status"], "open")
        self.assertEqual(result["targets"]["b-link"]["status"], "pending")
        self.assertEqual(result["completion"], "open")
        self.assertEqual(self.call("resume")["targets"], result["targets"])

    def test_v2_rejects_wrong_map_identity_object_path_hash_and_semantics(self) -> None:
        for kind in ("candidate", "build", "scope", "detail-scope", "object", "path", "hash",
                     "semantic", "page-kind", "unsafe-path"):
            with self.subTest(kind=kind):
                self.setUp()
                files = self.map_files()
                plan = self.plan(files)
                if kind in ("candidate", "build", "scope"):
                    manifest = json.loads(files["manifest"].read_text())
                    manifest[{"candidate": "candidate_sha256", "build": "build_sha256",
                              "scope": "scope_id"}[kind]] = "wrong"
                    files["manifest"].write_text(json.dumps(manifest))
                    plan = self.plan(files)
                elif kind in ("detail-scope", "object", "semantic", "page-kind"):
                    detail = json.loads(files["a"].read_text())
                    if kind == "object":
                        detail["object"]["entity_id"] = "actual:wrong"
                    elif kind == "semantic":
                        detail["semantic"] = "BUILT"
                    elif kind == "detail-scope":
                        detail["scope_id"] = "scope:wrong"
                    else:
                        detail["page_kind"] = "SOURCE_FILE"
                    files["a"].write_text(json.dumps(detail))
                    plan = self.plan(files)
                elif kind == "path":
                    manifest = json.loads(files["manifest"].read_text())
                    manifest["structured_detail_map"]["actual:a"] = "b.json"
                    files["manifest"].write_text(json.dumps(manifest))
                    plan = self.plan(files)
                elif kind == "hash":
                    plan["blocks"][0]["map_anchor"]["detail"]["sha256"] = "0" * 64
                else:
                    plan["blocks"][0]["map_anchor"]["detail"]["path"] = "../outside.json"
                error = self.start_v2(plan, files, ok=False)
                self.assertNotIn("schema", error["reason"])
                self.assertFalse((self.task / "skill.state").exists())

    def test_extend_adds_same_object_knowledge_and_adjacent_region_without_rewriting_a(self) -> None:
        files = self.map_files()
        self.start_v2(files=files)
        self.complete("a-main")
        before = {stage: draft.sha(self.task / f"blocks/a-main/{stage}.json")
                  for stage in ("evidence", "draft", "review")}
        original_state = json.loads((self.task / "skill.state").read_text())
        old_plan_path = self.task / original_state.get("plan_path", "plan.json")
        old_plan = old_plan_path.read_bytes()
        result = self.extend(self.expanded(files))
        state = json.loads((self.task / "skill.state").read_text())
        self.assertEqual(state["plan_sha256"], draft.sha(self.task / state["plan_path"]))
        self.assertTrue(state["plan_path"].startswith("plans/"))
        self.assertEqual(old_plan_path.read_bytes(), old_plan)
        self.assertEqual(result["targets"]["a-entry"]["status"], "self_checked")
        self.assertEqual(result["targets"]["a-priority"], {"status": "open", "owners": ["a-priority-work"]})
        self.assertEqual(result["targets"]["b-link"], {"status": "open", "owners": ["b-relation"]})
        self.assertEqual(result["plan"]["not_started_blocks"], ["a-priority-work", "b-relation"])
        self.assertEqual({stage: draft.sha(self.task / f"blocks/a-main/{stage}.json") for stage in before}, before)
        self.assertTrue(state["plan_revisions"])
        self.assertIn("next bounded region", json.dumps(state["plan_revisions"]))
        self.complete("a-priority-work")
        self.complete("b-relation")
        resumed = self.call("resume")
        self.assertEqual(resumed["targets"]["b-link"]["status"], "self_checked")
        self.assertEqual({stage: draft.sha(self.task / f"blocks/a-main/{stage}.json") for stage in before}, before)

    def test_two_direct_owners_require_both_reviews_to_self_check_one_target(self) -> None:
        files = self.map_files()
        self.start_v2(files=files)
        self.complete("a-main")
        new = self.current_plan()
        second = copy.deepcopy(new["blocks"][0])
        second["id"] = "a-second"
        second["covers"] = ["a-entry"]
        new["blocks"].append(second)
        added = self.extend(new)
        self.assertEqual(added["targets"]["a-entry"],
                         {"status": "open", "owners": ["a-main", "a-second"]})
        self.complete("a-second", {"a-entry": {"status": "open", "note": "missing independent source"}})
        self.assertEqual(self.call("resume")["targets"]["a-entry"]["status"], "open")

    def test_resume_rejects_review_with_forged_per_target_status(self) -> None:
        self.start_v2()
        self.complete("a-main")
        review = self.task / "blocks/a-main/review.json"
        payload = json.loads(review.read_text())
        payload["target_checks"]["a-exit"]["status"] = "published"
        review.write_text(json.dumps(payload))
        state_path = self.task / "skill.state"
        state = json.loads(state_path.read_text())
        state["blocks"]["a-main"]["stages"]["review"]["sha256"] = draft.sha(review)
        state_path.write_text(json.dumps(state))
        result = self.call("resume")
        self.assertEqual(result["blocks"]["a-main"]["valid_stages"], ["evidence", "draft"])
        self.assertEqual(result["targets"]["a-exit"]["status"], "open")
        self.assertEqual(result["completion"], "open")

    def test_extend_rejects_old_edits_identity_and_bad_dependencies_without_state_change(self) -> None:
        files = self.map_files()
        self.start_v2(files=files)
        self.complete("a-main")
        valid = self.expanded(files)
        before = (self.task / "skill.state").read_bytes()
        mutations = []
        old_target = copy.deepcopy(valid)
        old_target["targets"][0]["question"] = "rewritten old question"
        mutations.append(old_target)
        old_block = copy.deepcopy(valid)
        old_block["blocks"][0]["covers"] = ["a-entry"]
        mutations.append(old_block)
        identity = copy.deepcopy(valid)
        identity["configuration"] = "changed configuration"
        mutations.append(identity)
        cycle = copy.deepcopy(valid)
        cycle["blocks"][-2]["depends_on"] = ["b-relation"]
        cycle["blocks"][-1]["depends_on"] = ["a-priority-work"]
        mutations.append(cycle)
        for changed in mutations:
            self.extend(changed, ok=False)
            self.assertEqual((self.task / "skill.state").read_bytes(), before)
        no_addition = self.current_plan()
        self.extend(no_addition, ok=False)
        self.assertEqual((self.task / "skill.state").read_bytes(), before)
        self.extend(valid, reason="", ok=False)
        self.assertEqual((self.task / "skill.state").read_bytes(), before)

    def test_extend_cannot_remove_out_of_scope_item_or_continue_after_source_drift(self) -> None:
        files = self.map_files()
        initial = self.plan(files)
        initial["uncovered"] = ["separate subsystem outside this Scope"]
        self.start_v2(initial, files)
        expanded = self.expanded(files)
        expanded["uncovered"] = []
        before = (self.task / "skill.state").read_bytes()
        self.extend(expanded, ok=False)
        self.assertEqual((self.task / "skill.state").read_bytes(), before)
        expanded["uncovered"] = initial["uncovered"]
        (self.source / "unit.sv").write_text("module unit; wire changed; endmodule\n")
        self.extend(expanded, ok=False)
        self.assertEqual((self.task / "skill.state").read_bytes(), before)

    def test_interrupt_before_state_commit_keeps_old_plan_recoverable(self) -> None:
        files = self.map_files()
        self.start_v2(files=files)
        self.complete("a-main")
        expanded = self.expanded(files)
        source = self.task / "complete-new-plan.json"
        source.write_text(json.dumps(expanded))
        before = (self.task / "skill.state").read_bytes()
        spec = importlib.util.spec_from_file_location("isolated_region_state", draft.SCRIPT)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        original = module.atomic_json

        def fail_state(path: Path, payload: dict) -> None:
            if path.name == "skill.state":
                raise OSError("injected commit interruption")
            original(path, payload)

        argv = [str(draft.SCRIPT), "extend-plan", "--task", str(self.task),
                "--plan", str(source), "--reason", "interrupt fixture"]
        with mock.patch.object(module, "atomic_json", side_effect=fail_state), \
                mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(module.main(), 2)
        self.assertEqual((self.task / "skill.state").read_bytes(), before)
        self.assertEqual(self.call("resume")["targets"]["b-link"]["status"], "pending")
        self.assertTrue(list((self.task / "plans").glob("*.json")))

    def test_extend_stales_old_combination_and_preserves_its_bytes(self) -> None:
        files = self.map_files()
        initial = self.plan(files)
        initial["targets"].pop()  # A-only first cycle has no pending B.
        self.start_v2(initial, files)
        self.complete("a-main")
        review_sha = draft.sha(self.task / "blocks/a-main/review.json")
        combination = {
            "schema": "rtl-analysis-agent/build2-combination-v1", "maturity": "DRAFT_UNREVIEWED",
            "blocks": {"a-main": review_sha}, "coverage": {"a-entry": ["a-main"], "a-exit": ["a-main"]},
            "checks": {key: {"status": "pass", "note": "fixture check"} for key in
                       ("object_configuration", "interface", "priority_defaults", "timing",
                        "duplicate_conflict", "coverage_unknowns")},
            "conflicts": [], "uncovered": [], "unknowns": [],
            "references": [{"base": "task", "path": "blocks/a-main/review.json", "sha256": review_sha}],
            "next_action": "await independent review",
        }
        source = self.task / "input-combination.json"
        source.write_text(json.dumps(combination))
        self.assertEqual(self.call("save-combination", "--input", str(source))["completion"], "draft_self_checked")
        old_bytes = (self.task / "combination.json").read_bytes()
        extended = self.current_plan()
        extended["targets"].append({"id": "b-link", "question": "link to B"})
        extended["blocks"].append({
            "id": "b-relation", "goal": "boundary relation", "object": "actual:b",
            "configuration": "fixture configuration", "boundary": "B link only",
            "covers": ["b-link"], "depends_on": ["a-main"],
            "map_anchor": {"manifest": self.ref(files["manifest"]), "detail": self.ref(files["b"])},
        })
        result = self.extend(extended)
        self.assertEqual(result["completion"], "open")
        self.assertEqual(result["combination"]["status"], "stale")
        self.assertEqual((self.task / "combination.json").read_bytes(), old_bytes)

    def test_v2_map_drift_is_local_except_shared_manifest(self) -> None:
        files = self.map_files()
        self.start_v2(files=files)
        self.complete("a-main")
        self.extend(self.expanded(files))
        self.complete("a-priority-work")
        self.complete("b-relation")
        files["b"].write_text(files["b"].read_text() + "\n")
        drift = self.call("resume")
        self.assertEqual(drift["blocks"]["a-main"]["valid_stages"], ["evidence", "draft", "review"])
        self.assertEqual(drift["blocks"]["a-priority-work"]["valid_stages"], ["evidence", "draft", "review"])
        self.assertEqual(drift["blocks"]["b-relation"]["valid_stages"], [])
        self.assertEqual(drift["completion"], "open")
        files["manifest"].write_text(files["manifest"].read_text() + "\n")
        shared = self.call("resume")
        self.assertEqual(shared["blocks"]["a-main"]["valid_stages"], [])
        self.assertEqual(shared["blocks"]["a-priority-work"]["valid_stages"], [])

    def test_v2_upstream_revision_stales_relation_but_not_independent_a_work(self) -> None:
        files = self.map_files()
        self.start_v2(files=files)
        self.complete("a-main")
        self.extend(self.expanded(files))
        self.complete("a-priority-work")
        self.complete("b-relation")
        old = (self.task / "blocks/a-main/review.json").read_bytes()
        payload = json.loads(old)
        payload["checks"] = ["revised fixture check"]
        source = self.task / "changed-review.json"
        source.write_text(json.dumps(payload))
        self.call("revise", "--block", "a-main", "--stage", "review", "--input", str(source),
                  "--reason", "corrected upstream check")
        report = self.call("resume")
        self.assertEqual(report["blocks"]["a-priority-work"]["valid_stages"], ["evidence", "draft", "review"])
        self.assertEqual(report["blocks"]["b-relation"]["valid_stages"], [])
        archive = self.task / f"blocks/a-main/history/review-{draft.sha_bytes(old)}.json"
        self.assertEqual(archive.read_bytes(), old)

    def test_unstarted_extended_relation_binds_latest_upstream_on_first_evidence(self) -> None:
        files = self.map_files()
        self.start_v2(files=files)
        self.complete("a-main")
        self.extend(self.expanded(files))
        self.complete("a-priority-work")
        independent_sha = draft.sha(self.task / "blocks/a-priority-work/review.json")
        old_review = (self.task / "blocks/a-main/review.json").read_bytes()
        payload = json.loads(old_review)
        payload["checks"] = ["corrected upstream fixture check"]
        source = self.task / "revised-a-review.json"
        source.write_text(json.dumps(payload))
        self.call("revise", "--block", "a-main", "--stage", "review", "--input", str(source),
                  "--reason", "corrected upstream fixture")
        latest_sha = draft.sha(self.task / "blocks/a-main/review.json")
        self.assertNotEqual(latest_sha, draft.sha_bytes(old_review))
        self.assertEqual(draft.sha(self.task / "blocks/a-priority-work/review.json"), independent_sha)
        self.assertEqual((self.task / f"blocks/a-main/history/review-{draft.sha_bytes(old_review)}.json").read_bytes(),
                         old_review)
        self.assertFalse(json.loads((self.task / "skill.state").read_text())["blocks"]["b-relation"]["stages"])
        unused = self.task / "never-saved-b-evidence.json"
        unused.write_text(json.dumps({"stage": "evidence", "maturity": "DRAFT_UNREVIEWED",
                                      "references": [self.ref(self.candidate)], "unknowns": [],
                                      "next_action": "continue", "observations": ["fixture only"]}))
        rejected = self.call("revise", "--block", "b-relation", "--stage", "evidence",
                             "--input", str(unused), "--reason", "no old evidence", ok=False)
        self.assertIn("revise requires a recorded stage", rejected["reason"])
        result = self.stage("b-relation", "evidence")
        self.assertEqual(result["blocks"]["b-relation"]["valid_stages"], ["evidence"])
        state = json.loads((self.task / "skill.state").read_text())
        self.assertEqual(state["blocks"]["b-relation"]["dependencies"], {"a-main": latest_sha})
        self.stage("b-relation", "draft")
        self.stage("b-relation", "review")

    def test_unstarted_relation_waits_for_upstream_then_starts_without_plan_order_assumption(self) -> None:
        files = self.map_files()
        self.start_v2(files=files)
        expanded = self.expanded(files)
        relation = expanded["blocks"].pop()
        relation["depends_on"] = ["a-priority-work"]
        expanded["blocks"].insert(1, relation)  # Dependent precedes its new upstream in the plan.
        self.extend(expanded)
        waiting = self.stage("b-relation", "evidence", ok=False)
        self.assertIn("dependency not ready", waiting["reason"])
        self.complete("a-main")
        self.complete("a-priority-work")
        result = self.stage("b-relation", "evidence")
        self.assertEqual(result["blocks"]["b-relation"]["valid_stages"], ["evidence"])
        state = json.loads((self.task / "skill.state").read_text())
        self.assertEqual(state["blocks"]["b-relation"]["dependencies"], {
            "a-priority-work": draft.sha(self.task / "blocks/a-priority-work/review.json")})

    def test_unstarted_relation_rejects_stale_upstream_until_it_is_repaired(self) -> None:
        files = self.map_files()
        self.start_v2(files=files)
        self.complete("a-main")
        self.extend(self.expanded(files))
        review = self.task / "blocks/a-main/review.json"
        original = json.loads(review.read_text())
        review.write_text("{}")
        state_before = (self.task / "skill.state").read_bytes()
        refused = self.stage("b-relation", "evidence", ok=False)
        self.assertIn("dependency stale", refused["reason"])
        self.assertEqual((self.task / "skill.state").read_bytes(), state_before)
        repair = self.task / "repair-a-review.json"
        original["checks"] = ["rechecked after damaged artifact"]
        repair.write_text(json.dumps(original))
        self.call("revise", "--block", "a-main", "--stage", "review", "--input", str(repair),
                  "--reason", "restore upstream review from checked fixture")
        result = self.stage("b-relation", "evidence")
        self.assertEqual(result["blocks"]["b-relation"]["valid_stages"], ["evidence"])

    def test_started_relation_still_requires_revision_after_upstream_change(self) -> None:
        files = self.map_files()
        self.start_v2(files=files)
        self.complete("a-main")
        self.extend(self.expanded(files))
        self.stage("b-relation", "evidence")
        old_evidence = (self.task / "blocks/b-relation/evidence.json").read_bytes()
        old_binding = json.loads((self.task / "skill.state").read_text())["blocks"]["b-relation"]["dependencies"]
        review = self.task / "blocks/a-main/review.json"
        changed = json.loads(review.read_text())
        changed["checks"] = ["corrected upstream fixture"]
        source = self.task / "revised-a-review.json"
        source.write_text(json.dumps(changed))
        self.call("revise", "--block", "a-main", "--stage", "review", "--input", str(source),
                  "--reason", "upstream correction")
        self.assertEqual(json.loads((self.task / "skill.state").read_text())["blocks"]["b-relation"]["dependencies"],
                         old_binding)
        self.assertIn("dependency stale", self.stage("b-relation", "draft", ok=False)["reason"])
        evidence_input = self.task / "input-b-relation-evidence.json"
        self.call("revise", "--block", "b-relation", "--stage", "evidence", "--input", str(evidence_input),
                  "--reason", "rebind corrected upstream")
        state = json.loads((self.task / "skill.state").read_text())
        self.assertEqual(state["blocks"]["b-relation"]["dependencies"], {
            "a-main": draft.sha(review)})
        self.assertEqual((self.task / f"blocks/b-relation/history/evidence-{draft.sha_bytes(old_evidence)}.json").read_bytes(),
                         old_evidence)


if __name__ == "__main__":
    unittest.main()
