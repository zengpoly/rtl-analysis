"""B5 Stage1-only v0.2 candidate, legacy isolation, and publication-path safety."""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/rtl-pack/lib"))

import rtl_knowledge_prototype as prototype  # noqa: E402
import rtl_module_knowledge as module_knowledge  # noqa: E402


FIXTURE = ROOT / "tests/fixtures/runtime_module_candidate"
PYSLANG_SITE = Path(os.environ.get("RTL_ANALYSIS_TOOL_ROOT", str(ROOT / ".local/tools"))) / "pyslang/11.0.0/site-packages"


def _build(kind: str, output: Path, *, schema_version: str = "v0.2",
           publication_contract: Path | None = None) -> dict:
    kwargs = {
        "source_root": FIXTURE,
        "build_path": FIXTURE / "build.json",
        "pyslang_site": PYSLANG_SITE,
        "project_name": "g53-fixture",
        "source_revision": "fixture-v1",
        "output": output,
        "schema_version": schema_version,
        "publication_contract": publication_contract,
    }
    if kind == "module":
        return module_knowledge.build_runtime_module_candidate(
            target_path="fixture_top.u_low", **kwargs
        )
    return module_knowledge.build_runtime_design_candidate(**kwargs)


def _contending_build(kind: str, output: str, barrier, results) -> None:
    """Force both processes past the old shared-name check before either creates."""
    import rtl_query_runtime

    original_open = rtl_query_runtime.RuntimeQuerySession.open.__func__
    original_create = prototype.PrototypeStore.create.__func__

    def gated_open(cls, **kwargs):
        barrier.wait(timeout=30)
        return original_open(cls, **kwargs)

    def gated_create(cls, *args, **kwargs):
        barrier.wait(timeout=30)
        return original_create(cls, *args, **kwargs)

    rtl_query_runtime.RuntimeQuerySession.open = classmethod(gated_open)
    prototype.PrototypeStore.create = classmethod(gated_create)
    try:
        result = _build(kind, Path(output))
        results.put(("ok", result["candidate"]["sha256"]))
    except BaseException as error:
        results.put(("error", type(error).__name__, str(error)))


class G53Stage1SchemaTests(unittest.TestCase):
    def test_v02_schema_requires_scope_and_rejects_legacy_semantic_writers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "v02.sqlite"
            store = prototype.PrototypeStore.create(
                database, {"project": "g53"}, schema_version="v0.2"
            )
            self.assertEqual(store.schema_version, "v0.2")
            self.assertEqual(store.metadata("schema"),
                             "rtl-analysis-agent/knowledge-prototype-v02-candidate")
            tables = {row[0] for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            self.assertTrue({"entity", "fact", "evidence", "behavior_object",
                             "ai_knowledge", "ai_fact_support", "obligation_coverage",
                             "obligation_coverage_claim"} <= tables)
            self.assertNotIn("knowledge_coverage", tables)
            with self.assertRaises(prototype.StoreError):
                store.add_ai_knowledge("missing", "missing", "text",
                                       support_fact_ids=[], evidence_ids=[])
            with self.assertRaises(prototype.StoreError):
                store.add_knowledge_coverage(
                    "missing", section="function", subtree_scope="direct",
                    status="ready", child_instance_ids=[], request_kinds=[],
                    evidence_ids=[],
                )
            store.close()
            with self.assertRaises(prototype.StoreError):
                prototype.PrototypeStore.open(database, readonly=True)

    def test_unknown_schema_and_v01_without_scope_remain_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(prototype.StoreError):
                prototype.PrototypeStore.create(
                    root / "unknown.sqlite", {"project": "g53"},
                    schema_version="future",
                )
            self.assertFalse((root / "unknown.sqlite").exists())
            database = root / "old.sqlite"
            old = prototype.PrototypeStore.create(database, {"project": "legacy"})
            old.close()
            reopened = prototype.PrototypeStore.open(database, readonly=True)
            self.assertEqual(reopened.schema_version, "v0.1")
            reopened.close()


class G53TransactionTests(unittest.TestCase):
    def test_commit_busy_rolls_back_and_clears_optimistic_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "busy.sqlite"
            store = prototype.PrototypeStore.create(database, {"project": "g53"})
            store.close()
            store = prototype.PrototypeStore.open(database)
            store.connection.execute("PRAGMA journal_mode=DELETE")
            store.connection.execute("PRAGMA busy_timeout=100")
            reader = sqlite3.connect(database)
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM entity").fetchone()
            kind = store.contract_id("entities", "MODULE")
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    with store.transaction():
                        store.add_entity(kind, "uncommitted")
                self.assertFalse(store.connection.in_transaction)
                self.assertEqual(store._entity_kind_cache, {})
                self.assertTrue(all(not ids for ids in store._known_ids.values()))
            finally:
                reader.rollback()
                reader.close()
            with store.transaction():
                store.add_entity(kind, "reused")
            store.close()
            check = prototype.PrototypeStore.open(database, readonly=True)
            self.assertEqual([row[0] for row in check.connection.execute(
                "SELECT qualified_name FROM entity ORDER BY qualified_name"
            )], ["reused"])
            check.close()

    def test_commit_response_lost_discards_connection_without_false_rollback(self) -> None:
        class LostReply:
            def __init__(self, connection):
                self.raw = connection

            def __getattr__(self, name):
                return getattr(self.raw, name)

            def commit(self):
                self.raw.commit()
                raise sqlite3.OperationalError("reply lost after commit")

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "lost.sqlite"
            store = prototype.PrototypeStore.create(database, {"project": "g53"})
            store.connection = LostReply(store.connection)
            kind = store.contract_id("entities", "MODULE")
            with self.assertRaisesRegex(sqlite3.OperationalError, "reply lost"):
                with store.transaction():
                    store.add_entity(kind, "committed")
            self.assertTrue(store._discarded)
            with self.assertRaises(prototype.StoreError):
                with store.transaction():
                    pass
            check = prototype.PrototypeStore.open(database, readonly=True)
            self.assertEqual(check.connection.execute(
                "SELECT count(*) FROM entity WHERE qualified_name='committed'"
            ).fetchone()[0], 1)
            check.close()

    def test_catchable_cancellation_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "cancel.sqlite"
            store = prototype.PrototypeStore.create(database, {"project": "g53"})
            kind = store.contract_id("entities", "MODULE")
            with self.assertRaises(KeyboardInterrupt):
                with store.transaction():
                    store.add_entity(kind, "cancelled")
                    raise KeyboardInterrupt
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM entity"
            ).fetchone()[0], 0)
            store.close()


@unittest.skipUnless(PYSLANG_SITE.is_dir(), "locked PySlang is unavailable")
class G53RuntimeCandidateTests(unittest.TestCase):
    def test_missing_only_fact_evidence_link_blocks_publication(self) -> None:
        for kind in ("module", "design"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / "target.sqlite"
                outsider = root / "other-builder.building"
                outsider.mkdir()
                (outsider / "owned.sqlite").write_bytes(b"other-owner")
                original = module_knowledge._audit_stage1_candidate

                def corrupt_before_audit(store, **kwargs):
                    row = store.connection.execute(
                        "SELECT fact_id FROM fact_evidence GROUP BY fact_id "
                        "HAVING count(*) = 1 ORDER BY fact_id LIMIT 1"
                    ).fetchone()
                    self.assertIsNotNone(row)
                    store.connection.execute(
                        "DELETE FROM fact_evidence WHERE fact_id = ?", (row[0],)
                    )
                    self.assertEqual(store.integrity(), {
                        "integrity_check": "ok", "foreign_key_check": [],
                    })
                    return original(store, **kwargs)

                with mock.patch.object(
                    module_knowledge, "_audit_stage1_candidate",
                    side_effect=corrupt_before_audit,
                ):
                    with self.assertRaisesRegex(RuntimeError, "Stage1 .* audit failed"):
                        _build(kind, output)
                self.assertFalse(output.exists())
                self.assertEqual((outsider / "owned.sqlite").read_bytes(),
                                 b"other-owner")
                self.assertEqual(sorted(path.name for path in root.iterdir()),
                                 [outsider.name])

    def test_missing_or_replaced_second_link_blocks_publication(self) -> None:
        for kind in ("module", "design"):
            for replace in (False, True):
                with self.subTest(kind=kind, replace=replace), tempfile.TemporaryDirectory() as directory:
                    output = Path(directory) / "target.sqlite"
                    original = module_knowledge._audit_stage1_candidate

                    def corrupt_before_audit(store, **kwargs):
                        fact = store.connection.execute(
                            "SELECT fact_id FROM fact_evidence GROUP BY fact_id "
                            "HAVING count(*) > 1 ORDER BY fact_id LIMIT 1"
                        ).fetchone()
                        self.assertIsNotNone(fact)
                        link = store.connection.execute(
                            "SELECT evidence_id FROM fact_evidence WHERE fact_id=? "
                            "ORDER BY evidence_id LIMIT 1", (fact[0],)
                        ).fetchone()[0]
                        store.connection.execute(
                            "DELETE FROM fact_evidence WHERE fact_id=? AND evidence_id=?",
                            (fact[0], link),
                        )
                        if replace:
                            other = store.connection.execute(
                                "SELECT evidence_id FROM evidence WHERE evidence_id "
                                "NOT IN (SELECT evidence_id FROM fact_evidence WHERE fact_id=?) "
                                "AND evidence_id != ? ORDER BY evidence_id LIMIT 1",
                                (fact[0], link),
                            ).fetchone()[0]
                            store.connection.execute(
                                "INSERT INTO fact_evidence(fact_id,evidence_id) VALUES (?,?)",
                                (fact[0], other),
                            )
                        self.assertEqual(store.integrity(), {
                            "integrity_check": "ok", "foreign_key_check": [],
                        })
                        self.assertEqual(store.connection.execute(
                            "SELECT count(*) FROM fact_evidence WHERE fact_id=?",
                            (fact[0],),
                        ).fetchone()[0], 2 if replace else 1)
                        return original(store, **kwargs)

                    with mock.patch.object(
                        module_knowledge, "_audit_stage1_candidate",
                        side_effect=corrupt_before_audit,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "Stage1 .* audit failed"):
                            _build(kind, output)
                    self.assertFalse(output.exists())
                    self.assertEqual(list(output.parent.iterdir()), [])

    def test_v02_builds_preserve_stage1_rows_and_scope(self) -> None:
        for kind in ("module", "design"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                old = root / "old.sqlite"
                new = root / "new.sqlite"
                _build(kind, old, schema_version="v0.1")
                report = _build(kind, new)
                left = prototype.PrototypeStore.open(old, readonly=True)
                right = prototype.PrototypeStore.open(new, readonly=True)
                try:
                    self.assertEqual(right.schema_version, "v0.2")
                    for table in ("entity", "fact", "evidence", "fact_evidence"):
                        self.assertEqual(
                            [tuple(row) for row in left.connection.execute(
                                f"SELECT * FROM {table} ORDER BY 1")],
                            [tuple(row) for row in right.connection.execute(
                                f"SELECT * FROM {table} ORDER BY 1")],
                        )
                    self.assertEqual(right.integrity(), {
                        "integrity_check": "ok", "foreign_key_check": []
                    })
                    self.assertEqual(right.analysis_scope(), report["analysis_scope"])
                    self.assertEqual(right.analysis_scope()["completeness"]["semantic"],
                                     "NOT_BUILT")
                    self.assertFalse(list(right.connection.execute(
                        "SELECT * FROM ai_knowledge")))
                finally:
                    left.close()
                    right.close()

    def test_both_builders_reject_late_target_without_overwrite(self) -> None:
        for kind in ("module", "design"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "target.sqlite"
                original = module_knowledge._link_candidate

                def late_target(source, destination):
                    destination.write_bytes(b"other-owner")
                    return original(source, destination)

                with mock.patch.object(module_knowledge, "_link_candidate",
                                       side_effect=late_target):
                    with self.assertRaises(FileExistsError):
                        _build(kind, output)
                self.assertEqual(output.read_bytes(), b"other-owner")
                self.assertEqual(sorted(path.name for path in output.parent.iterdir()),
                                 ["target.sqlite"])

    def test_both_builders_keep_linked_target_when_directory_sync_fails(self) -> None:
        for kind in ("module", "design"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "target.sqlite"
                original = module_knowledge._fsync_directory

                def fail_final_parent(path):
                    if path == output.parent and output.exists():
                        raise OSError("directory sync failure")
                    return original(path)

                with mock.patch.object(module_knowledge, "_fsync_directory",
                                       side_effect=fail_final_parent):
                    with self.assertRaisesRegex(OSError, "directory sync failure"):
                        _build(kind, output)
                self.assertTrue(output.is_file())
                store = prototype.PrototypeStore.open(output, readonly=True)
                try:
                    self.assertEqual(store.integrity()["integrity_check"], "ok")
                finally:
                    store.close()

    def test_both_builders_fail_closed_before_link_and_keep_existing_targets(self) -> None:
        for kind in ("module", "design"):
            for operation in ("audit", "file-fsync", "link"):
                with self.subTest(kind=kind, operation=operation), tempfile.TemporaryDirectory() as directory:
                    output = Path(directory) / "target.sqlite"
                    patched = {
                        "audit": ("_audit_stage1_candidate", RuntimeError("audit rejected")),
                        "file-fsync": ("_fsync_file", OSError("file fsync rejected")),
                        "link": ("_link_candidate", OSError("link rejected")),
                    }
                    name, error = patched[operation]
                    with mock.patch.object(module_knowledge, name, side_effect=error):
                        with self.assertRaises(type(error)):
                            _build(kind, output)
                    self.assertFalse(output.exists())
                    self.assertEqual(list(output.parent.iterdir()), [])
            with self.subTest(kind=kind, operation="preexisting"), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "target.sqlite"
                output.write_bytes(b"preexisting")
                with self.assertRaisesRegex(RuntimeError, "already exists"):
                    _build(kind, output)
                self.assertEqual(output.read_bytes(), b"preexisting")

    def test_combination_profile_uses_existing_prelink_cleanup_and_exclusive_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "combination.sqlite"
            with mock.patch.object(module_knowledge, "_publish_staged_candidate",
                                   side_effect=RuntimeError("injected prelink failure")):
                with self.assertRaisesRegex(RuntimeError, "injected prelink failure"):
                    _build("design", output, publication_contract=prototype.Q2_FIFO_PUBLICATION_PATH)
            self.assertFalse(output.exists())
            self.assertEqual(list(output.parent.iterdir()), [])
            result = _build("design", output, publication_contract=prototype.Q2_FIFO_PUBLICATION_PATH)
            self.assertTrue(output.is_file())
            self.assertEqual(result["analysis_scope"]["profile"],
                             {"id": "rtl-analysis-agent/ibex-q2-fifo", "version": "1"})
            original = output.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                _build("design", output, publication_contract=prototype.Q2_FIFO_PUBLICATION_PATH)
            self.assertEqual(output.read_bytes(), original)

    def test_both_builders_compete_without_cross_cleanup(self) -> None:
        context = multiprocessing.get_context("fork")
        for kind in ("module", "design"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "target.sqlite"
                barrier = context.Barrier(2)
                results = context.Queue()
                processes = [context.Process(
                    target=_contending_build,
                    args=(kind, str(output), barrier, results),
                ) for _ in range(2)]
                for process in processes:
                    process.start()
                for process in processes:
                    process.join(timeout=60)
                    self.assertFalse(process.is_alive(), "builder process hung")
                    self.assertEqual(process.exitcode, 0)
                outcomes = [results.get(timeout=5) for _ in processes]
                self.assertEqual(sorted(item[0] for item in outcomes), ["error", "ok"])
                self.assertTrue(output.is_file())
                store = prototype.PrototypeStore.open(output, readonly=True)
                self.assertEqual(store.integrity()["integrity_check"], "ok")
                store.close()
                self.assertEqual(sorted(path.name for path in output.parent.iterdir()),
                                 ["target.sqlite"])
