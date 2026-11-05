"""Task-local Build2 draft checkpoints, including stale and interrupted recovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "src/rtl-pack/skills/rtl-build2-draft/scripts/state.py"
STAGES_DOC = SCRIPT.parents[1] / "references/stages.md"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class DraftStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.source = self.project / "source"
        self.source.mkdir(parents=True)
        (self.source / "unit.sv").write_text("module unit; endmodule\n")
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        subprocess.run(["git", "-C", str(self.source), "add", "unit.sv"], check=True)
        env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
        subprocess.run(["git", "-C", str(self.source), "commit", "-qm", "fixture"], check=True, env=env)
        revision = subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip()
        (self.project / "rtl-project.json").write_text(json.dumps({"source": "source", "revision": revision}))
        (self.project / "runtime-query-build.json").write_text('{"top":"unit"}\n')
        self.candidate = self.project / "analysis/model/build1.sqlite"
        self.candidate.parent.mkdir(parents=True)
        self.candidate.write_bytes(b"fixed candidate fixture")
        self.task = self.project / "analysis/tasks/trial"
        self.task.mkdir(parents=True)
        receipt = {
            "schema": "rtl-analysis-agent/build1-status-v1", "status": "READY",
            "candidate": {"status": "READY", "reused": True,
                          "path": "analysis/model/build1.sqlite", "sha256": sha(self.candidate)},
            "heavy_actions": {"compile_build_count": 0, "render_count": 0},
            "build_identity": {"source_revision": revision, "build_sha256": "b" * 64,
                               "build_spec_sha256": hashlib.sha256(json.dumps({"top": "unit"}, sort_keys=True,
                                   separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
                               "source_closure_sha256": "c" * 64},
            "scope_id": "scope:test",
        }
        (self.task / "build1-status.json").write_text(json.dumps(receipt))
        self.call("start", "--project", str(self.project), "--receipt", str(self.task / "build1-status.json"),
                  "--target", "one bounded question", "--block", "first", "--budget-turns", "20")

    def call(self, command: str, *args: str, ok: bool = True) -> dict:
        result = subprocess.run([sys.executable, str(SCRIPT), command, "--task", str(self.task), *args],
                                text=True, capture_output=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return json.loads(result.stderr)

    def save(self, block: str, stage: str, references: list[dict] | None = None) -> dict:
        if references is None:
            if stage == "evidence":
                references = [{"base": "project", "path": "analysis/model/build1.sqlite", "sha256": sha(self.candidate)}]
            else:
                prior = "evidence" if stage == "draft" else "draft"
                path = self.task / f"blocks/{block}/{prior}.json"
                references = [{"base": "task", "path": f"blocks/{block}/{prior}.json", "sha256": sha(path)}]
        payload = {"stage": stage, "maturity": "DRAFT_UNREVIEWED", "references": references,
                   "unknowns": ["remaining boundary"], "next_action": "continue bounded work",
                   {"evidence": "observations", "draft": "claims", "review": "checks"}[stage]: ["bounded item"]}
        source = self.task / f"input-{block}-{stage}.json"
        source.write_text(json.dumps(payload))
        return self.call("save", "--block", block, "--stage", stage, "--input", str(source))

    def complete(self, block: str = "first") -> None:
        for stage in ("evidence", "draft", "review"):
            self.save(block, stage)

    def start_plan(self, include_independent: bool = False, *, task_path: str = "planned") -> dict:
        planned = self.project / "analysis/tasks" / task_path
        planned.mkdir(parents=True)
        (planned / "build1-status.json").write_bytes((self.task / "build1-status.json").read_bytes())
        plan = {
            "schema": "rtl-analysis-agent/build2-plan-v1", "maturity": "DRAFT_UNREVIEWED",
            "target": "bounded behavior", "object": "unit", "configuration": "fixed test configuration",
            "boundary": "unit only", "uncovered": [],
            "targets": [{"id": "entry", "question": "entry behavior"},
                        {"id": "exit", "question": "exit behavior"},
                        {"id": "clear", "question": "clear behavior"}],
            "blocks": [
                {"id": "first", "goal": "entry behavior", "object": "unit", "configuration": "fixed",
                 "boundary": "entry path", "covers": ["entry"], "depends_on": []},
                {"id": "alpha", "goal": "exit behavior", "object": "unit", "configuration": "fixed",
                 "boundary": "exit path", "covers": ["exit"], "depends_on": ["first"]},
                {"id": "beta", "goal": "clear behavior", "object": "unit", "configuration": "fixed",
                 "boundary": "clear path", "covers": ["clear"], "depends_on": ["alpha"]},
            ],
        }
        if include_independent:
            plan["targets"].append({"id": "independent", "question": "unrelated behavior"})
            plan["blocks"].append({"id": "independent", "goal": "unrelated behavior", "object": "unit",
                                   "configuration": "fixed", "boundary": "unrelated path",
                                   "covers": ["independent"], "depends_on": []})
        source = planned / "input-plan.json"
        source.write_text(json.dumps(plan))
        self.task = planned
        return self.call("start", "--project", str(self.project),
                         "--receipt", str(planned / "build1-status.json"), "--plan", str(source),
                         "--budget-turns", "20")

    def save_clean(self, block: str, stage: str, note: str = "bounded item") -> dict:
        prior = "evidence" if stage == "draft" else "draft"
        references = ([{"base": "project", "path": "analysis/model/build1.sqlite",
                        "sha256": sha(self.candidate)}] if stage == "evidence" else
                      [{"base": "task", "path": f"blocks/{block}/{prior}.json",
                        "sha256": sha(self.task / f"blocks/{block}/{prior}.json")}])
        payload = {"stage": stage, "maturity": "DRAFT_UNREVIEWED", "references": references,
                   "unknowns": [], "next_action": "continue bounded work",
                   {"evidence": "observations", "draft": "claims", "review": "checks"}[stage]: [note]}
        source = self.task / f"input-{block}-{stage}.json"
        source.write_text(json.dumps(payload))
        return self.call("save", "--block", block, "--stage", stage, "--input", str(source))

    def complete_clean(self, block: str) -> None:
        for stage in ("evidence", "draft", "review"):
            self.save_clean(block, stage)

    def combination_payload(self) -> dict:
        state = json.loads((self.task / "skill.state").read_text())
        reviews = {name: block["stages"]["review"]["sha256"] for name, block in state["blocks"].items()}
        return {"schema": "rtl-analysis-agent/build2-combination-v1", "maturity": "DRAFT_UNREVIEWED",
                "blocks": reviews, "coverage": {"entry": ["first"], "exit": ["alpha"], "clear": ["beta"]},
                "checks": {key: {"status": "pass", "note": "checked against cited drafts"} for key in
                           ("object_configuration", "interface", "priority_defaults", "timing",
                            "duplicate_conflict", "coverage_unknowns")},
                "conflicts": [], "uncovered": [], "unknowns": [],
                "references": [{"base": "task", "path": f"blocks/{name}/review.json", "sha256": value}
                               for name, value in reviews.items()],
                "next_action": "await independent review"}

    def save_combination(self, payload: dict) -> dict:
        source = self.task / "input-combination.json"
        source.write_text(json.dumps(payload))
        args = ("--reason", "updated self-check") if (self.task / "combination.json").exists() else ()
        return self.call("save-combination", "--input", str(source), *args)

    def test_plan_keeps_unstarted_and_combination_pending(self) -> None:
        started = self.start_plan()
        self.assertEqual(started["plan"]["not_started_blocks"], ["first", "alpha", "beta"])
        self.assertEqual(len(started["remaining_scope"]), 3)
        self.complete_clean("first")
        partial = self.call("resume")
        self.assertEqual(partial["plan"]["not_started_blocks"], ["alpha", "beta"])
        self.assertEqual(partial["completion"], "open")
        self.assertEqual(len(partial["remaining_scope"]), 3)
        self.complete_clean("alpha")
        self.complete_clean("beta")
        pending = self.call("resume")
        self.assertEqual(pending["combination"]["status"], "missing")
        self.assertEqual(pending["completion"], "open")
        self.assertEqual(len(pending["remaining_scope"]), 3)
        self.assertEqual(self.save_combination(self.combination_payload())["completion"], "draft_self_checked")
        self.assertEqual(json.loads((self.task / "skill.state").read_text())["remaining_scope"], [])

    def test_planned_evidence_checkpoint_waits_for_upstream_and_keeps_scope(self) -> None:
        self.start_plan()
        self.save_clean("first", "evidence")
        before = sha(self.task / "skill.state")
        resumed = self.call("resume")
        self.assertEqual(sha(self.task / "skill.state"), before)
        self.assertEqual(resumed["blocks"]["first"]["valid_stages"], ["evidence"])
        self.assertEqual(resumed["plan"]["not_started_blocks"], ["alpha", "beta"])
        self.assertEqual(len(resumed["remaining_scope"]), 3)
        payload = {"stage": "evidence", "maturity": "DRAFT_UNREVIEWED",
                   "references": [{"base": "project", "path": "analysis/model/build1.sqlite",
                                   "sha256": sha(self.candidate)}],
                   "unknowns": [], "next_action": "draft", "observations": ["premature"]}
        source = self.task / "premature.json"
        source.write_text(json.dumps(payload))
        self.assertIn("dependency not ready", self.call("save", "--block", "alpha", "--stage", "evidence",
                                                         "--input", str(source), ok=False)["reason"])

    def test_saved_stage_recovers_after_project_move_and_rejects_changed_identity(self) -> None:
        self.save("first", "evidence")
        state_sha = sha(self.task / "skill.state")
        stage_sha = sha(self.task / "blocks/first/evidence.json")
        moved = Path(self.temp.name) / "another parent" / "renamed project"
        moved.parent.mkdir()
        shutil.move(str(self.project), str(moved))
        self.project = moved
        self.source = moved / "source"
        self.candidate = moved / "analysis/model/build1.sqlite"
        self.task = moved / "analysis/tasks/trial"
        self.assertFalse((Path(self.temp.name) / "project").exists())
        resumed = self.call("resume")
        self.assertEqual(resumed["status"], "ready")
        self.assertEqual(resumed["blocks"]["first"]["valid_stages"], ["evidence"])
        self.assertEqual(sha(self.task / "skill.state"), state_sha)
        self.assertEqual(sha(self.task / "blocks/first/evidence.json"), stage_sha)
        self.save("first", "draft")
        (moved / "runtime-query-build.json").write_text('{"top":"different"}\n')
        rejected = self.call("resume")
        self.assertEqual(rejected["status"], "stale")
        self.assertTrue(rejected["identity_issues"])
        self.assertIn("identity", self.call("save", "--block", "first", "--stage", "review",
                                            "--input", str(self.task / "input-first-draft.json"),
                                            ok=False)["reason"])

    def test_planned_task_paths_move_and_keep_identity_guards(self) -> None:
        for relative in ("direct", "group/trial", "group/analysis/tasks/trial"):
            with self.subTest(task=relative):
                fixture = DraftStateTests()
                fixture.setUp()
                try:
                    fixture.start_plan(task_path=relative)
                    fixture.save("first", "evidence")
                    old = fixture.project
                    moved = Path(fixture.temp.name) / "other parent" / "project B"
                    moved.parent.mkdir()
                    task_relative = fixture.task.relative_to(old)
                    state_sha = sha(fixture.task / "skill.state")
                    stage_sha = sha(fixture.task / "blocks/first/evidence.json")
                    shutil.move(str(old), str(moved))
                    self.assertFalse(old.exists())
                    fixture.project = moved
                    fixture.task = moved / task_relative
                    fixture.candidate = moved / "analysis/model/build1.sqlite"
                    self.assertEqual(fixture.call("resume")["status"], "ready")
                    self.assertEqual(sha(fixture.task / "skill.state"), state_sha)
                    fixture.save("first", "draft", references=[
                        {"base": "task", "path": "blocks/first/evidence.json", "sha256": stage_sha},
                        {"base": "project", "path": "analysis/model/build1.sqlite", "sha256": sha(fixture.candidate)},
                    ])
                    self.assertEqual(sha(fixture.task / "blocks/first/evidence.json"), stage_sha)
                    bad = moved.parent / "changed identity"
                    shutil.copytree(moved, bad)
                    fixture.project = bad
                    fixture.task = bad / task_relative
                    (bad / "runtime-query-build.json").write_text('{"top":"changed"}\n')
                    before = sha(fixture.task / "skill.state")
                    self.assertEqual(fixture.call("resume")["status"], "stale")
                    self.assertIn("identity", fixture.call("save", "--block", "first", "--stage", "review",
                        "--input", str(fixture.task / "input-first-draft.json"), ok=False)["reason"])
                    self.assertEqual(sha(fixture.task / "skill.state"), before)
                finally:
                    fixture.doCleanups()

    def test_start_path_forms_from_workspace_and_project_cwd(self) -> None:
        # Published guidance: paths resolve from the current working directory, so a
        # Workspace-root run needs full Workspace-relative or absolute paths; bare task
        # names and Project-relative forms only work from the Project root itself.
        workspace = Path(self.temp.name) / "workspace"
        (workspace / "projects").mkdir(parents=True)
        project = workspace / "projects" / "unit-core"
        shutil.move(str(self.project), str(project))
        self.project = project
        self.source = project / "source"
        self.candidate = project / "analysis/model/build1.sqlite"
        plan = {
            "schema": "rtl-analysis-agent/build2-plan-v1", "maturity": "DRAFT_UNREVIEWED",
            "target": "bounded behavior", "object": "unit", "configuration": "fixed test configuration",
            "boundary": "unit only", "uncovered": [],
            "targets": [{"id": "entry", "question": "entry behavior"}],
            "blocks": [{"id": "first", "goal": "entry behavior", "object": "unit",
                        "configuration": "fixed", "boundary": "entry path",
                        "covers": ["entry"], "depends_on": []}],
        }

        def fresh(name: str) -> Path:
            task = project / "analysis/tasks" / name
            task.mkdir(parents=True)
            (task / "build1-status.json").write_bytes(
                (project / "analysis/tasks/trial/build1-status.json").read_bytes())
            (task / "input-plan.json").write_text(json.dumps(plan))
            return task

        def start(cwd: Path, task: str, project_arg: str, receipt: str, plan_arg: str):
            return subprocess.run([sys.executable, str(SCRIPT), "start", "--task", task,
                                   "--project", project_arg, "--receipt", receipt,
                                   "--plan", plan_arg], cwd=cwd, text=True, capture_output=True)

        identities = {}
        for name in ("ws-relative", "absolute"):
            with self.subTest(form=name):
                task = fresh(name)
                if name == "ws-relative":
                    args = (f"projects/unit-core/analysis/tasks/{name}", "projects/unit-core",
                            f"projects/unit-core/analysis/tasks/{name}/build1-status.json",
                            f"projects/unit-core/analysis/tasks/{name}/input-plan.json")
                else:
                    args = (str(task), str(project), str(task / "build1-status.json"),
                            str(task / "input-plan.json"))
                result = start(workspace, *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                identities[name] = json.loads((task / "skill.state").read_text())["identity"]
        self.assertEqual(identities["ws-relative"], identities["absolute"])

        task = fresh("from-project-root")
        result = start(project, "analysis/tasks/from-project-root", ".",
                       "analysis/tasks/from-project-root/build1-status.json",
                       "analysis/tasks/from-project-root/input-plan.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(identities["absolute"],
                         json.loads((task / "skill.state").read_text())["identity"])

        for form, bad_task in (("bare-name", "no-such-task"),
                               ("project-relative", "analysis/tasks/no-such-task")):
            with self.subTest(form=form):
                # Receipt/plan point at a real task; only the --task form is wrong.
                result = start(workspace, bad_task, "projects/unit-core",
                               "projects/unit-core/analysis/tasks/ws-relative/build1-status.json",
                               "projects/unit-core/analysis/tasks/ws-relative/input-plan.json")
                self.assertEqual(result.returncode, 2)
                self.assertIn("must be under Project analysis/tasks", result.stderr)
                self.assertFalse((workspace / "no-such-task").exists())

    def test_outside_task_rejected_before_persistent_writes(self) -> None:
        self.start_plan()
        plan = self.task / "input-plan.json"
        receipt = (self.task / "build1-status.json").read_bytes()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        alias = self.project / "analysis/tasks/escape"
        alias.symlink_to(outside, target_is_directory=True)
        for path in (outside, alias, self.project / "analysis/tasks"):
            with self.subTest(task=str(path)):
                self.task = path
                (path / "build1-status.json").write_bytes(receipt)
                before = sorted(str(p) for p in path.rglob("*"))
                self.call("start", "--project", str(self.project), "--receipt",
                          str(path / "build1-status.json"), "--plan", str(plan), ok=False)
                self.assertEqual(sorted(str(p) for p in path.rglob("*")), before)
                self.assertFalse((path / "skill.state").exists())
                self.assertFalse((path / "plan.json").exists())

    def test_plan_unassigned_target_remains_visible(self) -> None:
        self.start_plan()
        plan_path = self.task / "plan.json"
        plan = json.loads(plan_path.read_text())
        plan["targets"].append({"id": "missing", "question": "still uncovered"})
        # Rebuild a fresh task with the extra question, rather than rewriting a frozen plan.
        fresh = self.project / "analysis/tasks/uncovered-plan"
        fresh.mkdir()
        (fresh / "build1-status.json").write_bytes((self.task / "build1-status.json").read_bytes())
        source = fresh / "input-plan.json"
        source.write_text(json.dumps(plan))
        self.task = fresh
        result = self.call("start", "--project", str(self.project),
                           "--receipt", str(fresh / "build1-status.json"), "--plan", str(source))
        self.assertEqual(result["plan"]["uncovered_targets"], ["missing"])
        self.assertIn("still uncovered", result["remaining_scope"])

    def test_combination_conflicts_and_omissions_stay_open(self) -> None:
        self.start_plan()
        for block in ("first", "alpha", "beta"):
            self.complete_clean(block)
        conflict = self.combination_payload()
        conflict["conflicts"] = ["entry and clear claims disagree"]
        conflict["checks"]["duplicate_conflict"]["status"] = "open"
        result = self.save_combination(conflict)
        self.assertEqual(result["completion"], "open")
        self.assertIn("conflicts remain", result["combination"]["issues"])
        omission = self.combination_payload()
        omission["coverage"]["clear"] = []
        omission["uncovered"] = ["clear behavior"]
        result = self.save_combination(omission)
        self.assertEqual(result["completion"], "open")
        self.assertIn("target uncovered: clear", result["combination"]["issues"])

    def test_revision_invalidates_dependencies_and_combination_without_deleting(self) -> None:
        self.start_plan()
        for block in ("first", "alpha", "beta"):
            self.complete_clean(block)
        self.assertEqual(self.save_combination(self.combination_payload())["completion"], "draft_self_checked")
        old_review = self.task / "blocks/first/review.json"
        old_sha = sha(old_review)
        old_payload = json.loads(old_review.read_text())
        source = self.task / "revision.json"
        old_payload["checks"] = ["corrected bounded item"]
        source.write_text(json.dumps(old_payload))
        changed = self.call("revise", "--block", "first", "--stage", "review",
                            "--input", str(source), "--reason", "corrected source reading")
        self.assertEqual(changed["blocks"]["alpha"]["valid_stages"], [])
        self.assertEqual(changed["blocks"]["beta"]["valid_stages"], [])
        self.assertEqual(changed["combination"]["status"], "stale")
        self.assertTrue((self.task / f"blocks/first/history/review-{old_sha}.json").is_file())
        self.assertEqual(sha(self.task / f"blocks/first/history/review-{old_sha}.json"), old_sha)
        self.assertEqual(changed["completion"], "open")
        self.assertIn("corrected source reading", json.dumps(json.loads((self.task / "skill.state").read_text())))
        for block in ("alpha", "beta"):
            evidence = json.loads((self.task / f"blocks/{block}/evidence.json").read_text())
            evidence["observations"] = [f"rechecked {block} after upstream revision"]
            source.write_text(json.dumps(evidence))
            self.call("revise", "--block", block, "--stage", "evidence", "--input", str(source),
                      "--reason", "rechecked changed upstream")
            self.save_clean(block, "draft")
            self.save_clean(block, "review")
        rebased = self.call("resume")
        self.assertEqual(rebased["blocks"]["beta"]["valid_stages"], ["evidence", "draft", "review"])
        self.assertEqual(rebased["combination"]["status"], "stale")
        self.assertEqual(self.save_combination(self.combination_payload())["completion"], "draft_self_checked")
        self.assertTrue(list((self.task / "history").glob("combination-*.json")))

    def test_revise_evidence_preserves_all_old_stage_bytes_and_records(self) -> None:
        self.start_plan(include_independent=True)
        for block in ("first", "alpha", "beta", "independent"):
            self.complete_clean(block)
        combo = self.combination_payload()
        combo["coverage"]["independent"] = ["independent"]
        self.save_combination(combo)
        old_state = json.loads((self.task / "skill.state").read_text())
        old_bytes = {stage: (self.task / f"blocks/first/{stage}.json").read_bytes() for stage in
                     ("evidence", "draft", "review")}
        old_combo = (self.task / "combination.json").read_bytes()
        payload = json.loads(old_bytes["evidence"])
        payload["observations"] = ["corrected evidence"]
        source = self.task / "revised-evidence.json"
        source.write_text(json.dumps(payload))
        changed = self.call("revise", "--block", "first", "--stage", "evidence", "--input", str(source),
                            "--reason", "corrected evidence")
        revision = json.loads((self.task / "skill.state").read_text())["blocks"]["first"]["revisions"][-1]
        self.assertEqual(set(revision["affected_stages"]), {"evidence", "draft", "review"})
        for stage, original in old_bytes.items():
            old_record = old_state["blocks"]["first"]["stages"][stage]
            entry = revision["affected_stages"][stage]
            self.assertEqual(entry["record"], old_record)
            self.assertEqual((self.task / entry["archive"]).read_bytes(), original)
            self.assertTrue(entry["matches_recorded_sha256"])
        self.assertEqual(revision["old_dependencies"], {})
        self.assertEqual(changed["blocks"]["alpha"]["valid_stages"], [])
        self.assertEqual(changed["blocks"]["beta"]["valid_stages"], [])
        self.assertEqual(changed["blocks"]["independent"]["valid_stages"], ["evidence", "draft", "review"])
        self.assertEqual(changed["combination"]["status"], "stale")
        self.save_clean("first", "draft", "new draft")
        self.save_clean("first", "review", "new review")
        for stage, original in old_bytes.items():
            self.assertEqual((self.task / f"blocks/first/history/{stage}-{sha_bytes(original)}.json").read_bytes(),
                             original)
        self.assertEqual((self.task / "combination.json").read_bytes(), old_combo)
        self.assertEqual(combo["blocks"]["first"], sha_bytes(old_bytes["review"]))

    def test_revise_draft_preserves_old_draft_and_review(self) -> None:
        self.start_plan()
        self.complete_clean("first")
        old = {stage: (self.task / f"blocks/first/{stage}.json").read_bytes() for stage in ("draft", "review")}
        payload = json.loads(old["draft"])
        payload["claims"] = ["corrected claim"]
        source = self.task / "revised-draft.json"
        source.write_text(json.dumps(payload))
        self.call("revise", "--block", "first", "--stage", "draft", "--input", str(source),
                  "--reason", "corrected claim")
        revision = json.loads((self.task / "skill.state").read_text())["blocks"]["first"]["revisions"][-1]
        self.assertEqual(set(revision["affected_stages"]), {"draft", "review"})
        self.save_clean("first", "review", "new review")
        for stage, original in old.items():
            self.assertEqual((self.task / revision["affected_stages"][stage]["archive"]).read_bytes(), original)

    def test_revision_archive_failure_preserves_state_and_old_bytes(self) -> None:
        self.start_plan()
        self.complete_clean("first")
        old_state = (self.task / "skill.state").read_bytes()
        old = {stage: (self.task / f"blocks/first/{stage}.json").read_bytes() for stage in
               ("evidence", "draft", "review")}
        history = self.task / f"blocks/first/history/draft-{sha_bytes(old['draft'])}.json"
        history.parent.mkdir(parents=True)
        history.write_bytes(b"colliding archive")
        payload = json.loads(old["evidence"])
        payload["observations"] = ["corrected evidence"]
        source = self.task / "revised-evidence.json"
        source.write_text(json.dumps(payload))
        self.assertIn("revision archive changed", self.call("revise", "--block", "first", "--stage", "evidence",
                                                            "--input", str(source), "--reason", "correction",
                                                            ok=False)["reason"])
        self.assertEqual((self.task / "skill.state").read_bytes(), old_state)
        for stage, original in old.items():
            self.assertEqual((self.task / f"blocks/first/{stage}.json").read_bytes(), original)

    def test_revision_records_missing_and_damaged_old_stage(self) -> None:
        self.start_plan()
        self.complete_clean("first")
        old_state = json.loads((self.task / "skill.state").read_text())
        (self.task / "blocks/first/draft.json").unlink()
        (self.task / "blocks/first/review.json").write_bytes(b"damaged review")
        payload = json.loads((self.task / "blocks/first/evidence.json").read_text())
        payload["observations"] = ["corrected evidence"]
        source = self.task / "revised-evidence.json"
        source.write_text(json.dumps(payload))
        self.call("revise", "--block", "first", "--stage", "evidence", "--input", str(source),
                  "--reason", "repair after damage")
        revision = json.loads((self.task / "skill.state").read_text())["blocks"]["first"]["revisions"][-1]
        affected = revision["affected_stages"]
        self.assertIsNone(affected["draft"]["archive"])
        self.assertEqual(affected["draft"]["record"], old_state["blocks"]["first"]["stages"]["draft"])
        self.assertFalse(affected["draft"]["matches_recorded_sha256"])
        self.assertEqual((self.task / affected["review"]["archive"]).read_bytes(), b"damaged review")
        self.assertFalse(affected["review"]["matches_recorded_sha256"])
        self.assertNotEqual(affected["review"]["observed_sha256"], affected["review"]["recorded_sha256"])

    def test_plan_cycle_and_drift_are_rejected(self) -> None:
        self.start_plan()
        plan_path = self.task / "plan.json"
        original = plan_path.read_bytes()
        plan_path.write_bytes(original + b" ")
        self.assertIn("plan", " ".join(self.call("resume")["identity_issues"]))
        plan_path.write_bytes(original)
        source = self.task / "bad-plan.json"
        plan = json.loads((self.task / "input-plan.json").read_text())
        plan["blocks"][0]["depends_on"] = ["beta"]
        source.write_text(json.dumps(plan))
        fresh = self.project / "analysis/tasks/cyclic-plan"
        fresh.mkdir()
        (fresh / "build1-status.json").write_bytes((self.task / "build1-status.json").read_bytes())
        result = subprocess.run([sys.executable, str(SCRIPT), "start", "--project", str(self.project),
                                 "--task", str(fresh), "--receipt", str(fresh / "build1-status.json"),
                                 "--plan", str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("dependency cycle", result.stderr)

    def test_public_plan_id_and_direct_coverage_errors(self) -> None:
        self.start_plan()
        plan = json.loads((self.task / "plan.json").read_text())
        plan["targets"][0]["id"] = "Bad_ID"
        fresh = self.project / "analysis/tasks/invalid-id"
        fresh.mkdir()
        (fresh / "build1-status.json").write_bytes((self.task / "build1-status.json").read_bytes())
        source = fresh / "input-plan.json"
        source.write_text(json.dumps(plan))
        self.task = fresh
        error = self.call("start", "--project", str(self.project), "--receipt", str(fresh / "build1-status.json"),
                          "--plan", str(source), ok=False)
        self.assertIn("plan.targets[0].id must match [a-z][a-z0-9-]{0,63}", error["reason"])
        self.assertFalse((fresh / "skill.state").exists())

        self.task = self.project / "analysis/tasks/planned"
        for block in ("first", "alpha", "beta"):
            self.complete_clean(block)
        invalid = self.combination_payload()
        invalid["coverage"]["entry"] = ["alpha"]
        source = self.task / "bad-combination.json"
        source.write_text(json.dumps(invalid))
        error = self.call("save-combination", "--input", str(source), ok=False)
        self.assertIn("plan.blocks[alpha].covers; dependency does not confer coverage", error["reason"])
        self.assertFalse((self.task / "combination.json").exists())

    def test_documented_minimal_plan_and_combination_use_public_cli(self) -> None:
        doc = STAGES_DOC.read_text()
        def example(name: str) -> dict:
            match = re.search(rf"<!-- example:{name} -->\s*```json\n(.*?)\n```", doc, re.S)
            self.assertIsNotNone(match, name)
            return json.loads(match.group(1))

        plan = example("plan")
        source = self.task / "input-plan.json"
        source.write_text(json.dumps(plan))
        fresh = self.project / "analysis/tasks/documented"
        fresh.mkdir()
        (fresh / "build1-status.json").write_bytes((self.task / "build1-status.json").read_bytes())
        (fresh / "input-plan.json").write_bytes(source.read_bytes())
        self.task = fresh
        self.call("start", "--project", str(self.project), "--receipt", str(fresh / "build1-status.json"),
                  "--plan", str(fresh / "input-plan.json"))
        context = self.task / "public-context.json"
        context.write_text('{"status":"pass"}\n')
        for block in ("entry-path", "clear-path"):
            for stage in ("evidence", "draft", "review"):
                payload = example(stage)
                ref = payload["references"][0]
                ref["path"] = ref["path"].replace("entry-path", block)
                ref["sha256"] = sha(context if stage == "evidence" else
                                   self.task / f"blocks/{block}/{'evidence' if stage == 'draft' else 'draft'}.json")
                source = self.task / f"input-{block}-{stage}.json"
                source.write_text(json.dumps(payload))
                self.call("save", "--block", block, "--stage", stage, "--input", str(source))
        combo = example("combination")
        for name, placeholder in (("entry-path", "__ENTRY_REVIEW_SHA256__"),
                                  ("clear-path", "__CLEAR_REVIEW_SHA256__")):
            review_sha = sha(self.task / f"blocks/{name}/review.json")
            combo["blocks"][name] = review_sha
            for ref in combo["references"]:
                if ref["sha256"] == placeholder:
                    ref["sha256"] = review_sha
        self.assertEqual(self.save_combination(combo)["combination"]["status"], "open")

    def test_new_process_reuses_three_saved_stages(self) -> None:
        self.complete()
        state_before = sha(self.task / "skill.state")
        result = self.call("resume")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["blocks"]["first"]["valid_stages"], ["evidence", "draft", "review"])
        self.assertEqual(sha(self.task / "skill.state"), state_before)
        self.assertEqual(json.loads((self.task / "skill.state").read_text())["remaining_scope"], [])

    def test_first_legal_build1_receipt_is_accepted_for_new_task(self) -> None:
        receipt_path = self.task / "build1-status.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["candidate"]["reused"] = False
        receipt["heavy_actions"]["compile_build_count"] = 1
        receipt_path.write_text(json.dumps(receipt))
        # Existing state remains bound to the old receipt; start another task from the legal new-build receipt.
        fresh_task = self.project / "analysis/tasks/first-build"
        fresh_task.mkdir()
        fresh_receipt = fresh_task / "build1-status.json"
        fresh_receipt.write_text(json.dumps(receipt))
        result = subprocess.run([sys.executable, str(SCRIPT), "start", "--project", str(self.project),
                                 "--task", str(fresh_task), "--receipt", str(fresh_receipt),
                                 "--target", "new question", "--block", "first"],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_old_receipt_with_new_build_is_rejected_at_start(self) -> None:
        fresh_task = self.project / "analysis/tasks/mixed-build"
        fresh_task.mkdir()
        receipt = fresh_task / "build1-status.json"
        receipt.write_bytes((self.task / "build1-status.json").read_bytes())
        (self.project / "runtime-query-build.json").write_text('{"top":"different"}\n')
        result = subprocess.run([sys.executable, str(SCRIPT), "start", "--project", str(self.project),
                                 "--task", str(fresh_task), "--receipt", str(receipt),
                                 "--target", "question", "--block", "first"],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("receipt and current build specification differ", result.stderr)
        self.assertFalse((fresh_task / "skill.state").exists())

    def test_offline_source_snapshot_without_git_is_bound_by_bytes(self) -> None:
        shutil.rmtree(self.source / ".git")
        fresh_task = self.project / "analysis/tasks/offline"
        fresh_task.mkdir()
        receipt = fresh_task / "build1-status.json"
        receipt.write_bytes((self.task / "build1-status.json").read_bytes())
        result = subprocess.run([sys.executable, str(SCRIPT), "start", "--project", str(self.project),
                                 "--task", str(fresh_task), "--receipt", str(receipt),
                                 "--target", "offline question", "--block", "first"],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((fresh_task / "skill.state").read_text())
        self.assertEqual(state["identity"]["source"]["mode"], "snapshot-sha256")
        (self.source / "unit.sv").write_text("module changed; endmodule\n")
        result = subprocess.run([sys.executable, str(SCRIPT), "resume", "--task", str(fresh_task)],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("identity mismatch: source", json.loads(result.stdout)["identity_issues"])

    def test_snapshot_fallback_when_git_executable_is_unavailable(self) -> None:
        fresh_task = self.project / "analysis/tasks/no-git-command"
        fresh_task.mkdir()
        receipt = fresh_task / "build1-status.json"
        receipt.write_bytes((self.task / "build1-status.json").read_bytes())
        result = subprocess.run([sys.executable, str(SCRIPT), "start", "--project", str(self.project),
                                 "--task", str(fresh_task), "--receipt", str(receipt),
                                 "--target", "offline question", "--block", "first"],
                                text=True, capture_output=True, env={**os.environ, "PATH": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((fresh_task / "skill.state").read_text())
        self.assertEqual(state["identity"]["source"]["mode"], "snapshot-sha256")

    def test_identity_drifts_refuse_reuse_without_erasing_artifacts(self) -> None:
        self.complete()
        artifact = self.task / "blocks/first/evidence.json"
        original = sha(artifact)
        targets = [self.source / "unit.sv", self.project / "rtl-project.json",
                   self.project / "runtime-query-build.json", self.candidate]
        for target in targets:
            before = target.read_bytes()
            target.write_bytes(before + b"changed\n")
            result = self.call("resume")
            self.assertEqual(result["status"], "stale")
            self.assertTrue(result["identity_issues"])
            self.assertEqual(sha(artifact), original)
            target.write_bytes(before)
        state = json.loads((self.task / "skill.state").read_text())
        state["identity"]["skill_sha256"] = "0" * 64
        (self.task / "skill.state").write_text(json.dumps(state))
        result = self.call("resume")
        self.assertIn("identity mismatch: skill_sha256", result["identity_issues"])

    def test_artifact_reference_and_dependency_drift(self) -> None:
        self.complete()
        self.call("add-block", "--block", "second", "--target", "dependent question", "--depends-on", "first")
        self.save("second", "evidence")
        self.save("second", "draft")
        self.save("second", "review")
        self.assertIn("remaining boundary", json.loads((self.task / "skill.state").read_text())["unresolved"])
        upstream = self.task / "blocks/first/review.json"
        before = upstream.read_bytes()
        upstream.write_bytes(before + b"changed")
        result = self.call("resume")
        self.assertEqual(result["status"], "stale")
        self.assertIn("dependency artifact changed: first", result["blocks"]["second"]["issues"])
        upstream.write_bytes(before)
        referenced = self.task / "blocks/first/evidence.json"
        referenced.unlink()
        result = self.call("resume")
        self.assertEqual(result["blocks"]["first"]["valid_stages"], [])
        self.assertIn("artifact missing or changed: first/evidence", result["blocks"]["first"]["issues"])
        self.assertIn("dependency stale: first", result["blocks"]["second"]["issues"])

    def test_dependency_staleness_ignores_block_name_order(self) -> None:
        dependent = "alpha"
        self.complete("first")
        self.call("add-block", "--block", dependent, "--target", "dependent question",
                  "--depends-on", "first")
        self.complete(dependent)
        self.call("add-block", "--block", "delta", "--target", "partial dependent",
                  "--depends-on", "first")
        self.save("delta", "evidence")
        review = self.task / "blocks/first/review.json"
        review_hash = sha(review)
        (self.task / "blocks/first/evidence.json").unlink()
        result = self.call("resume")
        self.assertEqual(result["status"], "stale")
        self.assertEqual(sha(review), review_hash)
        self.assertEqual(result["blocks"][dependent]["valid_stages"], [])
        self.assertEqual(result["blocks"][dependent]["preserved_stages"],
                         ["evidence", "draft", "review"])
        self.assertIn("dependency stale: first", result["blocks"][dependent]["issues"])
        self.assertIn("dependent question", result["remaining_scope"])
        self.assertIn("partial dependent", result["remaining_scope"])
        self.assertIn("repair evidence", result["next_action"])
        self.call("add-block", "--block", "beta", "--target", "new dependent",
                  "--depends-on", dependent, ok=False)
        draft = {"stage": "draft", "maturity": "DRAFT_UNREVIEWED",
                 "references": [{"base": "task", "path": "blocks/delta/evidence.json",
                                 "sha256": sha(self.task / "blocks/delta/evidence.json")}],
                 "unknowns": [], "next_action": "review", "claims": ["pending"]}
        source = self.task / "input-delta-draft.json"
        source.write_text(json.dumps(draft))
        self.assertIn("dependency stale", self.call("save", "--block", "delta", "--stage", "draft",
                                                    "--input", str(source), ok=False)["reason"])
        self.assertFalse((self.task / "blocks/delta/draft.json").exists())

    def test_two_level_dependency_reference_staleness_and_recovery(self) -> None:
        reference = self.task / "public-context.json"
        reference.write_text('{"status":"pass"}')
        self.save("first", "evidence", [{"base": "task", "path": "public-context.json",
                                          "sha256": sha(reference)}])
        self.save("first", "draft")
        self.save("first", "review")
        self.call("add-block", "--block", "alpha", "--target", "middle question", "--depends-on", "first")
        self.complete("alpha")
        self.call("add-block", "--block", "beta", "--target", "last question", "--depends-on", "alpha")
        self.complete("beta")
        self.call("add-block", "--block", "independent", "--target", "unrelated question")
        self.complete("independent")
        state = json.loads((self.task / "skill.state").read_text())
        self.assertEqual(self.call("resume")["status"], "ready")
        review_hash = sha(self.task / "blocks/first/review.json")
        reference.write_text('{"status":"changed"}')
        result = self.call("resume")
        self.assertEqual(sha(self.task / "blocks/first/review.json"), review_hash)
        for block in ("alpha", "beta"):
            self.assertEqual(result["blocks"][block]["valid_stages"], [])
            self.assertEqual(result["blocks"][block]["preserved_stages"], ["evidence", "draft", "review"])
            self.assertIn(f"dependency stale: {'first' if block == 'alpha' else 'alpha'}",
                          result["blocks"][block]["issues"])
            self.assertIn(state["blocks"][block]["goal"], result["remaining_scope"])
            self.call("add-block", "--block", f"child-{block}", "--target", "blocked",
                      "--depends-on", block, ok=False)
        self.assertEqual(result["blocks"]["independent"]["valid_stages"],
                         ["evidence", "draft", "review"])
        self.assertEqual(result["blocks"]["first"]["valid_stages"], [])
        reference.write_text('{"status":"pass"}')
        recovered = self.call("resume")
        self.assertEqual(recovered["status"], "ready")
        self.assertEqual(recovered["blocks"]["beta"]["valid_stages"], ["evidence", "draft", "review"])

    def test_missing_and_cyclic_dependencies_are_rejected(self) -> None:
        self.complete("first")
        self.call("add-block", "--block", "alpha", "--target", "dependent", "--depends-on", "first")
        self.complete("alpha")
        state_path = self.task / "skill.state"
        state = json.loads(state_path.read_text())
        state["blocks"]["alpha"]["dependencies"] = {"absent": "0" * 64}
        state_path.write_text(json.dumps(state))
        result = self.call("resume")
        self.assertIn("dependency missing: absent", result["blocks"]["alpha"]["issues"])
        self.assertEqual(result["blocks"]["alpha"]["valid_stages"], [])
        state["blocks"]["alpha"]["dependencies"] = {"first": state["blocks"]["first"]["stages"]["review"]["sha256"]}
        state["blocks"]["first"]["dependencies"] = {"alpha": state["blocks"]["alpha"]["stages"]["review"]["sha256"]}
        state_path.write_text(json.dumps(state))
        self.assertIn("dependency cycle", self.call("resume", ok=False)["reason"])

    def test_missing_reference_and_partial_write_do_not_advance(self) -> None:
        missing = [{"base": "project", "path": "source/no-such-file.sv", "sha256": "0" * 64}]
        bad = {"stage": "evidence", "maturity": "DRAFT_UNREVIEWED", "references": missing,
               "observations": ["unverified"]}
        source = self.task / "bad.json"
        source.write_text(json.dumps(bad))
        self.call("save", "--block", "first", "--stage", "evidence", "--input", str(source), ok=False)
        self.assertEqual(self.call("resume")["blocks"]["first"]["valid_stages"], [])
        self.save("first", "evidence")
        (self.task / "blocks/first/draft.json").write_text('{"stage":"draft"}')
        result = self.call("resume")
        self.assertEqual(result["blocks"]["first"]["valid_stages"], ["evidence"])
        self.assertEqual(result["blocks"]["first"]["next_action"], "save draft")

    def test_reference_drift_invalidates_only_affected_stage_and_later(self) -> None:
        self.complete()
        raw = self.task / "public-context.json"
        raw.write_text('{"status":"pass"}')
        reference = [{"base": "task", "path": "public-context.json", "sha256": sha(raw)}]
        evidence = self.task / "blocks/first/evidence.json"
        payload = json.loads(evidence.read_text())
        payload["references"] = reference
        evidence.write_text(json.dumps(payload))
        state = json.loads((self.task / "skill.state").read_text())
        state["blocks"]["first"]["stages"]["evidence"]["sha256"] = sha(evidence)
        (self.task / "skill.state").write_text(json.dumps(state))
        raw.write_text('{"status":"changed"}')
        result = self.call("resume")
        self.assertEqual(result["status"], "stale")
        self.assertIn("reference missing or changed: task:public-context.json", result["blocks"]["first"]["issues"])

    def test_repairing_evidence_clears_downstream_checkpoints(self) -> None:
        self.complete()
        (self.task / "blocks/first/evidence.json").unlink()
        self.assertEqual(self.call("resume")["blocks"]["first"]["valid_stages"], [])
        repaired = self.save("first", "evidence")
        self.assertEqual(repaired["blocks"]["first"]["valid_stages"], ["evidence"])
        state = json.loads((self.task / "skill.state").read_text())
        self.assertEqual(set(state["blocks"]["first"]["stages"]), {"evidence"})
        self.assertIn("one bounded question", state["remaining_scope"])


if __name__ == "__main__":
    unittest.main()
