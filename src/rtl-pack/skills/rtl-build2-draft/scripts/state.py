#!/usr/bin/env python3
"""Save and validate task-local Build2 draft checkpoints. No Store writes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


SCHEMA = "rtl-analysis-agent/build2-draft-state-v1"
PLAN_SCHEMA = "rtl-analysis-agent/build2-plan-v1"
PLAN_SCHEMA_V2 = "rtl-analysis-agent/build2-plan-v2"
COMBINATION_SCHEMA = "rtl-analysis-agent/build2-combination-v1"
STAGES = ("evidence", "draft", "review")
NAME = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
COMBINATION_CHECKS = ("object_configuration", "interface", "priority_defaults", "timing",
                      "duplicate_conflict", "coverage_unknowns")


class StateError(Exception):
    pass


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise StateError(f"missing or unsafe file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateError(f"invalid JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise StateError(f"JSON object required: {path}")
    return data


def atomic_json(path: Path, data: dict) -> None:
    atomic_bytes(path, (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".building-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def immutable_plan(task: Path, plan: dict) -> tuple[str, str]:
    """Publish plan bytes once; a crash before the state swap leaves the old pointer valid."""
    payload = (json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    sha = hashlib.sha256(payload).hexdigest()
    relative = f"plans/{sha}.json"
    path = safe_file(task, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != payload:
            raise StateError(f"immutable plan path changed: {relative}")
        return relative, sha
    fd, name = tempfile.mkstemp(prefix=".building-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(name, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != payload:
                raise StateError(f"immutable plan path changed: {relative}")
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(name)
    return relative, sha


def safe_file(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(p in (".", "..") for p in path.parts):
        raise StateError(f"unsafe relative path: {relative}")
    result = root / path
    if not result.resolve().is_relative_to(root.resolve()) or result.is_symlink():
        raise StateError(f"path escapes root or is symlink: {relative}")
    return result


def plan_file(task: Path, state: dict) -> Path:
    return safe_file(task, state.get("plan_path", "plan.json"))


def current_plan(task: Path, state: dict) -> dict:
    path = plan_file(task, state)
    plan = read_json(path)
    validate_plan(plan)
    if digest(path) != state["plan_sha256"]:
        raise StateError("identity mismatch: plan_sha256")
    return plan


def source_identity(project: Path, config: dict) -> dict:
    source = safe_file(project, config["source"])
    if not source.is_dir():
        raise StateError("Source directory missing")

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(source), *args], capture_output=True, text=True, check=False
        )
        if result.returncode:
            raise StateError(f"Source git identity unavailable: {result.stderr.strip()}")
        return result.stdout.strip()

    try:
        top = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        top = None
    if top is not None and top.returncode == 0 and Path(top.stdout.strip()).resolve() == source.resolve():
        head = git("rev-parse", "HEAD")
        if git("status", "--porcelain", "--untracked-files=all"):
            raise StateError("Source worktree is not clean")
        if head != config["revision"]:
            raise StateError("Source HEAD differs from rtl-project.json revision")
        return {"revision": head, "source_dir": config["source"], "mode": "git-clean"}

    # Portable offline Source snapshots may have no Git metadata. Bind their actual bytes.
    h = hashlib.sha256()
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        if ".git" in path.relative_to(source).parts:
            continue
        if path.is_symlink():
            if not path.resolve().is_relative_to(source.resolve()):
                raise StateError(f"Source symlink escapes root: {relative}")
            h.update(b"L\0" + relative.encode() + b"\0" + os.readlink(path).encode() + b"\n")
        elif path.is_file():
            h.update(b"F\0" + relative.encode() + b"\0" + digest(path).encode() + b"\n")
    return {"revision": "unversioned" if config["revision"] is None else config["revision"], "source_dir": config["source"],
            "mode": "snapshot-sha256", "tree_sha256": h.hexdigest()}


def skill_identity() -> str:
    root = Path(__file__).resolve().parents[1]
    files = sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    h = hashlib.sha256()
    for path in files:
        if path.is_symlink():
            raise StateError("Skill contains a symlink")
        h.update(path.relative_to(root).as_posix().encode() + b"\0" + digest(path).encode() + b"\n")
    return h.hexdigest()


def identity(project: Path, task: Path, receipt_path: Path | None = None) -> dict:
    config_path = project / "rtl-project.json"
    build_path = project / "runtime-query-build.json"
    config = read_json(config_path)
    source = source_identity(project, config)
    previous = read_json(receipt_path) if receipt_path else None
    if previous is None:
        previous = read_json(task / "build1-status.json")
    if previous.get("schema") != "rtl-analysis-agent/build1-status-v1" or previous.get("status") not in ("READY", "PARTIAL"):
        raise StateError("public Build1 receipt has no reusable candidate")
    compile_count = previous.get("heavy_actions", {}).get("compile_build_count")
    if compile_count not in (0, 1):
        raise StateError("Build1 receipt has invalid compile count")
    candidate_record = previous["candidate"]
    if candidate_record.get("status") != "READY" or candidate_record.get("reused") not in (True, False):
        raise StateError("public Build1 candidate is not READY")
    candidate = safe_file(project, candidate_record["path"])
    if digest(candidate) != candidate_record["sha256"]:
        raise StateError("candidate SHA-256 differs from public Build1 receipt")
    build = previous["build_identity"]
    if build["source_revision"] != source["revision"]:
        raise StateError("Build1 Source revision mismatch")
    build_spec = read_json(build_path)
    canonical_build = hashlib.sha256(
        json.dumps(build_spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    if build.get("build_spec_sha256") != canonical_build:
        raise StateError("Build1 receipt and current build specification differ")
    return {
        "project": str(project),
        "source": source,
        "project_sha256": digest(config_path),
        "build_file_sha256": digest(build_path),
        "build_identity": build,
        "scope_id": previous["scope_id"],
        "build1_status": previous["status"],
        "candidate": {"path": candidate_record["path"], "sha256": candidate_record["sha256"]},
        "skill_sha256": skill_identity(),
        "receipt_sha256": digest(receipt_path or task / "build1-status.json"),
    }


def validate_refs(project: Path, task: Path, refs: list) -> list[str]:
    faults = []
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("base") not in ("project", "task"):
            faults.append("invalid reference descriptor")
            continue
        try:
            root = project if ref["base"] == "project" else task
            path = safe_file(root, ref["path"])
            if not path.is_file() or digest(path) != ref.get("sha256"):
                faults.append(f"reference missing or changed: {ref['base']}:{ref['path']}")
        except (StateError, KeyError, TypeError) as exc:
            faults.append(f"reference invalid: {exc}")
    return faults


def nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def map_file(project: Path, task: Path, ref: dict) -> Path:
    if not isinstance(ref, dict) or ref.get("base") not in ("project", "task"):
        raise StateError("invalid map reference descriptor")
    root = project if ref["base"] == "project" else task
    path = safe_file(root, ref.get("path"))
    if not path.is_file() or digest(path) != ref.get("sha256"):
        raise StateError(f"map reference missing or changed: {ref['base']}:{ref['path']}")
    return path


def validate_map_anchor(project: Path, task: Path, block: dict, frozen: dict) -> None:
    anchor = block["map_anchor"]
    manifest_path = map_file(project, task, anchor["manifest"])
    detail_path = map_file(project, task, anchor["detail"])
    manifest = read_json(manifest_path)
    detail = read_json(detail_path)
    build = frozen["build_identity"]
    if (manifest.get("schema") != "rtl-analysis-agent/manual-v6-build1-manifest-v1"
            or manifest.get("candidate_sha256") != frozen["candidate"]["sha256"]
            or manifest.get("build_sha256") != build["build_sha256"]
            or manifest.get("scope_id") != frozen["scope_id"]):
        raise StateError("Build1 map manifest identity mismatch")
    if (detail.get("schema") != "rtl-analysis-agent/manual-v6-structured-detail-v1"
            or detail.get("page_kind") not in ("ACTUAL_INSTANCE", "MODULE_DEFINITION")
            or detail.get("semantic") != "NOT_BUILT"
            or detail.get("scope_id") != frozen["scope_id"]
            or not isinstance(detail.get("object"), dict)
            or detail["object"].get("entity_id") != block["object"]):
        raise StateError(f"Build1 map detail identity mismatch: {block['id']}")
    detail_map = manifest.get("structured_detail_map")
    if not isinstance(detail_map, dict) or not isinstance(detail_map.get(block["object"]), str):
        raise StateError(f"Build1 map has no detail for object: {block['object']}")
    mapped = safe_file(manifest_path.parent, detail_map[block["object"]])
    if mapped.resolve() != detail_path.resolve():
        raise StateError(f"Build1 map detail path mismatch: {block['id']}")


def validate_target_checks(payload: dict, block: dict) -> None:
    checks = payload.get("target_checks")
    if not isinstance(checks, dict) or set(checks) != set(block["covers"]):
        raise StateError(f"review target_checks must exactly cover block {block['id']}")
    for target, check in checks.items():
        if (not isinstance(check, dict) or check.get("status") not in ("self_checked", "open")
                or not nonempty(check.get("note"))):
            raise StateError(f"invalid review target_check: {target}")


def validate_plan(plan: dict) -> None:
    if plan.get("schema") not in (PLAN_SCHEMA, PLAN_SCHEMA_V2) or plan.get("maturity") != "DRAFT_UNREVIEWED":
        raise StateError("invalid Build2 plan schema or maturity")
    if not all(nonempty(plan.get(key)) for key in ("target", "object", "configuration", "boundary")):
        raise StateError("plan requires target, object, configuration and boundary")
    if not isinstance(plan.get("uncovered"), list) or not all(nonempty(item) for item in plan["uncovered"]):
        raise StateError("plan uncovered must be a list of nonempty questions")
    targets = plan.get("targets")
    entries = plan.get("blocks")
    if not isinstance(targets, list) or not targets or not isinstance(entries, list) or not entries:
        raise StateError("plan requires target questions and blocks")
    target_ids = set()
    for index, item in enumerate(targets):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not NAME.fullmatch(item["id"]):
            raise StateError(f"plan.targets[{index}].id must match [a-z][a-z0-9-]{{0,63}}")
        if not nonempty(item.get("question")):
            raise StateError(f"plan.targets[{index}].question must be nonempty")
        if item["id"] in target_ids:
            raise StateError(f"duplicate plan target: {item['id']}")
        target_ids.add(item["id"])
    blocks = {}
    for index, item in enumerate(entries):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not NAME.fullmatch(item["id"]):
            raise StateError(f"plan.blocks[{index}].id must match [a-z][a-z0-9-]{{0,63}}")
        name = item["id"]
        if name in blocks:
            raise StateError(f"duplicate plan block: {name}")
        if not all(nonempty(item.get(key)) for key in ("goal", "object", "configuration", "boundary")):
            raise StateError(f"plan block lacks goal/object/configuration/boundary: {name}")
        covers, depends = item.get("covers"), item.get("depends_on")
        if (not isinstance(covers, list) or not covers or any(value not in target_ids for value in covers)
                or len(set(covers)) != len(covers)):
            raise StateError(f"invalid target coverage: {name}")
        if not isinstance(depends, list) or any(not isinstance(value, str) for value in depends) or len(set(depends)) != len(depends):
            raise StateError(f"invalid dependencies: {name}")
        if plan["schema"] == PLAN_SCHEMA_V2:
            anchor = item.get("map_anchor")
            if not isinstance(anchor, dict) or set(anchor) != {"manifest", "detail"}:
                raise StateError(f"v2 block requires map_anchor: {name}")
            for ref in anchor.values():
                if (not isinstance(ref, dict) or ref.get("base") not in ("project", "task")
                        or not isinstance(ref.get("path"), str) or not re.fullmatch(r"[0-9a-f]{64}", str(ref.get("sha256")))):
                    raise StateError(f"invalid map_anchor reference: {name}")
        blocks[name] = depends
    visiting, visited = set(), set()

    def check(name: str) -> None:
        if name in visiting:
            raise StateError(f"dependency cycle: {name}")
        if name in visited:
            return
        visiting.add(name)
        for upstream in blocks[name]:
            if upstream not in blocks:
                raise StateError(f"dependency missing: {upstream}")
            check(upstream)
        visiting.remove(name)
        visited.add(name)

    for name in blocks:
        check(name)


def validate_combination(payload: dict, plan: dict) -> None:
    if payload.get("schema") != COMBINATION_SCHEMA or payload.get("maturity") != "DRAFT_UNREVIEWED":
        raise StateError("invalid combination schema or maturity")
    if not isinstance(payload.get("blocks"), dict) or not isinstance(payload.get("coverage"), dict):
        raise StateError("combination requires blocks and coverage")
    if set(payload["coverage"]) != {item["id"] for item in plan["targets"]}:
        raise StateError("combination coverage must name every plan target")
    planned_blocks = {item["id"]: item for item in plan["blocks"]}
    for target, names in payload["coverage"].items():
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
            raise StateError(f"combination.coverage.{target} must be a list of distinct plan block IDs")
        for name in names:
            if name not in planned_blocks or target not in planned_blocks[name]["covers"]:
                raise StateError(f"combination.coverage.{target}: {name} must directly list {target} in "
                                 f"plan.blocks[{name}].covers; dependency does not confer coverage")
        if plan["schema"] == PLAN_SCHEMA_V2 and set(names) != {
            name for name, block in planned_blocks.items() if target in block["covers"]
        }:
            raise StateError(f"combination.coverage.{target} must list every direct owner")
    checks = payload.get("checks")
    if not isinstance(checks, dict) or set(checks) != set(COMBINATION_CHECKS):
        raise StateError("combination requires all six checks")
    for name, check in checks.items():
        if (not isinstance(check, dict) or check.get("status") not in ("pass", "open", "conflict")
                or not nonempty(check.get("note"))):
            raise StateError(f"invalid combination check: {name}")
    for key in ("conflicts", "uncovered", "unknowns"):
        if not isinstance(payload.get(key), list) or not all(nonempty(item) for item in payload[key]):
            raise StateError(f"combination {key} must be a list of nonempty descriptions")
    if not nonempty(payload.get("next_action")) or not isinstance(payload.get("references"), list) or not payload["references"]:
        raise StateError("combination requires references and next_action")


def archive_artifact(task: Path, record: dict, relative: str) -> dict:
    original = safe_file(task, record["path"])
    result = {"recorded_sha256": record["sha256"], "observed_sha256": None,
              "matches_recorded_sha256": False, "archive": None}
    if not original.is_file():
        return result
    observed = digest(original)
    archive = safe_file(task, f"{relative}-{observed}.json")
    if archive.exists():
        if digest(archive) != observed:
            raise StateError(f"revision archive changed: {archive}")
    else:
        atomic_bytes(archive, original.read_bytes())
    result.update({"observed_sha256": observed, "matches_recorded_sha256": observed == record["sha256"],
                   "archive": archive.relative_to(task).as_posix()})
    return result


def inspect_combination(task: Path, state: dict, plan: dict, blocks: dict, project: Path) -> dict:
    record = state.get("combination")
    if record is None:
        return {"status": "missing", "issues": ["combination self-check not saved"]}
    issues = []
    try:
        if plan["schema"] == PLAN_SCHEMA_V2 and record.get("plan_sha256") != state["plan_sha256"]:
            issues.append("combination plan version stale")
        artifact = safe_file(task, record["path"])
        if not artifact.is_file() or digest(artifact) != record["sha256"]:
            raise StateError("combination artifact missing or changed")
        payload = read_json(artifact)
        validate_combination(payload, plan)
        issues.extend(validate_refs(project, task, payload["references"]))
        if set(payload["blocks"]) != set(state["blocks"]):
            issues.append("combination block set differs from plan")
        for name, block in state["blocks"].items():
            review = block["stages"].get("review")
            if (review is None or payload["blocks"].get(name) != review["sha256"]
                    or len(blocks[name]["valid_stages"]) != len(STAGES)):
                issues.append(f"combination block version stale: {name}")
        if issues:
            return {"status": "stale", "issues": issues}
        for target in plan["targets"]:
            covered_by = payload["coverage"][target["id"]]
            if not covered_by or any(name not in state["blocks"] or target["id"] not in state["blocks"][name]["covers"] for name in covered_by):
                issues.append(f"target uncovered: {target['id']}")
            if plan["schema"] == PLAN_SCHEMA_V2:
                for name in covered_by:
                    report = blocks[name]
                    if len(report["valid_stages"]) != len(STAGES):
                        issues.append(f"target not self-checked: {target['id']}/{name}")
                        continue
                    review_path = safe_file(task, state["blocks"][name]["stages"]["review"]["path"])
                    review_payload = read_json(review_path)
                    if review_payload["target_checks"][target["id"]]["status"] != "self_checked":
                        issues.append(f"target not self-checked: {target['id']}/{name}")
        if payload["conflicts"]:
            issues.append("conflicts remain")
        if payload["uncovered"]:
            issues.append("uncovered questions remain")
        if payload["unknowns"]:
            issues.append("unknowns remain")
        for name, check in payload["checks"].items():
            if check["status"] != "pass":
                issues.append(f"combination check open: {name}")
        return {"status": "open" if issues else "draft_self_checked", "issues": issues,
                "sha256": record["sha256"]}
    except (StateError, KeyError, TypeError, OSError) as exc:
        return {"status": "stale", "issues": [str(exc)]}


def task_project(task: Path) -> Path:
    # A task can be nested below analysis/tasks. Locate its current Project,
    # never the frozen absolute root; a nested group may itself be named tasks.
    for parent in task.parents:
        if (parent.name == "tasks" and parent.parent.name == "analysis"
                and (parent.parent.parent / "rtl-project.json").is_file()):
            return parent.parent.parent
    raise StateError("task is not under Project analysis/tasks")


def inspect(task: Path, state: dict) -> dict:
    project = task_project(task)
    faults = []
    plan = None
    try:
        current = identity(project, task)
        for key, value in state["identity"].items():
            if key == "project":
                continue  # Location only; all content and receipt identities stay frozen.
            if current.get(key) != value:
                faults.append(f"identity mismatch: {key}")
    except (StateError, KeyError, OSError) as exc:
        faults.append(f"identity invalid: {exc}")
    if "plan_sha256" in state:
        try:
            plan = current_plan(task, state)
            planned = {item["id"]: item for item in plan["blocks"]}
            if set(planned) != set(state["blocks"]):
                faults.append("identity mismatch: plan blocks")
            else:
                for name, item in planned.items():
                    block = state["blocks"][name]
                    keys = ("goal", "object", "configuration", "boundary", "covers")
                    if plan["schema"] == PLAN_SCHEMA_V2:
                        keys += ("map_anchor",)
                    if any(block.get(key) != item[key] for key in keys) or set(block["dependencies"]) != set(item["depends_on"]):
                        faults.append(f"identity mismatch: plan block {name}")
        except (StateError, KeyError, TypeError, OSError) as exc:
            faults.append(f"identity invalid: plan: {exc}")
    global_stale = bool(faults)
    local = {}
    for name, block in state["blocks"].items():
        valid = []
        issues = []
        if plan is not None and plan["schema"] == PLAN_SCHEMA_V2:
            try:
                planned_block = next(item for item in plan["blocks"] if item["id"] == name)
                validate_map_anchor(project, task, planned_block, state["identity"])
            except (StateError, KeyError, TypeError, OSError, StopIteration) as exc:
                issues.append(f"map anchor invalid: {name}: {exc}")
        for stage in STAGES:
            record = block["stages"].get(stage)
            if record is None:
                break
            if issues:
                break
            try:
                artifact = safe_file(task, record["path"])
                if not artifact.is_file() or digest(artifact) != record["sha256"]:
                    issues.append(f"artifact missing or changed: {name}/{stage}")
                    break
                payload = read_json(artifact)
                if payload.get("stage") != stage or payload.get("maturity") != "DRAFT_UNREVIEWED":
                    issues.append(f"invalid stage artifact: {name}/{stage}")
                    break
                if plan is not None and plan["schema"] == PLAN_SCHEMA_V2 and stage == "review":
                    validate_target_checks(payload, planned_block)
                issues.extend(validate_refs(project, task, payload.get("references", [])))
                if issues:
                    break
                valid.append(stage)
            except (StateError, KeyError, TypeError, OSError) as exc:
                issues.append(f"invalid artifact: {name}/{stage}: {exc}")
                break
        local[name] = (valid, issues)

    # Resolve dependencies after every block has been checked. JSON keys are sorted on
    # disk, so a single pass would miss stale upstreams whose names sort later.
    blocks = {}
    visiting = set()

    def resolve(name: str) -> dict:
        if name in visiting:
            raise StateError(f"dependency cycle: {name}")
        if name in blocks:
            return blocks[name]
        visiting.add(name)
        block = state["blocks"][name]
        valid, local_issues = local[name]
        dependency_issues = []
        waiting_on = []
        for upstream, expected in block["dependencies"].items():
            other = state["blocks"].get(upstream)
            if other is None:
                dependency_issues.append(f"dependency missing: {upstream}")
                continue
            upstream_report = resolve(upstream)
            review = other.get("stages", {}).get("review")
            if expected is None and not block["stages"]:
                if upstream_report["issues"]:
                    dependency_issues.append(f"dependency stale: {upstream}")
                elif len(upstream_report["valid_stages"]) != len(STAGES):
                    waiting_on.append(upstream)
                continue
            if expected is None or review is None or review.get("sha256") != expected:
                dependency_issues.append(f"dependency changed or missing: {upstream}")
            else:
                try:
                    artifact = safe_file(task, review["path"])
                    if not artifact.is_file() or digest(artifact) != expected:
                        dependency_issues.append(f"dependency artifact changed: {upstream}")
                except (StateError, KeyError, TypeError, OSError):
                    dependency_issues.append(f"dependency artifact changed: {upstream}")
            if upstream_report["issues"] or len(upstream_report["valid_stages"]) != len(STAGES):
                dependency_issues.append(f"dependency stale: {upstream}")
        visiting.remove(name)
        issues = [*faults, *local_issues, *dependency_issues] if global_stale else [*local_issues, *dependency_issues]
        next_stage = STAGES[len(valid)] if len(valid) < len(STAGES) else None
        if global_stale:
            next_action = "revalidate frozen identity; start a new task version if changed"
        elif local_issues:
            next_action = f"repair {next_stage}"
        elif dependency_issues:
            upstream = dependency_issues[0].split(": ", 1)[1]
            next_action = f"repair dependency {upstream}"
        elif waiting_on:
            next_action = f"wait for dependency {waiting_on[0]}/review"
        elif next_stage:
            next_action = f"save {next_stage}"
        else:
            next_action = "draft block saved; await review"
        blocks[name] = {
            "valid_stages": [] if global_stale or dependency_issues else valid,
            "preserved_stages": valid,
            "issues": issues,
            "waiting_on": waiting_on,
            "next_action": next_action,
        }
        return blocks[name]

    for name in state["blocks"]:
        resolve(name)
    stale = global_stale or any(b["issues"] for b in blocks.values())
    repair = next((f"{name}: {report['next_action']}" for name, report in blocks.items()
                   if report["issues"] and not all(issue.startswith("dependency") for issue in report["issues"])), None)
    if repair is None:
        repair = next((f"{name}: {report['next_action']}" for name, report in blocks.items()
                       if report["issues"]), None)
    if plan is None:
        remaining = list(state["remaining_scope"])
        for name, report in blocks.items():
            if report["issues"] and state["blocks"][name]["goal"] not in remaining:
                remaining.append(state["blocks"][name]["goal"])
        return {"schema": SCHEMA, "status": "stale" if stale else "ready",
                "identity_issues": faults, "blocks": blocks, "remaining_scope": remaining,
                "unresolved": state["unresolved"], "budget": state["budget"],
                "next_action": repair if stale else state["next_action"]}

    not_started = [name for name, block in state["blocks"].items() if not block["stages"]]
    incomplete = [name for name, report in blocks.items() if len(report["valid_stages"]) != len(STAGES)]
    uncovered_targets = [item["id"] for item in plan["targets"] if not any(
        item["id"] in block["covers"] for block in plan["blocks"])]
    targets = {}
    if plan["schema"] == PLAN_SCHEMA_V2:
        for item in plan["targets"]:
            target = item["id"]
            owners = [block["id"] for block in plan["blocks"] if target in block["covers"]]
            status = "pending" if not owners else "self_checked"
            for owner in owners:
                report = blocks[owner]
                if report["issues"] or len(report["valid_stages"]) != len(STAGES):
                    status = "open"
                    break
                review = read_json(safe_file(task, state["blocks"][owner]["stages"]["review"]["path"]))
                if review["target_checks"][target]["status"] != "self_checked":
                    status = "open"
                    break
            targets[target] = {"status": status, "owners": owners}
    combination = inspect_combination(task, state, plan, blocks, project)
    if combination["status"] == "stale":
        stale = True
    complete = (not stale and not incomplete and not uncovered_targets and not plan["uncovered"]
                and (not targets or all(item["status"] == "self_checked" for item in targets.values()))
                and not state["unresolved"] and combination["status"] == "draft_self_checked")
    remaining = [] if complete else [item["question"] for item in plan["targets"]]
    remaining.extend(plan["uncovered"])
    if not complete and combination["status"] == "missing" and not incomplete:
        action = "save combination self-check"
    elif not complete and combination["status"] in ("stale", "open") and not incomplete:
        action = "revise combination self-check"
    elif incomplete:
        first = next((name for name in state["blocks"] if name in incomplete and not blocks[name]["waiting_on"]),
                     incomplete[0])
        action = f"{first}: {blocks[first]['next_action']}"
    elif state["unresolved"] or plan["uncovered"] or uncovered_targets:
        action = "resolve unknown or uncovered target; revise affected draft and combination"
    else:
        action = state["next_action"]
    result = {"schema": SCHEMA, "status": "stale" if stale else "ready", "completion":
            "draft_self_checked" if complete else "open", "identity_issues": faults,
            "blocks": blocks, "plan": {"sha256": state["plan_sha256"],
                                   "not_started_blocks": not_started, "uncovered_targets": uncovered_targets,
                                   "uncovered": plan["uncovered"]},
            "combination": combination, "remaining_scope": remaining,
            "unresolved": state["unresolved"], "budget": state["budget"],
            "next_action": repair if stale and repair else action}
    if plan["schema"] == PLAN_SCHEMA_V2:
        result["targets"] = targets
    return result


def load_state(task: Path) -> dict:
    state = read_json(task / "skill.state")
    if state.get("schema") != SCHEMA or not isinstance(state.get("blocks"), dict):
        raise StateError("unsupported skill.state")
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "add-block", "extend-plan", "save", "revise", "save-combination", "resume"))
    parser.add_argument("--task", required=True)
    parser.add_argument("--project")
    parser.add_argument("--receipt")
    parser.add_argument("--plan")
    parser.add_argument("--target")
    parser.add_argument("--block")
    parser.add_argument("--depends-on", action="append", default=[])
    parser.add_argument("--stage", choices=STAGES)
    parser.add_argument("--input")
    parser.add_argument("--budget-turns", type=int)
    parser.add_argument("--stop-reason")
    parser.add_argument("--reason")
    args = parser.parse_args()
    try:
        task = Path(args.task).resolve()
        if args.budget_turns is not None and args.budget_turns < 0:
            raise StateError("budget turns cannot be negative")
        if args.command == "start":
            if (task / "skill.state").exists():
                raise StateError("skill.state already exists")
            if not args.project or not args.receipt:
                raise StateError("start requires project and receipt")
            project = Path(args.project).resolve()
            if (not task.is_relative_to(project / "analysis" / "tasks")
                    or task_project(task) != project):
                raise StateError("task must be under Project analysis/tasks")
            receipt = Path(args.receipt).resolve()
            if receipt != task / "build1-status.json":
                raise StateError("save public receipt as task/build1-status.json")
            frozen = identity(project, task, receipt)
            if args.plan:
                if args.target or args.block:
                    raise StateError("planned start uses --plan instead of --target/--block")
                plan = read_json(Path(args.plan))
                validate_plan(plan)
                if plan["schema"] == PLAN_SCHEMA_V2:
                    for item in plan["blocks"]:
                        validate_map_anchor(project, task, item, frozen)
                    relative, plan_sha = immutable_plan(task, plan)
                    plan_path = safe_file(task, relative)
                else:
                    plan_path = task / "plan.json"
                    if plan_path.exists():
                        raise StateError("plan.json already exists")
                    atomic_json(plan_path, plan)
                    plan_sha = digest(plan_path)
                blocks = {item["id"]: {"goal": item["goal"], "object": item["object"],
                                       "configuration": item["configuration"], "boundary": item["boundary"],
                                       "covers": item["covers"],
                                       **({"map_anchor": item["map_anchor"]} if plan["schema"] == PLAN_SCHEMA_V2 else {}),
                                       "dependencies": {upstream: None for upstream in item["depends_on"]},
                                       "stages": {}, "unknowns": [], "revisions": []}
                          for item in plan["blocks"]}
                state = {"schema": SCHEMA, "identity": frozen, "plan_sha256": plan_sha,
                         **({"plan_path": relative, "plan_revisions": []} if plan["schema"] == PLAN_SCHEMA_V2 else {}),
                         "target": plan["target"], "remaining_scope":
                         [item["question"] for item in plan["targets"]] + plan["uncovered"],
                         "unresolved": [], "budget": {"turns_remaining": args.budget_turns, "stop_reason": None},
                         "next_action": "save first planned block/evidence", "blocks": blocks}
            else:
                if not args.target or not args.block or not NAME.fullmatch(args.block):
                    raise StateError("legacy start requires target and safe block ID; new tasks should use --plan")
                state = {"schema": SCHEMA, "identity": frozen,
                         "target": args.target, "remaining_scope": [args.target], "unresolved": [],
                         "budget": {"turns_remaining": args.budget_turns, "stop_reason": None},
                         "next_action": f"save {args.block}/evidence",
                         "blocks": {args.block: {"goal": args.target, "dependencies": {}, "stages": {}, "unknowns": []}}}
            atomic_json(task / "skill.state", state)
        elif args.command == "resume":
            state = load_state(task)
            print(json.dumps(inspect(task, state), indent=2, ensure_ascii=False, sort_keys=True))
            return 0
        else:
            state = load_state(task)
            report = inspect(task, state)
            project = task_project(task)
            if report["identity_issues"]:
                raise StateError("; ".join(report["identity_issues"]))
            if args.command == "extend-plan":
                if "plan_sha256" not in state or not args.plan or not nonempty(args.reason):
                    raise StateError("extend-plan requires a v2 planned task, complete --plan and nonempty --reason")
                old = current_plan(task, state)
                if old["schema"] != PLAN_SCHEMA_V2:
                    raise StateError("extend-plan supports v2 plans only")
                if any(block["issues"] for block in report["blocks"].values()):
                    raise StateError("repair stale block stages, dependencies or map anchors before extending")
                new = read_json(Path(args.plan))
                validate_plan(new)
                if set(new) != set(old):
                    raise StateError("extend-plan changes frozen top-level fields")
                for key in old.keys() - {"targets", "blocks", "uncovered"}:
                    if new[key] != old[key]:
                        raise StateError(f"extend-plan changes frozen field: {key}")
                for key in ("targets", "blocks", "uncovered"):
                    if new[key][:len(old[key])] != old[key]:
                        raise StateError(f"extend-plan changes or removes old {key}")
                if len(new["targets"]) == len(old["targets"]) and len(new["blocks"]) == len(old["blocks"]):
                    raise StateError("extend-plan requires at least one new target or block")
                for item in new["blocks"]:
                    validate_map_anchor(project, task, item, state["identity"])
                relative, sha = immutable_plan(task, new)
                prior_path, prior_sha = plan_file(task, state).relative_to(task).as_posix(), state["plan_sha256"]
                for item in new["blocks"][len(old["blocks"]):]:
                    state["blocks"][item["id"]] = {
                        "goal": item["goal"], "object": item["object"],
                        "configuration": item["configuration"], "boundary": item["boundary"],
                        "covers": item["covers"], "map_anchor": item["map_anchor"],
                        "dependencies": {upstream: None for upstream in item["depends_on"]},
                        "stages": {}, "unknowns": [], "revisions": []}
                state.setdefault("plan_revisions", []).append({
                    "reason": args.reason, "before_path": prior_path, "before_sha256": prior_sha,
                    "after_path": relative, "after_sha256": sha})
                state["plan_path"], state["plan_sha256"] = relative, sha
                state["remaining_scope"] = [item["question"] for item in new["targets"]] + new["uncovered"]
                state["next_action"] = "save new planned block/evidence; recombine after changes"
                atomic_json(task / "skill.state", state)
            elif args.command == "add-block":
                if "plan_sha256" in state:
                    raise StateError("planned task already contains every block; revise the plan in a new task version")
                if not args.block or not NAME.fullmatch(args.block) or not args.target:
                    raise StateError("add-block requires safe block ID and target")
                if args.block in state["blocks"]:
                    raise StateError("block already exists")
                deps = {}
                for upstream in args.depends_on:
                    other = state["blocks"].get(upstream)
                    if not other or report["blocks"][upstream]["issues"] or len(report["blocks"][upstream]["valid_stages"]) != 3:
                        raise StateError(f"dependency is not a saved draft block: {upstream}")
                    deps[upstream] = other["stages"]["review"]["sha256"]
                state["blocks"][args.block] = {"goal": args.target, "dependencies": deps, "stages": {}, "unknowns": []}
                state["remaining_scope"].append(args.target)
                state["next_action"] = f"save {args.block}/evidence"
            elif args.command == "save-combination":
                if "plan_sha256" not in state or not args.input:
                    raise StateError("save-combination requires a planned task and input")
                if report["status"] == "stale" and report["combination"]["status"] != "stale":
                    raise StateError("task is stale; repair blocks or identity first")
                if any(len(block["valid_stages"]) != len(STAGES) for block in report["blocks"].values()):
                    raise StateError("all planned blocks need valid review before combination")
                plan = current_plan(task, state)
                payload = read_json(Path(args.input))
                validate_combination(payload, plan)
                if set(payload["blocks"]) != set(state["blocks"]) or any(
                    payload["blocks"][name] != block["stages"]["review"]["sha256"]
                    for name, block in state["blocks"].items()
                ):
                    raise StateError("combination block versions differ from current reviews")
                faults = validate_refs(project, task, payload["references"])
                if faults:
                    raise StateError("; ".join(faults))
                previous = state.get("combination")
                if previous:
                    if not nonempty(args.reason):
                        raise StateError("replacing combination requires --reason")
                    archive = archive_artifact(task, previous, "history/combination")
                    state.setdefault("combination_revisions", []).append({"reason": args.reason, **archive})
                artifact_rel = "combination.json"
                artifact = safe_file(task, artifact_rel)
                atomic_json(artifact, payload)
                state["combination"] = {"path": artifact_rel, "sha256": digest(artifact)}
                if plan["schema"] == PLAN_SCHEMA_V2:
                    state["combination"]["plan_sha256"] = state["plan_sha256"]
                covered = all(payload["coverage"][item["id"]] and all(
                    name in state["blocks"] and item["id"] in state["blocks"][name]["covers"]
                    for name in payload["coverage"][item["id"]]
                ) for item in plan["targets"])
                open_checks = any(check["status"] != "pass" for check in payload["checks"].values())
                target_done = (plan["schema"] != PLAN_SCHEMA_V2 or all(
                    report["targets"][item["id"]]["status"] == "self_checked" for item in plan["targets"]))
                complete = (covered and target_done and not plan["uncovered"] and not state["unresolved"] and
                            not open_checks and not payload["conflicts"] and not payload["uncovered"] and
                            not payload["unknowns"])
                state["remaining_scope"] = ([] if complete else
                                            [item["question"] for item in plan["targets"]] + plan["uncovered"])
                state["next_action"] = payload["next_action"]
                atomic_json(task / "skill.state", state)
            else:
                if not args.block or args.block not in state["blocks"] or not args.stage or not args.input:
                    raise StateError("save/revise requires known block, stage and input")
                block = state["blocks"][args.block]
                status = report["blocks"][args.block]
                stale_dependency = any(issue.startswith("dependency") for issue in status["issues"])
                rebind = ("plan_sha256" in state and args.command == "revise" and args.stage == "evidence")
                if stale_dependency and not rebind:
                    raise StateError("dependency stale; revise this block's evidence after repairing upstream")
                if rebind:
                    for upstream in block["dependencies"]:
                        other_status = report["blocks"][upstream]
                        if other_status["issues"] or len(other_status["valid_stages"]) != len(STAGES):
                            raise StateError(f"dependency not ready: {upstream}")
                index = STAGES.index(args.stage)
                if status["preserved_stages"][:index] != list(STAGES[:index]):
                    raise StateError("save/revise must follow valid stage order")
                if args.command == "save" and status["valid_stages"] != list(STAGES[:index]):
                    raise StateError("save must follow valid stage order; completed stages are reused")
                if "plan_sha256" in state and args.command == "save" and args.stage in block["stages"]:
                    raise StateError("stage already recorded; use revise with a reason")
                if args.command == "revise" and (args.stage not in block["stages"] or not nonempty(args.reason)):
                    raise StateError("revise requires a recorded stage and --reason")
                if status.get("waiting_on"):
                    raise StateError(f"dependency not ready: {status['waiting_on'][0]}")
                if "plan_sha256" in state and args.stage == "evidence" and not block["stages"]:
                    for upstream in block["dependencies"]:
                        other_status = report["blocks"][upstream]
                        if other_status["issues"] or len(other_status["valid_stages"]) != len(STAGES):
                            raise StateError(f"dependency not ready: {upstream}")
                        block["dependencies"][upstream] = state["blocks"][upstream]["stages"]["review"]["sha256"]
                payload = read_json(Path(args.input))
                if payload.get("stage") != args.stage or payload.get("maturity") != "DRAFT_UNREVIEWED":
                    raise StateError("input stage/maturity mismatch")
                if not isinstance(payload.get("references"), list) or not payload["references"]:
                    raise StateError("stage requires at least one existing Fact/Source/artifact reference")
                if (not isinstance(payload.get("unknowns"), list)
                    or not all(isinstance(item, str) for item in payload["unknowns"])
                    or not isinstance(payload.get("next_action"), str)
                    or not payload["next_action"].strip()):
                    raise StateError("stage requires unknowns list and nonempty next_action")
                faults = validate_refs(project, task, payload["references"])
                if faults:
                    raise StateError("; ".join(faults))
                required = {"evidence": "observations", "draft": "claims", "review": "checks"}[args.stage]
                if not isinstance(payload.get(required), list) or not payload[required]:
                    raise StateError(f"stage requires nonempty {required}")
                if "plan_sha256" in state:
                    plan = current_plan(task, state)
                    if plan["schema"] == PLAN_SCHEMA_V2 and args.stage == "review":
                        planned_block = next(item for item in plan["blocks"] if item["id"] == args.block)
                        validate_target_checks(payload, planned_block)
                artifact_rel = f"blocks/{args.block}/{args.stage}.json"
                artifact = safe_file(task, artifact_rel)
                if args.command == "revise":
                    affected = {}
                    for old_stage in STAGES[index:]:
                        old_record = block["stages"].get(old_stage)
                        if old_record is not None:
                            archive = archive_artifact(task, old_record,
                                                       f"blocks/{args.block}/history/{old_stage}")
                            affected[old_stage] = {"record": dict(old_record), **archive}
                    revision = {"stage": args.stage, "reason": args.reason,
                                "affected_stages": affected, "old_dependencies": dict(block["dependencies"]),
                                **{key: value for key, value in affected[args.stage].items() if key != "record"}}
                    if rebind:
                        for upstream in block["dependencies"]:
                            block["dependencies"][upstream] = state["blocks"][upstream]["stages"]["review"]["sha256"]
                        revision["new_dependencies"] = dict(block["dependencies"])
                    block.setdefault("revisions", []).append(revision)
                atomic_json(artifact, payload)
                for later in STAGES[index:]:
                    block["stages"].pop(later, None)
                block["stages"][args.stage] = {"path": artifact_rel, "sha256": digest(artifact)}
                block["unknowns"] = payload["unknowns"]
                state["unresolved"] = list(dict.fromkeys(
                    item for current_block in state["blocks"].values()
                    for item in current_block.get("unknowns", [])
                ))
                if args.budget_turns is not None:
                    state["budget"]["turns_remaining"] = args.budget_turns
                if args.stop_reason:
                    state["budget"]["stop_reason"] = args.stop_reason
                if "plan_sha256" not in state:
                    state["remaining_scope"] = [
                        entry["goal"] for entry in state["blocks"].values()
                        if "review" not in entry["stages"]
                    ]
                else:
                    plan = current_plan(task, state)
                    state["remaining_scope"] = [item["question"] for item in plan["targets"]] + plan["uncovered"]
                state["next_action"] = payload.get("next_action") or (
                    f"save {args.block}/{STAGES[index + 1]}" if index + 1 < len(STAGES)
                    else "await independent review; draft remains UNREVIEWED"
                )
                atomic_json(task / "skill.state", state)
            if args.command == "add-block":
                atomic_json(task / "skill.state", state)
        print(json.dumps(inspect(task, state), indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    except (StateError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
