#!/usr/bin/env python3
"""Task-local, compile-once PySlang query worker.

This module deliberately projects a small set of native compiler facts.  It is
not a language server, graph, persistent index, or RTL answer engine.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Iterator


BUILD_SCHEMA = "rtl-analysis-agent/runtime-query-build-v1"
RESULT_SCHEMA = "rtl-analysis-agent/runtime-query-result-v1"
SESSION_SCHEMA = "rtl-analysis-agent/runtime-query-session-v1"
MAX_RESULTS = 100
MAX_VISITED_NODES = 50_000
MAX_DIAGNOSTICS = 20
MAX_EXCERPT_BYTES = 1_024
MAX_EXPRESSION_NODES = 64
MAX_NESTED_RESULTS = 32
MAX_HANDLES = 5_000
MAX_OUTPUT_BYTES = 512_000
MAX_REQUEST_BYTES = 65_536
MAX_REQUEST_ID_CHARS = 128
MAX_COLLECTION_SCAN = 50_000
MAX_REFERENCE_CHARS = 4_096
MAX_BUILD_ITEMS = 10_000
MAX_TOPS = 32
MAX_DIAGNOSTIC_ARGS = 8
MAX_DIAGNOSTIC_ARG_BYTES = 512
SUPPORTED_OPERATIONS = ("scope", "inspect", "bindings", "uses")
SUPPORTED_USE_MODES = ("drivers", "procedure_reads")


class QueryRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _enum_name(value: Any) -> str:
    text = str(value)
    return text.rsplit(".", 1)[-1]


def _bounded_text(value: Any, byte_limit: int) -> tuple[str, bool]:
    encoded = str(value).encode("utf-8")
    truncated = len(encoded) > byte_limit
    if truncated:
        encoded = encoded[:byte_limit]
    return encoded.decode("utf-8", errors="replace"), truncated


def _take_bounded(values: Any, limit: int) -> tuple[list[Any], bool]:
    result: list[Any] = []
    for value in values:
        if len(result) >= limit:
            return result, True
        result.append(value)
    return result, False


def _safe_relative(root: Path, value: str, label: str, *, require_file: bool) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or "\n" in value
        or len(value) > MAX_REFERENCE_CHARS
    ):
        raise QueryRuntimeError("invalid_build", f"{label} must be a non-empty relative path")
    supplied = Path(value)
    if supplied.is_absolute():
        raise QueryRuntimeError("invalid_build", f"{label} must be relative to source root")
    candidate = (root / supplied).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise QueryRuntimeError("invalid_build", f"{label} escapes source root") from error
    if require_file and not candidate.is_file():
        raise QueryRuntimeError("invalid_build", f"{label} does not exist: {value}")
    if not require_file and not candidate.is_dir():
        raise QueryRuntimeError("invalid_build", f"{label} directory does not exist: {value}")
    return candidate


def _load_build(source_root: Path, build_path: Path) -> dict[str, Any]:
    try:
        spec = json.loads(build_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise QueryRuntimeError("invalid_build", f"cannot read build identity: {error}") from error
    if not isinstance(spec, dict) or spec.get("schema") != BUILD_SCHEMA:
        raise QueryRuntimeError("invalid_build", f"build identity must use {BUILD_SCHEMA}")
    allowed = {
        "schema",
        "files",
        "filelists",
        "include_dirs",
        "defines",
        "tops",
        "single_unit",
        "environment",
        "top_parameters",
    }
    if set(spec) - allowed:
        raise QueryRuntimeError(
            "invalid_build", f"unknown build identity fields: {sorted(set(spec) - allowed)}"
        )
    for field in ("files", "filelists", "include_dirs", "defines", "tops"):
        if not isinstance(spec.get(field), list) or not all(
            isinstance(item, str) for item in spec[field]
        ):
            raise QueryRuntimeError("invalid_build", f"{field} must be a string list")
        if len(spec[field]) > (MAX_TOPS if field == "tops" else MAX_BUILD_ITEMS):
            raise QueryRuntimeError("invalid_build", f"too many {field} entries")
    if not spec["files"] and not spec["filelists"]:
        raise QueryRuntimeError("invalid_build", "at least one source file or filelist is required")
    if not spec["tops"]:
        raise QueryRuntimeError("invalid_build", "at least one top module is required")
    if not isinstance(spec.get("single_unit"), bool):
        raise QueryRuntimeError("invalid_build", "single_unit must be boolean")
    environment = spec.get("environment")
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environment.items()
    ):
        raise QueryRuntimeError("invalid_build", "environment must map names to relative directories")
    parameters = spec.get("top_parameters", {})
    if not isinstance(parameters, dict) or not all(
        isinstance(key, str) and isinstance(value, (str, int, bool))
        for key, value in parameters.items()
    ):
        raise QueryRuntimeError("invalid_build", "top_parameters must map names to scalar values")

    resolved = dict(spec)
    resolved["_files"] = [
        _safe_relative(source_root, item, "source file", require_file=True)
        for item in spec["files"]
    ]
    resolved["_filelists"] = [
        _safe_relative(source_root, item, "filelist", require_file=True)
        for item in spec["filelists"]
    ]
    resolved["_include_dirs"] = [
        _safe_relative(source_root, item, "include directory", require_file=False)
        for item in spec["include_dirs"]
    ]
    resolved["_environment"] = {}
    for name, value in environment.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise QueryRuntimeError("invalid_build", f"invalid environment name: {name}")
        resolved["_environment"][name] = str(
            _safe_relative(source_root, value, f"environment {name}", require_file=False)
        )
    for define in spec["defines"]:
        if len(define) > MAX_REFERENCE_CHARS or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*(?:=.*)?", define
        ):
            raise QueryRuntimeError("invalid_build", f"invalid define: {define}")
    for top in spec["tops"]:
        if len(top) > 256 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", top):
            raise QueryRuntimeError("invalid_build", f"invalid top module: {top}")
    for name in parameters:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
            raise QueryRuntimeError("invalid_build", f"invalid top parameter: {name}")
    return resolved


@contextlib.contextmanager
def _isolated_environment(values: dict[str, str]) -> Iterator[None]:
    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def _import_pyslang(site_packages: Path | None = None) -> Any:
    if site_packages is not None:
        if not site_packages.is_dir():
            raise QueryRuntimeError("pyslang_missing", "locked PySlang site-packages is missing")
        site_text = str(site_packages)
        if site_text not in sys.path:
            sys.path.insert(0, site_text)
    try:
        import pyslang  # type: ignore[import-not-found]
    except ImportError as error:
        raise QueryRuntimeError("pyslang_missing", "PySlang 11.0.0 is not installed") from error
    if getattr(pyslang, "__version__", None) != "11.0.0":
        raise QueryRuntimeError(
            "pyslang_version", f"expected PySlang 11.0.0, got {getattr(pyslang, '__version__', None)}"
        )
    return pyslang


class RuntimeQuerySession:
    """One in-memory PySlang compilation owned by one worker process."""

    def __init__(self) -> None:
        self.closed = False
        self.compilation_count = 1
        self.query_count = 0
        self.total_query_ms = 0.0
        self.analysis_init_ms: float | None = None
        self.analysis_manager: Any = None
        self.handles: dict[str, Any] = {}
        self.object_handles: dict[int, str] = {}
        self.next_handle = 1
        self.source_snapshots: dict[str, dict[str, Any]] = {}
        self.input_snapshots: dict[str, dict[str, Any]] = {}
        self.invalidated = False

    @classmethod
    def open(
        cls,
        *,
        source_root: Path,
        build_path: Path,
        project_name: str,
        source_revision: str,
        pyslang_site: Path | None = None,
    ) -> "RuntimeQuerySession":
        started = time.perf_counter()
        session = cls()
        session.source_root = source_root.resolve()
        if not session.source_root.is_dir():
            raise QueryRuntimeError("invalid_build", "source root does not exist")
        session.build_path = build_path.resolve()
        if (
            not project_name
            or len(project_name) > 256
            or "\n" in project_name
            or not source_revision
            or len(source_revision) > 256
            or "\n" in source_revision
        ):
            raise QueryRuntimeError(
                "invalid_build", "project name and source revision must be bounded strings"
            )
        session.project_name = project_name
        session.source_revision = source_revision
        session.pyslang = _import_pyslang(pyslang_site)
        session.spec = _load_build(session.source_root, session.build_path)
        build_stat = session.build_path.stat()
        session.build_snapshot = {
            "sha256": _sha256_bytes(session.build_path.read_bytes()),
            "mtime_ns": build_stat.st_mtime_ns,
            "ctime_ns": build_stat.st_ctime_ns,
            "size": build_stat.st_size,
        }
        identity_spec = {
            key: value
            for key, value in session.spec.items()
            if not key.startswith("_")
        }
        session.identity = {
            "project": project_name,
            "source_revision": source_revision,
            "build_spec_sha256": _canonical_sha256(identity_spec),
            "top_modules": list(session.spec["tops"]),
            "single_unit": session.spec["single_unit"],
            "backend": "pyslang",
            "backend_version": session.pyslang.__version__,
        }

        from pyslang.driver import CommandLineOptions, Driver

        driver = Driver()
        driver.addStandardArgs()
        arguments = ["slang"]
        if session.spec["single_unit"]:
            arguments.append("--single-unit")
        for top in session.spec["tops"]:
            arguments.extend(("--top", top))
        for name, value in session.spec.get("top_parameters", {}).items():
            rendered = "1" if value is True else "0" if value is False else str(value)
            arguments.extend(("-G", f"{name}={rendered}"))
        for directory in session.spec["_include_dirs"]:
            arguments.extend(("-I", str(directory)))
        for define in session.spec["defines"]:
            arguments.extend(("-D", define))
        for filelist in session.spec["_filelists"]:
            arguments.extend(("-f", str(filelist)))
        arguments.extend(str(path) for path in session.spec["_files"])

        options = CommandLineOptions()
        options.ignoreProgramName = False
        options.expandEnvVars = True
        with _isolated_environment(session.spec["_environment"]):
            if not driver.parseCommandLine(shlex.join(arguments), options):
                raise QueryRuntimeError("compile_failed", "PySlang rejected build arguments")
            if not driver.processOptions():
                raise QueryRuntimeError("compile_failed", "PySlang rejected build options")
            if not driver.parseAllSources():
                raise QueryRuntimeError("compile_failed", "PySlang could not parse all sources")
        session.driver = driver
        session.source_manager = driver.sourceManager
        session.compilation = driver.createCompilation()
        session.root = session.compilation.getRoot()
        closure = session._freeze_input_closure()
        session.identity["source_closure_sha256"] = _canonical_sha256(closure)
        session.identity["effective_input_count"] = len(closure)
        session.identity["build_sha256"] = _canonical_sha256(
            {"build_spec": identity_spec, "source_closure": closure}
        )
        diagnostics, diagnostic_scan_truncated = _take_bounded(
            session.compilation.getAllDiagnostics(), MAX_COLLECTION_SCAN
        )
        error_count = sum(1 for item in diagnostics if item.isError())
        warning_count = len(diagnostics) - error_count
        session.diagnostic_summary = {
            "total": None if diagnostic_scan_truncated else len(diagnostics),
            "total_lower_bound": len(diagnostics)
            + (1 if diagnostic_scan_truncated else 0),
            "errors": error_count,
            "warnings": warning_count,
            "returned": min(len(diagnostics), MAX_DIAGNOSTICS),
        }
        session.diagnostics = [
            session._diagnostic_record(item) for item in diagnostics[:MAX_DIAGNOSTICS]
        ]
        session.diagnostics_truncated = (
            diagnostic_scan_truncated or len(diagnostics) > MAX_DIAGNOSTICS
        )
        if diagnostic_scan_truncated:
            session.close()
            raise QueryRuntimeError(
                "compile_failed",
                "PySlang compilation diagnostics exceeded the runtime work limit",
                {
                    "compilation_identity": session.identity,
                    "diagnostics": session.diagnostics,
                    "diagnostics_truncated": True,
                },
            )
        if error_count:
            session.close()
            raise QueryRuntimeError(
                "compile_failed",
                f"PySlang reported {error_count} compilation error(s)",
                {
                    "compilation_identity": session.identity,
                    "diagnostics": session.diagnostics,
                    "diagnostics_truncated": session.diagnostics_truncated,
                },
            )
        session.compile_ms = round((time.perf_counter() - started) * 1000, 3)
        session._register(session.root, "root")
        for top in session.root.topInstances:
            session._register(top)
        return session

    def __enter__(self) -> "RuntimeQuerySession":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.handles.clear()
        self.object_handles.clear()
        self.source_snapshots.clear()
        self.input_snapshots.clear()
        self.analysis_manager = None
        self.root = None
        self.compilation = None
        self.driver = None
        self.source_manager = None

    def ready_result(self) -> dict[str, Any]:
        return {
            "schema": SESSION_SCHEMA,
            "status": "ready",
            "operations": list(SUPPORTED_OPERATIONS),
            "compilation_identity": self.identity,
            "diagnostic_summary": self.diagnostic_summary,
            "diagnostics": self.diagnostics,
            "diagnostics_truncated": self.diagnostics_truncated,
            "telemetry": self._telemetry(0.0),
        }

    def _telemetry(self, query_ms: float) -> dict[str, Any]:
        result: dict[str, Any] = {
            "cold_compile_ms": self.compile_ms,
            "query_ms": round(query_ms, 3),
            "total_query_ms": round(self.total_query_ms, 3),
            "query_count": self.query_count,
            "compilation_count": self.compilation_count,
            "session_reused": self.query_count > 1,
        }
        if self.analysis_init_ms is not None:
            result["analysis_init_ms"] = self.analysis_init_ms
        return result

    def _base(self, request_id: str, operation: str) -> dict[str, Any]:
        safe_request_id, _ = _bounded_text(request_id, MAX_REQUEST_ID_CHARS)
        safe_operation, _ = _bounded_text(operation, 32)
        return {
            "schema": RESULT_SCHEMA,
            "request_id": safe_request_id,
            "operation": safe_operation,
            "compilation_identity": self.identity,
        }

    def query(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.closed:
            raise QueryRuntimeError("session_closed", "query session is closed")
        started = time.perf_counter()
        self.query_count += 1
        request_id = request.get("id")
        operation = request.get("op")
        if self.invalidated:
            result = self._base(str(request_id or "invalid"), str(operation or "unknown"))
            result.update(
                {
                    "status": "stale_input",
                    "message": "source input changed; restart the runtime session",
                    "completeness": "invalidated",
                }
            )
        elif (
            not isinstance(request_id, str)
            or not request_id
            or "\n" in request_id
            or len(request_id) > MAX_REQUEST_ID_CHARS
        ):
            result = self._base("invalid", str(operation or "unknown"))
            result.update(
                {
                    "status": "unsupported",
                    "message": (
                        "request id must be a non-empty string of at most "
                        f"{MAX_REQUEST_ID_CHARS} characters"
                    ),
                }
            )
        elif operation not in SUPPORTED_OPERATIONS:
            result = self._base(request_id, str(operation or "unknown"))
            result.update(
                {
                    "status": "unsupported",
                    "message": f"operation must be one of {SUPPORTED_OPERATIONS}",
                }
            )
        else:
            try:
                self._verify_inputs()
                result = getattr(self, f"_query_{operation}")(request_id, request)
            except QueryRuntimeError as error:
                if error.code == "stale_input":
                    self.invalidated = True
                result = self._base(request_id, operation)
                result.update(
                    {"status": error.code, "message": str(error), **error.details}
                )
            except Exception as error:  # PySlang exceptions are not a stable public hierarchy.
                result = self._base(request_id, operation)
                result.update(
                    {
                        "status": "partial",
                        "message": f"PySlang query failed without inferred fallback: {error}",
                        "completeness": "unknown",
                    }
                )
        query_ms = (time.perf_counter() - started) * 1000
        self.total_query_ms += query_ms
        result["telemetry"] = self._telemetry(query_ms)
        return self._bound_payload(result)

    def _bound_payload(self, result: dict[str, Any]) -> dict[str, Any]:
        records = result.get("records")
        original_omitted = result.get("omitted_count", 0)
        original_lower_bound = result.get("omitted_count_lower_bound", 0)
        removed = 0
        while True:
            encoded = json.dumps(result, sort_keys=True).encode("utf-8")
            if len(encoded) <= MAX_OUTPUT_BYTES:
                return result
            if isinstance(records, list) and records:
                records.pop()
                removed += 1
                result["status"] = "partial"
                result["completeness"] = "truncated"
                result["truncated"] = True
                result["omitted_count"] = (
                    original_omitted + removed
                    if isinstance(original_omitted, int)
                    and not isinstance(original_omitted, bool)
                    else None
                )
                result["omitted_count_lower_bound"] = (
                    original_lower_bound + removed
                    if isinstance(original_lower_bound, int)
                    and not isinstance(original_lower_bound, bool)
                    else removed
                )
                result["message"] = (
                    f"result limited to {MAX_OUTPUT_BYTES} output bytes"
                )
                continue
            bounded = {
                **self._base(
                    str(result.get("request_id", "invalid")),
                    str(result.get("operation", "unknown")),
                ),
                "status": "partial",
                "message": f"result exceeded {MAX_OUTPUT_BYTES} byte output limit",
                "completeness": "truncated",
                "telemetry": result["telemetry"],
            }
            if len(json.dumps(bounded, sort_keys=True).encode("utf-8")) > MAX_OUTPUT_BYTES:
                raise QueryRuntimeError("internal_error", "bounded result metadata is too large")
            return bounded

    def _register(self, value: Any, preferred: str | None = None) -> str:
        key = id(value)
        existing = self.object_handles.get(key)
        if existing is not None:
            return existing
        if len(self.handles) >= MAX_HANDLES:
            raise QueryRuntimeError(
                "partial",
                f"task-local handle limit {MAX_HANDLES} reached",
                {"completeness": "truncated"},
            )
        handle = preferred or f"h{self.next_handle}"
        if preferred is None:
            self.next_handle += 1
        self.handles[handle] = value
        self.object_handles[key] = handle
        return handle

    def _lookup_exact(self, path: str) -> Any:
        if path in self.handles:
            return self.handles[path]
        if path in {"$root", "root"}:
            return self.root
        try:
            return self.root.lookupName(path)
        except Exception:
            return None

    def _resolve(self, reference: Any) -> tuple[str, Any, list[Any]]:
        if (
            not isinstance(reference, str)
            or not reference
            or "\n" in reference
            or len(reference) > MAX_REFERENCE_CHARS
        ):
            raise QueryRuntimeError("unsupported", "query reference must be a non-empty string")
        exact = self._lookup_exact(reference)
        if exact is not None:
            return "exact", exact, []
        matches: list[Any] = []
        visited = 0
        exhausted = False
        visit_action = self.pyslang.ast.VisitAction

        def visitor(node: Any) -> Any:
            nonlocal visited, exhausted
            visited += 1
            if visited > MAX_VISITED_NODES:
                exhausted = True
                return visit_action.Interrupt
            name = getattr(node, "name", None)
            path = getattr(node, "hierarchicalPath", None)
            if name == reference or (
                isinstance(path, str) and path.endswith(f".{reference}")
            ):
                matches.append(node)
                if len(matches) > MAX_RESULTS:
                    exhausted = True
                    return visit_action.Interrupt
            return visit_action.Advance

        self.root.visit(visitor)
        unique: dict[tuple[str, str, str, int], Any] = {}
        for item in matches:
            location = getattr(item, "location", None)
            unique[
                (
                    str(getattr(item, "hierarchicalPath", "")),
                    _enum_name(getattr(item, "kind", "unknown")),
                    str(getattr(location, "buffer", "")) if location else "",
                    int(getattr(location, "offset", 0)) if location else 0,
                )
            ] = item
        ordered = [unique[key] for key in sorted(unique)]
        if exhausted:
            raise QueryRuntimeError(
                "partial",
                "symbol suffix search budget exhausted; use an exact path or bounded scope",
                {"completeness": "truncated"},
            )
        if not ordered:
            return "not_found", None, []
        if len(ordered) != 1:
            return "ambiguous", None, ordered[:MAX_RESULTS]
        return "suffix", ordered[0], []

    def _scope_members(self, value: Any) -> Any:
        if value is self.root:
            return iter(self.root.topInstances)
        body = getattr(value, "body", None)
        if body is not None and hasattr(body, "__iter__"):
            return iter(body)
        if hasattr(value, "__iter__"):
            return iter(value)
        raise QueryRuntimeError("unsupported", "resolved object is not a scope")

    def _query_scope(self, request_id: str, request: dict[str, Any]) -> dict[str, Any]:
        path = request.get("path", "root")
        limit = self._limit(request)
        resolved_by, scope, candidates = self._resolve(path)
        base = self._base(request_id, "scope")
        if scope is None:
            base.update(
                {
                    "status": resolved_by,
                    "candidates": [self._symbol_record(item) for item in candidates],
                }
            )
            return base
        name_filter = request.get("name")
        kind_filter = request.get("kind")
        if name_filter is not None and (
            not isinstance(name_filter, str) or len(name_filter) > MAX_REFERENCE_CHARS
        ):
            raise QueryRuntimeError("unsupported", "scope name filter must be literal text")
        if kind_filter is not None and (
            not isinstance(kind_filter, str) or len(kind_filter) > 128
        ):
            raise QueryRuntimeError("unsupported", "scope kind filter must be literal text")
        native_members, scan_truncated = _take_bounded(
            self._scope_members(scope), MAX_COLLECTION_SCAN
        )
        members = []
        for item in native_members:
            if name_filter is not None and getattr(item, "name", None) != name_filter:
                continue
            if kind_filter is not None and _enum_name(getattr(item, "kind", "")) != kind_filter:
                continue
            members.append(item)
        members.sort(key=self._symbol_sort_key)
        records = [self._symbol_record(item) for item in members[:limit]]
        result_truncated = scan_truncated or len(members) > limit
        base.update(
            {
                "status": "partial" if scan_truncated else "ok",
                "resolved_by": resolved_by,
                "scope": self._object_identity(scope),
                "records": records,
                "completeness": "truncated" if result_truncated else "complete",
                "truncated": result_truncated,
                "omitted_count": None
                if scan_truncated
                else max(0, len(members) - limit),
                "omitted_count_lower_bound": max(1, len(members) - limit + 1)
                if scan_truncated
                else max(0, len(members) - limit),
            }
        )
        return base

    def _query_inspect(self, request_id: str, request: dict[str, Any]) -> dict[str, Any]:
        resolved_by, value, candidates = self._resolve(request.get("path"))
        base = self._base(request_id, "inspect")
        if value is None:
            base.update(
                {
                    "status": resolved_by,
                    "candidates": [self._symbol_record(item) for item in candidates],
                }
            )
            return base
        base.update(
            {
                "status": "ok",
                "resolved_by": resolved_by,
                "record": self._inspect_record(value),
                "completeness": "native-local",
                "unsupported": [
                    "semantic module summary",
                    "effective update guard",
                    "next-state equation",
                    "reset or priority proof",
                ],
            }
        )
        return base

    def _connection_syntax_map(
        self, instance: Any, connections: list[Any]
    ) -> tuple[dict[str, Any], bool]:
        syntax = getattr(instance, "syntax", None)
        syntax_items, syntax_truncated = _take_bounded(
            getattr(syntax, "connections", ()), MAX_COLLECTION_SCAN * 2
        )
        nodes = []
        for node in syntax_items:
            kind = _enum_name(getattr(node, "kind", ""))
            if kind in {
                "NamedPortConnection",
                "OrderedPortConnection",
                "EmptyPortConnection",
                "WildcardPortConnection",
            }:
                nodes.append(node)
        named = {
            str(node.name): node
            for node in nodes
            if _enum_name(getattr(node, "kind", "")) == "NamedPortConnection"
        }
        wildcard = next(
            (
                node
                for node in nodes
                if _enum_name(getattr(node, "kind", "")) == "WildcardPortConnection"
            ),
            None,
        )
        if named or wildcard is not None:
            mapped = dict(named)
            if wildcard is not None:
                for connection in connections:
                    mapped.setdefault(connection.port.name, wildcard)
            return mapped, syntax_truncated
        return (
            {
                connection.port.name: node
                for connection, node in zip(connections, nodes, strict=False)
            },
            syntax_truncated,
        )

    def _connection_classification(
        self, connection: Any, syntax: Any | None, interface: bool
    ) -> tuple[str, str]:
        if interface:
            return "interface", "interface"
        expression = connection.expression
        if syntax is None:
            if expression is not None:
                return "defaulted", "formal_default"
            return "missing", "omitted"
        kind = _enum_name(getattr(syntax, "kind", ""))
        if kind == "EmptyPortConnection":
            return "explicit_open", "ordered_open"
        if kind == "NamedPortConnection":
            has_parentheses = bool(str(getattr(syntax, "openParen", "")))
            if has_parentheses and getattr(syntax, "expr", None) is None:
                return "explicit_open", "named_open"
            if not has_parentheses:
                return "connected", "implicit_named"
            return "connected", "named"
        if kind == "WildcardPortConnection":
            return "connected", "wildcard"
        return "connected", "ordered"

    def _query_bindings(self, request_id: str, request: dict[str, Any]) -> dict[str, Any]:
        limit = self._limit(request)
        resolved_by, instance, candidates = self._resolve(request.get("instance"))
        base = self._base(request_id, "bindings")
        if instance is None:
            base.update(
                {
                    "status": resolved_by,
                    "candidates": [self._symbol_record(item) for item in candidates],
                }
            )
            return base
        if _enum_name(getattr(instance, "kind", "")) != "Instance":
            raise QueryRuntimeError("unsupported", "bindings requires an elaborated module instance")
        port_filter = request.get("port")
        if port_filter is not None and (
            not isinstance(port_filter, str) or len(port_filter) > MAX_REFERENCE_CHARS
        ):
            raise QueryRuntimeError("unsupported", "port must be an exact literal name")
        records = []
        unsupported = []
        native_connections, scan_truncated = _take_bounded(
            instance.portConnections, MAX_COLLECTION_SCAN
        )
        connections = [
            connection
            for connection in native_connections
            if port_filter is None or connection.port.name == port_filter
        ]
        connection_syntax, syntax_truncated = self._connection_syntax_map(
            instance, native_connections
        )
        scan_truncated = scan_truncated or syntax_truncated
        connections.sort(key=lambda item: item.port.name)
        for connection in connections[:limit]:
            port = connection.port
            iface = tuple(item for item in connection.ifaceConn if item is not None)
            if iface:
                unsupported.append(f"interface connection for {port.name}")
            expression = connection.expression
            actual = self._expression_record(expression) if expression is not None else None
            internal_expression = getattr(port, "internalExpr", None)
            syntax = connection_syntax.get(port.name)
            connection_status, connection_origin = self._connection_classification(
                connection, syntax, bool(iface)
            )
            records.append(
                {
                    "relation": "port_binding",
                    "instance": instance.hierarchicalPath,
                    "formal": port.name,
                    "direction": _enum_name(getattr(port, "direction", "unknown")),
                    "formal_type": self._type_record(getattr(port, "type", None)),
                    "internal_endpoint": self._object_identity(
                        getattr(port, "internalSymbol", None)
                    ),
                    "internal_expression": self._expression_record(internal_expression)
                    if internal_expression is not None
                    else None,
                    "connection_status": connection_status,
                    "connection_origin": connection_origin,
                    "actual": actual,
                    "connection_evidence": self._evidence(
                        getattr(syntax, "sourceRange", None)
                    )
                    if syntax is not None
                    else None,
                    "default_evidence": self._evidence(
                        getattr(expression, "sourceRange", None)
                    )
                    if connection_status == "defaulted"
                    else None,
                    "formal_evidence": self._evidence_for(port),
                }
            )
        truncated = scan_truncated or len(connections) > limit
        base.update(
            {
                "status": "partial" if unsupported or scan_truncated else "ok",
                "resolved_by": resolved_by,
                "instance": self._symbol_record(instance),
                "records": records[:limit],
                "completeness": "partial" if unsupported else "truncated" if truncated else "complete",
                "truncated": truncated,
                "omitted_count": None
                if scan_truncated
                else max(0, len(connections) - limit),
                "omitted_count_lower_bound": max(1, len(connections) - limit + 1)
                if scan_truncated
                else max(0, len(connections) - limit),
                "unsupported": sorted(unsupported),
                "boundary": "one immediate elaborated instance boundary only",
            }
        )
        return base

    def _query_uses(self, request_id: str, request: dict[str, Any]) -> dict[str, Any]:
        mode = request.get("mode")
        limit = self._limit(request)
        base = self._base(request_id, "uses")
        if mode not in SUPPORTED_USE_MODES:
            base.update(
                {
                    "status": "unsupported",
                    "message": f"uses mode must be one of {SUPPORTED_USE_MODES}",
                    "unsupported": [
                        "general fanout",
                        "transitive dependency cone",
                        "interprocedural side effects",
                    ],
                }
            )
            return base
        resolved_by, value, candidates = self._resolve(request.get("path"))
        if value is None:
            base.update(
                {
                    "status": resolved_by,
                    "candidates": [self._symbol_record(item) for item in candidates],
                }
            )
            return base
        manager = self._analysis()
        if mode == "drivers":
            try:
                drivers, scan_truncated = _take_bounded(
                    manager.getDrivers(value), MAX_COLLECTION_SCAN
                )
            except TypeError as error:
                raise QueryRuntimeError(
                    "unsupported", "native driver query requires a value symbol"
                ) from error
            drivers.sort(key=lambda item: self._native_sort_key(item.sourceRange))
            records = [self._driver_record(item) for item in drivers[:limit]]
            truncated = scan_truncated or len(drivers) > limit
            base.update(
                {
                    "status": "partial" if scan_truncated else "ok",
                    "resolved_by": resolved_by,
                    "subject": self._symbol_record(value),
                    "records": records,
                    "completeness": "truncated"
                    if truncated
                    else "native-driver-analysis",
                    "truncated": truncated,
                    "omitted_count": None
                    if scan_truncated
                    else max(0, len(drivers) - limit),
                    "omitted_count_lower_bound": max(1, len(drivers) - limit + 1)
                    if scan_truncated
                    else max(0, len(drivers) - limit),
                    "unsupported": [
                        "effective guard",
                        "normalized next value",
                        "assignment operator (blocking vs nonblocking)",
                        "clock-domain equivalence",
                    ],
                    **self._analysis_diagnostic_fields(),
                }
            )
            return base
        analysis_scope = (
            value.body
            if _enum_name(getattr(value, "kind", "")) == "Instance"
            else value
        )
        try:
            analyzed_scope = manager.getAnalyzedScope(analysis_scope)
        except TypeError as error:
            raise QueryRuntimeError(
                "unsupported", "procedure_reads requires an exact scope handle"
            ) from error
        procedures, scan_truncated = _take_bounded(
            analyzed_scope.procedures, MAX_COLLECTION_SCAN
        )
        procedures.sort(
            key=lambda item: self._native_sort_key(
                getattr(item.analyzedSymbol, "location", None)
            )
        )
        records = [self._procedure_record(item) for item in procedures[:limit]]
        truncated = scan_truncated or len(procedures) > limit
        base.update(
            {
                "status": "partial" if scan_truncated else "ok",
                "resolved_by": resolved_by,
                "subject": self._object_identity(value),
                "records": records,
                "completeness": "truncated"
                if truncated
                else "native-procedure-analysis",
                "truncated": truncated,
                "omitted_count": None
                if scan_truncated
                else max(0, len(procedures) - limit),
                "omitted_count_lower_bound": max(1, len(procedures) - limit + 1)
                if scan_truncated
                else max(0, len(procedures) - limit),
                "boundary": "read sets and timing facts are procedure-local, not a temporal proof",
                **self._analysis_diagnostic_fields(),
            }
        )
        return base

    def _limit(self, request: dict[str, Any]) -> int:
        limit = request.get("limit", 50)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_RESULTS:
            raise QueryRuntimeError("unsupported", f"limit must be 1..{MAX_RESULTS}")
        return limit

    def _analysis(self) -> Any:
        if self.analysis_manager is None:
            started = time.perf_counter()
            from pyslang.analysis import AnalysisManager

            manager = AnalysisManager()
            manager.analyze(self.compilation)
            self.analysis_manager = manager
            diagnostics, diagnostic_scan_truncated = _take_bounded(
                manager.getDiagnostics(), MAX_COLLECTION_SCAN
            )
            error_count = sum(1 for item in diagnostics if item.isError())
            self.analysis_diagnostic_summary = {
                "total": None if diagnostic_scan_truncated else len(diagnostics),
                "total_lower_bound": len(diagnostics)
                + (1 if diagnostic_scan_truncated else 0),
                "errors": error_count,
                "warnings": len(diagnostics) - error_count,
                "returned": min(len(diagnostics), MAX_DIAGNOSTICS),
            }
            self.analysis_diagnostics = [
                self._diagnostic_record(item) for item in diagnostics[:MAX_DIAGNOSTICS]
            ]
            self.analysis_diagnostics_truncated = (
                diagnostic_scan_truncated or len(diagnostics) > MAX_DIAGNOSTICS
            )
            self.analysis_init_ms = round((time.perf_counter() - started) * 1000, 3)
        return self.analysis_manager

    def _analysis_diagnostic_fields(self) -> dict[str, Any]:
        return {
            "analysis_diagnostic_summary": self.analysis_diagnostic_summary,
            "analysis_diagnostics": self.analysis_diagnostics,
            "analysis_diagnostics_truncated": self.analysis_diagnostics_truncated,
        }

    def _native_sort_key(self, value: Any) -> tuple[str, int]:
        source_range = self._as_range(value)
        if source_range is None:
            return ("", 0)
        return (str(source_range.start.buffer), source_range.start.offset)

    def _object_identity(
        self, value: Any, *, register_handle: bool = True
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if hasattr(value, "kind") and hasattr(value, "name"):
            record = {
                "kind": _enum_name(value.kind),
                "name": value.name,
                "path": getattr(value, "hierarchicalPath", ""),
            }
            if register_handle:
                record["handle"] = self._register(value)
            return record
        record = {"kind": type(value).__name__}
        if register_handle:
            record["handle"] = self._register(value)
        return record

    def _symbol_sort_key(self, value: Any) -> tuple[str, str, int]:
        location = getattr(value, "location", None)
        return (
            str(getattr(value, "hierarchicalPath", "")),
            _enum_name(getattr(value, "kind", "unknown")),
            int(getattr(location, "offset", 0)) if location else 0,
        )

    def _symbol_record(
        self, symbol: Any, *, register_handle: bool = True
    ) -> dict[str, Any]:
        record = self._object_identity(symbol, register_handle=register_handle) or {}
        record["type"] = str(getattr(symbol, "type", "")) or None
        record["evidence"] = self._evidence_for(symbol)
        if hasattr(symbol, "direction"):
            record["direction"] = _enum_name(symbol.direction)
        if _enum_name(getattr(symbol, "kind", "")) == "Instance":
            record["definition"] = symbol.definition.name
            record["definition_evidence"] = self._evidence_for(symbol.definition)
        if hasattr(symbol, "isUninstantiated"):
            record["active"] = not bool(symbol.isUninstantiated)
        if hasattr(symbol, "arrayIndex"):
            record["array_index"] = str(symbol.arrayIndex)
        return record

    def _inspect_record(self, value: Any) -> dict[str, Any]:
        if hasattr(value, "kind") and hasattr(value, "name"):
            record = self._symbol_record(value)
            kind = record["kind"]
            if kind == "Instance":
                parameters, parameters_truncated = _take_bounded(
                    value.body.parameters, MAX_NESTED_RESULTS
                )
                parameter_overrides, overrides_truncated = self._parameter_override_map(value)
                record["parameters"] = [
                    self._parameter_record(
                        parameter, parameter_overrides.get(str(parameter.name))
                    )
                    for parameter in parameters
                ]
                record["parameters_truncated"] = (
                    parameters_truncated or overrides_truncated
                )
            if kind == "ProceduralBlock":
                record["procedure_kind"] = _enum_name(value.procedureKind)
                record["timing"] = self._timing_record(getattr(value.body, "timing", None))
                record["body_evidence"] = self._evidence(getattr(value.body, "sourceRange", None))
            return record
        if hasattr(value, "sourceRange"):
            return self._expression_record(value)
        return {"handle": self._register(value), "kind": type(value).__name__}

    def _parameter_override_map(self, instance: Any) -> tuple[dict[str, Any], bool]:
        syntax = getattr(instance, "syntax", None)
        instantiation = getattr(syntax, "parent", None)
        assignment_list = getattr(instantiation, "parameters", None)
        assignments, truncated = _take_bounded(
            getattr(assignment_list, "parameters", ()), MAX_COLLECTION_SCAN
        )
        result: dict[str, Any] = {}
        ambiguous: set[str] = set()
        for assignment in assignments:
            if _enum_name(getattr(assignment, "kind", "")) != "NamedParamAssignment":
                continue
            name = str(getattr(assignment, "name", ""))
            if not name or name in result:
                ambiguous.add(name)
                continue
            result[name] = assignment
        for name in ambiguous:
            result.pop(name, None)
        return result, truncated

    def _parameter_record(
        self, parameter: Any, override_syntax: Any | None = None
    ) -> dict[str, Any]:
        kind = _enum_name(getattr(parameter, "kind", "unknown"))
        declaration_evidence = self._evidence_for(parameter)
        syntax = getattr(parameter, "syntax", None)
        initializer = getattr(syntax, "initializer", None)
        default_expression = getattr(initializer, "expr", None)
        default = None
        if default_expression is not None:
            default_text, default_truncated = _bounded_text(
                self._source_text(default_expression.sourceRange).strip(),
                MAX_EXCERPT_BYTES,
            )
            default = {
                "text": default_text,
                "text_truncated": default_truncated,
                "evidence": self._evidence(default_expression.sourceRange),
            }
        override = None
        if override_syntax is not None:
            expression = getattr(override_syntax, "expr", None)
            if expression is not None:
                expression_text, expression_truncated = _bounded_text(
                    self._source_text(expression.sourceRange).strip(),
                    MAX_EXCERPT_BYTES,
                )
                override = {
                    "binding_kind": "named",
                    "formal_name": str(getattr(override_syntax, "name", "")),
                    "expression_text": expression_text,
                    "text_truncated": expression_truncated,
                    "evidence": self._evidence(override_syntax.sourceRange),
                }
        overridden = bool(getattr(parameter, "isOverridden", False))
        default_complete = default_expression is None or (
            default is not None and default["evidence"] is not None
        )
        record: dict[str, Any] = {
            "name": parameter.name,
            "kind": kind,
            "overridden": overridden,
            "evidence": declaration_evidence,
            "declaration_evidence": declaration_evidence,
            "default": default,
            "override": override,
            "provenance_complete": bool(
                declaration_evidence is not None
                and default_complete
                and (not overridden or override is not None)
            ),
        }
        if kind == "TypeParameter":
            record["target_type"] = self._type_record(
                getattr(getattr(parameter, "targetType", None), "type", None)
            )
        else:
            record["value"] = str(getattr(parameter, "value", ""))
            record["type"] = self._type_record(getattr(parameter, "type", None))
        return record

    def _type_record(self, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        record: dict[str, Any] = {"text": str(value), "kind": _enum_name(value.kind)}
        for key, attribute in (
            ("bit_width", "bitWidth"),
            ("signed", "isSigned"),
            ("four_state", "isFourState"),
            ("packed", "isSimpleBitVector"),
        ):
            try:
                record[key] = getattr(value, attribute)
            except Exception:
                continue
        return record

    def _expression_record(
        self, expression: Any, *, register_handle: bool = True
    ) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        references: list[dict[str, Any]] = []
        visit_action = self.pyslang.ast.VisitAction

        def visitor(node: Any) -> Any:
            if len(nodes) >= MAX_EXPRESSION_NODES:
                return visit_action.Interrupt
            if hasattr(node, "sourceRange") and hasattr(node, "kind"):
                item: dict[str, Any] = {"kind": _enum_name(node.kind)}
                symbol = getattr(node, "symbol", None)
                if symbol is not None:
                    item["symbol"] = getattr(symbol, "hierarchicalPath", symbol.name)
                    references.append(
                        {
                            "path": getattr(symbol, "hierarchicalPath", symbol.name),
                            "kind": _enum_name(symbol.kind),
                            "evidence": self._evidence(getattr(node, "sourceRange", None)),
                        }
                    )
                nodes.append(item)
            return visit_action.Advance

        expression.visit(visitor)
        text = self._source_text(expression.sourceRange).strip()
        text_bytes = text.encode("utf-8")
        text_truncated = len(text_bytes) > MAX_EXCERPT_BYTES
        if text_truncated:
            text = text_bytes[:MAX_EXCERPT_BYTES].decode("utf-8", errors="replace")
        record = {
            "kind": _enum_name(expression.kind),
            "type": self._type_record(getattr(expression, "type", None)),
            "text": text,
            "text_truncated": text_truncated,
            "representation": "bounded-flat-preorder-with-source-text",
            "structure_complete": False,
            "nodes": nodes,
            "nodes_truncated": len(nodes) >= MAX_EXPRESSION_NODES,
            "references": references,
            "evidence": self._evidence(expression.sourceRange),
        }
        if register_handle:
            record["handle"] = self._register(expression)
        return record

    def _driver_record(self, driver: Any) -> dict[str, Any]:
        containing = driver.containingSymbol
        record: dict[str, Any] = {
            "relation": "drives",
            "kind": _enum_name(driver.kind),
            "source": _enum_name(driver.source),
            "bounds": list(driver.bounds),
            "flags": _enum_name(driver.flags),
            "subject": self._object_identity(driver.symbol),
            "containing": self._object_identity(containing),
            "evidence": self._evidence(driver.sourceRange),
            "construct_evidence": self._evidence_for(containing, include_excerpt=True),
        }
        if _enum_name(getattr(containing, "kind", "")) == "ProceduralBlock":
            record["procedure_kind"] = _enum_name(containing.procedureKind)
            record["timing"] = self._timing_record(
                getattr(getattr(containing, "body", None), "timing", None)
            )
        return record

    def _procedure_record(self, analyzed: Any) -> dict[str, Any]:
        symbol = analyzed.analyzedSymbol
        native_reads, reads_truncated = _take_bounded(
            analyzed.readSet, MAX_NESTED_RESULTS
        )
        native_reads.sort(
            key=lambda item: (
                getattr(item.symbol, "hierarchicalPath", item.symbol.name),
                tuple(item.bitRange),
            )
        )
        reads = [
            {
                "path": item.symbol.hierarchicalPath,
                "bit_range": list(item.bitRange),
                "evidence": self._evidence_for(item.symbol),
            }
            for item in native_reads
        ]
        record = {
            "relation": "procedure_reads",
            "procedure": self._object_identity(symbol),
            "procedure_kind": _enum_name(getattr(symbol, "procedureKind", "continuous")),
            "reads": reads,
            "read_count": None if reads_truncated else len(native_reads),
            "read_count_lower_bound": len(native_reads) + (1 if reads_truncated else 0),
            "reads_truncated": reads_truncated,
            "driver_count": len(analyzed.drivers),
            "sensitivity_kind": _enum_name(analyzed.sensitivityList.kind),
            "timing": self._timing_record(analyzed.sensitivityList.timingControl),
            "evidence": self._evidence_for(symbol, include_excerpt=True),
        }
        return record

    def _timing_record(self, timing: Any) -> dict[str, Any] | None:
        if timing is None:
            return None
        text, text_truncated = _bounded_text(
            self._source_text(timing.sourceRange).strip(), MAX_EXCERPT_BYTES
        )
        record: dict[str, Any] = {
            "kind": _enum_name(timing.kind),
            "text": text,
            "text_truncated": text_truncated,
            "evidence": self._evidence(timing.sourceRange),
        }
        events = getattr(timing, "events", None)
        if events is not None:
            native_events, events_truncated = _take_bounded(
                events, MAX_NESTED_RESULTS
            )
            record["events"] = [
                self._timing_record(item) for item in native_events
            ]
            record["events_truncated"] = events_truncated
        expression = getattr(timing, "expr", None)
        if expression is not None:
            record["expression"] = self._expression_record(expression)
        edge = getattr(timing, "edge", None)
        if edge is not None:
            record["edge"] = _enum_name(edge)
        return record

    def _diagnostic_record(self, diagnostic: Any) -> dict[str, Any]:
        record = {
            "code": str(diagnostic.code),
            "severity": "error" if diagnostic.isError() else "warning",
            "arguments": [
                _bounded_text(item, MAX_DIAGNOSTIC_ARG_BYTES)[0]
                for item in list(diagnostic.args)[:MAX_DIAGNOSTIC_ARGS]
            ],
            "arguments_truncated": len(diagnostic.args) > MAX_DIAGNOSTIC_ARGS,
        }
        evidence = self._evidence(diagnostic.location)
        if evidence:
            record["evidence"] = evidence
        return record

    def _evidence_for(
        self, value: Any, *, include_excerpt: bool = False
    ) -> dict[str, Any] | None:
        syntax = getattr(value, "syntax", None)
        source_range = getattr(syntax, "sourceRange", None) if syntax is not None else None
        return self._evidence(
            source_range or getattr(value, "location", None),
            include_excerpt=include_excerpt,
        )

    def _source_text(self, value: Any) -> str:
        source_range = self._as_range(value)
        if source_range is None:
            return ""
        try:
            snapshot = self._source_snapshot(source_range.start.buffer)
            return snapshot["bytes"][
                source_range.start.offset : source_range.end.offset
            ].decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _as_range(self, value: Any) -> Any:
        if value is None or not bool(value):
            return None
        if hasattr(value, "start") and hasattr(value, "end"):
            source_range = value
        elif hasattr(value, "buffer") and hasattr(value, "offset"):
            source_range = self.pyslang.SourceRange(value, value)
        else:
            return None
        try:
            source_range = self.source_manager.getFullyOriginalRange(source_range)
        except Exception:
            pass
        if str(source_range.start.buffer) != str(source_range.end.buffer):
            return None
        return source_range

    def _freeze_input_closure(self) -> list[dict[str, str]]:
        physical_kinds = {"DesignFile", "IncludeFile", "LibraryFile", "LibraryMap"}
        closure: dict[str, dict[str, str]] = {}
        buffers, buffers_truncated = _take_bounded(
            self.source_manager.getAllBuffers(), MAX_COLLECTION_SCAN
        )
        if buffers_truncated:
            raise QueryRuntimeError(
                "invalid_build", "effective input closure exceeds runtime work limit"
            )
        for buffer in buffers:
            if _enum_name(self.source_manager.getBufferKind(buffer)) not in physical_kinds:
                continue
            full_path = Path(self.source_manager.getFullPath(buffer)).resolve()
            if not full_path.is_file() and str(
                self.source_manager.getRawFileName(buffer)
            ).startswith("<unnamed_buffer"):
                continue
            try:
                relative = full_path.relative_to(self.source_root).as_posix()
            except ValueError as error:
                raise QueryRuntimeError(
                    "invalid_build",
                    f"effective input escapes source root: {full_path}",
                ) from error
            if not full_path.is_file():
                raise QueryRuntimeError(
                    "invalid_build", f"effective input is not a regular file: {relative}"
                )
            compiled_bytes = self.source_manager.getSourceText(buffer).rstrip("\0").encode(
                "utf-8"
            )
            disk_bytes = full_path.read_bytes()
            if disk_bytes != compiled_bytes:
                raise QueryRuntimeError(
                    "stale_input", f"compiled input differs from source file: {relative}"
                )
            digest = _sha256_bytes(disk_bytes)
            stat = full_path.stat()
            snapshot = {
                "path": relative,
                "full_path": full_path,
                "bytes": compiled_bytes,
                "sha256": digest,
                "mtime_ns": stat.st_mtime_ns,
                "ctime_ns": stat.st_ctime_ns,
                "size": stat.st_size,
            }
            self.source_snapshots[str(buffer)] = snapshot
            self.input_snapshots[relative] = snapshot
            closure[relative] = {"path": relative, "sha256": digest}
        if not closure:
            raise QueryRuntimeError("invalid_build", "PySlang reported no effective file inputs")
        return [closure[path] for path in sorted(closure)]

    def _verify_inputs(self) -> None:
        try:
            build_stat = self.build_path.stat()
        except OSError as error:
            raise QueryRuntimeError(
                "stale_input", "build identity disappeared during session"
            ) from error
        if (
            build_stat.st_mtime_ns != self.build_snapshot["mtime_ns"]
            or build_stat.st_ctime_ns != self.build_snapshot["ctime_ns"]
            or build_stat.st_size != self.build_snapshot["size"]
        ):
            if _sha256_bytes(self.build_path.read_bytes()) != self.build_snapshot["sha256"]:
                raise QueryRuntimeError("stale_input", "build identity changed during session")
            self.build_snapshot["mtime_ns"] = build_stat.st_mtime_ns
            self.build_snapshot["ctime_ns"] = build_stat.st_ctime_ns
            self.build_snapshot["size"] = build_stat.st_size
        for snapshot in self.input_snapshots.values():
            full_path = snapshot["full_path"]
            try:
                stat = full_path.stat()
            except OSError as error:
                raise QueryRuntimeError(
                    "stale_input", f"effective input disappeared: {snapshot['path']}"
                ) from error
            if (
                stat.st_mtime_ns != snapshot["mtime_ns"]
                or stat.st_ctime_ns != snapshot["ctime_ns"]
                or stat.st_size != snapshot["size"]
            ):
                if _sha256_bytes(full_path.read_bytes()) != snapshot["sha256"]:
                    raise QueryRuntimeError(
                        "stale_input", f"effective input changed: {snapshot['path']}"
                    )
                snapshot["mtime_ns"] = stat.st_mtime_ns
                snapshot["ctime_ns"] = stat.st_ctime_ns
                snapshot["size"] = stat.st_size

    def _source_snapshot(self, buffer: Any) -> dict[str, Any]:
        key = str(buffer)
        snapshot = self.source_snapshots.get(key)
        full_path = Path(self.source_manager.getFullPath(buffer)).resolve()
        if snapshot is None:
            relative = full_path.relative_to(self.source_root).as_posix()
            compiled_bytes = self.source_manager.getSourceText(buffer).rstrip("\0").encode(
                "utf-8"
            )
            compiled_digest = _sha256_bytes(compiled_bytes)
            stat = full_path.stat()
            disk_digest = _sha256_bytes(full_path.read_bytes())
            if disk_digest != compiled_digest:
                raise QueryRuntimeError(
                    "stale_input", f"source changed after compilation: {relative}"
                )
            snapshot = {
                "path": relative,
                "full_path": full_path,
                "bytes": compiled_bytes,
                "sha256": compiled_digest,
                "mtime_ns": stat.st_mtime_ns,
                "ctime_ns": stat.st_ctime_ns,
                "size": stat.st_size,
            }
            self.source_snapshots[key] = snapshot
            return snapshot

        stat = full_path.stat()
        if (
            stat.st_mtime_ns != snapshot["mtime_ns"]
            or stat.st_ctime_ns != snapshot["ctime_ns"]
            or stat.st_size != snapshot["size"]
        ):
            if _sha256_bytes(full_path.read_bytes()) != snapshot["sha256"]:
                raise QueryRuntimeError(
                    "stale_input", f"source changed after compilation: {snapshot['path']}"
                )
            snapshot["mtime_ns"] = stat.st_mtime_ns
            snapshot["ctime_ns"] = stat.st_ctime_ns
            snapshot["size"] = stat.st_size
        return snapshot

    def _evidence(
        self, value: Any, *, include_excerpt: bool = False
    ) -> dict[str, Any] | None:
        source_range = self._as_range(value)
        if source_range is None:
            return None
        try:
            snapshot = self._source_snapshot(source_range.start.buffer)
            start = source_range.start.offset
            end = source_range.end.offset
            before_start = snapshot["bytes"][:start]
            before_end = snapshot["bytes"][:end]
            start_line_offset = before_start.rfind(b"\n")
            end_line_offset = before_end.rfind(b"\n")
            result = {
                "path": snapshot["path"],
                "sha256": snapshot["sha256"],
                "byte_start": start,
                "byte_end": end,
                "line_start": before_start.count(b"\n") + 1,
                "column_start": start - start_line_offset,
                "line_end": before_end.count(b"\n") + 1,
                "column_end": end - end_line_offset,
                "configuration_identity": self.identity["build_sha256"],
                "status": "source-backed",
            }
            logical_path = self.source_manager.getFileName(source_range.start)
            logical_line = self.source_manager.getLineNumber(source_range.start)
            logical_text = str(logical_path)
            ordinary_path = logical_text == snapshot["path"] or logical_text.endswith(
                f"/{snapshot['path']}"
            )
            if logical_text and (
                not ordinary_path or logical_line != result["line_start"]
            ):
                result["logical_location"] = {
                    "path": str(logical_path),
                    "line": logical_line,
                    "column": self.source_manager.getColumnNumber(source_range.start),
                }
            if include_excerpt:
                excerpt_bytes = snapshot["bytes"][start:end]
                truncated = len(excerpt_bytes) > MAX_EXCERPT_BYTES
                if truncated:
                    excerpt_bytes = excerpt_bytes[:MAX_EXCERPT_BYTES]
                excerpt = excerpt_bytes.decode("utf-8", errors="replace")
                result["excerpt"] = excerpt
                result["excerpt_truncated"] = truncated
            return result
        except QueryRuntimeError:
            raise
        except Exception:
            return None


def serve(session: RuntimeQuerySession, input_stream: Any, output_stream: Any) -> int:
    output_stream.write(json.dumps(session.ready_result(), sort_keys=True) + "\n")
    output_stream.flush()
    while True:
        line = input_stream.readline(MAX_REQUEST_BYTES + 1)
        if not line:
            break
        encoded_size = len(line.encode("utf-8"))
        line_incomplete = len(line) > MAX_REQUEST_BYTES and not line.endswith("\n")
        if line_incomplete:
            while line and not line.endswith("\n"):
                line = input_stream.readline(MAX_REQUEST_BYTES + 1)
        if line_incomplete or encoded_size > MAX_REQUEST_BYTES:
            result = {
                "schema": RESULT_SCHEMA,
                "request_id": "invalid",
                "operation": "unknown",
                "status": "unsupported",
                "message": f"request exceeds {MAX_REQUEST_BYTES} byte input limit",
                "compilation_identity": session.identity,
                "telemetry": session._telemetry(0.0),
            }
            output_stream.write(json.dumps(result, sort_keys=True) + "\n")
            output_stream.flush()
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            result = session.query(request)
        except ValueError as error:
            result = {
                "schema": RESULT_SCHEMA,
                "request_id": "invalid",
                "operation": "unknown",
                "status": "unsupported",
                "message": str(error),
                "compilation_identity": session.identity,
                "telemetry": session._telemetry(0.0),
            }
        output_stream.write(json.dumps(result, sort_keys=True) + "\n")
        output_stream.flush()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--build", required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--pyslang-site")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with RuntimeQuerySession.open(
            source_root=Path(args.source_root),
            build_path=Path(args.build),
            project_name=args.project_name,
            source_revision=args.source_revision,
            pyslang_site=Path(args.pyslang_site) if args.pyslang_site else None,
        ) as session:
            return serve(session, sys.stdin, sys.stdout)
    except QueryRuntimeError as error:
        payload = {
            "schema": SESSION_SCHEMA,
            "status": error.code,
            "message": str(error),
            **error.details,
        }
        print(json.dumps(payload, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
