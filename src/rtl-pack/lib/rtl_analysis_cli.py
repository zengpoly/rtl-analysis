#!/usr/bin/env python3
"""Bounded, read-only adapter over locked upstream RTL command-line tools."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

import rtl_project_model as project_model
import rtl_build1 as build1
import rtl_knowledge_prototype as knowledge_prototype
import rtl_manual_v6 as manual_v6
import rtl_module_knowledge as module_knowledge
import rtl_query_runtime as query_runtime
import rtl_query_v02 as query_v02


SCHEMA = "rtl-analysis-agent/result-v1"
PROJECT_SCHEMA = "rtl-analysis-agent/project-v1"
SOURCE_SNAPSHOT_SCHEMA = "rtl-analysis-agent/offline-source-v1"
MAX_DIAGNOSTICS = 100
MAX_SOURCE_BYTES = 5 * 1024 * 1024
TOOL_TIMEOUT_SECONDS = 30
YOSYS_TIMEOUT_SECONDS = 90
DEFAULT_CONTEXT_MODEL = "analysis/model/rtl-knowledge-v01-candidate.sqlite"
CONTEXT_ACTIONS = {
    "driver": {
        "profile": "driver",
        "roles": ["data-flow", "connectivity"],
    },
    "binding": {
        "profile": "cross-module",
        "roles": ["connectivity", "structure"],
    },
    "register": {
        "profile": "register-state",
        "roles": ["state", "clock-reset", "data-flow", "control"],
    },
    "module": {
        "profile": "module-overview",
        "roles": ["structure", "connectivity", "data-flow"],
    },
}
SLANG_LOCATION = re.compile(
    r"^(?P<file>.*?):(?P<line>\d+):(?P<column>\d+): "
    r"(?P<severity>fatal|error|warning|note): (?P<message>.*)$"
)


class AdapterError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def project_root() -> Path:
    capability = Path(__file__).resolve().parents[1]
    repository = capability.parents[1]
    if capability == repository / "src/rtl-pack" and (repository / "src/rtl-pack/config/domain-pack.json").is_file():
        return repository
    return capability


def capability_root(root: Path) -> Path:
    nested = root / "src/rtl-pack"
    if (nested / "config").is_dir():
        return nested
    return root


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AdapterError("configuration", f"cannot read {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise AdapterError("configuration", f"{path.name} must contain a JSON object")
    return value


def find_named(items: Iterable[dict[str, Any]], name: str, kind: str) -> dict[str, Any]:
    for item in items:
        if item.get("name") == name:
            return item
    raise AdapterError("configuration", f"unknown {kind}: {name}")


def run_git(target_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(target_root), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise AdapterError("target-integrity", "cannot inspect target Git state")
    return result.stdout.strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_source_snapshot(project: Path, source_root: Path, revision: str) -> None:
    lock_path = project / "source-lock.json"
    manifest_path = project / "source-files.sha256"
    if (
        not lock_path.is_file()
        or lock_path.is_symlink()
        or not manifest_path.is_file()
        or manifest_path.is_symlink()
    ):
        raise AdapterError("project-integrity", "offline source snapshot lock is missing or unsafe")
    lock = load_json(lock_path)
    if (
        lock.get("schema") != SOURCE_SNAPSHOT_SCHEMA
        or lock.get("revision") != revision
        or not isinstance(lock.get("tree"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", str(lock["tree"]))
        or lock.get("tracked_content_manifest_sha256") != sha256_file(manifest_path)
    ):
        raise AdapterError("project-integrity", "offline source snapshot identity is invalid")
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise AdapterError("project-integrity", "cannot read offline source manifest") from error
    if not lines:
        raise AdapterError("project-integrity", "offline source manifest is empty")
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\r\n]+)", line)
        if match is None:
            raise AdapterError("project-integrity", "offline source manifest has an invalid row")
        relative = Path(match.group(2))
        if relative.is_absolute() or ".." in relative.parts:
            raise AdapterError("project-integrity", "offline source manifest path is unsafe")
        source_file = source_root / relative
        if not source_file.is_file() or source_file.is_symlink():
            raise AdapterError("project-integrity", "offline source snapshot file is missing or unsafe")
        if sha256_file(source_file) != match.group(1):
            raise AdapterError("project-integrity", "offline source snapshot content hash mismatch")


def temporary_root(root: Path) -> Path:
    configured = os.environ.get("RTL_ANALYSIS_TMPDIR")
    if configured is None:
        selected = root / ".local/tmp"
    else:
        selected = Path(configured)
        if not selected.is_absolute() or "\n" in configured:
            raise AdapterError("temporary-directory", "RTL_ANALYSIS_TMPDIR must be absolute")
    if selected.is_symlink():
        raise AdapterError("temporary-directory", "temporary directory must not be a symbolic link")
    try:
        selected.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise AdapterError("temporary-directory", "temporary directory is not writable") from error
    if not selected.is_dir() or selected.is_symlink():
        raise AdapterError("temporary-directory", "temporary directory is unsafe")
    return selected.resolve()


def project_context(project_dir: str) -> tuple[dict[str, Any], Path]:
    supplied = Path(project_dir)
    project = (supplied if supplied.is_absolute() else Path.cwd() / supplied).resolve()
    if not project.is_dir() or supplied.is_symlink():
        raise AdapterError("project-missing", "selected project directory does not exist")
    config_path = project / "rtl-project.json"
    if not config_path.is_file() or config_path.is_symlink():
        raise AdapterError("project-configuration", "rtl-project.json is missing or unsafe")
    config = load_json(config_path)
    expected = {"schema", "name", "source", "analysis", "revision", "entry"}
    if set(config) != expected or config.get("schema") != PROJECT_SCHEMA:
        raise AdapterError("project-configuration", "rtl-project.json shape or schema is invalid")
    name = config.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", name):
        raise AdapterError("project-configuration", "project name is invalid")
    source_value = config.get("source")
    if config.get("analysis") != "analysis" or not isinstance(source_value, str):
        raise AdapterError("project-configuration", "analysis must use its fixed relative directory")
    external_source = Path(source_value).is_absolute()
    if external_source:
        if os.environ.get("RTL_ANALYSIS_ALLOW_EXTERNAL_SOURCE") != "1" or "\n" in source_value:
            raise AdapterError(
                "project-configuration", "absolute source requires the Native deployment boundary"
            )
        source = Path(source_value)
    elif source_value == "source":
        source = project / "source"
    else:
        raise AdapterError(
            "project-configuration",
            "source must use its fixed relative directory or an allowed absolute path",
        )
    analysis = project / "analysis"
    if not source.is_dir() or source.is_symlink() or not analysis.is_dir() or analysis.is_symlink():
        raise AdapterError("project-configuration", "source or analysis directory is missing or unsafe")
    source_root = source.resolve()
    if not external_source:
        try:
            source_root.relative_to(project)
        except ValueError as error:
            raise AdapterError("project-configuration", "source directory escapes the project") from error

    revision = config.get("revision")
    if revision is not None:
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise AdapterError("project-configuration", "revision must be null or a full lowercase Git SHA")
        if (source_root / ".git").exists():
            if run_git(source_root, "rev-parse", "HEAD") != revision:
                raise AdapterError("project-integrity", "source revision does not match rtl-project.json")
            if run_git(source_root, "status", "--porcelain", "--untracked-files=all"):
                raise AdapterError("project-integrity", "versioned project source is not clean")
        elif os.environ.get("RTL_ANALYSIS_ALLOW_SOURCE_SNAPSHOT") == "1":
            verify_source_snapshot(project, source_root, revision)
        else:
            raise AdapterError("project-integrity", "revision is set but source is not a Git worktree")

    entry = config.get("entry")
    if not isinstance(entry, str) or not entry or "\n" in entry:
        raise AdapterError("project-configuration", "entry must be a non-empty relative source path")
    _, normalized_entry = resolve_target_file(source_root, entry)
    return {
        "name": name,
        "revision": revision or "unversioned",
        "entry": normalized_entry,
    }, source_root


def target_context(
    root: Path, target_name: str, project_dir: str | None = None
) -> tuple[dict[str, Any], Path]:
    if project_dir is not None:
        return project_context(project_dir)
    config = load_json(capability_root(root) / "config/targets.json")
    target = find_named(config.get("targets", []), target_name, "target")
    target_root = (root / str(target["workspace"])).resolve()
    if not (target_root / ".git").is_dir():
        raise AdapterError("target-missing", f"target {target_name} is not installed")
    actual_revision = run_git(target_root, "rev-parse", "HEAD")
    if actual_revision != target.get("revision"):
        raise AdapterError("target-integrity", f"target {target_name} revision does not match its lock")
    if run_git(target_root, "status", "--porcelain", "--untracked-files=all"):
        raise AdapterError("target-integrity", f"target {target_name} worktree is not clean")
    return target, target_root


def resolve_target_file(target_root: Path, file_name: str) -> tuple[Path, str]:
    relative = Path(file_name)
    if relative.is_absolute():
        raise AdapterError("target-path", "--file must be relative to the selected target")
    candidate = (target_root / relative).resolve()
    try:
        normalized = candidate.relative_to(target_root)
    except ValueError as error:
        raise AdapterError("target-path", "--file cannot escape the selected target") from error
    if not candidate.is_file():
        raise AdapterError("target-path", f"target file does not exist: {normalized.as_posix()}")
    if candidate.stat().st_size > MAX_SOURCE_BYTES:
        raise AdapterError("source-too-large", f"target file exceeds {MAX_SOURCE_BYTES} bytes")
    return candidate, normalized.as_posix()


def tool_context(root: Path, tool_name: str) -> tuple[dict[str, Any], Path]:
    config = load_json(capability_root(root) / "config/toolchain.json")
    tool = find_named(config.get("tools", []), tool_name, "tool")
    tool_root = Path(os.environ.get("RTL_ANALYSIS_TOOL_ROOT", root / ".local/tools")).resolve()
    install = tool_root / tool_name / str(tool["version"])
    executable = install / str(tool["version_command"][0])
    if not executable.is_file():
        raise AdapterError("tool-missing", f"locked tool is not installed: {tool_name}@{tool['version']}")
    return tool, install


def run_tool(
    command: list[str],
    cwd: Path,
    *,
    timeout: int = TOOL_TIMEOUT_SECONDS,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        raise AdapterError(
            "tool-timeout", f"tool exceeded the {timeout}s operation limit"
        ) from error
    except OSError as error:
        raise AdapterError("tool-execution", f"cannot start tool: {error}") from error


def limited_lines(text: str, limit: int = MAX_DIAGNOSTICS) -> tuple[list[str], bool]:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[:limit], len(lines) > limit


def normalize_slang_diagnostics(data: Any, fallback: str) -> tuple[list[dict[str, Any]], bool]:
    candidates: list[dict[str, Any]] = []
    if isinstance(data, list):
        candidates = [item for item in data if isinstance(item, dict)]
    elif isinstance(data, dict):
        for key in ("diagnostics", "issues", "messages"):
            value = data.get(key)
            if isinstance(value, list):
                candidates = [item for item in value if isinstance(item, dict)]
                break

    diagnostics: list[dict[str, Any]] = []
    for item in candidates[:MAX_DIAGNOSTICS]:
        diagnostic: dict[str, Any] = {
            "severity": item.get("severity") or item.get("level") or "unknown",
            "message": item.get("message") or item.get("text") or "diagnostic",
        }
        code = item.get("code") or item.get("optionName")
        if code:
            diagnostic["rule"] = code
        location = item.get("location") or item.get("source")
        if isinstance(location, dict):
            normalized_location = {
                key: location[key]
                for key in ("file", "line", "column")
                if key in location
            }
            if normalized_location:
                diagnostic["location"] = normalized_location
        diagnostics.append(diagnostic)

    if not diagnostics and fallback.strip():
        lines, truncated = limited_lines(fallback)
        for line in lines:
            match = SLANG_LOCATION.match(line)
            if match:
                diagnostics.append(
                    {
                        "severity": match.group("severity"),
                        "message": match.group("message"),
                        "location": {
                            "file": match.group("file"),
                            "line": int(match.group("line")),
                            "column": int(match.group("column")),
                        },
                    }
                )
        if not diagnostics:
            diagnostics = [{"severity": "unknown", "message": line} for line in lines]
        return diagnostics, truncated
    return diagnostics, len(candidates) > MAX_DIAGNOSTICS


VERIBLE_LOCATION = re.compile(
    r"^(?P<file>.*?):(?P<line>\d+):(?P<column>\d+)(?:-(?P<end_column>\d+))?: (?P<body>.*)$"
)


def normalize_verible_diagnostics(text: str) -> tuple[list[dict[str, Any]], bool]:
    lines, truncated = limited_lines(text)
    diagnostics: list[dict[str, Any]] = []
    for line in lines:
        match = VERIBLE_LOCATION.match(line)
        if not match:
            diagnostics.append({"severity": "warning", "message": line})
            continue
        body = match.group("body")
        labels = re.findall(r"\[([^]]+)\]", body)
        message = re.sub(r"(?:\s*\[[^]]+\])+\s*$", "", body).strip()
        diagnostic: dict[str, Any] = {
            "severity": "warning",
            "message": message,
            "location": {
                "file": match.group("file"),
                "line": int(match.group("line")),
                "column": int(match.group("column")),
            },
        }
        if match.group("end_column"):
            diagnostic["location"]["end_column"] = int(match.group("end_column"))
        if labels:
            diagnostic["rule"] = labels[-1]
        if len(labels) > 1:
            diagnostic["category"] = labels[-2]
        diagnostics.append(diagnostic)
    return diagnostics, truncated


def identifiers(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        if node.get("kind") == "Identifier" and isinstance(node.get("text"), str):
            found.append(node["text"])
        for value in node.values():
            found.extend(identifiers(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(identifiers(value))
    return found


def nodes_of_kind(node: Any, kind: str) -> Iterable[dict[str, Any]]:
    if isinstance(node, dict):
        if node.get("kind") == kind:
            yield node
        for value in node.values():
            yield from nodes_of_kind(value, kind)
    elif isinstance(node, list):
        for value in node:
            yield from nodes_of_kind(value, kind)


def module_outline(cst: dict[str, Any]) -> list[dict[str, Any]]:
    modules: list[dict[str, Any]] = []
    for module in nodes_of_kind(cst, "ModuleDeclaration"):
        header = module.get("header", {})
        name = header.get("name", {}).get("text") if isinstance(header, dict) else None
        if not isinstance(name, str):
            continue
        ports_node = header.get("ports", {})
        port_items = ports_node.get("ports", []) if isinstance(ports_node, dict) else []
        ports: list[str] = []
        if isinstance(port_items, list):
            for item in port_items:
                if not isinstance(item, dict) or "Port" not in str(item.get("kind", "")):
                    continue
                names = identifiers(item)
                if names and names[-1] not in ports:
                    ports.append(names[-1])
        member_counts: collections.Counter[str] = collections.Counter()
        members = module.get("members", [])
        if isinstance(members, list):
            member_counts.update(
                str(member["kind"])
                for member in members
                if isinstance(member, dict) and isinstance(member.get("kind"), str)
            )
        modules.append(
            {
                "name": name,
                "ports": ports,
                "member_kinds": dict(sorted(member_counts.items())),
            }
        )
        if len(modules) >= 100:
            break
    return modules


def yosys_modules(netlist: dict[str, Any]) -> list[dict[str, Any]]:
    modules: list[dict[str, Any]] = []
    raw_modules = netlist.get("modules", {})
    if not isinstance(raw_modules, dict):
        raise AdapterError("result-parse", "Yosys JSON has no modules object")
    for name, module in list(raw_modules.items())[:100]:
        if not isinstance(module, dict):
            continue
        ports: list[dict[str, Any]] = []
        for port_name, port in module.get("ports", {}).items():
            if not isinstance(port, dict):
                continue
            bits = port.get("bits", [])
            ports.append(
                {
                    "name": port_name,
                    "direction": port.get("direction", "unknown"),
                    "width": len(bits) if isinstance(bits, list) else 0,
                }
            )
        cells = module.get("cells", {})
        cell_types: collections.Counter[str] = collections.Counter()
        if isinstance(cells, dict):
            cell_types.update(
                str(cell.get("type", "unknown"))
                for cell in cells.values()
                if isinstance(cell, dict)
            )
        netnames = module.get("netnames", {})
        modules.append(
            {
                "name": name,
                "ports": ports,
                "cell_count": len(cells) if isinstance(cells, dict) else 0,
                "cell_types": dict(sorted(cell_types.items())),
                "net_count": len(netnames) if isinstance(netnames, dict) else 0,
            }
        )
    return modules


def yosys_signal_connections(
    netlist: dict[str, Any], signal_name: str, source_label: str | None = None
) -> tuple[str, dict[str, Any]]:
    raw_modules = netlist.get("modules", {})
    if not isinstance(raw_modules, dict):
        raise AdapterError("result-parse", "Yosys JSON has no modules object")
    for module_name, module in raw_modules.items():
        if not isinstance(module, dict):
            continue
        netnames = module.get("netnames", {})
        if not isinstance(netnames, dict) or signal_name not in netnames:
            continue
        signal = netnames[signal_name]
        bits = signal.get("bits", []) if isinstance(signal, dict) else []
        bit_set = {bit for bit in bits if isinstance(bit, int)}
        module_ports: list[dict[str, Any]] = []
        for port_name, port in module.get("ports", {}).items():
            if not isinstance(port, dict):
                continue
            port_bits = port.get("bits", [])
            if isinstance(port_bits, list) and bit_set.intersection(
                bit for bit in port_bits if isinstance(bit, int)
            ):
                module_ports.append(
                    {"name": port_name, "direction": port.get("direction", "unknown")}
                )

        connections: list[dict[str, Any]] = []
        for cell_name, cell in module.get("cells", {}).items():
            if not isinstance(cell, dict):
                continue
            directions = cell.get("port_directions", {})
            for port_name, port_bits in cell.get("connections", {}).items():
                if not isinstance(port_bits, list):
                    continue
                matched = sorted(
                    bit_set.intersection(bit for bit in port_bits if isinstance(bit, int))
                )
                if matched:
                    connections.append(
                        {
                            "cell": cell_name.replace("/work/input.sv", source_label)
                            if source_label
                            else cell_name,
                            "cell_type": cell.get("type", "unknown"),
                            "port": port_name,
                            "direction": directions.get(port_name, "unknown")
                            if isinstance(directions, dict)
                            else "unknown",
                            "matched_bit_count": len(matched),
                        }
                    )
                if len(connections) >= MAX_DIAGNOSTICS:
                    break
            if len(connections) >= MAX_DIAGNOSTICS:
                break
        return module_name, {
            "name": signal_name,
            "width": len(bits) if isinstance(bits, list) else 0,
            "module_ports": module_ports,
            "connections": connections,
            "truncated": len(connections) >= MAX_DIAGNOSTICS,
        }
    raise AdapterError("signal-not-found", f"signal is not present in the synthesized netlist: {signal_name}")


def base_result(operation: str, target: dict[str, Any], file_name: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "operation": operation,
        "target": {"name": target["name"], "revision": target["revision"]},
    }
    if file_name is not None:
        result["target"]["file"] = file_name
    return result


def syntax_command(
    root: Path, target_name: str, file_name: str, project_dir: str | None = None
) -> tuple[int, dict[str, Any]]:
    target, target_root = target_context(root, target_name, project_dir)
    _, relative = resolve_target_file(target_root, file_name)
    tool, install = tool_context(root, "slang")
    executable = install / str(tool["version_command"][0])
    started = time.monotonic()
    temp_root = temporary_root(root)
    with tempfile.TemporaryDirectory(prefix="slang-syntax-", dir=temp_root) as temp_name:
        diagnostics_path = Path(temp_name) / "diagnostics.json"
        completed = run_tool(
            [
                str(executable),
                "--parse-only",
                "--quiet",
                "--diag-json",
                str(diagnostics_path),
                relative,
            ],
            target_root,
        )
        data: Any = []
        if diagnostics_path.is_file():
            try:
                data = json.loads(diagnostics_path.read_text(encoding="utf-8"))
            except ValueError as error:
                raise AdapterError("result-parse", "slang returned invalid diagnostic JSON") from error
    diagnostics, truncated = normalize_slang_diagnostics(
        data, "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    )
    status = "pass" if completed.returncode == 0 else "issues"
    result = base_result("syntax", target, relative)
    result.update(
        {
            "tool": {"name": "slang", "version": tool["version"]},
            "status": status,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "summary": {
                "diagnostic_count": len(diagnostics),
                "returned_count": len(diagnostics),
                "truncated": truncated,
            },
            "diagnostics": diagnostics,
        }
    )
    return (0 if status == "pass" else 1), result


def lint_command(
    root: Path, target_name: str, file_name: str, project_dir: str | None = None
) -> tuple[int, dict[str, Any]]:
    target, target_root = target_context(root, target_name, project_dir)
    _, relative = resolve_target_file(target_root, file_name)
    tool, install = tool_context(root, "verible")
    executable = install / "bin/verible-verilog-lint"
    started = time.monotonic()
    completed = run_tool(
        [
            str(executable),
            "--rules_config_search=false",
            "--autofix=no",
            relative,
        ],
        target_root,
    )
    diagnostics, truncated = normalize_verible_diagnostics(
        "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    )
    status = "pass" if completed.returncode == 0 else "issues"
    result = base_result("lint", target, relative)
    result.update(
        {
            "tool": {"name": "verible", "version": tool["version"]},
            "status": status,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "summary": {
                "diagnostic_count": len(diagnostics),
                "returned_count": len(diagnostics),
                "truncated": truncated,
            },
            "diagnostics": diagnostics,
        }
    )
    return (0 if status == "pass" else 1), result


def outline_command(
    root: Path, target_name: str, file_name: str, project_dir: str | None = None
) -> tuple[int, dict[str, Any]]:
    target, target_root = target_context(root, target_name, project_dir)
    _, relative = resolve_target_file(target_root, file_name)
    tool, install = tool_context(root, "slang")
    executable = install / str(tool["version_command"][0])
    started = time.monotonic()
    temp_root = temporary_root(root)
    with tempfile.TemporaryDirectory(prefix="slang-outline-", dir=temp_root) as temp_name:
        cst_path = Path(temp_name) / "cst.json"
        completed = run_tool(
            [
                str(executable),
                "--parse-only",
                "--quiet",
                "--cst-json",
                str(cst_path),
                "--cst-json-mode",
                "no-trivia",
                relative,
            ],
            target_root,
        )
        if not cst_path.is_file():
            raise AdapterError("result-parse", "slang did not produce CST JSON")
        try:
            cst = json.loads(cst_path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise AdapterError("result-parse", "slang returned invalid CST JSON") from error
    modules = module_outline(cst)
    diagnostic_lines, truncated = limited_lines(
        "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    )
    status = "pass" if completed.returncode == 0 else "issues"
    result = base_result("outline", target, relative)
    result.update(
        {
            "tool": {"name": "slang", "version": tool["version"]},
            "status": status,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "summary": {
                "module_count": len(modules),
                "diagnostic_count": len(diagnostic_lines),
                "truncated": truncated,
            },
            "modules": modules,
            "diagnostics": [
                {"severity": "unknown", "message": line} for line in diagnostic_lines
            ],
        }
    )
    return (0 if status == "pass" else 1), result


def build_yosys_netlist(
    root: Path, target_name: str, file_name: str, project_dir: str | None = None
) -> tuple[dict[str, Any], str, dict[str, Any], dict[str, Any], int]:
    target, target_root = target_context(root, target_name, project_dir)
    source, relative = resolve_target_file(target_root, file_name)
    tool, install = tool_context(root, "yosys")
    executable = install / str(tool["version_command"][0])
    temp_root = temporary_root(root)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="yosys-structure-", dir=temp_root) as temp_name:
        work = Path(temp_name)
        staged_source = work / "input.sv"
        netlist_path = work / "netlist.json"
        shutil.copyfile(source, staged_source)
        environment = os.environ.copy()
        environment.setdefault("YOWASP_CACHE_DIR", str(root / ".local/cache/yowasp"))
        environment["YOWASP_MOUNT"] = f"/work={work}"
        script = (
            "read_verilog -sv /work/input.sv; "
            "hierarchy -auto-top; proc; opt_clean; write_json /work/netlist.json"
        )
        completed = run_tool(
            [str(executable), "-q", "-p", script],
            root,
            timeout=YOSYS_TIMEOUT_SECONDS,
            environment=environment,
        )
        if completed.returncode != 0 or not netlist_path.is_file():
            lines, _ = limited_lines(
                "\n".join(part for part in (completed.stdout, completed.stderr) if part), 20
            )
            detail = " | ".join(lines) or "Yosys did not produce a netlist"
            raise AdapterError("yosys-analysis", detail)
        try:
            netlist = json.loads(netlist_path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise AdapterError("result-parse", "Yosys returned invalid netlist JSON") from error
    return target, relative, tool, netlist, round((time.monotonic() - started) * 1000)


def structure_command(
    root: Path, target_name: str, file_name: str, project_dir: str | None = None
) -> tuple[int, dict[str, Any]]:
    target, relative, tool, netlist, duration_ms = build_yosys_netlist(
        root, target_name, file_name, project_dir
    )
    modules = yosys_modules(netlist)
    result = base_result("structure", target, relative)
    result.update(
        {
            "tool": {
                "name": "yosys",
                "version": tool["version"],
                "upstream_version": tool["upstream_version"],
            },
            "status": "pass",
            "duration_ms": duration_ms,
            "summary": {
                "module_count": len(modules),
                "cell_count": sum(module["cell_count"] for module in modules),
                "net_count": sum(module["net_count"] for module in modules),
            },
            "modules": modules,
        }
    )
    return 0, result


def connectivity_command(
    root: Path,
    target_name: str,
    file_name: str,
    signal_name: str,
    project_dir: str | None = None,
) -> tuple[int, dict[str, Any]]:
    target, relative, tool, netlist, duration_ms = build_yosys_netlist(
        root, target_name, file_name, project_dir
    )
    module_name, signal = yosys_signal_connections(netlist, signal_name, relative)
    result = base_result("connectivity", target, relative)
    result.update(
        {
            "tool": {
                "name": "yosys",
                "version": tool["version"],
                "upstream_version": tool["upstream_version"],
            },
            "status": "pass",
            "duration_ms": duration_ms,
            "module": module_name,
            "signal": signal,
        }
    )
    return 0, result


def doctor_command(
    root: Path, target_name: str | None, project_dir: str | None = None
) -> tuple[int, dict[str, Any]]:
    target = None
    if target_name is not None or project_dir is not None:
        target, _ = target_context(root, target_name or "openc910", project_dir)
    tools: list[dict[str, str]] = []
    manifest = load_json(capability_root(root) / "config/toolchain.json")
    for tool in manifest.get("tools", []):
        current, install = tool_context(root, str(tool["name"]))
        command = [str(install / current["version_command"][0]), *current["version_command"][1:]]
        environment = os.environ.copy()
        environment.setdefault("YOWASP_CACHE_DIR", str(root / ".local/cache/yowasp"))
        timeout = YOSYS_TIMEOUT_SECONDS if current["name"] == "yosys" else TOOL_TIMEOUT_SECONDS
        completed = run_tool(command, root, timeout=timeout, environment=environment)
        output = (completed.stdout + completed.stderr).strip()
        if completed.returncode != 0 or str(current["version_contains"]) not in output:
            raise AdapterError("tool-version", f"locked tool version check failed: {current['name']}")
        tools.append({"name": str(current["name"]), "version": str(current["version"])})
    result = (
        base_result("doctor", target)
        if target is not None
        else {"schema": SCHEMA, "operation": "doctor", "scope": "toolchain"}
    )
    result.update({"status": "pass", "toolchain": tools})
    return 0, result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rtl", description=__doc__)
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--target", help="registered development target name")
    selector.add_argument("--project", help="path to a project containing rtl-project.json")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    subparsers.add_parser("doctor", help="verify locked tools and optionally selected target integrity")
    for operation, help_text in (
        ("syntax", "parse one target-relative source file with slang"),
        ("lint", "lint one target-relative source file with Verible"),
        ("outline", "extract a bounded module outline from one source file"),
        ("structure", "synthesize one source file into a bounded structural summary"),
    ):
        command = subparsers.add_parser(operation, help=help_text)
        command.add_argument("--file", help="path relative to source; defaults to project entry")
    connectivity = subparsers.add_parser(
        "connectivity", help="find synthesized cell and module-port connections for one signal"
    )
    connectivity.add_argument("--file", help="path relative to source; defaults to project entry")
    connectivity.add_argument("--signal", required=True, help="exact Yosys net name")
    context = subparsers.add_parser(
        "context", help="get deterministic context for one exact or unique-suffix symbol"
    )
    context.add_argument("action", choices=tuple(CONTEXT_ACTIONS))
    context.add_argument("symbol")
    query = subparsers.add_parser(
        "query", help="start one task-local compile-once PySlang JSONL query session"
    )
    query.add_argument("--build", required=True, help="runtime-query build identity JSON")
    model = subparsers.add_parser("model", help="build and query the SQLite RTL Project Model")
    model_commands = model.add_subparsers(dest="model_operation", required=True)
    model_build = model_commands.add_parser("build", help="build and atomically publish a model")
    model_build.add_argument("--project", required=True, help="path to a portable RTL Project")
    model_build.add_argument("--spec", required=True, help="project-model specification JSON")
    model_build.add_argument(
        "--output", default="analysis/model/rtl-project-model.sqlite", help="Project-relative model path"
    )
    prototype_build = model_commands.add_parser(
        "prototype-build", help="build a source/slang-only Knowledge Contract candidate"
    )
    prototype_build.add_argument("--project", required=True, help="path to a portable RTL Project")
    prototype_build.add_argument("--spec", required=True, help="project-model specification JSON")
    prototype_build.add_argument(
        "--output",
        default="analysis/model/rtl-knowledge-v01-candidate.sqlite",
        help="new Project-relative candidate path",
    )
    prototype_build.add_argument(
        "--ai-overlay", help="optional direct-ID AI overlay JSON"
    )
    runtime_module_build = model_commands.add_parser(
        "runtime-module-build",
        help="build one bounded module candidate from a full runtime-query build",
    )
    runtime_module_build.add_argument("--project", required=True)
    runtime_module_build.add_argument("--build", required=True)
    runtime_module_build.add_argument("--target-path", required=True)
    runtime_module_build.add_argument("--output", required=True)
    runtime_module_build.add_argument(
        "--publication-profile", choices=("module-q2",),
        help="pin the real module-q2 Profile into a new v0.2 Stage1 candidate",
    )
    runtime_design_build = model_commands.add_parser(
        "runtime-design-build",
        help="build a complete elaborated-design Knowledge candidate",
    )
    runtime_design_build.add_argument("--project", required=True)
    runtime_design_build.add_argument("--build", required=True)
    runtime_design_build.add_argument("--output", required=True)
    runtime_design_build.add_argument(
        "--publication-profile", choices=("module-q2", "ibex-q2-fifo", "ibex-q2-fifo-v2"),
        help="pin a fixed Profile into a new whole-design v0.2 Stage1 candidate",
    )
    knowledge_build = model_commands.add_parser(
        "knowledge-build",
        help="build, validate, and render one resumable scope-aware Build1 candidate",
    )
    knowledge_build.add_argument("--project", required=True)
    knowledge_build.add_argument(
        "--scope", required=True,
        help="whole-design or actual-root; unsupported modes return BLOCKED",
    )
    knowledge_build.add_argument(
        "--build", default="runtime-query-build.json",
        help="Project-relative Runtime build identity JSON",
    )
    knowledge_build.add_argument("--target-path")
    knowledge_build.add_argument(
        "--candidate", default="analysis/model/rtl-knowledge-build1.sqlite"
    )
    knowledge_build.add_argument(
        "--manual", default="analysis/manual-v6-build1"
    )
    knowledge_build.add_argument(
        "--source-viewer", choices=("locator", "full"), default="locator"
    )
    manual_preview = model_commands.add_parser(
        "manual-preview", help="render current validated Build2 drafts without publishing"
    )
    manual_preview.add_argument("--project", required=True)
    manual_preview.add_argument("--task", required=True, help="task path relative to cwd or absolute")
    manual_preview.add_argument("--output", required=True, help="new task-relative preview directory")
    manual_build = model_commands.add_parser(
        "manual-build", help="render a scope-aware V6 Build1 Manual from one candidate"
    )
    manual_build.add_argument("--project", required=True)
    manual_build.add_argument("--model", required=True)
    manual_build.add_argument("--output", required=True, help="new Project-relative site directory")
    manual_build.add_argument(
        "--source-viewer", choices=("locator", "full"), default="locator",
        help="locator keeps raw source authoritative; full adds optional source HTML",
    )
    manual_build.add_argument(
        "--formal-q2", action="store_true",
        help="require a complete approved module-q2 bundle in one readonly snapshot",
    )
    manual_build.add_argument(
        "--formal-combination", action="store_true",
        help="require a complete approved Q2+FIFO bundle in one readonly snapshot",
    )
    q2_publish = model_commands.add_parser(
        "q2-publish", help="publish one exact Human-approved module-q2 batch"
    )
    q2_publish.add_argument("--project", required=True)
    q2_publish.add_argument("--model", required=True)
    q2_publish.add_argument("--approval-root", required=True, help="Project-relative review artifact directory")
    q2_publish.add_argument("--approval-file", required=True, help="path relative to approval root")
    q2_publish.add_argument("--approved-sha256", required=True, help="Controller-pinned H(A)")
    q2_publish.add_argument("--batch-sha256", required=True)
    combo_publish = model_commands.add_parser(
        "combo-publish", help="publish one exact approved Q2+FIFO combination batch"
    )
    for flag in ("project", "model", "approval-root", "approval-file", "approved-sha256", "batch-sha256"):
        combo_publish.add_argument("--" + flag, required=True)
    model_query = model_commands.add_parser("query", help="query a fixed, read-only model view")
    model_query.add_argument("--project", required=True, help="path to a portable RTL Project")
    model_query.add_argument(
        "--model", default="analysis/model/rtl-project-model.sqlite", help="Project-relative model path"
    )
    model_query.add_argument(
        "--view",
        required=True,
        choices=("summary", "search", "entity", "path", "fsm", "clock-reset", "coverage", "semantics", "evidence", "context", "module-knowledge"),
    )
    model_query.add_argument("--text")
    model_query.add_argument("--kind")
    model_query.add_argument("--limit", type=int, default=20)
    model_query.add_argument("--name")
    model_query.add_argument("--entity-id")
    model_query.add_argument("--target-name")
    model_query.add_argument("--start")
    model_query.add_argument("--end")
    model_query.add_argument("--start-id")
    model_query.add_argument("--end-id")
    model_query.add_argument("--max-depth", type=int)
    model_query.add_argument(
        "--role", action="append",
        help="required for context; repeat with distinct roles (e.g. structure, connectivity); "
             "supported values come from query_roles in config/knowledge-contract-v01-{facts,ai}.json; "
             "driver is a profile, not a role",
    )
    model_query.add_argument(
        "--profile",
        choices=tuple(sorted(knowledge_prototype.CONTEXT_PROFILES)),
        help="optional context retrieval policy (e.g. driver); does not replace required --role",
    )
    model_query.add_argument("--assertion-id")
    model_query.add_argument("--build-sha256")
    model_query.add_argument("--section", action="append")
    model_query.add_argument("--obligation", action="append", help="exact v0.2 Q2 or FIFO obligation ID")
    model_query.add_argument("--actual-id", help="exact applicable v0.2 actual instance ID")
    model_query.add_argument("--child-instance-id", action="append")
    model_query.add_argument("--request-kind", default="module-understanding")
    model_query.add_argument("--additional-requirement", action="append")
    model_query.add_argument(
        "--knowledge-view", choices=("answer", "full", "audit"), default="answer",
        help="module-knowledge result shape; answer is the compact default",
    )
    model_query.add_argument(
        "--audit-ref", action="append",
        help="exact knowledge:/fact:/evidence: reference to expand in audit view",
    )
    model_query.add_argument(
        "--event-log", help="optional project-relative JSONL event log for this query"
    )
    model_query.add_argument(
        "--knowledge", choices=("on", "off"), default="on",
        help="explicit module-knowledge visibility switch; off never reads candidate content",
    )
    model_export = model_commands.add_parser("export", help="export canonical JSONL")
    model_export.add_argument("--project", required=True, help="path to a portable RTL Project")
    model_export.add_argument(
        "--model", default="analysis/model/rtl-project-model.sqlite", help="Project-relative model path"
    )
    model_export.add_argument("--output", required=True, help="Project-relative JSONL path")
    model_import = model_commands.add_parser("import", help="import canonical JSONL")
    model_import.add_argument("--project", required=True, help="path to a portable RTL Project")
    model_import.add_argument("--input", required=True, help="Project-relative JSONL path")
    model_import.add_argument("--output", required=True, help="Project-relative SQLite path")
    return parser


def emit(result: dict[str, Any]) -> None:
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))


def resolve_model_spec(root: Path, project_dir: Path, value: str) -> Path:
    supplied = Path(value)
    candidate = (supplied if supplied.is_absolute() else Path.cwd() / supplied).resolve()
    allowed = False
    for boundary in (root.resolve(), project_dir.resolve()):
        try:
            candidate.relative_to(boundary)
            allowed = True
            break
        except ValueError:
            continue
    if not allowed:
        raise AdapterError("project-path", "model spec must stay inside the repository or Project")
    if candidate.suffix != ".json" or not candidate.is_file() or candidate.is_symlink():
        raise AdapterError("project-path", "model spec must be a safe .json file")
    return candidate


def _resolve_context_symbol(
    store: knowledge_prototype.PrototypeStore, symbol: str
) -> tuple[str, list[Any]]:
    if not symbol or symbol != symbol.strip() or "\n" in symbol:
        raise AdapterError("context-symbol", "context symbol must be a non-empty exact name")
    query = """
        SELECT entity_id, kind, qualified_name
        FROM entity
        WHERE qualified_name = ?
        ORDER BY qualified_name, kind, entity_id
    """
    rows = store.connection.execute(query, (symbol,)).fetchall()
    if rows:
        return "exact", rows
    escaped = symbol.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = store.connection.execute(
        """
        SELECT entity_id, kind, qualified_name
        FROM entity
        WHERE qualified_name LIKE ? ESCAPE '\\'
        ORDER BY qualified_name, kind, entity_id
        """,
        (f"%.{escaped}",),
    ).fetchall()
    return "suffix", rows


def context_command(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    project_value = args.project or "."
    target, _ = project_context(project_value)
    supplied = Path(project_value)
    project_dir = (
        supplied if supplied.is_absolute() else Path.cwd() / supplied
    ).resolve()
    model_relative = os.environ.get("RTL_CONTEXT_MODEL", DEFAULT_CONTEXT_MODEL)
    database = project_model.resolve_project_file(project_dir, model_relative, ".sqlite")
    store = knowledge_prototype.PrototypeStore.open(database, readonly=True)
    try:
        resolved_by, rows = _resolve_context_symbol(store, args.symbol)
        candidates = [
            {"kind": row["kind"], "qualified_name": row["qualified_name"]}
            for row in rows
        ]
        result = base_result("context", target)
        tool = {
            "action": args.action,
            "symbol": args.symbol,
            "resolved_by": resolved_by,
        }
        if args.action == "module":
            tool["notice"] = (
                "deterministic module overview/localizer; not a semantic summary"
            )
        result["context_tool"] = tool
        if not rows:
            result.update({"status": "not-found", "candidates": []})
            return 2, result
        if len(rows) != 1:
            result.update({"status": "ambiguous", "candidates": candidates})
            return 2, result
        policy = CONTEXT_ACTIONS[args.action]
        details = knowledge_prototype.query_context(
            store,
            seed_entity_id=rows[0]["entity_id"],
            roles=policy["roles"],
            profile=policy["profile"],
            max_depth=2,
            limit=60,
        )
    finally:
        store.close()
    result.update({"status": "pass", **details})
    return 0, result


def model_command(root: Path, args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    if args.model_operation == "manual-preview":
        from rtl_build2_preview import build_preview
        return 0, build_preview(Path(args.project), args.task, args.output)
    target, source_root = project_context(args.project)
    project_dir = Path(args.project)
    project_dir = (project_dir if project_dir.is_absolute() else Path.cwd() / project_dir).resolve()
    operation = args.model_operation
    result = base_result(f"model-{operation}", target)
    if operation == "knowledge-build":
        build_path = project_model._resolve_project_output(project_dir, args.build, ".json")
        candidate = project_model._resolve_project_output(
            project_dir, args.candidate, ".sqlite"
        )
        manual_relative = Path(args.manual)
        if (
            manual_relative.is_absolute()
            or ".." in manual_relative.parts
            or not manual_relative.parts
            or "\n" in args.manual
        ):
            raise AdapterError(
                "project-path", "Manual output must be a safe Project-relative path"
            )
        manual = (project_dir / manual_relative).resolve()
        try:
            manual.relative_to(project_dir)
        except ValueError as error:
            raise AdapterError("project-path", "Manual output escapes the Project") from error
        pyslang_site = None
        scope_can_build = (
            args.scope == "whole-design"
            or (
                args.scope == "actual-root"
                and isinstance(args.target_path, str)
                and bool(args.target_path.strip())
                and args.target_path == args.target_path.strip()
                and "\n" not in args.target_path
            )
        )
        if scope_can_build and not candidate.exists() and not candidate.is_symlink():
            _, pyslang_install = tool_context(root, "pyslang")
            pyslang_site = pyslang_install / "site-packages"
        return build1.run_build1(
            project_name=str(target["name"]),
            source_revision=str(target["revision"]),
            source_root=source_root,
            build_path=build_path,
            pyslang_site=pyslang_site,
            requested_scope=args.scope,
            target_path=args.target_path,
            candidate=candidate,
            candidate_relative=Path(args.candidate).as_posix(),
            manual=manual,
            manual_relative=manual_relative.as_posix(),
            source_viewer=args.source_viewer,
        )
    if operation == "manual-build":
        total_started = time.perf_counter()
        database = project_model.resolve_project_file(project_dir, args.model, ".sqlite")
        relative = Path(args.output)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise AdapterError("project-path", "Manual output must be a safe Project-relative path")
        output = (project_dir / relative).resolve()
        try:
            output.relative_to(project_dir)
        except ValueError as error:
            raise AdapterError("project-path", "Manual output escapes the Project") from error
        projection_started = time.perf_counter()
        if args.formal_q2 and args.formal_combination:
            raise AdapterError("manual-mode", "Choose exactly one formal publication Profile")
        model = (manual_v6.load_formal_q2(database, source_root, combination=args.formal_combination)
                 if args.formal_q2 or args.formal_combination
                 else manual_v6.load_backbone(database, source_root))
        projection_elapsed = time.perf_counter() - projection_started
        render_started = time.perf_counter()
        manifest = manual_v6.build_site(
            model, output, source_viewer=args.source_viewer,
            manual_brand=("RTL Q2+FIFO Manual" if args.formal_combination else
                          "RTL Q2 Manual" if args.formal_q2 else "RTL Build1 Manual"),
        )
        render_elapsed = time.perf_counter() - render_started
        receipt = {
            key: value for key, value in manifest.items()
            if key not in {"page_map", "structured_detail_map"}
        }
        receipt["page_map_count"] = len(manifest["page_map"])
        receipt["structured_detail_map_count"] = len(
            manifest["structured_detail_map"]
        )
        result.update({
            "status": "pass",
            "output": relative.as_posix(),
            "candidate": {
                "path": Path(args.model).as_posix(),
                "sha256": model["candidate_sha256"],
                "build_sha256": model["scope"]["build_identity"]["build_sha256"],
            },
            "scope_id": model["scope"]["scope_id"],
            "scope_mode": model["scope"]["scope_mode"],
            "analysis_scope": model["scope_presentation"]["label"],
            "manual": {
                "path": relative.as_posix(),
                "page_counts": manifest["page_counts"],
                "structured_detail_counts": manifest["structured_detail_counts"],
            },
            "source_serving": manifest["source_serving"],
            "limitations": list(model["scope"].get("limitations", [])),
            "timing": {
                "candidate_projection_wall_seconds": round(projection_elapsed, 6),
                "render_wall_seconds": round(render_elapsed, 6),
                "total_wall_seconds": round(time.perf_counter() - total_started, 6),
            },
            "manifest": receipt,
        })
        return 0, result
    if operation in {"q2-publish", "combo-publish"}:
        publication_path = (knowledge_prototype.Q2_FIFO_V2_PUBLICATION_PATH
                            if operation == "combo-publish" else knowledge_prototype.MODULE_Q2_PUBLICATION_PATH)
        database = project_model.resolve_project_file(project_dir, args.model, ".sqlite")
        relative = Path(args.approval_root)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise AdapterError("project-path", "Approval root must be Project-relative")
        review_root = (project_dir / relative).resolve(strict=True)
        if not review_root.is_dir() or not review_root.is_relative_to(project_dir):
            raise AdapterError("project-path", "Approval root is outside the Project")
        approval, _ = knowledge_prototype._bound_json(review_root, args.approval_file)
        whole, _ = knowledge_prototype._bound_json(review_root, approval["input_path"])
        independent, independent_sha = knowledge_prototype._bound_json(review_root, approval["review_path"])
        matches = [batch for batch in whole["batches"] if hashlib.sha256(
            knowledge_prototype._canonical(batch).encode()
        ).hexdigest() == args.batch_sha256]
        if len(matches) != 1:
            raise AdapterError("publication-batch", "Approved batch digest is absent or duplicated")
        batch = matches[0]
        review = {
            "status": "VERIFIED", "input_sha256": args.batch_sha256,
            "artifact_sha256": independent_sha,
            "reviewer": independent["reviewer"], "generator": independent["generator"],
            "locator": approval["review_path"],
        }
        attrs = knowledge_prototype._publication_review_attrs(batch, review)
        review["evidence_id"] = knowledge_prototype._identity(
            "ev", "provenance", review["locator"], None, None,
            knowledge_prototype._canonical(attrs),
        )
        store = knowledge_prototype.PrototypeStore.open_publication(
            database, publication_contract=publication_path,
        )
        try:
            published = store.publish_reviewed_semantic_overlay(
                {**batch, "review": review}, source_root=source_root,
                review_root=review_root,
                publication_contract=publication_path,
                approval_file=args.approval_file, approved_sha256=args.approved_sha256,
            )
        finally:
            store.close()
        result.update({"status": "pass", "publication": published,
                       "approval_sha256": args.approved_sha256})
        return 0, result
    if operation == "runtime-design-build":
        build_path = resolve_model_spec(root, project_dir, args.build)
        _, pyslang_install = tool_context(root, "pyslang")
        output = project_model._resolve_project_output(project_dir, args.output, ".sqlite")
        details = module_knowledge.build_runtime_design_candidate(
            source_root=source_root,
            build_path=build_path,
            pyslang_site=pyslang_install / "site-packages",
            project_name=str(target["name"]),
            source_revision=str(target["revision"]),
            output=output,
            schema_version="v0.2" if args.publication_profile else "v0.1",
            publication_contract=({
                "module-q2": knowledge_prototype.MODULE_Q2_PUBLICATION_PATH,
                "ibex-q2-fifo": knowledge_prototype.Q2_FIFO_PUBLICATION_PATH,
                "ibex-q2-fifo-v2": knowledge_prototype.Q2_FIFO_V2_PUBLICATION_PATH,
            }.get(args.publication_profile)),
        )
        result.update({"status": "pass", **details})
        return 0, result
    if operation == "runtime-module-build":
        build_path = resolve_model_spec(root, project_dir, args.build)
        _, pyslang_install = tool_context(root, "pyslang")
        output = project_model._resolve_project_output(project_dir, args.output, ".sqlite")
        details = module_knowledge.build_runtime_module_candidate(
            source_root=source_root,
            build_path=build_path,
            pyslang_site=pyslang_install / "site-packages",
            project_name=str(target["name"]),
            source_revision=str(target["revision"]),
            target_path=args.target_path,
            output=output,
            schema_version="v0.2" if args.publication_profile else "v0.1",
            publication_contract=(knowledge_prototype.MODULE_Q2_PUBLICATION_PATH
                                  if args.publication_profile else None),
        )
        result.update({"status": "pass", **details})
        return 0, result
    if operation == "prototype-build":
        spec = resolve_model_spec(root, project_dir, args.spec)
        overlay = resolve_model_spec(root, project_dir, args.ai_overlay) if args.ai_overlay else None
        slang, slang_install = tool_context(root, "slang")
        output = project_model._resolve_project_output(project_dir, args.output, ".sqlite")
        details = knowledge_prototype.build_source_candidate(
            project_name=str(target["name"]),
            source_revision=str(target["revision"]),
            source_root=source_root,
            spec_path=spec,
            database=output,
            slang_executable=slang_install / str(slang["version_command"][0]),
            slang_version=str(slang["version"]),
            temporary_root=temporary_root(root),
            ai_overlay=load_json(overlay) if overlay else None,
        )
        result.update({
            "status": "pass",
            "output": Path(args.output).as_posix(),
            "size_bytes": output.stat().st_size,
            **details,
        })
        return 0, result
    if operation == "build":
        spec = resolve_model_spec(root, project_dir, args.spec)
        slang, slang_install = tool_context(root, "slang")
        yosys, yosys_install = tool_context(root, "yosys")
        details = project_model.build_project_model(
            project_dir=project_dir,
            target=target,
            source_root=source_root,
            spec_path=spec,
            output_relative=args.output,
            slang_executable=slang_install / str(slang["version_command"][0]),
            slang_version=str(slang["version"]),
            yosys_executable=yosys_install / str(yosys["version_command"][0]),
            yosys_version=str(yosys["version"]),
            temporary_root=temporary_root(root),
        )
        result.update({"status": "pass", **details})
        return 0, result
    if operation == "query":
        if args.view == "module-knowledge":
            if args.knowledge == "on" and (not args.build_sha256 or not (args.entity_id or args.target_name)):
                raise AdapterError(
                    "module-knowledge-request",
                    "enabled module-knowledge requires --target-name or --entity-id and --build-sha256",
                )
            started = time.perf_counter()
            if args.knowledge == "off":
                details = knowledge_prototype.empty_module_knowledge_response(
                    reason="knowledge-disabled", candidate_accessed=False,
                    requested_sections=args.section
                    or list(knowledge_prototype.MODULE_KNOWLEDGE_SECTIONS),
                    required_child_instance_ids=args.child_instance_id or [],
                    additional_requirements=args.additional_requirement or [],
                )
                details["response_view"] = args.knowledge_view
                if args.obligation:
                    details["coverage"] = {"requested_obligations": list(dict.fromkeys(args.obligation)),
                                           "ready_obligations": [], "partial_obligations": [],
                                           "missing_obligations": list(dict.fromkeys(args.obligation))}
            else:
                database = project_model.resolve_project_file(project_dir, args.model, ".sqlite")
                prototype_store = knowledge_prototype.PrototypeStore.open(database, readonly=True)
                try:
                    entity_id = args.entity_id
                    child_ids = args.child_instance_id or []
                    resolved_target = None
                    if entity_id is None:
                        resolved_target = knowledge_prototype.resolve_module_knowledge_target(
                            prototype_store, args.target_name
                        )
                        if resolved_target["status"] != "ok":
                            details = knowledge_prototype.empty_module_knowledge_response(
                                reason=f"target-{resolved_target['status']}",
                                candidate_accessed=True,
                                requested_sections=args.section
                                or list(knowledge_prototype.MODULE_KNOWLEDGE_SECTIONS),
                                required_child_instance_ids=[],
                                additional_requirements=args.additional_requirement or [],
                            )
                            details["response_view"] = args.knowledge_view
                            details["target_resolution"] = resolved_target
                        else:
                            entity_id = resolved_target["target_id"]
                            child_ids = resolved_target["direct_child_instance_ids"]
                    if entity_id is not None:
                        if prototype_store.schema_version == "v0.2":
                            if args.section or args.child_instance_id or args.additional_requirement or args.knowledge_view != "answer" or args.audit_ref:
                                details = query_v02._empty("unsupported-v02-query-option", args.obligation or [])
                            else:
                                details = query_v02.route_v02(
                                    prototype_store, database=database, project_root=project_dir,
                                    source_root=source_root,
                                    target_id=entity_id, build_sha256=args.build_sha256,
                                    actual_id=args.actual_id, obligations=args.obligation or [],
                                    request_kind=args.request_kind,
                                )
                        else:
                            if args.obligation or args.actual_id:
                                details = knowledge_prototype.empty_module_knowledge_response(
                                    reason="unsupported-v01-query-option", candidate_accessed=True)
                                details["response_view"] = args.knowledge_view
                            else:
                                details = knowledge_prototype.route_module_knowledge(
                                    prototype_store, enabled=True, target_id=entity_id,
                                    build_sha256=args.build_sha256,
                                    requested_sections=args.section
                                    or list(knowledge_prototype.MODULE_KNOWLEDGE_SECTIONS),
                                    required_child_instance_ids=child_ids,
                                    request_kind=args.request_kind,
                                    additional_requirements=args.additional_requirement or [],
                                    response_view=args.knowledge_view,
                                    audit_refs=args.audit_ref or [],
                                )
                        if resolved_target is not None:
                            details["target_resolution"] = resolved_target
                finally:
                    prototype_store.close()
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            details_bytes = len(json.dumps(
                details, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8"))
            event = {
                "event": "knowledge_route", "source_kind": (
                    "PREBUILT_FACT" if args.knowledge == "on" and args.request_kind == "deterministic-instance-of"
                    and details["route"] == "DIRECT" else "PREBUILT_SEMANTIC"),
                "visibility": args.knowledge, "route": details["route"],
                "reasons": details["reasons"],
                "candidate_accessed": details["candidate_accessed"],
                "response_view": details["response_view"],
                "returned_items": details["returned_items"],
                "returned_counts": details.get("returned_counts", {}),
                "bytes": details_bytes, "duration_ms": duration_ms,
            }
            event_ref = "UNAVAILABLE"
            if args.event_log:
                event_path = project_model._resolve_project_output(
                    project_dir, args.event_log, ".jsonl"
                )
                event_path.parent.mkdir(parents=True, exist_ok=True)
                with event_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(event, sort_keys=True) + "\n")
                event_ref = Path(args.event_log).as_posix()
            next_action = {
                "DIRECT": "STOP_AFTER_MINIMAL_PRESENTATION",
                "CONTEXT": "SUPPLEMENT_EXPLICIT_GAPS",
                "BYPASS": "SOURCE_OR_LIVE_TOOL",
            }[details["route"]]
            result.update({
                "status": "pass", "view": "module-knowledge", **details,
                "telemetry": {"knowledge_query_ms": duration_ms,
                              "source_reads": "UNAVAILABLE",
                              "live_tool_calls": "UNAVAILABLE"},
                "execution_summary": {
                    "object": args.target_name or args.entity_id or "knowledge-disabled",
                    "returned_route": details["route"],
                    "actual_path": "UNAVAILABLE",
                    "response_view": details["response_view"],
                    "returned_items": details["returned_items"],
                    "returned_bytes": details_bytes,
                    "navigation_items": "UNAVAILABLE",
                    "cited_items": "UNAVAILABLE",
                    "detailed_material": (
                        "NOT_EXPANDED; use exact audit refs"
                        if details["response_view"] == "answer" else "EXPANDED_ON_REQUEST"
                    ),
                    "recommended_next_action": next_action,
                    "actual_supplement": "UNAVAILABLE",
                    "returned_route_reasons": details["reasons"],
                    "stop_or_fallback_reason": "UNAVAILABLE",
                    "authoritative_knowledge_ms": duration_ms,
                    "event_ref": event_ref,
                },
            })
            return 0, result
        database = project_model.resolve_project_file(project_dir, args.model, ".sqlite")
        if args.view == "context":
            if not args.entity_id:
                raise AdapterError("context-seed", "context view requires --entity-id")
            if not args.role:
                raise AdapterError("context-role", "context view requires at least one --role")
            prototype_store = knowledge_prototype.PrototypeStore.open(database, readonly=True)
            try:
                details = knowledge_prototype.query_context(
                    prototype_store,
                    seed_entity_id=args.entity_id,
                    roles=args.role,
                    profile=args.profile,
                    max_depth=args.max_depth if args.max_depth is not None else 2,
                    limit=args.limit,
                )
            finally:
                prototype_store.close()
            result.update({"status": "pass", "view": "context", **details})
            return 0, result
        store = project_model.ModelStore.open(database, readonly=True)
        try:
            params = {
                "text": args.text,
                "kind": args.kind,
                "limit": args.limit,
                "name": args.name,
                "entity_id": args.entity_id,
                "start": args.start,
                "end": args.end,
                "start_id": args.start_id,
                "end_id": args.end_id,
                "max_depth": args.max_depth if args.max_depth is not None else 8,
                "assertion_id": args.assertion_id,
            }
            details = store.query(args.view, **params)
        finally:
            store.close()
        result.update({"status": "pass", "view": args.view, **details})
        return 0, result
    if operation == "export":
        database = project_model.resolve_project_file(project_dir, args.model, ".sqlite")
        output = project_model._resolve_project_output(project_dir, args.output, ".jsonl")
        if output.exists():
            raise AdapterError("project-path", "JSONL output already exists")
        output.parent.mkdir(parents=True, exist_ok=True)
        project_model.export_jsonl(database, output)
        result.update({"status": "pass", "output": Path(args.output).as_posix(), "size_bytes": output.stat().st_size})
        return 0, result
    input_path = project_model.resolve_project_file(project_dir, args.input, ".jsonl")
    output = project_model._resolve_project_output(project_dir, args.output, ".sqlite")
    project_model.import_jsonl(input_path, output)
    result.update({"status": "pass", "output": Path(args.output).as_posix(), "size_bytes": output.stat().st_size})
    return 0, result


def query_command(root: Path, args: argparse.Namespace) -> int:
    if args.project is None:
        raise AdapterError("project-configuration", "runtime query requires --project")
    target, source_root = project_context(args.project)
    supplied = Path(args.project)
    project_dir = (supplied if supplied.is_absolute() else Path.cwd() / supplied).resolve()
    build = resolve_model_spec(root, project_dir, args.build)
    _, install = tool_context(root, "pyslang")
    from rtl_source_path import project_runtime_evidence

    class ProjectEvidenceOutput:
        def write(self, line: str) -> None:
            try:
                result = project_runtime_evidence(json.loads(line), project_dir, source_root)
            except ValueError as error:
                raise AdapterError("source-evidence", "Runtime Source evidence path is invalid") from error
            sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")

        def flush(self) -> None:
            sys.stdout.flush()

    with query_runtime.RuntimeQuerySession.open(
        source_root=source_root,
        build_path=build,
        project_name=str(target["name"]),
        source_revision=str(target["revision"]),
        pyslang_site=install / "site-packages",
    ) as session:
        return query_runtime.serve(session, sys.stdin, ProjectEvidenceOutput())


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = project_root()
    try:
        target_name = args.target or "openc910"
        file_name = getattr(args, "file", None)
        if args.operation not in ("doctor", "model", "context", "query") and file_name is None:
            if args.project is None:
                raise AdapterError("target-path", "--file is required for a registered target")
            project, _ = target_context(root, target_name, args.project)
            file_name = str(project["entry"])
        if args.operation == "query":
            return query_command(root, args)
        if args.operation == "model":
            exit_code, result = model_command(root, args)
        elif args.operation == "context":
            exit_code, result = context_command(args)
        elif args.operation == "doctor":
            exit_code, result = doctor_command(root, args.target, args.project)
        elif args.operation == "syntax":
            exit_code, result = syntax_command(root, target_name, file_name, args.project)
        elif args.operation == "lint":
            exit_code, result = lint_command(root, target_name, file_name, args.project)
        elif args.operation == "structure":
            exit_code, result = structure_command(root, target_name, file_name, args.project)
        elif args.operation == "connectivity":
            exit_code, result = connectivity_command(
                root, target_name, file_name, args.signal, args.project
            )
        else:
            exit_code, result = outline_command(root, target_name, file_name, args.project)
    except (
        AdapterError,
        project_model.ModelError,
        knowledge_prototype.ContractError,
        knowledge_prototype.StoreError,
        query_runtime.QueryRuntimeError,
        KeyError,
        OSError,
        ValueError,
        RuntimeError,
        subprocess.TimeoutExpired,
    ) as error:
        if isinstance(error, (AdapterError, project_model.ModelError, query_runtime.QueryRuntimeError)):
            code = error.code
        elif isinstance(error, (knowledge_prototype.ContractError, knowledge_prototype.StoreError)):
            code = "knowledge-prototype"
        else:
            code = "adapter-error"
        if (
            args.operation == "model"
            and getattr(args, "model_operation", None) == "knowledge-build"
        ):
            emit(
                build1.blocked_result(
                    candidate_relative=str(getattr(args, "candidate", "")),
                    manual_relative=str(getattr(args, "manual", "")),
                    source_viewer=str(getattr(args, "source_viewer", "locator")),
                    reason_code="INPUT_OR_ENVIRONMENT_INVALID",
                    message=f"{code}: {error}",
                    next_action=(
                        "Restore the exact Project, source, build, tool, and safe output "
                        "inputs, then rerun the same public command."
                    ),
                )
            )
            return 2
        error_record: dict[str, Any] = {"code": code, "message": str(error)}
        if isinstance(error, query_runtime.QueryRuntimeError) and error.details:
            error_record["details"] = error.details
        emit(
            {
                "schema": SCHEMA,
                "operation": args.operation,
                "status": "error",
                "error": error_record,
            }
        )
        return 2
    emit(result)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
