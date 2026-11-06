#!/usr/bin/env python3
"""Own one rtl_query_runtime process and serialize rtlq requests to it."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import threading
import time
from typing import Any


sys_path = Path("/opt/rtl-analysis-agent/lib")
if sys_path.is_dir():
    import sys
    sys.path.insert(0, str(sys_path))


MAX_LINE = 1_100_000
STOP = threading.Event()
EVENT_SEQUENCE = itertools.count(1)
RESULT_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
KNOWLEDGE_ROUTES = {"DIRECT", "CONTEXT", "BYPASS"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def append_event(event_file: Path, event_type: str, **values: Any) -> None:
    payload = {"type": event_type, "at": now(), "sequence": next(EVENT_SEQUENCE), **values}
    encoded = (json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n").encode()
    descriptor = os.open(event_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, encoded)
    finally:
        os.close(descriptor)


def receive_line(client: socket.socket) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        data = client.recv(min(65536, MAX_LINE + 1 - total))
        if not data:
            break
        chunks.append(data)
        total += len(data)
        if total > MAX_LINE:
            raise ValueError("request exceeds broker limit")
        if b"\n" in data:
            break
    result = b"".join(chunks)
    if result.count(b"\n") != 1 or not result.endswith(b"\n"):
        raise ValueError("expected exactly one newline-terminated request")
    return result


def bounded_request(request: dict[str, Any]) -> dict[str, Any]:
    result = {
        "request_id": str(request.get("id", ""))[:128],
        "operation": str(request.get("op", ""))[:32],
    }
    if isinstance(request.get("code"), str):
        code = request["code"].encode("utf-8")
        result.update({"code_sha256": hashlib.sha256(code).hexdigest(), "code_bytes": len(code)})
    for key in (
        "path", "instance", "name", "kind", "port", "mode", "limit",
        "target", "sections", "request_kind", "additional_requirements",
        "view", "audit_refs", "client_format", "result_ref", "output_format",
        "actor", "phase",
    ):
        if key in request:
            value = request[key]
            result[key] = value[:512] if isinstance(value, str) else value
    return result


def _result_path(root: Path, result_ref: str, suffix: str) -> Path:
    if not RESULT_REF.fullmatch(result_ref):
        raise ValueError("result reference is invalid")
    return root / f"{result_ref}.{suffix}"


def _write_exclusive(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
    finally:
        os.close(descriptor)


def _knowledge_result_state(stored: dict[str, Any]) -> tuple[str, list[str]]:
    """Validate saved routing state without re-deciding knowledge sufficiency."""
    route = stored.get("route")
    reasons = stored.get("reasons")
    if route not in KNOWLEDGE_ROUTES:
        raise ValueError("saved knowledge result route is invalid")
    if (
        not isinstance(reasons, list)
        or not reasons
        or not all(isinstance(reason, str) and reason for reason in reasons)
    ):
        raise ValueError("saved knowledge result reasons are invalid")
    return route, reasons


def persist_knowledge_result(
    root: Path,
    *,
    run_id: str,
    request_id: str,
    response: dict[str, Any],
    candidate_sha256: str,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist two views of one response without creating a second query path."""
    from rtl_knowledge_prototype import render_module_knowledge_readable

    if not RESULT_REF.fullmatch(run_id):
        raise ValueError("run identity is invalid")
    _knowledge_result_state(response)
    result_ref = request_id
    structured_path = _result_path(root, result_ref, "json")
    readable_path = _result_path(root, result_ref, "md")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    if structured_path.exists() or readable_path.exists():
        raise FileExistsError(f"knowledge result already exists: {result_ref}")

    request_descriptor = bounded_request(request or {"id": request_id, "op": "knowledge"})
    request_sha256 = hashlib.sha256(
        json.dumps(
            request_descriptor, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()
    stored = dict(response)
    stored["result_identity"] = {
        "run_id": run_id,
        "request_id": request_id,
        "build_sha256": (response.get("target") or {}).get("build_sha256", "UNAVAILABLE"),
        "candidate_sha256": candidate_sha256,
        "request_sha256": request_sha256,
        "response_view": response.get("response_view", "answer"),
    }
    stored["result_files"] = {
        "structured": str(structured_path),
        "readable": str(readable_path),
    }
    structured = (
        json.dumps(stored, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()
    readable = render_module_knowledge_readable(
        stored,
        result_ref=result_ref,
        structured_ref=str(structured_path),
    ).encode()
    _write_exclusive(structured_path, structured)
    try:
        _write_exclusive(readable_path, readable)
    except Exception:
        structured_path.unlink(missing_ok=True)
        raise
    os.chmod(structured_path, 0o400)
    os.chmod(readable_path, 0o400)
    return {
        "result_ref": result_ref,
        "structured": {
            "path": str(structured_path),
            "sha256": hashlib.sha256(structured).hexdigest(),
            "bytes": len(structured),
        },
        "readable": {
            "path": str(readable_path),
            "sha256": hashlib.sha256(readable).hexdigest(),
            "bytes": len(readable),
        },
    }


def load_knowledge_result(
    root: Path,
    *,
    run_id: str,
    result_ref: str,
    output_format: str,
) -> tuple[dict[str, Any], str]:
    """Load one validated run-local result; never touches the candidate DB."""
    if output_format not in {"json", "readable"}:
        raise ValueError("result output format is invalid")
    structured_path = _result_path(root, result_ref, "json")
    readable_path = _result_path(root, result_ref, "md")
    if structured_path.is_symlink() or readable_path.is_symlink():
        raise ValueError("knowledge result path must not be a symlink")
    stored = json.loads(structured_path.read_text(encoding="utf-8"))
    identity = stored.get("result_identity") or {}
    if identity.get("run_id") != run_id or identity.get("request_id") != result_ref:
        raise ValueError("knowledge result does not belong to this run")
    _knowledge_result_state(stored)
    path = structured_path if output_format == "json" else readable_path
    return stored, path.read_text(encoding="utf-8")


def read_knowledge_result(
    root: Path,
    *,
    run_id: str,
    result_ref: str,
    output_format: str,
) -> str:
    """Compatibility wrapper returning only the selected run-local content."""
    _, content = load_knowledge_result(
        root,
        run_id=run_id,
        result_ref=result_ref,
        output_format=output_format,
    )
    return content


def serve_knowledge_result(
    root: Path,
    *,
    knowledge_mode: str,
    run_id: str,
    request_id: str,
    result_ref: str,
    output_format: str,
) -> dict[str, Any]:
    """Serve one result while separating file-read status from knowledge route."""
    if knowledge_mode == "off":
        return {
            "schema": "rtl-analysis-agent/runtime-query-result-v1",
            "request_id": request_id[:128],
            "operation": "knowledge_result", "status": "ok",
            "route": "BYPASS", "reasons": ["knowledge-disabled"],
            "candidate_accessed": False, "result_accessed": False,
            "content": "Knowledge is disabled for this run; no prior result was accessed.\n",
        }
    if knowledge_mode != "on":
        raise ValueError("knowledge mode is invalid")
    stored, content = load_knowledge_result(
        root,
        run_id=run_id,
        result_ref=result_ref,
        output_format=output_format,
    )
    route, reasons = _knowledge_result_state(stored)
    return {
        "schema": "rtl-analysis-agent/runtime-query-result-v1",
        "request_id": request_id[:128],
        "operation": "knowledge_result", "status": "ok",
        "route": route, "reasons": reasons,
        "candidate_accessed": False, "result_accessed": True,
        "result_ref": result_ref, "output_format": output_format,
        "response_view": stored.get("response_view", "UNAVAILABLE"),
        "coverage": stored.get("coverage", {}),
        "limitations": stored.get("limitations", {}),
        "content": content,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--result-root", type=Path, required=True)
    parser.add_argument("--knowledge-mode", choices=("on", "off"), default="off")
    parser.add_argument("--knowledge-candidate", type=Path)
    args = parser.parse_args()
    if args.knowledge_mode == "on" and args.knowledge_candidate is None:
        raise RuntimeError("knowledge ON requires a mounted candidate")
    if args.knowledge_mode == "off" and args.knowledge_candidate is not None:
        raise RuntimeError("knowledge OFF must not receive a candidate path")

    command = [
        "/opt/agent-runner/venv/bin/python3",
        "-B",
        "/opt/rtl-analysis-agent/lib/rtl_query_runtime.py",
        "--source-root",
        str(args.source),
        "--build",
        str(args.build),
        "--project-name",
        args.project,
        "--source-revision",
        args.revision,
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    append_event(
        args.events, "tool_started", run_id=args.run_id,
        worker_pid=process.pid, tool="rtl-query", knowledge_mode=args.knowledge_mode,
    )
    server: socket.socket | None = None
    knowledge_store: Any | None = None
    knowledge_sha256: str | None = None
    try:
        assert process.stdout is not None
        ready_line = process.stdout.readline()
        if not ready_line:
            detail = process.stderr.read().strip() if process.stderr else ""
            raise RuntimeError(f"rtl-query exited before ready: {detail}")
        ready = json.loads(ready_line)
        if ready.get("status") != "ready":
            raise RuntimeError(f"rtl-query not ready: {ready_line.strip()}")

        args.socket.parent.mkdir(parents=True, exist_ok=True)
        args.socket.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(args.socket))
        os.chmod(args.socket, 0o600)
        server.listen(8)
        server.settimeout(0.5)
        while not STOP.is_set():
            if process.poll() is not None:
                raise RuntimeError(f"rtl-query exited unexpectedly with {process.returncode}")
            try:
                client, _ = server.accept()
            except socket.timeout:
                continue
            with client:
                started = time.monotonic()
                request: dict[str, Any] | None = None
                try:
                    encoded = receive_line(client)
                    request = json.loads(encoded)
                    if not isinstance(request, dict):
                        raise ValueError("request must be a JSON object")
                    bounded = bounded_request(request)
                    event_context = {
                        "actor": str(request.get("actor", "unattributed"))[:32],
                        "phase": str(request.get("phase", "unattributed"))[:32],
                    }
                    append_event(args.events, "tool_call", run_id=args.run_id, **bounded)
                    if request.get("op") == "knowledge_result":
                        result_ref = str(request.get("result_ref", ""))
                        output_format = str(request.get("output_format", "readable"))
                        response = serve_knowledge_result(
                            args.result_root,
                            knowledge_mode=args.knowledge_mode,
                            run_id=args.run_id,
                            request_id=str(request.get("id", "")),
                            result_ref=result_ref,
                            output_format=output_format,
                        )
                        content = response["content"]
                        response_line = json.dumps(
                            response, sort_keys=True, ensure_ascii=False
                        ) + "\n"
                        append_event(
                            args.events, "knowledge_result_read", run_id=args.run_id,
                            operation="module-knowledge-result", result_ref=result_ref,
                            output_format=output_format,
                            status="pass", candidate_accessed=False,
                            result_accessed=response["result_accessed"],
                            route=response["route"], reason=response["reasons"],
                            bytes=len(content.encode()),
                            **event_context,
                        )
                    elif request.get("op") == "knowledge":
                        from rtl_knowledge_prototype import (
                            MODULE_KNOWLEDGE_SECTIONS,
                            PrototypeStore,
                            empty_module_knowledge_response,
                            resolve_module_knowledge_target,
                            route_module_knowledge,
                        )
                        append_event(
                            args.events, "knowledge_lookup", run_id=args.run_id,
                            operation="module-knowledge", object=str(request.get("target", ""))[:512],
                            source="PREBUILT_SEMANTIC", visibility=args.knowledge_mode,
                            response_view=str(request.get("view", "answer"))[:32],
                            status="started", artifact_ref="UNMOUNTED" if args.knowledge_mode == "off" else "candidate",
                            **event_context,
                        )
                        if args.knowledge_mode == "off":
                            details = empty_module_knowledge_response(
                                reason="knowledge-disabled", candidate_accessed=False,
                                requested_sections=request.get("sections")
                                or list(MODULE_KNOWLEDGE_SECTIONS),
                                required_child_instance_ids=[],
                                additional_requirements=request.get(
                                    "additional_requirements", []
                                ),
                            )
                            details["response_view"] = str(request.get("view", "answer"))
                        else:
                            if knowledge_store is None:
                                assert args.knowledge_candidate is not None
                                knowledge_store = PrototypeStore.open(
                                    args.knowledge_candidate, readonly=True
                                )
                                digest = hashlib.sha256()
                                with args.knowledge_candidate.open("rb") as stream:
                                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                                        digest.update(chunk)
                                knowledge_sha256 = digest.hexdigest()
                            resolution = resolve_module_knowledge_target(
                                knowledge_store, str(request.get("target", ""))
                            )
                            if resolution["status"] != "ok":
                                details = empty_module_knowledge_response(
                                    reason=f"target-{resolution['status']}",
                                    candidate_accessed=True,
                                    requested_sections=request.get("sections")
                                    or list(MODULE_KNOWLEDGE_SECTIONS),
                                    required_child_instance_ids=[],
                                    additional_requirements=request.get(
                                        "additional_requirements", []
                                    ),
                                )
                                details["response_view"] = str(request.get("view", "answer"))
                                details["target_resolution"] = resolution
                            else:
                                details = route_module_knowledge(
                                    knowledge_store, enabled=True,
                                    target_id=resolution["target_id"],
                                    build_sha256=ready["compilation_identity"]["build_sha256"],
                                    requested_sections=request.get("sections")
                                    or list(MODULE_KNOWLEDGE_SECTIONS),
                                    required_child_instance_ids=resolution[
                                        "direct_child_instance_ids"
                                    ],
                                    request_kind=str(
                                        request.get("request_kind", "module-understanding")
                                    ),
                                    additional_requirements=request.get(
                                        "additional_requirements", []
                                    ),
                                    response_view=str(request.get("view", "answer")),
                                    audit_refs=request.get("audit_refs", []),
                                )
                                details["target_resolution"] = resolution
                        query_ms = round((time.monotonic() - started) * 1000, 3)
                        next_action = {
                            "DIRECT": "STOP_AFTER_MINIMAL_PRESENTATION",
                            "CONTEXT": "SUPPLEMENT_EXPLICIT_GAPS",
                            "BYPASS": "SOURCE_OR_LIVE_TOOL",
                        }[details["route"]]
                        response = {
                            "schema": "rtl-analysis-agent/runtime-query-result-v1",
                            "request_id": str(request.get("id", ""))[:128],
                            "operation": "knowledge", "status": "ok", **details,
                            "telemetry": {
                                "knowledge_query_ms": query_ms,
                                "source_reads": "UNAVAILABLE",
                                "knowledge_navigation_items": "UNAVAILABLE",
                                "knowledge_cited_items": "UNAVAILABLE",
                            },
                            "execution_summary": {
                                "object": str(request.get("target", ""))[:512],
                                "scope": "direct", "returned_route": details["route"],
                                "actual_path": "UNAVAILABLE",
                                "response_view": details["response_view"],
                                "returned_items": details["returned_items"],
                                "navigation_items": "UNAVAILABLE",
                                "cited_items": "UNAVAILABLE",
                                "recommended_next_action": next_action,
                                "actual_supplement": "UNAVAILABLE",
                                "returned_route_reasons": details["reasons"],
                                "stop_or_fallback_reason": "UNAVAILABLE",
                                "knowledge_query_ms": query_ms,
                                "end_to_end_wall_ms": "UNAVAILABLE",
                                "event_ref": str(args.events),
                                "detailed_material": (
                                    "NOT_EXPANDED; use exact audit refs"
                                    if details["response_view"] == "answer"
                                    else "EXPANDED_ON_REQUEST"
                                ),
                            },
                        }
                        artifact_started = time.monotonic()
                        if args.knowledge_mode == "on":
                            artifacts = persist_knowledge_result(
                                args.result_root,
                                run_id=args.run_id,
                                request_id=response["request_id"],
                                response=response,
                                candidate_sha256=knowledge_sha256 or "UNAVAILABLE",
                                request=request,
                            )
                            response["result_artifacts"] = artifacts
                            response["client_output"] = read_knowledge_result(
                                args.result_root,
                                run_id=args.run_id,
                                result_ref=artifacts["result_ref"],
                                output_format="readable",
                            )
                            append_event(
                                args.events, "knowledge_result_written", run_id=args.run_id,
                                operation="module-knowledge-result",
                                request_id=response["request_id"],
                                result_ref=artifacts["result_ref"], status="pass",
                                structured_bytes=artifacts["structured"]["bytes"],
                                readable_bytes=artifacts["readable"]["bytes"],
                                structured_sha256=artifacts["structured"]["sha256"],
                                readable_sha256=artifacts["readable"]["sha256"],
                                **event_context,
                            )
                        else:
                            from rtl_knowledge_prototype import render_module_knowledge_readable
                            response["client_output"] = render_module_knowledge_readable(response)
                        artifact_ms = round((time.monotonic() - artifact_started) * 1000, 3)
                        response["telemetry"]["result_render_write_ms"] = artifact_ms
                        response_line = json.dumps(response, sort_keys=True, ensure_ascii=False) + "\n"
                        returned_bytes = len(response_line.encode())
                        append_event(
                            args.events, "knowledge_result", run_id=args.run_id,
                            operation="module-knowledge",
                            object=str(request.get("target", ""))[:512],
                            source="PREBUILT_SEMANTIC", status="pass",
                            response_view=details["response_view"],
                            route=details["route"], reason=details["reasons"],
                            candidate_accessed=details["candidate_accessed"],
                            records=details["returned_items"], bytes=returned_bytes,
                            returned_counts=details.get("returned_counts", {}),
                            duration_ms=query_ms,
                            artifact_ref=(
                                response.get("result_artifacts", {}).get("result_ref")
                                or knowledge_sha256 or "UNMOUNTED"
                            ),
                            delivered_bytes=len(response["client_output"].encode()),
                            actual_use="PROVIDED_NOT_PROVEN_CITED",
                            **event_context,
                        )
                    else:
                        assert process.stdin is not None
                        process.stdin.write(encoded.decode())
                        process.stdin.flush()
                        response_line = process.stdout.readline()
                        if not response_line:
                            raise RuntimeError("rtl-query exited before returning a result")
                        response = json.loads(response_line)
                    append_event(
                        args.events,
                        "tool_result", run_id=args.run_id,
                        request_id=str(response.get("request_id", ""))[:128],
                        operation=str(response.get("operation", ""))[:32],
                        status=str(response.get("status", ""))[:64],
                        wall_ms=round((time.monotonic() - started) * 1000, 3),
                        **event_context,
                    )
                    client.sendall(response_line.encode())
                except Exception as error:
                    append_event(
                        args.events, "tool_error", run_id=args.run_id,
                        operation=str(request.get("op", ""))[:32]
                        if isinstance(request, dict) else "unknown",
                        status="wrapper_error", error_type=type(error).__name__,
                        actor=str(request.get("actor", "unattributed"))[:32]
                        if isinstance(request, dict) else "unattributed",
                        phase=str(request.get("phase", "unattributed"))[:32]
                        if isinstance(request, dict) else "unattributed",
                    )
                    payload = {
                        "schema": "rtl-analysis-agent/rtlq-wrapper-error-v1",
                        "status": "wrapper_error",
                        "message": str(error),
                    }
                    client.sendall((json.dumps(payload, sort_keys=True) + "\n").encode())
        return 0
    finally:
        if knowledge_store is not None:
            knowledge_store.close()
        if server is not None:
            server.close()
        args.socket.unlink(missing_ok=True)
        if process.stdin is not None:
            process.stdin.close()
        try:
            worker_code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                worker_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                worker_code = process.wait()
        detail = process.stderr.read().strip() if process.stderr else ""
        append_event(
            args.events,
            "tool_stopped",
            run_id=args.run_id,
            tool="rtl-query",
            worker_pid=process.pid,
            exit_code=worker_code,
            stderr_present=bool(detail),
        )


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    raise SystemExit(main())
