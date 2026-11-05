"""Mechanical preview contract; fixture claims are not model/semantic validation."""
import json
import subprocess
import sys
import unittest
import shutil
from pathlib import Path

from tests import test_build2_region_state as region

ROOT = Path(__file__).resolve().parents[1]


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.f = region.RegionStateTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        config = self.f.project / "rtl-project.json"
        data = json.loads(config.read_text())
        data.update(schema="rtl-analysis-agent/project-v1", name="fixture", analysis="analysis", entry="unit.sv")
        config.write_text(json.dumps(data))
        files = self.f.map_files()
        detail = json.loads(files["a"].read_text())
        detail.update(definition={"entity_id": "definition:unit", "qualified_name": "unit"},
                      effective_parameters=[{"name": "WIDTH", "value": "8", "overridden": True,
                                             "provenance_complete": False}])
        files["a"].write_text(json.dumps(detail))
        plan = self.f.plan(files)
        plan["configuration"] = "Historical description: WIDTH=8, not overridden"
        self.f.start_v2(plan, files)
        self.f.stage("a-main", "evidence")
        self.f.stage("a-main", "draft")
        self.revise("在当前配置下观察输出。<script>alert(1)</script>")

    def revise(self, claim):
        source = self.f.task / "input-a-main-draft.json"
        data = json.loads(source.read_text())
        data["claims"] = [claim]
        data["unknowns"] = ["跨块时序尚未核验"]
        source.write_text(json.dumps(data, ensure_ascii=False))
        self.f.call("revise", "--block", "a-main", "--stage", "draft", "--input", str(source),
                    "--reason", "mechanical preview fixture revision")

    def preview(self, output="artifacts/preview", ok=True, project=None, cli=None):
        result = subprocess.run([sys.executable, str(cli or ROOT / "src/rtl-pack/bin/rtl"), "model", "manual-preview",
                                 "--project", str(project or self.f.project), "--task", str(self.f.task),
                                 "--output", output], capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return result

    def test_chinese_identity_status_trace_escape_and_readonly(self):
        before = (self.f.task / "skill.state").read_bytes()
        self.preview()
        page = (self.f.task / "artifacts/preview/index.html").read_text()
        for text in ("Build2草稿预览", "DRAFT_UNREVIEWED", "NOT PUBLISHED", "definition:unit", "actual:a",
                     "not overridden", "冲突", "true", "跨块时序尚未核验", "missing", "open", "<details", "claims[0]"):
            self.assertIn(text, page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertEqual(before, (self.f.task / "skill.state").read_bytes())
        manifest = json.loads((self.f.task / "artifacts/preview/preview-manifest.json").read_text())
        self.assertEqual(manifest["blocks"]["a-main"]["valid_stages"], ["evidence", "draft"])

    def test_revise_only_new_effective_draft(self):
        self.preview()
        self.revise("修订后的当前正文")
        self.preview("artifacts/new-preview")
        page = (self.f.task / "artifacts/new-preview/index.html").read_text()
        self.assertIn("修订后的当前正文", page)
        self.assertNotIn("在当前配置下观察输出", page)
        self.assertTrue(list((self.f.task / "blocks/a-main/history").rglob("*.json")))

    def test_reject_existing_escape_and_symlink(self):
        self.preview()
        self.preview(ok=False)
        for output in ("../outside", "/tmp/preview", ".", "artifacts/link/new"):
            if "link" in output:
                (self.f.task / "artifacts/link").symlink_to(self.f.project, target_is_directory=True)
            self.preview(output, ok=False)

    def test_reject_stale_stage_and_no_success_output(self):
        (self.f.task / "blocks/a-main/evidence.json").write_text("{}")
        self.preview(ok=False)
        self.assertFalse((self.f.task / "artifacts/preview").exists())

    def test_reject_source_drift(self):
        (self.f.source / "unit.sv").write_text("module changed; endmodule")
        self.preview(ok=False)

    def test_reject_project_mismatch(self):
        self.preview(project=self.f.project.parent, ok=False)

    def test_reject_skill_drift_with_installed_copy(self):
        capability = Path(self.f.temp.name) / "changed capability"
        shutil.copytree(ROOT / "src/rtl-pack", capability, ignore=shutil.ignore_patterns("__pycache__"))
        skill = capability / "skills/rtl-build2-draft/SKILL.md"
        skill.write_text(skill.read_text() + "\nChanged installed Skill.\n")
        result = self.preview(ok=False, cli=capability / "bin/rtl")
        self.assertIn("skill_sha256", result.stdout)

    def test_reject_config_receipt_map_and_reference_drift(self):
        paths = [self.f.project / "runtime-query-build.json", self.f.task / "build1-status.json",
                 self.f.project / "analysis/manual/build1/a.json", self.f.candidate]
        for path in paths:
            with self.subTest(path=path):
                before = path.read_bytes()
                path.write_bytes(before + b" ")
                self.preview(ok=False)
                path.write_bytes(before)

    def test_reject_missing_draft_after_evidence_revision(self):
        self.f.call("revise", "--block", "a-main", "--stage", "evidence", "--input",
                    str(self.f.task / "input-a-main-evidence.json"), "--reason", "invalidate downstream")
        self.preview(ok=False)
        self.assertFalse((self.f.task / "artifacts/preview").exists())

    def test_dependency_revision_invalidates_consumer(self):
        self.f.stage("a-main", "review")
        files = {"manifest": self.f.project / "analysis/manual/build1/manifest.json",
                 "a": self.f.project / "analysis/manual/build1/a.json",
                 "b": self.f.project / "analysis/manual/build1/b.json"}
        self.f.extend(self.f.expanded(files))
        self.f.stage("b-relation", "evidence")
        self.f.stage("b-relation", "draft")
        self.f.call("revise", "--block", "a-main", "--stage", "review", "--input",
                    str(self.f.task / "input-a-main-review.json"), "--reason", "check dependency")
        # Change review bytes through public revise, not through state manipulation.
        source = self.f.task / "input-a-main-review.json"
        data = json.loads(source.read_text())
        data["checks"] = ["new upstream check"]
        source.write_text(json.dumps(data))
        self.f.call("revise", "--block", "a-main", "--stage", "review", "--input", str(source),
                    "--reason", "new upstream check")
        result = self.preview(ok=False)
        self.assertIn("dependency", result.stdout)

    def test_relocation_and_binary_reference(self):
        binary = self.f.task / "binary-evidence.bin"
        binary.write_bytes(b"\x00\xff\x80")
        source = self.f.task / "input-a-main-draft.json"
        data = json.loads(source.read_text())
        import hashlib
        data["references"].append({"base": "task", "path": binary.name,
                                   "sha256": hashlib.sha256(binary.read_bytes()).hexdigest()})
        source.write_text(json.dumps(data))
        self.f.call("revise", "--block", "a-main", "--stage", "draft", "--input", str(source), "--reason", "binary evidence")
        moved = self.f.project.parent / "moved project with spaces"
        relative = self.f.task.relative_to(self.f.project)
        shutil.move(self.f.project, moved)
        self.f.project, self.f.task = moved, moved / relative
        self.preview()

    def test_input_change_during_render_refuses_output(self):
        from unittest import mock
        sys.path.insert(0, str(ROOT / "src/rtl-pack/lib"))
        self.addCleanup(sys.path.remove, str(ROOT / "src/rtl-pack/lib"))
        import rtl_build2_preview as preview
        original = preview.Environment.get_template
        def changed(environment, *args, **kwargs):
            template = original(environment, *args, **kwargs)
            path = self.f.task / "blocks/a-main/draft.json"
            path.write_bytes(path.read_bytes() + b" ")
            return template
        with mock.patch.object(preview.Environment, "get_template", changed):
            with self.assertRaisesRegex(ValueError, "changed"):
                preview.build_preview(self.f.project, str(self.f.task), "artifacts/preview")
        self.assertFalse((self.f.task / "artifacts/preview").exists())


if __name__ == "__main__":
    unittest.main()
