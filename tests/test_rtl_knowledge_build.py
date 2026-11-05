from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PYSLANG_SITE = ROOT / ".local/tools/pyslang/11.0.0/site-packages"
sys.path.insert(0, str(ROOT / "src/rtl-pack/lib"))

import rtl_build1 as build1  # noqa: E402
import rtl_query_runtime as query_runtime  # noqa: E402


class Build1OrchestrationTests(unittest.TestCase):
    def fixture(self, parent: Path) -> tuple[Path, Path, Path, Path]:
        source = parent / "source"
        source.mkdir()
        (source / "top.sv").write_text("module top; endmodule\n", encoding="utf-8")
        build = parent / "runtime-query-build.json"
        build.write_text(
            json.dumps(
                {
                    "schema": "rtl-analysis-agent/runtime-query-build-v1",
                    "files": ["top.sv"],
                    "filelists": [],
                    "include_dirs": [],
                    "defines": [],
                    "tops": ["top"],
                    "single_unit": True,
                    "environment": {},
                }
            ),
            encoding="utf-8",
        )
        return source, build, parent / "candidate.sqlite", parent / "manual"

    def model(
        self,
        *,
        source: Path,
        build: Path,
        candidate: Path,
        requested_scope: str = "whole-design",
        target_path: str | None = None,
        parameters_truncated: bool = False,
        build_spec_sha256: str | None = None,
    ) -> dict[str, object]:
        root_id = "e:root"
        spec = query_runtime._load_build(source.resolve(), build.resolve())
        spec_identity = {
            "build_spec_sha256": query_runtime._canonical_sha256(
                {key: value for key, value in spec.items() if not key.startswith("_")}
            )
        }
        if build_spec_sha256 is not None:
            spec_identity["build_spec_sha256"] = build_spec_sha256
        whole = requested_scope == "whole-design"
        return {
            "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "scope": {
                "scope_id": "scope:test",
                "scope_mode": "ACTUAL_INSTANCE_ROOT",
                "roots": [{"entity_id": root_id, "kind": "rtl.sv/1:instance"}],
                "build_identity": {
                    "source_revision": "revision-test",
                    "build_spec_sha256": spec_identity["build_spec_sha256"],
                    "source_closure_sha256": "closure",
                    "build_sha256": "build",
                },
                "producer_capability": {
                    "actual_hierarchy": "FULL" if whole else "DIRECT"
                },
                "completeness": {
                    "structure": "COMPLETE" if whole else "COMPLETE_WITHIN_BOUNDARY"
                },
                "limitations": ["fixture limitation"],
            },
            "metadata": (
                {"scope_selection": "whole-design"}
                if whole
                else {
                    "target_instance_path": target_path,
                    "module_core_v1_cycle1_conformance": {"status": "READY"},
                }
            ),
            "entities": {
                root_id: {
                    "entity_id": root_id,
                    "qualified_name": target_path or "top",
                }
            },
            "actuals": [
                {
                    "structured_detail": {
                        "effective_parameters": [{"name": "WIDTH"}],
                        "parameters_truncated": parameters_truncated,
                        "bindings": [{"formal": "clk"}],
                    }
                }
            ],
            "modules": [{"entity_id": "e:module"}],
            "sources": [{"entity_id": "e:source"}],
            "ai_count": 0,
            "behavior_count": 0,
        }

    def invoke(
        self,
        *,
        source: Path,
        build: Path,
        candidate: Path,
        manual: Path,
        requested_scope: str = "whole-design",
        target_path: str | None = None,
        pyslang_site: Path | None = None,
    ) -> tuple[int, dict[str, object]]:
        return build1.run_build1(
            project_name="fixture",
            source_revision="revision-test",
            source_root=source,
            build_path=build,
            pyslang_site=source if pyslang_site is None else pyslang_site,
            requested_scope=requested_scope,
            target_path=target_path,
            candidate=candidate,
            candidate_relative="analysis/candidate.sqlite",
            manual=manual,
            manual_relative="analysis/manual",
            source_viewer="locator",
        )

    def real_ready_fixture(
        self, parent: Path
    ) -> tuple[Path, Path, Path, Path, dict[str, object]]:
        source, build, candidate, manual = self.fixture(parent)
        code, result = self.invoke(
            source=source,
            build=build,
            candidate=candidate,
            manual=manual,
            pyslang_site=PYSLANG_SITE,
        )
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "READY")
        return source, build, candidate, manual, result

    @staticmethod
    def render(model: dict[str, object], output: Path, *, source_viewer: str) -> dict[str, object]:
        output.mkdir(parents=True)
        page_map = {
            "scope": "index.html",
            "instance": "instance/root.html",
            "definition": "definition/top.html",
        }
        structured_detail_map = {
            "instance": "structured/instance/root.json",
            "definition": "structured/definition/top.json",
        }
        manifest = {
            "schema": "rtl-analysis-agent/manual-v6-build1-manifest-v1",
            "scope_id": model["scope"]["scope_id"],
            "build_sha256": model["scope"]["build_identity"]["build_sha256"],
            "candidate_sha256": model["candidate_sha256"],
            "source_serving": source_viewer,
            "page_counts": {
                "scope": 1,
                "instance": 1,
                "module_definition": 1,
                "source": 0,
                "container": 0,
            },
            "structured_detail_counts": {"instance": 1, "module_definition": 1},
            "page_map": page_map,
            "structured_detail_map": structured_detail_map,
        }
        for relative in {
            *page_map.values(),
            *structured_detail_map.values(),
            "assets/manual.css",
            "assets/manual.js",
            "assets/tree.json",
            "assets/search-index.json",
            "assets/tree-data.js",
            "assets/search-data.js",
        }:
            path = output / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
        (output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    def test_unsupported_scope_stops_without_heavy_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, build, candidate, manual = self.fixture(Path(directory))
            code, result = self.invoke(
                source=source,
                build=build,
                candidate=candidate,
                manual=manual,
                requested_scope="multi-root",
            )
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_code"], "SCOPE_MODE_UNSUPPORTED")
        self.assertEqual(
            result["heavy_actions"], {"compile_build_count": 0, "render_count": 0}
        )

    def test_fresh_whole_build_calls_existing_producer_and_renderer_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, build, candidate, manual = self.fixture(Path(directory))

            def produce(**kwargs: object) -> dict[str, object]:
                Path(kwargs["output"]).write_bytes(b"candidate")
                return {"status": "pass"}

            def load(*_: object, **__: object) -> dict[str, object]:
                return self.model(source=source, build=build, candidate=candidate)

            with (
                mock.patch.object(
                    build1.module_knowledge,
                    "build_runtime_design_candidate",
                    side_effect=produce,
                ) as producer,
                mock.patch.object(build1.manual_v6, "load_backbone", side_effect=load),
                mock.patch.object(
                    build1.manual_v6, "build_site", side_effect=self.render
                ) as renderer,
            ):
                code, result = self.invoke(
                    source=source, build=build, candidate=candidate, manual=manual
                )
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["counts"]["parameters"], 1)
        self.assertEqual(
            result["heavy_actions"], {"compile_build_count": 1, "render_count": 1}
        )
        producer.assert_called_once()
        renderer.assert_called_once()

    def test_matching_candidate_and_manual_are_reused_without_heavy_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, build, candidate, manual = self.fixture(Path(directory))
            candidate.write_bytes(b"candidate")
            model = self.model(source=source, build=build, candidate=candidate)
            self.render(model, manual, source_viewer="locator")
            with (
                mock.patch.object(
                    build1,
                    "_validate_candidate_lightweight",
                    return_value=(model, build1._counts(model)),
                ),
                mock.patch.object(
                    build1.module_knowledge, "build_runtime_design_candidate"
                ) as producer,
                mock.patch.object(build1.manual_v6, "load_backbone") as loader,
                mock.patch.object(build1.manual_v6, "build_site") as renderer,
            ):
                code, result = self.invoke(
                    source=source, build=build, candidate=candidate, manual=manual
                )
        self.assertEqual(code, 0)
        self.assertTrue(result["candidate"]["reused"])
        self.assertTrue(result["manual"]["reused"])
        self.assertEqual(
            result["heavy_actions"], {"compile_build_count": 0, "render_count": 0}
        )
        producer.assert_not_called()
        loader.assert_not_called()
        renderer.assert_not_called()

    @unittest.skipUnless(PYSLANG_SITE.is_dir(), "locked PySlang is not installed")
    def test_missing_declared_manual_html_or_json_is_partial_without_heavy_action(self) -> None:
        for map_name in ("page_map", "structured_detail_map"):
            with self.subTest(map_name=map_name), tempfile.TemporaryDirectory() as directory:
                source, build, candidate, manual, _ = self.real_ready_fixture(
                    Path(directory)
                )
                manifest = json.loads(
                    (manual / "manifest.json").read_text(encoding="utf-8")
                )
                relative = next(iter(manifest[map_name].values()))
                (manual / relative).unlink()
                code, result = self.invoke(
                    source=source,
                    build=build,
                    candidate=candidate,
                    manual=manual,
                    pyslang_site=PYSLANG_SITE,
                )
                self.assertEqual(code, 3)
                self.assertEqual(result["status"], "PARTIAL")
                self.assertEqual(result["reason_code"], "MANUAL_INCOMPLETE")
                self.assertEqual(
                    result["heavy_actions"],
                    {"compile_build_count": 0, "render_count": 0},
                )

        with tempfile.TemporaryDirectory() as directory:
            source, build, candidate, manual, _ = self.real_ready_fixture(Path(directory))
            (manual / "assets/manual.css").unlink()
            code, result = self.invoke(
                source=source,
                build=build,
                candidate=candidate,
                manual=manual,
                pyslang_site=PYSLANG_SITE,
            )
            self.assertEqual(code, 3)
            self.assertEqual(result["reason_code"], "MANUAL_INCOMPLETE")
            self.assertEqual(
                result["heavy_actions"],
                {"compile_build_count": 0, "render_count": 0},
            )

        with tempfile.TemporaryDirectory() as directory:
            source, build, candidate, manual, _ = self.real_ready_fixture(Path(directory))
            manifest_path = manual / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["page_map"].pop(next(
                key for key, value in manifest["page_map"].items()
                if value != "index.html"
            ))
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            code, result = self.invoke(
                source=source,
                build=build,
                candidate=candidate,
                manual=manual,
                pyslang_site=PYSLANG_SITE,
            )
            self.assertEqual(code, 3)
            self.assertEqual(result["reason_code"], "MANUAL_INCOMPLETE")

    @unittest.skipUnless(PYSLANG_SITE.is_dir(), "locked PySlang is not installed")
    def test_manual_manifest_paths_fail_closed(self) -> None:
        cases = ("absolute", "parent", "symlink", "directory")
        for map_name in ("page_map", "structured_detail_map"):
            for case in cases:
                with self.subTest(map_name=map_name, case=case), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source, build, candidate, manual, _ = self.real_ready_fixture(root)
                    manifest_path = manual / "manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    key, relative = next(iter(manifest[map_name].items()))
                    target = manual / relative
                    if case == "absolute":
                        manifest[map_name][key] = (root / "outside.html").as_posix()
                    elif case == "parent":
                        manifest[map_name][key] = "../outside.html"
                    elif case == "symlink":
                        target.unlink()
                        outside = root / "outside.html"
                        outside.write_text("outside", encoding="utf-8")
                        target.symlink_to(outside)
                    else:
                        target.unlink()
                        target.mkdir()
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    code, result = self.invoke(
                        source=source,
                        build=build,
                        candidate=candidate,
                        manual=manual,
                        pyslang_site=PYSLANG_SITE,
                    )
                    self.assertEqual(code, 3)
                    self.assertEqual(result["status"], "PARTIAL")
                    self.assertEqual(result["reason_code"], "MANUAL_INCOMPLETE")
                    self.assertEqual(
                        result["heavy_actions"],
                        {"compile_build_count": 0, "render_count": 0},
                    )

    @unittest.skipUnless(PYSLANG_SITE.is_dir(), "locked PySlang is not installed")
    def test_fast_resume_keeps_candidate_integrity_boundary_and_source_checks(self) -> None:
        for case, reason in (
            ("stale-source", "SOURCE_STALE"),
            ("corrupt-sqlite", "CANDIDATE_INTEGRITY_FAILED"),
            ("truncated-parameters", "MANDATORY_SKELETON_TRUNCATED"),
            ("behavior-table", "BUILD1_BOUNDARY_VIOLATION"),
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, build, candidate, manual, _ = self.real_ready_fixture(root)
                if case == "stale-source":
                    (source / "top.sv").write_text(
                        "module top; wire changed; endmodule\n", encoding="utf-8"
                    )
                elif case == "corrupt-sqlite":
                    candidate.write_bytes(b"not a sqlite database")
                else:
                    connection = sqlite3.connect(candidate)
                    if case == "truncated-parameters":
                        row = connection.execute(
                            "SELECT entity_id, attrs_json FROM entity "
                            "WHERE json_extract(attrs_json, '$.effective_parameters') IS NOT NULL "
                            "ORDER BY entity_id LIMIT 1"
                        ).fetchone()
                        attrs = json.loads(row[1])
                        attrs["parameters_truncated"] = True
                        connection.execute(
                            "UPDATE entity SET attrs_json=? WHERE entity_id=?",
                            (json.dumps(attrs), row[0]),
                        )
                    else:
                        connection.execute("CREATE TABLE behavior_object(id TEXT)")
                    connection.commit()
                    connection.close()
                with mock.patch.object(build1.manual_v6, "load_backbone") as loader:
                    code, result = self.invoke(
                        source=source,
                        build=build,
                        candidate=candidate,
                        manual=manual,
                        pyslang_site=PYSLANG_SITE,
                    )
                self.assertEqual(code, 2)
                self.assertEqual(result["status"], "BLOCKED")
                self.assertEqual(result["reason_code"], reason)
                self.assertEqual(
                    result["heavy_actions"],
                    {"compile_build_count": 0, "render_count": 0},
                )
                loader.assert_not_called()

    @unittest.skipUnless(PYSLANG_SITE.is_dir(), "locked PySlang is not installed")
    def test_ready_resume_avoids_full_page_view_loader(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, build, candidate, manual, _ = self.real_ready_fixture(
                Path(directory)
            )
            with mock.patch.object(build1.manual_v6, "load_backbone") as loader:
                code, result = self.invoke(
                    source=source,
                    build=build,
                    candidate=candidate,
                    manual=manual,
                    pyslang_site=PYSLANG_SITE,
                )
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(
            result["heavy_actions"],
            {"compile_build_count": 0, "render_count": 0},
        )
        loader.assert_not_called()
        self.assertNotIn("controller_overhead_wall_seconds", result["timing"])
        self.assertEqual(
            set(result["timing"]),
            {
                "candidate_validation_wall_seconds",
                "manual_validation_wall_seconds",
                "build_wall_seconds",
                "render_wall_seconds",
                "total_wall_seconds",
            },
        )

    def test_identity_mismatch_blocks_before_render(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, build, candidate, manual = self.fixture(Path(directory))
            candidate.write_bytes(b"candidate")
            model = self.model(
                source=source,
                build=build,
                candidate=candidate,
                build_spec_sha256="not-current",
            )
            with (
                mock.patch.object(build1.manual_v6, "load_backbone", return_value=model),
                mock.patch.object(build1.manual_v6, "build_site") as renderer,
            ):
                code, result = self.invoke(
                    source=source, build=build, candidate=candidate, manual=manual
                )
        self.assertEqual(code, 2)
        self.assertEqual(result["reason_code"], "BUILD_IDENTITY_MISMATCH")
        self.assertEqual(result["heavy_actions"]["render_count"], 0)
        renderer.assert_not_called()

    def test_truncated_skeleton_blocks_before_render(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, build, candidate, manual = self.fixture(Path(directory))
            candidate.write_bytes(b"candidate")
            model = self.model(
                source=source,
                build=build,
                candidate=candidate,
                parameters_truncated=True,
            )
            with mock.patch.object(
                build1.manual_v6, "load_backbone", return_value=model
            ):
                code, result = self.invoke(
                    source=source, build=build, candidate=candidate, manual=manual
                )
        self.assertEqual(code, 2)
        self.assertEqual(result["reason_code"], "MANDATORY_SKELETON_TRUNCATED")

    def test_render_failure_retry_reuses_candidate_and_only_renders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, build, candidate, manual = self.fixture(Path(directory))

            def produce(**kwargs: object) -> dict[str, object]:
                Path(kwargs["output"]).write_bytes(b"candidate")
                return {"status": "pass"}

            def load(*_: object, **__: object) -> dict[str, object]:
                return self.model(source=source, build=build, candidate=candidate)

            def fail_render(_: object, output: Path, **__: object) -> dict[str, object]:
                output.mkdir(parents=True)
                (output / "partial").write_text("partial", encoding="utf-8")
                raise RuntimeError("injected render failure")

            with (
                mock.patch.object(
                    build1.module_knowledge,
                    "build_runtime_design_candidate",
                    side_effect=produce,
                ) as producer,
                mock.patch.object(build1.manual_v6, "load_backbone", side_effect=load),
                mock.patch.object(
                    build1.manual_v6, "build_site", side_effect=fail_render
                ),
            ):
                first_code, first = self.invoke(
                    source=source, build=build, candidate=candidate, manual=manual
                )
            self.assertEqual(first_code, 3)
            self.assertEqual(first["status"], "PARTIAL")
            self.assertEqual(first["reason_code"], "MANUAL_RENDER_FAILED")
            self.assertTrue(candidate.is_file())
            self.assertFalse(manual.with_name(manual.name + ".building").exists())

            with (
                mock.patch.object(
                    build1.module_knowledge, "build_runtime_design_candidate"
                ) as retry_producer,
                mock.patch.object(build1.manual_v6, "load_backbone", side_effect=load),
                mock.patch.object(
                    build1.manual_v6, "build_site", side_effect=self.render
                ),
            ):
                second_code, second = self.invoke(
                    source=source, build=build, candidate=candidate, manual=manual
                )
        self.assertEqual(second_code, 0)
        self.assertEqual(second["status"], "READY")
        self.assertEqual(
            second["heavy_actions"], {"compile_build_count": 0, "render_count": 1}
        )
        self.assertEqual(producer.call_count, 1)
        retry_producer.assert_not_called()

    def test_actual_root_requires_exact_path_and_uses_module_producer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, build, candidate, manual = self.fixture(Path(directory))
            missing_code, missing = self.invoke(
                source=source,
                build=build,
                candidate=candidate,
                manual=manual,
                requested_scope="actual-root",
            )
            self.assertEqual(missing_code, 2)
            self.assertEqual(missing["reason_code"], "ACTUAL_ROOT_REQUIRED")

            target = "top.u_leaf"

            def produce(**kwargs: object) -> dict[str, object]:
                Path(kwargs["output"]).write_bytes(b"candidate")
                return {"status": "pass"}

            def load(*_: object, **__: object) -> dict[str, object]:
                return self.model(
                    source=source,
                    build=build,
                    candidate=candidate,
                    requested_scope="actual-root",
                    target_path=target,
                )

            with (
                mock.patch.object(
                    build1.module_knowledge,
                    "build_runtime_module_candidate",
                    side_effect=produce,
                ) as producer,
                mock.patch.object(build1.manual_v6, "load_backbone", side_effect=load),
                mock.patch.object(
                    build1.manual_v6, "build_site", side_effect=self.render
                ),
            ):
                code, result = self.invoke(
                    source=source,
                    build=build,
                    candidate=candidate,
                    manual=manual,
                    requested_scope="actual-root",
                    target_path=target,
                )
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "READY")
        producer.assert_called_once()

    @unittest.skipUnless(PYSLANG_SITE.is_dir(), "locked PySlang is not installed")
    def test_public_cli_builds_and_resumes_real_fixture_without_repeating_work(self) -> None:
        fixture = ROOT / "tests/fixtures/runtime_module_candidate"
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            source = project / "source"
            source.mkdir(parents=True)
            (project / "analysis").mkdir()
            shutil.copy2(fixture / "top.sv", source / "top.sv")
            shutil.copy2(fixture / "leaf.sv", source / "leaf.sv")
            shutil.copy2(fixture / "build.json", project / "runtime-query-build.json")
            (project / "rtl-project.json").write_text(
                json.dumps(
                    {
                        "schema": "rtl-analysis-agent/project-v1",
                        "name": "build1-cli-fixture",
                        "source": "source",
                        "analysis": "analysis",
                        "revision": None,
                        "entry": "top.sv",
                    }
                ),
                encoding="utf-8",
            )
            command = [
                str(ROOT / "src/rtl-pack/bin/rtl"),
                "model", "knowledge-build",
                "--project", str(project),
                "--scope", "whole-design",
            ]
            first = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True, check=False
            )
            shutil.rmtree(project / "analysis/manual-v6-build1")
            second = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True, check=False
            )
            third = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True, check=False
            )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
        first_payload = json.loads(first.stdout)
        second_payload = json.loads(second.stdout)
        third_payload = json.loads(third.stdout)
        self.assertEqual(first_payload["status"], "READY")
        self.assertEqual(first_payload["counts"]["actual_instances"], 3)
        self.assertEqual(
            first_payload["heavy_actions"],
            {"compile_build_count": 1, "render_count": 1},
        )
        self.assertEqual(
            second_payload["heavy_actions"],
            {"compile_build_count": 0, "render_count": 1},
        )
        self.assertEqual(
            third_payload["heavy_actions"],
            {"compile_build_count": 0, "render_count": 0},
        )
        self.assertEqual(
            first_payload["candidate"]["sha256"],
            second_payload["candidate"]["sha256"],
        )
        self.assertEqual(
            first_payload["candidate"]["sha256"],
            third_payload["candidate"]["sha256"],
        )


if __name__ == "__main__":
    unittest.main()
