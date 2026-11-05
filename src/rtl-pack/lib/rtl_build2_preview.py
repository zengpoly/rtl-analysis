"""Read-only projection of validated current Build2 artifacts; no RTL inference."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

from jinja2 import DictLoader, Environment, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def _helper():
    # Only installed product code is executable. Never import from a task.
    spec = importlib.util.spec_from_file_location(
        "rtl_build2_preview_state", ROOT / "skills/rtl-build2-draft/scripts/state.py")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _no_links(root: Path, path: Path) -> Path:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"preview does not consume or write through symlinks: {path}")
    return path


def build_preview(project: Path, task_value: str, output: str) -> dict:
    helper = _helper()
    try:
        return _build(helper, project.resolve(), Path(task_value).resolve(), output)
    except helper.StateError as exc:
        raise ValueError(f"Build2 validation failed: {exc}") from exc


def _build(helper, project: Path, task: Path, output: str) -> dict:
    if not task.is_relative_to(project / "analysis/tasks") or helper.task_project(task) != project:
        raise ValueError("task must belong to the specified Project analysis/tasks")
    _no_links(project, task)
    destination = _no_links(task, helper.safe_file(task, output))
    if destination.exists():
        raise ValueError("preview output already exists; choose a new task-relative directory")
    snapshots = {}

    def capture(path, expected=None, parse=False, text=False):
        root = project if path.is_relative_to(project) else ROOT
        _no_links(root, path)
        payload = path.read_bytes() if parse or text else None
        sha = hashlib.sha256(payload).hexdigest() if payload is not None else helper.digest(path)
        if expected is not None and sha != expected:
            raise ValueError(f"input changed: {path}")
        if path in snapshots and snapshots[path] != sha:
            raise ValueError(f"input changed during preview: {path}")
        snapshots[path] = sha
        return json.loads(payload) if parse else payload

    state = capture(task / "skill.state", parse=True)
    report = helper.inspect(task, state)
    if report["identity_issues"] or report["status"] != "ready":
        raise ValueError("Build2 task is stale: " + json.dumps(report, ensure_ascii=False)
                         + "; preserve the matching old installation or start a new task; never edit hashes")
    if not any("draft" in item["valid_stages"] for item in report["blocks"].values()):
        raise ValueError("preview requires at least one current valid draft")
    plan = (capture(helper.plan_file(task, state), state["plan_sha256"], True)
            if "plan_sha256" in state else None)
    for name in ("rtl-project.json", "runtime-query-build.json"):
        capture(project / name)
    capture(task / "build1-status.json", state["identity"]["receipt_sha256"])
    candidate = state["identity"]["candidate"]
    capture(helper.safe_file(project, candidate["path"]), candidate["sha256"])
    capture(ROOT / "skills/rtl-build2-draft/scripts/state.py")

    def reference(ref, parse=False):
        root = project if ref["base"] == "project" else task
        return capture(helper.safe_file(root, ref["path"]), ref["sha256"], parse)

    blocks = []
    for name, block in state["blocks"].items():
        valid = report["blocks"][name]["valid_stages"]
        stages = {}
        for stage in valid:
            record = block["stages"][stage]
            payload = capture(helper.safe_file(task, record["path"]), record["sha256"], True)
            for ref in payload["references"]:
                reference(ref)
            stages[stage] = {"payload": payload, **record}
        detail = None
        if block.get("map_anchor"):
            anchor = block["map_anchor"]
            mapping = reference(anchor["manifest"], True)
            detail = reference(anchor["detail"], True)
            root = project if anchor["manifest"]["base"] == "project" else task
            map_root = helper.safe_file(root, anchor["manifest"]["path"]).parent
            # Even unlinked map locators must not claim paths outside their site.
            for relative in mapping["structured_detail_map"].values():
                _no_links(map_root, helper.safe_file(map_root, relative))
        definition = (detail or {}).get("definition") or {}
        definition_id = definition.get("entity_id")
        if detail and detail["page_kind"] == "MODULE_DEFINITION":
            definition_id = detail["object"]["entity_id"]
        elif detail and definition_id == detail["object"]["entity_id"]:
            definition_id = None  # A self reference is not an instance-of relation.
        blocks.append({"name": name, "block": block, "stages": stages, "detail": detail,
                       "definition_id": definition_id or "unknown",
                       "missing": [s for s in helper.STAGES if s not in valid]})
    combination = None
    if state.get("combination"):
        record = state["combination"]
        combination = capture(helper.safe_file(task, record["path"]), record["sha256"], True)
        for ref in combination.get("references", []):
            reference(ref)
    template_path = ROOT / "templates/build2-preview/index.html"
    template = capture(template_path, text=True).decode("utf-8")
    env = Environment(loader=DictLoader({"index.html": template}), autoescape=select_autoescape(["html"]))
    env.filters["pretty"] = lambda data: json.dumps(data, ensure_ascii=False, indent=2)
    html = env.get_template("index.html").render(
        plan=plan, state=state, report=report, blocks=blocks, combination=combination)

    def stable():
        for path, sha in snapshots.items():
            root = project if path.is_relative_to(project) else ROOT
            _no_links(root, path)
            if helper.digest(path) != sha:
                raise ValueError(f"input changed during preview: {path}")
        if helper.inspect(task, state) != report:
            raise ValueError("Source/config/Skill/dependency identity changed during preview")

    stable()
    manifest = {"schema": "rtl-analysis-agent/build2-preview-v1", "maturity": "DRAFT_UNREVIEWED",
                "publication": "NOT PUBLISHED", "identity": state["identity"], "blocks": report["blocks"],
                "completion": report.get("completion", "open"), "combination": report.get("combination"),
                "stages": {b["name"]: {s: {k: v for k, v in record.items() if k != "payload"}
                                       for s, record in b["stages"].items()} for b in blocks},
                "inputs": [{"base": "project" if p.is_relative_to(project) else "capability",
                            "path": p.relative_to(project if p.is_relative_to(project) else ROOT).as_posix(),
                            "sha256": sha} for p, sha in snapshots.items()],
                "index_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest()}
    _no_links(task, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()  # Exclusive reservation; never overwrite an existing preview.
    try:
        (destination / "preview-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (destination / "index.html").write_text(html, encoding="utf-8")
        stable()
    except Exception:
        shutil.rmtree(destination)
        raise
    return {"status": "ready", "maturity": "DRAFT_UNREVIEWED", "publication": "NOT PUBLISHED",
            "completion": manifest["completion"], "output": str(destination),
            "manifest": str(destination / "preview-manifest.json")}
