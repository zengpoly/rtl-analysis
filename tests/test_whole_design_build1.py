#!/usr/bin/env python3
"""Whole-design hierarchy projection tests that do not need a compiler."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/rtl-pack/lib"))

import rtl_module_knowledge as module_knowledge  # noqa: E402


class FakeDefinition:
    def __init__(self, name: str):
        self.name = name


class FakeBody(list):
    def __init__(
        self,
        children: list[object] | None = None,
        parameters: list[object] | None = None,
    ):
        super().__init__(children or [])
        self.parameters = parameters or []


class FakeInstance:
    kind = "Instance"
    isUninstantiated = False

    def __init__(
        self,
        path: str,
        children: list[object] | None = None,
        parameters: list[object] | None = None,
        connections: list[object] | None = None,
        definition: str | None = None,
    ):
        self.hierarchicalPath = path
        self.name = path.rsplit(".", 1)[-1]
        self.definition = FakeDefinition(
            definition or ("leaf" if ".u" in path else "top")
        )
        self.body = FakeBody(children, parameters)
        self.portConnections = connections or []


class FakeParameter:
    def __init__(self, name: str):
        self.name = name


class FakePort:
    def __init__(self, name: str):
        self.name = name
        self.direction = "In"
        self.type = None
        self.internalSymbol = None
        self.internalExpr = None


class FakeConnection:
    def __init__(self, name: str):
        self.port = FakePort(name)
        self.ifaceConn = ()
        self.expression = None


class FakeGenerate(list):
    kind = "GenerateBlock"
    isUninstantiated = False

    def __init__(self, path: str, children: list[object]):
        super().__init__(children)
        self.hierarchicalPath = path
        self.name = path.rsplit(".", 1)[-1]


class FakeRoot:
    def __init__(self, tops: list[FakeInstance]):
        self.topInstances = tops


class WholeDesignHierarchyTest(unittest.TestCase):
    @staticmethod
    def _exact_root_session(
        *, root_parameters=0, child_parameters=0, root_bindings=0, bindings=0
    ):
        root_path = "top"
        child_path = "top.u_large"
        child = FakeInstance(
            child_path,
            parameters=[FakeParameter(f"CP{index:03d}") for index in range(child_parameters)],
            connections=[FakeConnection(f"port_{index:03d}") for index in range(bindings)],
            definition="large_leaf",
        )
        root = FakeInstance(
            root_path,
            children=[child],
            parameters=[FakeParameter(f"RP{index:03d}") for index in range(root_parameters)],
            connections=[
                FakeConnection(f"root_port_{index:03d}")
                for index in range(root_bindings)
            ],
            definition="top",
        )

        class Session:
            def __init__(self):
                self.native = {root_path: root, child_path: child}

            def _lookup_exact(self, path):
                return self.native.get(path)

            def _symbol_record(self, instance, *, register_handle):
                if register_handle:
                    raise AssertionError("Build1 projection must not register handles")
                return {
                    "kind": "Instance",
                    "name": instance.name,
                    "path": instance.hierarchicalPath,
                    "definition": instance.definition.name,
                }

            def _parameter_override_map(self, instance):
                return {}, False

            def _parameter_record(self, parameter, override):
                return {"name": parameter.name, "value": parameter.name}

            def _connection_syntax_map(self, instance, connections):
                return {}, False

            def _connection_classification(self, connection, syntax, interface):
                return "unconnected", "none"

            def _type_record(self, value):
                return None

            def _object_identity(self, value, *, register_handle):
                return None

            def _expression_record(self, value, *, register_handle):
                return None

            def _evidence_for(self, value):
                return None

            def query(self, request):
                operation = request["op"]
                path = request.get("path") or request.get("instance")
                if operation == "scope":
                    records = []
                    if request["kind"] == "Instance" and path == root_path:
                        records = [{
                            "kind": "Instance", "name": child.name,
                            "path": child_path, "definition": child.definition.name,
                            "active": True,
                        }]
                    return {
                        "status": "ok", "completeness": "complete",
                        "truncated": False, "records": records,
                    }
                if operation == "inspect":
                    instance = self.native[path]
                    parameters = list(instance.body.parameters)[:32]
                    return {
                        "status": "ok",
                        "record": {
                            **self._symbol_record(instance, register_handle=False),
                            "parameters": [
                                self._parameter_record(parameter, None)
                                for parameter in parameters
                            ],
                            "parameters_truncated": len(instance.body.parameters) > 32,
                        },
                    }
                if operation == "bindings":
                    return {
                        "status": "ok", "completeness": "complete",
                        "records": [
                            {"formal": item.port.name}
                            for item in self.native[path].portConnections[:request["limit"]]
                        ],
                    }
                raise AssertionError(request)

        return Session(), root, child

    def test_exact_root_target_keeps_more_than_32_parameters(self) -> None:
        session, _, _ = self._exact_root_session(root_parameters=421)
        snapshot = module_knowledge.collect_module_scope(session, "top")
        self.assertEqual(len(snapshot["target"]["parameters"]), 421)
        self.assertFalse(snapshot["target"]["parameters_truncated"])

    def test_exact_root_target_keeps_more_than_100_bindings(self) -> None:
        session, _, _ = self._exact_root_session(root_bindings=128)
        snapshot = module_knowledge.collect_module_scope(session, "top")
        self.assertEqual(len(snapshot["target_bindings"]), 128)

    def test_exact_root_child_keeps_more_than_32_parameters(self) -> None:
        session, _, _ = self._exact_root_session(child_parameters=421)
        snapshot = module_knowledge.collect_module_scope(session, "top")
        child = snapshot["children"][0]["inspection"]
        self.assertEqual(len(child["parameters"]), 421)
        self.assertFalse(child["parameters_truncated"])

    def test_exact_root_child_keeps_more_than_100_bindings(self) -> None:
        session, _, _ = self._exact_root_session(bindings=128)
        snapshot = module_knowledge.collect_module_scope(session, "top")
        self.assertEqual(len(snapshot["children"][0]["bindings"]), 128)

    def test_exact_native_resolution_fails_closed_without_suffix_fallback(self) -> None:
        session, root, _ = self._exact_root_session()
        self.assertIs(module_knowledge._resolve_exact_native_instance(session, "top"), root)
        with self.assertRaisesRegex(RuntimeError, "exact native instance"):
            module_knowledge._resolve_exact_native_instance(session, "missing.top")

        session.native["top"] = FakeGenerate("top", [])
        with self.assertRaisesRegex(RuntimeError, "exact native instance"):
            module_knowledge._resolve_exact_native_instance(session, "top")

        mismatched = FakeInstance("other.top")
        session.native["top"] = mismatched
        with self.assertRaisesRegex(RuntimeError, "exact native instance"):
            module_knowledge._resolve_exact_native_instance(session, "top")

        mismatched.hierarchicalPath = "top"
        mismatched.isUninstantiated = True
        with self.assertRaisesRegex(RuntimeError, "exact native instance"):
            module_knowledge._resolve_exact_native_instance(session, "top")

    def test_whole_projection_keeps_all_effective_parameters_and_fails_budget(self) -> None:
        class Parameter:
            def __init__(self, name: str):
                self.name = name

        class Session:
            def _symbol_record(self, instance: object, *, register_handle: bool) -> dict[str, object]:
                self.assert_handle = register_handle
                return {"kind": "Instance", "path": instance.hierarchicalPath}

            def _parameter_override_map(self, instance: object) -> tuple[dict[str, object], bool]:
                return {}, False

            def _parameter_record(self, parameter: object, override: object) -> dict[str, object]:
                return {"name": parameter.name, "value": parameter.name}

        parameters = [Parameter(f"P{index}") for index in range(421)]
        session = Session()
        record = module_knowledge._project_instance_record(
            session, FakeInstance("top", parameters=parameters)
        )
        self.assertEqual(len(record["parameters"]), 421)
        self.assertFalse(record["parameters_truncated"])
        self.assertFalse(session.assert_handle)

        original = module_knowledge.DESIGN_PARAMETER_BUDGET
        try:
            module_knowledge.DESIGN_PARAMETER_BUDGET = 420
            with self.assertRaisesRegex(RuntimeError, "parameter budget exhausted"):
                module_knowledge._project_instance_record(
                    session, FakeInstance("top", parameters=parameters)
                )
        finally:
            module_knowledge.DESIGN_PARAMETER_BUDGET = original

    def test_native_binding_projection_fails_closed_at_design_budget(self) -> None:
        session, _, child = self._exact_root_session(bindings=3)
        original = module_knowledge.DESIGN_CONNECTION_BUDGET
        try:
            module_knowledge.DESIGN_CONNECTION_BUDGET = 2
            with self.assertRaisesRegex(RuntimeError, "connection budget exhausted"):
                module_knowledge._project_instance_bindings(session, child)
        finally:
            module_knowledge.DESIGN_CONNECTION_BUDGET = original

    def test_expression_projection_does_not_duplicate_ast_or_evidence(self) -> None:
        compact = module_knowledge._compact_expression_record({
            "kind": "Assignment",
            "text": "a | b",
            "text_truncated": False,
            "structure_complete": False,
            "nodes": [{"kind": "Assignment"}, {"kind": "NamedValue"}],
            "evidence": {"path": "rtl/top.sv", "line_start": 4},
            "type": {
                "kind": "ScalarType", "text": "logic", "bit_width": 1,
                "signed": False, "four_state": True, "packed": True,
                "extra": "drop",
            },
            "references": [
                {"kind": "Net", "path": "top.b", "evidence": {"line_start": 4}},
                {"kind": "Net", "path": "top.a", "evidence": {"line_start": 4}},
                {"kind": "Net", "path": "top.a", "evidence": {"line_start": 4}},
            ],
        })
        self.assertEqual(compact["text"], "a | b")
        self.assertNotIn("nodes", compact)
        self.assertNotIn("evidence", compact)
        self.assertEqual(
            compact["references"],
            [
                {"kind": "Net", "path": "top.a"},
                {"kind": "Net", "path": "top.b"},
            ],
        )
        self.assertNotIn("extra", compact["type"])

    def test_handle_free_walk_exceeds_interactive_query_budgets(self) -> None:
        children = [FakeInstance(f"top.u{index:04d}") for index in range(5_101)]
        children.append(FakeGenerate("top.g", [FakeInstance("top.g.u_generated")]))
        root = FakeInstance("top", children)
        rows = list(module_knowledge.iter_design_instances(FakeRoot([root])))
        self.assertEqual(len(rows), 5_103)
        self.assertEqual(rows[0]["path"], "top")
        self.assertEqual(rows[0]["parent_path"], None)
        by_path = {row["path"]: row for row in rows}
        self.assertEqual(by_path["top.g.u_generated"]["parent_path"], "top")
        self.assertEqual(by_path["top.g.u_generated"]["depth"], 1)
        self.assertNotIn("top.g", by_path)

    def test_walk_rejects_duplicate_actual_identity(self) -> None:
        child = FakeInstance("top.u")
        root = FakeInstance("top", [child, child])
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            list(module_knowledge.iter_design_instances(FakeRoot([root])))


if __name__ == "__main__":
    unittest.main()
