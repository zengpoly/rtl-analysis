"""Exercise the public CLI from an exact domain-pack whitelist snapshot."""

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


PACK = Path(__file__).resolve().parents[1] / "src/rtl-pack"
BUILD = "0994de23d7f61b3829504686d35a167ffc48bd8597fa2328fd4bbc8a30898866"


class DomainPackAssemblyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(prefix="rtl-domain-pack-test-")
        cls.root = Path(cls.scratch.name)
        manifest = json.loads((PACK / "config/domain-pack.json").read_text())
        for relative in [*manifest["files"], *(f"skills/{name}" for name in manifest["skills"])]:
            source = PACK / relative
            target = cls.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target)
        cls.cli = cls.root / "bin/rtl"
        cls.env = {key: value for key, value in os.environ.items()
                   if key not in ("PYTHONPATH", "PYTHONHOME")}
        cls.env["PYTHONNOUSERSITE"] = "1"

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def invoke(self, *args, stdin=None):
        return subprocess.run([str(self.cli), *args], input=stdin, text=True,
                              capture_output=True, cwd=self.root, env=self.env, timeout=90)

    def test_public_cli_starts_from_manifest_only(self):
        result = self.invoke("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("query", result.stdout)

    def test_q2_fifo_profile_preserves_q2_shapes_and_keeps_content_unready(self):
        q2 = json.loads((self.root / "config/module-q2-publication-profile.v2.json").read_text())
        combo = json.loads((self.root / "config/ibex-q2-fifo-publication-profile.v1.json").read_text())
        self.assertNotEqual(combo["profile"], q2["profile"])
        for section in ("claim_shapes", "obligations"):
            for name, value in q2[section].items():
                self.assertEqual(combo[section][name], value)
        self.assertEqual({name for name in combo["obligations"] if name.startswith("fifo.")}, {
            "fifo.entry-storage@1", "fifo.push-select@1", "fifo.output-select@1",
            "fifo.accept-pop@1", "fifo.clear-error@1",
        })
        for name in combo["obligations"]:
            self.assertFalse(combo["obligations"][name]["source_only_ready"])

    @unittest.skipUnless(os.environ.get("RTL_ANALYSIS_TOOL_ROOT"), "locked tool root not installed")
    def test_public_q2_fifo_stage1_build_and_identity(self):
        with tempfile.TemporaryDirectory(prefix="rtl-combo-stage1-test-") as directory:
            project = Path(directory) / "project"
            project.mkdir()
            shutil.copytree(PACK.parents[1] / "tests/fixtures/runtime_module_candidate", project / "source")
            (project / "analysis/model").mkdir(parents=True)
            shutil.copy2(project / "source/build.json", project / "build.json")
            (project / "rtl-project.json").write_text(json.dumps({
                "schema": "rtl-analysis-agent/project-v1", "name": "combination-fixture",
                "source": "source", "analysis": "analysis", "revision": None, "entry": "top.sv",
            }))

            def build(profile, name):
                return self.invoke("model", "runtime-design-build", "--project", str(project),
                                   "--build", str(project / "build.json"),
                                   "--output", f"analysis/model/{name}.sqlite",
                                   "--publication-profile", profile)

            old = build("module-q2", "q2")
            self.assertEqual(old.returncode, 0, old.stderr + old.stdout)
            new = build("ibex-q2-fifo", "combination")
            self.assertEqual(new.returncode, 0, new.stderr + new.stdout)
            reports = [json.loads(item.stdout) for item in (old, new)]
            self.assertNotEqual(reports[0]["analysis_scope"]["scope_id"],
                                reports[1]["analysis_scope"]["scope_id"])
            old_db, new_db = [project / "analysis/model" / f"{name}.sqlite"
                              for name in ("q2", "combination")]
            for name in ("q2", "combination"):
                deterministic = self.invoke(
                    "model", "query", "--project", str(project),
                    "--model", f"analysis/model/{name}.sqlite", "--view", "module-knowledge",
                    "--target-name", "fixture_top", "--build-sha256",
                    reports[0]["compilation_identity"]["build_sha256"],
                    "--request-kind", "deterministic-instance-of")
                self.assertEqual(deterministic.returncode, 0, deterministic.stderr + deterministic.stdout)
                self.assertEqual(json.loads(deterministic.stdout)["route"], "DIRECT")
            with sqlite3.connect(old_db) as left, sqlite3.connect(new_db) as right:
                for table in ("entity", "fact", "fact_evidence"):
                    self.assertEqual(sorted(left.execute(f"SELECT * FROM {table}")),
                                     sorted(right.execute(f"SELECT * FROM {table}")))
                self.assertEqual(sorted(left.execute("SELECT * FROM evidence WHERE kind='source'")),
                                 sorted(right.execute("SELECT * FROM evidence WHERE kind='source'")))
                for connection in (left, right):
                    self.assertEqual(connection.execute("SELECT count(*) FROM ai_knowledge").fetchone()[0], 0)
                metadata = lambda connection, key: json.loads(connection.execute(
                    "SELECT value_json FROM model_metadata WHERE key=?", (key,)).fetchone()[0])
                self.assertNotEqual(metadata(left, "candidate_id"), metadata(right, "candidate_id"))
                self.assertNotEqual(metadata(left, "semantic_publication_contract")["sha256"],
                                    metadata(right, "semantic_publication_contract")["sha256"])
            previous_bytes = new_db.read_bytes()
            self.assertNotEqual(build("ibex-q2-fifo", "combination").returncode, 0)
            self.assertEqual(new_db.read_bytes(), previous_bytes)
            self.assertNotEqual(build("no-such-profile", "unknown").returncode, 0)
            self.assertFalse((project / "analysis/model/unknown.sqlite").exists())
            (project / "bad-build.json").write_text(json.dumps({
                **json.loads((project / "build.json").read_text()), "tops": ["no_such_top"],
            }))
            invalid_build = self.invoke("model", "runtime-design-build", "--project", str(project),
                                        "--build", str(project / "bad-build.json"),
                                        "--output", "analysis/model/bad-build.sqlite",
                                        "--publication-profile", "ibex-q2-fifo")
            self.assertNotEqual(invalid_build.returncode, 0)
            self.assertFalse((project / "analysis/model/bad-build.sqlite").exists())
            missing_build = self.invoke("model", "runtime-design-build", "--project", str(project),
                                        "--build", str(project / "missing-build.json"),
                                        "--output", "analysis/model/missing-build.sqlite",
                                        "--publication-profile", "ibex-q2-fifo")
            self.assertNotEqual(missing_build.returncode, 0)
            self.assertFalse((project / "analysis/model/missing-build.sqlite").exists())
            scope_copy = project / "analysis/model/bad-scope.sqlite"
            shutil.copy2(new_db, scope_copy)
            with sqlite3.connect(scope_copy) as connection:
                scope = json.loads(connection.execute(
                    "SELECT value_json FROM model_metadata WHERE key='analysis_scope'").fetchone()[0])
                scope["profile"] = json.loads((self.root / "config/module-q2-publication-profile.v2.json").read_text())["profile"]
                connection.execute("UPDATE model_metadata SET value_json=? WHERE key='analysis_scope'",
                                   (json.dumps(scope),))
            mismatch = self.invoke("model", "query", "--project", str(project),
                                   "--model", "analysis/model/bad-scope.sqlite",
                                   "--view", "module-knowledge", "--target-name", "fixture_top",
                                   "--build-sha256", reports[0]["compilation_identity"]["build_sha256"],
                                   "--request-kind", "deterministic-instance-of")
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertEqual(json.loads(mismatch.stdout)["status"], "error")
            original = (self.root / "config/ibex-q2-fifo-publication-profile.v1.json")
            staged = Path(directory) / "tampered-pack"
            shutil.copytree(self.root, staged, symlinks=True)
            (staged / "config/ibex-q2-fifo-publication-profile.v1.json").write_bytes(original.read_bytes() + b"\n")
            tampered = subprocess.run([str(staged / "bin/rtl"), "model", "runtime-design-build",
                                      "--project", str(project), "--build", str(project / "build.json"),
                                      "--output", "analysis/model/tampered.sqlite",
                                      "--publication-profile", "ibex-q2-fifo"],
                                     capture_output=True, text=True, cwd=staged, env=self.env)
            self.assertNotEqual(tampered.returncode, 0)
            self.assertFalse((project / "analysis/model/tampered.sqlite").exists())
            (project / "rtl-project.json").write_text(json.dumps({
                "schema": "rtl-analysis-agent/project-v1", "name": "combination-fixture",
                "source": "source", "analysis": "analysis", "revision": "0" * 40, "entry": "top.sv",
            }))
            wrong_source = build("ibex-q2-fifo", "wrong-source")
            self.assertNotEqual(wrong_source.returncode, 0)
            self.assertFalse((project / "analysis/model/wrong-source.sqlite").exists())

    @unittest.skipUnless(os.environ.get("RTL_QUERY_Q2_PROJECT") and os.environ.get("RTL_ANALYSIS_TOOL_ROOT"),
                         "approved Q2 Project and locked tool root not installed")
    def test_runtime_and_v02_knowledge_off_from_manifest_only(self):
        project = Path(os.environ["RTL_QUERY_Q2_PROJECT"])
        runtime = self.invoke("--project", str(project), "query", "--build",
                              str(project / "runtime-query-build.json"),
                              stdin='{"id":"scope","op":"scope","path":"ibex_core","name":"id_stage_i","limit":5}\n')
        self.assertEqual(runtime.returncode, 0, runtime.stderr)
        self.assertEqual([json.loads(line)["status"] for line in runtime.stdout.splitlines()],
                         ["ready", "ok"])
        query = self.invoke("model", "query", "--project", str(project),
                            "--model", "analysis/model/no-candidate.sqlite",
                            "--view", "module-knowledge", "--target-name", "ibex_controller",
                            "--build-sha256", BUILD, "--knowledge", "off",
                            "--request-kind", "deterministic-instance-of")
        self.assertEqual(query.returncode, 0, query.stderr)
        result = json.loads(query.stdout)
        self.assertEqual(result["route"], "BYPASS")
        self.assertFalse(result["candidate_accessed"])


if __name__ == "__main__":
    unittest.main()
