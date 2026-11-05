#!/usr/bin/env python3
"""Container entrypoint for one reproducible Agent run."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid
from typing import Any


OUTPUT = Path("/workspace/output")
EVENTS = OUTPUT / "events.jsonl"
ANSWER = OUTPUT / "answer.md"
AGENT_LOG = OUTPUT / "agent.log"
AGENT_STDERR = OUTPUT / "agent.stderr"
RUNTIME_LOG = OUTPUT / "runtime.log"
SOCKET = Path("/tmp/agent-runner/rtlq.sock")

COMMON_CONTRACT = """You are a fresh Agent inside a reproducible DEV/benchmark Docker sandbox.

Workspace contract:
- `/workspace/source` is the only target source and is read-only.
- `/workspace/input/question.md` and `/workspace/input/build.json` are read-only inputs.
- `/workspace/scratch` is your writable working directory.
- Do not seek Golden, Claim Ledger, prior answers, benchmark author files, host paths, or Git history outside the mounted source.
- Do not install, start, repair, or maintain sandbox infrastructure. If the declared environment is unavailable, stop and return `RUNNER_INFRASTRUCTURE_FAILURE`.
- Return only the final answer to the question. Do not write directly into `/workspace/output`; the harness captures your final message.
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def append_event(event_type: str, **values: Any) -> None:
    payload = {"type": event_type, "at": now(), **values}
    with EVENTS.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")


def event_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in EVENTS.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def event_count(event_type: str) -> int:
    return sum(row.get("type") == event_type for row in event_rows())


def usage() -> dict[str, int] | str:
    found: dict[str, int] | None = None
    for line in AGENT_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            found = {
                str(key): int(value)
                for key, value in event["usage"].items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
    return found if found is not None else "unavailable"


def wait_for_socket(process: subprocess.Popen[Any], timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    while not SOCKET.exists():
        if process.poll() is not None:
            raise RuntimeError(f"runtime server exited before ready with {process.returncode}")
        if time.monotonic() >= deadline:
            raise RuntimeError("runtime server did not become ready within 180 seconds")
        time.sleep(0.05)


def stop_process(process: subprocess.Popen[Any] | None, timeout: float = 15.0) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def resolve_knowledge_delivery(
    knowledge_mode: str,
    configured: str,
    target: str = "",
) -> str:
    delivery = configured or (
        "prefetch" if knowledge_mode == "on" and target else "off"
    )
    unresolved_target = (
        knowledge_mode == "on" and not configured and not target and delivery == "off"
    )
    if (
        knowledge_mode == "on"
        and delivery not in {"available", "prefetch"}
        and not unresolved_target
    ):
        raise ValueError("knowledge ON requires available or prefetch delivery")
    if knowledge_mode == "off" and delivery != "off":
        raise ValueError("prefetch/available delivery requires knowledge ON")
    if knowledge_mode not in {"on", "off"}:
        raise ValueError("knowledge mode is invalid")
    return delivery


def capability_contract(
    treatment: str,
    knowledge_mode: str = "off",
    knowledge_delivery: str = "",
    knowledge_requested_mode: str | None = None,
    no_prefetch_reason: str = "",
) -> str:
    delivery = resolve_knowledge_delivery(knowledge_mode, knowledge_delivery)
    requested_mode = knowledge_requested_mode or knowledge_mode
    tool = """
- The deterministic `rtlq` command is available when useful. Its task-local runtime is already running exactly once; never start, repair, or manage it yourself.
- Tool use is optional. A correct source-based answer with zero Tool calls is valid.
"""
    if delivery == "prefetch":
        knowledge = (
            "- Reviewed module knowledge has already been prefetched by the Controller "
            "and its route material is included below; usable results also include a run-local reference.\n"
        )
    elif requested_mode == "on" and delivery == "off":
        reason = no_prefetch_reason or "target-unresolved"
        knowledge = (
            "- Reviewed module knowledge was not queried because no reliable exact target "
            f"was supplied ({reason}). Source and `rtlq` remain available.\n"
        )
    else:
        knowledge = (
            "- Reviewed module knowledge is visible. `rtlq knowledge <module> --format readable` "
            "returns readable material and a run-local structured result reference.\n"
            if knowledge_mode == "on"
            else "- Reviewed module knowledge is disabled and no candidate is mounted.\n"
        )
    if treatment == "A":
        return """
Use the supplied source and ordinary shell navigation to answer the engineering question.
"""
    if treatment == "B":
        return tool + knowledge
    return tool + knowledge + """
- `/workspace/skills` is the only task Skill input and is read-only. Read its applicable `SKILL.md`.
"""


def build_agent_prompt(
    *,
    treatment: str,
    knowledge_mode: str,
    knowledge_delivery: str,
    question: str,
    prefetched_knowledge: str | None = None,
    knowledge_requested_mode: str | None = None,
    no_prefetch_reason: str = "",
) -> str:
    delivery = resolve_knowledge_delivery(knowledge_mode, knowledge_delivery)
    prefetch = ""
    if delivery == "prefetch":
        if not prefetched_knowledge:
            raise ValueError("prefetch delivery requires readable knowledge material")
        prefetch = f"""
The material below is the Controller's deterministic knowledge delivery for the current frozen Build.
- Its route, reasons, coverage, gaps, reviewed content, and any result reference are delivery facts.
- Source and `rtlq` remain available.
- The Controller already fetched this knowledge; do not repeat the same `rtlq knowledge` query.
- A delivered run-local result reference can be reread with `rtlq result` without querying SQLite again.

----- BEGIN REVIEWED PREBUILT KNOWLEDGE -----
{prefetched_knowledge}----- END REVIEWED PREBUILT KNOWLEDGE -----
"""
    return f"""{COMMON_CONTRACT}{capability_contract(
        treatment,
        knowledge_mode,
        delivery,
        knowledge_requested_mode,
        no_prefetch_reason,
    )}{prefetch}
----- BEGIN QUESTION -----
{question}
----- END QUESTION -----
"""


def prompt_checks(treatment: str) -> dict[str, bool]:
    knowledge_mode = os.environ.get("AGENT_RUNNER_KNOWLEDGE_MODE", "off")
    knowledge_target = os.environ.get("AGENT_RUNNER_KNOWLEDGE_PROBE_TARGET", "")
    delivery = resolve_knowledge_delivery(
        knowledge_mode,
        os.environ.get("AGENT_RUNNER_KNOWLEDGE_DELIVERY", ""),
        knowledge_target,
    )
    contract = capability_contract(
        treatment,
        knowledge_mode,
        delivery,
        os.environ.get("AGENT_RUNNER_KNOWLEDGE_REQUESTED_MODE", knowledge_mode),
        os.environ.get("AGENT_RUNNER_KNOWLEDGE_SKIP_REASON", ""),
    )
    has_tool = "`rtlq`" in contract
    has_skill = "/workspace/skills" in contract or "`SKILL.md`" in contract
    return {
        "prompt_tool_hint_expected": has_tool == (treatment in {"B", "C"}),
        "prompt_skill_hint_expected": has_skill == (treatment == "C"),
        "knowledge_delivery_valid": delivery in {"available", "prefetch", "off"},
    }


def controller_prefetch(*, run_id: str, target: str) -> dict[str, Any]:
    if not target:
        raise ValueError("prefetch target is required")
    append_event(
        "controller_prefetch_started", run_id=run_id, actor="controller",
        phase="prefetch", target=target,
    )
    environment = os.environ.copy()
    environment.update({
        "RTLQ_SOCKET": str(SOCKET),
        "RTLQ_ACTOR": "controller",
        "RTLQ_PHASE": "prefetch",
    })
    completed = subprocess.run(
        ["rtlq", "knowledge", target, "--format", "readable"],
        capture_output=True, text=True, check=False, env=environment,
    )
    result_match = re.search(
        r"^- Result ref: ([A-Za-z0-9._-]+)$", completed.stdout, re.M
    )
    route_match = re.search(r"^- Route: (DIRECT|CONTEXT|BYPASS)$", completed.stdout, re.M)
    reason_match = re.search(r"^- Reason: (.+)$", completed.stdout, re.M)
    if (
        completed.returncode != 0
        or not result_match
        or not route_match
        or not reason_match
        or not completed.stdout.startswith(
        "# RTL module knowledge\n"
        )
    ):
        append_event(
            "controller_prefetch_finished", run_id=run_id, actor="controller",
            phase="prefetch", target=target, status="fail",
            exit_code=completed.returncode,
        )
        raise RuntimeError("controller knowledge prefetch failed")
    route = route_match.group(1)
    reasons = [item.strip() for item in reason_match.group(1).split(",") if item.strip()]
    if route == "BYPASS":
        delivery_material = (
            "# RTL module knowledge route\n\n"
            "- Route: BYPASS\n"
            f"- Reason: {', '.join(reasons)}\n"
        )
    else:
        delivery_material = completed.stdout
    result = {
        "readable": completed.stdout,
        "delivery_material": delivery_material,
        "result_ref": result_match.group(1),
        "route": route,
        "reasons": reasons,
        "readable_bytes": len(completed.stdout.encode()),
        "delivered_bytes": len(delivery_material.encode()),
        "bytes": len(delivery_material.encode()),
    }
    append_event(
        "controller_prefetch_finished", run_id=run_id, actor="controller",
        phase="prefetch", target=target, status="pass",
        result_ref=result["result_ref"], route=result["route"],
        readable_bytes=result["readable_bytes"],
        delivered_bytes=result["delivered_bytes"],
    )
    return result


def validate_exact_identities(answer: str, structured: dict[str, Any]) -> dict[str, Any]:
    """Check exact direct-instance identities without rewriting the answer."""
    entity_names = {
        item.get("ref"): item.get("qualified_name")
        for item in structured.get("entities", [])
        if isinstance(item, dict)
    }
    entries: list[dict[str, str]] = []
    for item in (structured.get("structure") or {}).get("direct_instances", []):
        characteristics = item.get("characteristics") or {}
        name = characteristics.get("name")
        qualified_name = entity_names.get(item.get("instance_ref"))
        if not isinstance(name, str) or not isinstance(qualified_name, str):
            continue
        if qualified_name in answer:
            status = "PASS"
        elif name in answer:
            status = "ALTERED"
        else:
            status = "MISSING"
        entries.append({
            "name": name,
            "qualified_name": qualified_name,
            "status": status,
        })
    overall = (
        "PASS"
        if entries and all(item["status"] == "PASS" for item in entries)
        else "ALTERED"
        if any(item["status"] == "ALTERED" for item in entries)
        else "MISSING"
    )
    return {
        "status": overall,
        "expected_count": len(entries),
        "pass_count": sum(item["status"] == "PASS" for item in entries),
        "altered_count": sum(item["status"] == "ALTERED" for item in entries),
        "missing_count": sum(item["status"] == "MISSING" for item in entries),
        "items": entries,
        "answer_modified": False,
    }


def shell_probe(
    run_id: str,
    treatment: str,
    prefetch: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    checks: dict[str, Any] = prompt_checks(treatment)
    candidate = Path("/workspace/knowledge/candidate.sqlite")
    if os.environ.get("AGENT_RUNNER_KNOWLEDGE_MODE") == "on":
        checks["candidate_read_only_mount"] = (
            candidate.is_file()
            and bool(os.statvfs(candidate).f_flag & os.ST_RDONLY)
        )
    else:
        checks["candidate_absent"] = not candidate.exists()
    source_file = next(
        (path for path in Path("/workspace/source").rglob("*") if path.is_file()), None
    )
    checks["source_readable"] = source_file is not None and bool(source_file.read_bytes())
    for name, path in (
        ("input_readable", Path("/workspace/input/question.md")),
        ("build_readable", Path("/workspace/input/build.json")),
    ):
        checks[name] = path.is_file() and bool(path.read_bytes())

    for name, path in (
        ("source_read_only", Path("/workspace/source/.runner-write-probe")),
        ("input_read_only", Path("/workspace/input/.runner-write-probe")),
    ):
        try:
            path.write_text("must fail", encoding="utf-8")
        except OSError:
            checks[name] = True
        else:
            checks[name] = False
            path.unlink(missing_ok=True)

    scratch_probe = Path("/workspace/scratch/probe.txt")
    output_probe = OUTPUT / "probe.txt"
    scratch_probe.write_text(run_id + "\n", encoding="utf-8")
    output_probe.write_text(run_id + "\n", encoding="utf-8")
    checks["scratch_writable"] = scratch_probe.read_text().strip() == run_id
    checks["output_writable"] = output_probe.read_text().strip() == run_id

    sentinel = os.environ.get("AGENT_RUNNER_SENTINEL_PATH", "")
    checks["author_sentinel_hidden"] = bool(sentinel) and not Path(sentinel).exists()
    git_probe = subprocess.run(
        ["git", "-C", "/workspace/source", "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    checks["git_history_absent"] = git_probe.returncode != 0
    dedicated_stdout = ""
    dedicated_stderr = ""
    if treatment == "A":
        checks["skill_mount_absent"] = not Path("/workspace/skills").exists()
        checks["rtlq_absent"] = shutil.which("rtlq") is None
        checks["runtime_code_absent"] = not Path("/opt/agent-runner/runtime_server.py").exists()
        checks["runtime_library_absent"] = not Path("/opt/rtl-analysis-agent/lib/rtl_query_runtime.py").exists()
        checks["runtime_socket_absent"] = not SOCKET.exists()
        imported = subprocess.run(
            ["python3", "-c", "import pyslang"], capture_output=True, text=True, check=False
        )
        checks["pyslang_absent"] = imported.returncode != 0
        dedicated_stderr = imported.stderr
    else:
        checks["rtlq_present"] = shutil.which("rtlq") is not None
        checks["runtime_code_present"] = Path("/opt/agent-runner/runtime_server.py").is_file()
        checks["runtime_library_present"] = Path("/opt/rtl-analysis-agent/lib/rtl_query_runtime.py").is_file()
        checks["runtime_socket_present"] = SOCKET.is_socket()
        imported = subprocess.run(
            ["python3", "-c", "import pyslang"], capture_output=True, text=True, check=False
        )
        checks["pyslang_present"] = imported.returncode == 0
        if treatment == "B":
            checks["skill_mount_absent"] = not Path("/workspace/skills").exists()
        else:
            skill = Path("/workspace/skills/SKILL.md")
            checks["skill_readable"] = skill.is_file() and bool(skill.read_bytes())
            try:
                Path("/workspace/skills/.runner-write-probe").write_text("must fail", encoding="utf-8")
            except OSError:
                checks["skill_read_only"] = True
            else:
                checks["skill_read_only"] = False
                Path("/workspace/skills/.runner-write-probe").unlink(missing_ok=True)
        build_payload = json.loads(Path("/workspace/input/build.json").read_text(encoding="utf-8"))
        tops = build_payload.get("tops", [])
        query_top = str(tops[0]) if isinstance(tops, list) and tops else ""
        checks["build_top_available"] = bool(query_top)
        completed = subprocess.run(
            ["rtlq", "scope", query_top, "--limit", "2"],
            capture_output=True,
            text=True,
            check=False,
        )
        dedicated_stdout = completed.stdout
        dedicated_stderr = completed.stderr or imported.stderr
        checks["rtlq_success"] = completed.returncode == 0
        try:
            result = json.loads(completed.stdout)
            checks["rtlq_result_ok"] = result.get("status") in {"ok", "partial"}
        except ValueError:
            checks["rtlq_result_ok"] = False
        knowledge_target = os.environ.get("AGENT_RUNNER_KNOWLEDGE_PROBE_TARGET") or query_top
        knowledge_on = os.environ.get("AGENT_RUNNER_KNOWLEDGE_MODE") == "on"
        delivery = resolve_knowledge_delivery(
            os.environ.get("AGENT_RUNNER_KNOWLEDGE_MODE", "off"),
            os.environ.get("AGENT_RUNNER_KNOWLEDGE_DELIVERY", ""),
            os.environ.get("AGENT_RUNNER_KNOWLEDGE_PROBE_TARGET", ""),
        )
        no_prefetch_reason = os.environ.get("AGENT_RUNNER_KNOWLEDGE_SKIP_REASON", "")
        if no_prefetch_reason:
            checks["knowledge_lookup_skipped"] = no_prefetch_reason == "target-unresolved"
            checks["knowledge_candidate_absent_for_unresolved_target"] = not Path(
                "/workspace/knowledge/candidate.sqlite"
            ).exists()
            knowledge = subprocess.CompletedProcess(
                ["controller-no-prefetch"],
                0,
                stdout=json.dumps({
                    "route": "BYPASS",
                    "execution_summary": {
                        "returned_route": "BYPASS",
                        "actual_path": "UNAVAILABLE",
                        "actual_supplement": "UNAVAILABLE",
                    },
                }),
                stderr="",
            )
        elif delivery == "prefetch":
            assert prefetch is not None
            knowledge = subprocess.CompletedProcess(
                ["controller-prefetch"], 0, stdout=str(prefetch["readable"]), stderr=""
            )
        else:
            knowledge_command = ["rtlq", "knowledge", knowledge_target]
            if knowledge_on:
                knowledge_command.extend(["--format", "readable"])
            probe_environment = os.environ.copy()
            probe_environment.update({"RTLQ_ACTOR": "controller", "RTLQ_PHASE": "probe"})
            knowledge = subprocess.run(
                knowledge_command,
                capture_output=True, text=True, check=False, env=probe_environment,
            )
        checks["knowledge_query_success"] = knowledge.returncode == 0
        try:
            if knowledge_on:
                match = re.search(r"^- Result ref: ([A-Za-z0-9._-]+)$", knowledge.stdout, re.M)
                result_ref = match.group(1) if match else ""
                checks["knowledge_readable_delivered"] = (
                    knowledge.stdout.startswith("# RTL module knowledge\n")
                    and bool(result_ref)
                    and len(knowledge.stdout.encode()) <= 32 * 1024
                )
                if delivery == "prefetch":
                    prompt_material = str(prefetch["delivery_material"])
                    prompt = build_agent_prompt(
                        treatment=treatment,
                        knowledge_mode="on",
                        knowledge_delivery="prefetch",
                        question=Path("/workspace/input/question.md").read_text(encoding="utf-8"),
                        prefetched_knowledge=prompt_material,
                    )
                    checks["prefetch_prompt_single_copy"] = (
                        prompt.count("----- BEGIN REVIEWED PREBUILT KNOWLEDGE -----") == 1
                        and prompt.count("----- END REVIEWED PREBUILT KNOWLEDGE -----") == 1
                        and prompt.count(prompt_material) == 1
                    )
                    checks["prefetch_prompt_delivery_only"] = not any(
                        policy in prompt
                        for policy in (
                            "organize the answer from covered material",
                            "retain covered material and investigate only",
                            "use the ordinary Source/Tool route",
                            "must be copied verbatim",
                        )
                    )
                reread = subprocess.run(
                    ["rtlq", "result", result_ref, "--format", "json"],
                    capture_output=True,
                    text=True,
                    check=False,
                    env={
                        **os.environ,
                        "RTLQ_ACTOR": "controller",
                        "RTLQ_PHASE": "probe",
                    },
                )
                checks["knowledge_result_reread_success"] = reread.returncode == 0
                knowledge_result = json.loads(reread.stdout)
                artifacts = knowledge_result.get("result_files") or {}
                checks["knowledge_result_artifacts_present"] = bool(
                    artifacts.get("structured") and artifacts.get("readable")
                )
                checks["knowledge_result_identity_matches"] = (
                    knowledge_result.get("request_id") == result_ref
                    and (knowledge_result.get("result_identity") or {}).get("run_id") == run_id
                )
                bindings = [
                    binding
                    for group in (knowledge_result.get("structure") or {}).get(
                        "instance_bindings", []
                    )
                    for binding in group.get("bindings", [])
                ]
                checks["knowledge_binding_aliases_unique"] = (
                    not bindings
                    or (
                        len({item.get("ref") for item in bindings}) == len(bindings)
                        and all(item.get("audit_ref") for item in bindings)
                        and all(item.get("source_evidence_refs") for item in bindings)
                    )
                )
                dedicated_stdout += "\n" + reread.stdout
                dedicated_stderr += "\n" + reread.stderr
            else:
                knowledge_result = json.loads(knowledge.stdout)
            checks["knowledge_route_valid"] = knowledge_result.get("route") in {
                "DIRECT", "CONTEXT", "BYPASS"
            }
            checks["knowledge_summary_present"] = isinstance(
                knowledge_result.get("execution_summary"), dict
            )
            summary = knowledge_result.get("execution_summary") or {}
            checks["knowledge_route_not_actual_path"] = (
                summary.get("returned_route") == knowledge_result.get("route")
                and summary.get("actual_path") == "UNAVAILABLE"
                and summary.get("actual_supplement") == "UNAVAILABLE"
            )
            if not knowledge_on:
                checks["knowledge_result_artifacts_absent"] = (
                    "result_artifacts" not in knowledge_result
                    and not Path("/workspace/output/knowledge-results").exists()
                )
        except ValueError:
            checks["knowledge_route_valid"] = False
            checks["knowledge_summary_present"] = False
            checks["knowledge_route_not_actual_path"] = False
        dedicated_stdout += "\n" + knowledge.stdout
        dedicated_stderr += "\n" + knowledge.stderr
    passed = all(checks.values())
    ANSWER.write_text(
        "# Agent Runner shell probe\n\n"
        + f"run_id: `{run_id}`\n\n"
        + "```json\n"
        + json.dumps(checks, indent=2, sort_keys=True)
        + "\n```\n",
        encoding="utf-8",
    )
    AGENT_LOG.write_text(
        json.dumps(
            {
                "type": "shell_probe.completed",
                "run_id": run_id,
                "checks": checks,
                "treatment": treatment,
                "dedicated_stdout": dedicated_stdout,
                "dedicated_stderr": dedicated_stderr,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return (0 if passed else 1), checks


def agent_run(
    model: str,
    effort: str,
    treatment: str,
    prefetch: dict[str, Any] | None = None,
) -> int:
    auth_source = Path("/run/codex-auth.json")
    if not auth_source.is_file():
        raise RuntimeError("read-only Codex auth mount is missing")
    auth_target = Path("/codex-home/auth.json")
    shutil.copyfile(auth_source, auth_target)
    os.chmod(auth_target, 0o600)

    proxy = subprocess.Popen(
        [
            "python3",
            "/opt/agent-runner/proxy_bridge.py",
            "container",
            "--socket",
            "/run/agent-proxy/proxy.sock",
            "--port",
            "18081",
        ],
        stdout=subprocess.DEVNULL,
        stderr=RUNTIME_LOG.open("a", encoding="utf-8"),
    )
    try:
        time.sleep(0.1)
        if proxy.poll() is not None:
            raise RuntimeError("container proxy bridge failed to start")
        question = Path("/workspace/input/question.md").read_text(encoding="utf-8")
        knowledge_mode = os.environ.get("AGENT_RUNNER_KNOWLEDGE_MODE", "off")
        knowledge_delivery = resolve_knowledge_delivery(
            knowledge_mode,
            os.environ.get("AGENT_RUNNER_KNOWLEDGE_DELIVERY", ""),
            os.environ.get("AGENT_RUNNER_KNOWLEDGE_PROBE_TARGET", ""),
        )
        prompt = build_agent_prompt(
            treatment=treatment,
            knowledge_mode=knowledge_mode,
            knowledge_delivery=knowledge_delivery,
            question=question,
            prefetched_knowledge=str(prefetch["delivery_material"]) if prefetch else None,
            knowledge_requested_mode=os.environ.get(
                "AGENT_RUNNER_KNOWLEDGE_REQUESTED_MODE", knowledge_mode
            ),
            no_prefetch_reason=os.environ.get(
                "AGENT_RUNNER_KNOWLEDGE_SKIP_REASON", ""
            ),
        )
        shell_environment = {
            "PATH": os.environ["PATH"],
            "HOME": "/workspace/scratch/home",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "RTLQ_ACTOR": "agent",
            "RTLQ_PHASE": "analysis",
        }
        if treatment in {"B", "C"}:
            shell_environment["RTLQ_SOCKET"] = str(SOCKET)
        Path(shell_environment["HOME"]).mkdir(mode=0o700)
        shell_toml = "{" + ",".join(
            key + "=" + json.dumps(value) for key, value in shell_environment.items()
        ) + "}"
        command = [
            "codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--ephemeral",
            "--ignore-user-config", "--skip-git-repo-check", "--model", model,
            "--config", f"model_reasoning_effort={json.dumps(effort)}",
            "--config", 'shell_environment_policy.inherit="none"',
            "--config", f"shell_environment_policy.set={shell_toml}",
            "--json", "--output-last-message", str(ANSWER), "-",
        ]
        environment = {
            "CODEX_HOME": "/codex-home", "HOME": "/workspace/scratch/home",
            "PATH": os.environ["PATH"], "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "HTTP_PROXY": "http://127.0.0.1:18081", "HTTPS_PROXY": "http://127.0.0.1:18081",
            "http_proxy": "http://127.0.0.1:18081", "https_proxy": "http://127.0.0.1:18081",
        }
        append_event(
            "agent_started", model=model, effort=effort,
            actor="agent", phase="analysis",
        )
        with AGENT_LOG.open("w", encoding="utf-8") as stdout, AGENT_STDERR.open("w", encoding="utf-8") as stderr:
            completed = subprocess.run(command, input=prompt, text=True, stdout=stdout, stderr=stderr, env=environment, check=False)
        append_event(
            "agent_finished", exit_code=completed.returncode,
            actor="agent", phase="analysis",
        )
        return completed.returncode
    finally:
        stop_process(proxy)

def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for path in (EVENTS, ANSWER, AGENT_LOG, AGENT_STDERR, RUNTIME_LOG):
        path.write_text("", encoding="utf-8")
    started_monotonic = time.monotonic()
    started_at = now()
    run_id = uuid.uuid4().hex
    mode = os.environ.get("AGENT_RUNNER_MODE", "agent")
    treatment = os.environ.get("AGENT_RUNNER_TREATMENT", "")
    knowledge_mode = os.environ.get("AGENT_RUNNER_KNOWLEDGE_MODE", "off")
    knowledge_requested_mode = os.environ.get(
        "AGENT_RUNNER_KNOWLEDGE_REQUESTED_MODE", knowledge_mode
    )
    no_prefetch_reason = os.environ.get("AGENT_RUNNER_KNOWLEDGE_SKIP_REASON", "")
    knowledge_delivery = resolve_knowledge_delivery(
        knowledge_mode,
        os.environ.get("AGENT_RUNNER_KNOWLEDGE_DELIVERY", ""),
        os.environ.get("AGENT_RUNNER_KNOWLEDGE_PROBE_TARGET", ""),
    )
    image_treatment = os.environ.get("AGENT_RUNNER_IMAGE_TREATMENT", "")
    model = os.environ.get("AGENT_RUNNER_MODEL", "")
    effort = os.environ.get("AGENT_RUNNER_EFFORT", "")
    fresh_codex_home = not any(Path("/codex-home").iterdir())
    scratch_was_empty = not any(Path("/workspace/scratch").iterdir())
    append_event(
        "container_started", run_id=run_id, mode=mode, treatment=treatment,
        knowledge_requested_mode=knowledge_requested_mode,
        knowledge_mode=knowledge_mode, knowledge_delivery=knowledge_delivery,
        no_prefetch_reason=no_prefetch_reason or None,
    )

    runtime: subprocess.Popen[Any] | None = None
    runtime_start_seconds: float | None = None
    exit_code = 1
    error: str | None = None
    checks: dict[str, Any] = {}
    prefetch: dict[str, Any] | None = None
    try:
        if treatment not in {"A", "B", "C"} or image_treatment != treatment:
            raise RuntimeError(
                f"treatment/image mismatch: requested={treatment!r} image={image_treatment!r}"
            )
        if treatment in {"B", "C"}:
            SOCKET.parent.mkdir(parents=True, exist_ok=True)
            runtime_started = time.monotonic()
            runtime_command = [
                "python3",
                "/opt/agent-runner/runtime_server.py",
                "--socket",
                str(SOCKET),
                "--events",
                str(EVENTS),
                "--source",
                "/workspace/source",
                "--build",
                "/workspace/input/build.json",
                "--project",
                os.environ.get("AGENT_RUNNER_PROJECT", "source"),
                "--revision",
                os.environ.get("AGENT_RUNNER_REVISION", "unversioned"),
                "--run-id", run_id,
                "--result-root", "/workspace/output/knowledge-results",
                "--knowledge-mode", knowledge_mode,
            ]
            if knowledge_mode == "on":
                runtime_command.extend([
                    "--knowledge-candidate", "/workspace/knowledge/candidate.sqlite"
                ])
            with RUNTIME_LOG.open("a", encoding="utf-8") as runtime_log:
                runtime = subprocess.Popen(
                    runtime_command, stdout=runtime_log, stderr=runtime_log, text=True
                )
            wait_for_socket(runtime)
            runtime_start_seconds = round(time.monotonic() - runtime_started, 3)
        if knowledge_delivery == "prefetch":
            prefetch = controller_prefetch(
                run_id=run_id,
                target=os.environ.get("AGENT_RUNNER_KNOWLEDGE_PROBE_TARGET", ""),
            )
        if mode == "probe":
            exit_code, checks = shell_probe(run_id, treatment, prefetch)
        else:
            exit_code = agent_run(model, effort, treatment, prefetch)
    except Exception as caught:
        error = str(caught)
        exit_code = 1
    finally:
        stop_process(runtime)
        finished_at = now()
        append_event("container_finished", exit_code=exit_code, error=error)

    rows = event_rows()
    tool_calls = sum(row.get("type") == "tool_call" for row in rows)
    controller_tool_calls = sum(
        row.get("type") == "tool_call" and row.get("actor") == "controller"
        for row in rows
    )
    agent_tool_calls = sum(
        row.get("type") == "tool_call" and row.get("actor") == "agent"
        for row in rows
    )
    expected_workers = 0 if treatment == "A" else 1
    worker_ok = (
        sum(row.get("type") == "tool_started" for row in rows) == expected_workers
        and sum(row.get("type") == "tool_stopped" for row in rows) == expected_workers
    )
    answer_present = ANSWER.is_file() and bool(ANSWER.read_text(encoding="utf-8").strip())
    exact_identity_guard: dict[str, Any] | None = None
    if (
        mode == "agent"
        and answer_present
        and prefetch
        and prefetch["route"] in {"DIRECT", "CONTEXT"}
    ):
        try:
            structured_path = (
                OUTPUT / "knowledge-results" / f"{prefetch['result_ref']}.json"
            )
            exact_identity_guard = validate_exact_identities(
                ANSWER.read_text(encoding="utf-8"),
                json.loads(structured_path.read_text(encoding="utf-8")),
            )
        except (OSError, ValueError, TypeError) as caught:
            exact_identity_guard = {
                "status": "UNAVAILABLE",
                "reason": str(caught),
                "answer_modified": False,
            }
    valid = bool(exit_code == 0 and worker_ok and answer_present)
    metrics = {
        "schema": "rtl-analysis-agent/agent-runner-metrics-v2",
        "run_id": run_id,
        "model": model if mode == "agent" else "shell-probe",
        "effort": effort if mode == "agent" else "none",
        "wall_seconds": round(time.monotonic() - started_monotonic, 3),
        "exit_code": exit_code,
        "tool_calls": tool_calls,
        "treatment": treatment,
        "started_at": started_at,
        "finished_at": finished_at,
        "token_usage": usage() if mode == "agent" else "unavailable",
        "runtime_start_seconds": runtime_start_seconds,
        "knowledge_mode": knowledge_mode,
        "knowledge_requested_mode": knowledge_requested_mode,
        "knowledge_delivery": knowledge_delivery,
        "knowledge_skip_reason": no_prefetch_reason or None,
        "controller_tool_calls": controller_tool_calls,
        "agent_tool_calls": agent_tool_calls,
        "prefetch": {
            "result_ref": prefetch["result_ref"],
            "route": prefetch["route"],
            "reasons": prefetch["reasons"],
            "readable_bytes": prefetch["readable_bytes"],
            "delivered_bytes": prefetch["delivered_bytes"],
        } if prefetch else None,
        "exact_identity_guard": exact_identity_guard,
    }
    status = {
        "schema": "rtl-analysis-agent/agent-runner-status-v2",
        "run_id": run_id,
        "mode": mode,
        "treatment": treatment,
        "image_treatment": image_treatment,
        "valid": valid,
        "exit_code": exit_code,
        "answer_present": answer_present,
        "fresh_codex_home": fresh_codex_home,
        "fresh_scratch": scratch_was_empty,
        "worker_started_count": event_count("tool_started"),
        "worker_stopped_count": event_count("tool_stopped"),
        "tool_calls": tool_calls,
        "dedicated_capability_expected": treatment in {"B", "C"},
        "tool_expected": treatment in {"B", "C"},
        "skill_expected": treatment == "C",
        "prompt_contract_checks": prompt_checks(treatment),
        "knowledge_mode": knowledge_mode,
        "knowledge_requested_mode": knowledge_requested_mode,
        "knowledge_delivery": knowledge_delivery,
        "knowledge_skip_reason": no_prefetch_reason or None,
        "controller_tool_calls": controller_tool_calls,
        "agent_tool_calls": agent_tool_calls,
        "prefetch": {
            "result_ref": prefetch["result_ref"],
            "route": prefetch["route"],
            "reasons": prefetch["reasons"],
            "readable_bytes": prefetch["readable_bytes"],
            "delivered_bytes": prefetch["delivered_bytes"],
        } if prefetch else None,
        "exact_identity_guard": exact_identity_guard,
        "knowledge_candidate_mounted": Path("/workspace/knowledge/candidate.sqlite").is_file(),
        "error": error,
        "probe_checks": checks if mode == "probe" else None,
    }
    (OUTPUT / "metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (OUTPUT / "status.json").write_text(
        json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if valid else exit_code or 1


if __name__ == "__main__":
    raise SystemExit(main())
