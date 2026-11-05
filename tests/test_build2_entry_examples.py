"""Execute the public map-reference example, including path rejection cases."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "src/rtl-pack/skills/rtl-build2-draft/references/bounded-reading.md"
SKILL = REFERENCE.parent.parent / "SKILL.md"


class Build2ShellExamplesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.task = self.workspace / "projects/example/analysis/tasks/example"
        self.task.mkdir(parents=True)
        (self.task / "build1-status.json").write_text(
            json.dumps({"candidate": {"path": "analysis/candidate"}}))
        bin_dir = self.workspace / "bin"
        bin_dir.mkdir()
        fake = ("#!/bin/sh\n"
                "printf '{\"status\":\"%s\"}\\n' \"$FIXTURE_STATUS\"\n"
                "printf 'fixture exit %s\\n' \"$FIXTURE_EXIT\" >&2\n"
                "exit \"$FIXTURE_EXIT\"\n")
        for name in ("rtl", "workspace"):
            path = bin_dir / name
            script = fake
            if name == "workspace":
                script = ("#!/bin/sh\nif [ \"$2\" = - ]; then\n"
                          f"  shift; exec {shlex.quote(sys.executable)} \"$@\"\n"
                          "fi\n" + fake)
            path.write_text(script)
            path.chmod(0o755)

    def example(self, kind, attempt=1):
        source = SKILL if kind == "start" else REFERENCE
        marker = "scripts/state.py start" if kind == "start" else "./bin/rtl model query"
        blocks = re.findall(r"```sh\n(.*?)\n```", source.read_text(), re.S)
        script, = [block for block in blocks if marker in block]
        for placeholder, value in {
            "<Skill>": "capability/skills/rtl-build2-draft",
            "<name>": "example", "<task>": "example", "<剩余轮次>": "10",
            "<Project绝对路径>": str(self.workspace / "projects/example"),
            "<该Project下task绝对路径>": str(self.task),
            "<自选exact entity-id>": "e:fixture",
        }.items():
            script = script.replace(placeholder, value)
        # Only the documented attempt number changes on the second invocation.
        return script.replace(f"{kind}.1.", f"{kind}.{attempt}.") if kind == "start" else script.replace(
            "public-context.1.", f"public-context.{attempt}.")

    def run_example(self, kind, code, shell, attempt=1, suffix=""):
        env = dict(os.environ, FIXTURE_EXIT=str(code),
                   FIXTURE_STATUS="pass" if code == 0 else "fail")
        return subprocess.run([*shell, "-c", self.example(kind, attempt) + suffix],
                              cwd=self.workspace, env=env, capture_output=True, text=True)

    def originals(self, kind, attempt):
        prefix = "start" if kind == "start" else "public-context"
        extensions = ("out", "err", "exit") if kind == "start" else ("json", "stderr.log", "exit")
        return [(self.task / f"{prefix}.{attempt}.{ext}").read_bytes() for ext in extensions]

    def test_public_examples_propagate_success_and_failure(self):
        for kind in ("start", "query"):
            for shell in (("sh",), ("bash",), ("bash", "-e")):
                for code in (0, 2):
                    with self.subTest(kind=kind, shell=shell, code=code):
                        result = self.run_example(kind, code, shell)
                        self.assertEqual(result.returncode, code, result.stderr)
                        stdout, stderr, saved_code = self.originals(kind, 1)
                        self.assertEqual(saved_code, f"{code}\n".encode())
                        self.assertEqual(json.loads(stdout)["status"], "pass" if code == 0 else "fail")
                        self.assertEqual(stderr, f"fixture exit {code}\n".encode())

    def test_retry_preserves_failed_originals_and_query_promotion(self):
        for kind in ("start", "query"):
            with self.subTest(kind=kind):
                first = self.run_example(kind, 2, ("bash",))
                original = self.originals(kind, 1)
                if kind == "query":
                    self.assertFalse((self.task / "public-context.json").exists())
                retry = self.run_example(kind, 0, ("bash",), attempt=2)
                self.assertEqual(self.originals(kind, 1), original)
                self.assertEqual((first.returncode, retry.returncode), (2, 0))
                if kind == "query":
                    self.assertEqual((self.task / "public-context.json").read_bytes(),
                                     self.originals(kind, 2)[0])

    def test_failure_does_not_exit_calling_shell(self):
        for kind in ("start", "query"):
            with self.subTest(kind=kind):
                result = self.run_example(kind, 2, ("bash",), suffix="\nprintf 'caller alive\\n'")
                self.assertIn("caller alive", result.stdout)


class Build2EntryExamplesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "workspace/projects/example with spaces"
        self.manual = self.project / "analysis/manual"
        self.manual.mkdir(parents=True)
        self.detail = self.manual / "detail.json"
        self.detail.write_text(json.dumps({
            "schema": "rtl-analysis-agent/manual-v6-structured-detail-v1",
            "scope_id": "scope:test", "semantic": "NOT_BUILT",
            "object": {"entity_id": "e:fixture", "qualified_name": "unit"},
            "children": [], "effective_parameters": [], "parameters_truncated": False,
        }))
        self.manifest = self.manual / "manifest.json"
        self.mapping = {"e:fixture": "detail.json"}
        self.write_manifest()

    def write_manifest(self):
        self.manifest.write_text(json.dumps({
            "schema": "rtl-analysis-agent/manual-v6-build1-manifest-v1",
            "scope_id": "scope:test", "build_sha256": "build", "candidate_sha256": "candidate",
            "structured_detail_map": self.mapping,
        }))

    def example(self, manual="analysis/manual", selector="e:fixture"):
        # Run the exact published Python, never a copied implementation.
        scripts = re.findall(r"<<'PY'\n(.*?)\nPY", REFERENCE.read_text(), re.S)
        return subprocess.run([sys.executable, "-", str(self.project), manual, selector, "0"],
                              input=scripts[1], capture_output=True, text=True, cwd="/")

    def test_project_relative_refs_hash_complete_originals(self):
        result = self.example()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(len(result.stdout.encode()), 8192)
        data = json.loads(result.stdout)
        self.assertEqual(data["detail_relative_to_manifest"], "detail.json")
        for key, path in (("manifest", self.manifest), ("detail", self.detail)):
            self.assertEqual(data["map_anchor"][key], {
                "base": "project", "path": path.relative_to(self.project).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })

    def test_wrong_prefix_absolute_and_escape_fail_without_output(self):
        for manual in ("projects/example with spaces/analysis/manual", str(self.manual), "../outside"):
            with self.subTest(manual=manual):
                result = self.example(manual)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_detail_escape_and_symlink_escape_fail(self):
        outside = Path(self.temp.name) / "outside.json"
        outside.write_bytes(self.detail.read_bytes())
        (self.manual / "linked.json").symlink_to(outside)
        for relative in (str(outside), "../../../../outside.json", "linked.json"):
            with self.subTest(relative=relative):
                self.mapping["e:fixture"] = relative
                self.write_manifest()
                result = self.example()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_missing_and_ambiguous_object_fail(self):
        self.assertNotEqual(self.example(selector="e:missing").returncode, 0)
        second = json.loads(self.detail.read_text())
        second["object"]["entity_id"] = "e:second"
        (self.manual / "second.json").write_text(json.dumps(second))
        self.mapping["e:second"] = "second.json"
        self.write_manifest()
        result = self.example(selector="unit")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
