"""Public evidence path projection; immutable Source locators keep their own identity."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def source_path(project_root: Path, source_root: Path, locator: str) -> tuple[str, str]:
    """Resolve a validated Source-root locator for a Project-facing answer."""
    relative = Path(locator)
    if not locator or relative.is_absolute() or ".." in relative.parts or "\n" in locator:
        raise ValueError("invalid Source locator")
    root = source_root.resolve()
    target = root / relative
    if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(root):
        raise ValueError("Source locator is missing or escapes its root")
    try:
        return target.resolve().relative_to(project_root.resolve()).as_posix(), "project"
    except ValueError:
        return target.resolve().as_posix(), "external-source"


def project_runtime_evidence(value: Any, project_root: Path, source_root: Path) -> Any:
    """Map only physical Runtime Source evidence, including nested records."""
    if isinstance(value, list):
        return [project_runtime_evidence(item, project_root, source_root) for item in value]
    if not isinstance(value, dict):
        return value
    if (value.get("status") == "source-backed" and "configuration_identity" in value
        and "sha256" in value and "byte_start" in value and "line_start" in value
        and isinstance(value.get("path"), str)):
        if "locator" in value and "path_root" in value:
            expected_path, expected_root = source_path(project_root, source_root, value["locator"])
            if value["path"] != expected_path or value["path_root"] != expected_root:
                raise ValueError("projected Source evidence path differs")
            return value
        path, path_root = source_path(project_root, source_root, value["path"])
        return {**value, "locator": value["path"], "path": path, "path_root": path_root}
    return {key: project_runtime_evidence(item, project_root, source_root)
            for key, item in value.items()}
