#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
RUNNER="$ROOT/tools/agent-runner/run"
AGENT_RUNS=3
SKIP_BUILD=0

while (($#)); do
  case "$1" in
    --agent-runs) AGENT_RUNS="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    *) printf 'smoke: unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done
[[ "$AGENT_RUNS" =~ ^[0-9]+$ ]] || { printf 'smoke: --agent-runs must be non-negative\n' >&2; exit 2; }

SMOKE_TMP="$(mktemp -d -t agent-runner-smoke.XXXXXXXX)"
mkdir -p "$ROOT/tmp"
SENTINEL="$(mktemp "$ROOT/tmp/agent-runner-hidden.XXXXXXXX")"
cleanup() {
  rm -f -- "$SENTINEL"
  rm -rf -- "$SMOKE_TMP"
}
trap cleanup EXIT INT TERM

SOURCE="$ROOT/tests/fixtures/runtime_query"
SKILL="$ROOT/src/rtl-pack/skills/rtl-query"
BUILD="$SOURCE/build.json"
PROMPT_A="$ROOT/tools/agent-runner/tests/smoke-question-a.md"
PROMPT_B="$ROOT/tools/agent-runner/tests/smoke-question-b.md"
PROMPT_C="$ROOT/tools/agent-runner/tests/smoke-question.md"

if (( ! SKIP_BUILD )); then
  "$RUNNER" --build-images
fi

common=(--source "$SOURCE" --build "$BUILD")
args_a=(--treatment A "${common[@]}" --prompt "$PROMPT_A")
args_b=(--treatment B "${common[@]}" --prompt "$PROMPT_B")

if "$RUNNER" --treatment A "${common[@]}" --skill "$SKILL" --prompt "$PROMPT_A" \
  --output "$SMOKE_TMP/a-forbidden-skill" --probe >/dev/null 2>&1; then
  printf 'smoke: treatment A accepted --skill\n' >&2
  exit 1
fi
if "$RUNNER" --treatment B "${common[@]}" --skill "$SKILL" --prompt "$PROMPT_B" \
  --output "$SMOKE_TMP/b-forbidden-skill" --probe >/dev/null 2>&1; then
  printf 'smoke: treatment B accepted --skill\n' >&2
  exit 1
fi
if "$RUNNER" --treatment C "${common[@]}" --prompt "$PROMPT_C" \
  --output "$SMOKE_TMP/c-missing-skill" --probe >/dev/null 2>&1; then
  printf 'smoke: treatment C accepted missing --skill\n' >&2
  exit 1
fi

args_c=(--treatment C "${common[@]}" --skill "$SKILL" --prompt "$PROMPT_C")
declare -a probe_ids=()
for treatment in A B C; do
  for index in 1 2 3 4 5; do
    output="$SMOKE_TMP/probe-${treatment}-${index}"
    case "$treatment" in
      A) args=("${args_a[@]}") ;;
      B) args=("${args_b[@]}") ;;
      C) args=("${args_c[@]}") ;;
    esac
    AGENT_RUNNER_SENTINEL_PATH="$SENTINEL" "$RUNNER" "${args[@]}" \
      --output "$output" --probe >/dev/null
    expected_workers=1
    expected_calls=2
    [[ "$treatment" == A ]] && expected_workers=0 && expected_calls=0
    jq -e --arg treatment "$treatment" --argjson workers "$expected_workers" --argjson calls "$expected_calls" \
      '.valid and .fresh_codex_home and .fresh_scratch and .treatment == $treatment
       and .image_treatment == $treatment
       and .worker_started_count == $workers and .worker_stopped_count == $workers
       and .tool_calls == $calls and ([.probe_checks[]] | all)' \
      "$output/status.json" >/dev/null
    probe_ids+=("$(jq -r .run_id "$output/status.json")")
  done
done
[[ "$(printf '%s\n' "${probe_ids[@]}" | sort -u | wc -l)" -eq 15 ]]

if "$RUNNER" "${args_a[@]}" --output "$SMOKE_TMP/probe-A-1" --probe >/dev/null 2>&1; then
  printf 'smoke: existing output was overwritten without --force\n' >&2
  exit 1
fi
AGENT_RUNNER_SENTINEL_PATH="$SENTINEL" "$RUNNER" "${args_a[@]}" \
  --output "$SMOKE_TMP/probe-A-1" --probe --force >/dev/null
jq -e '.valid and .treatment == "A" and .worker_started_count == 0' \
  "$SMOKE_TMP/probe-A-1/status.json" >/dev/null

missing_error="$SMOKE_TMP/missing-image.stderr"
if AGENT_RUNNER_IMAGE_A="agent-runner-deliberately-missing:smoke" \
  "$RUNNER" "${args_a[@]}" --output "$SMOKE_TMP/missing-image" --probe \
  >/dev/null 2>"$missing_error"; then
  printf 'smoke: missing image unexpectedly succeeded\n' >&2
  exit 1
fi
rg -Fq './tools/agent-runner/run --build-images' "$missing_error"

declare -a thread_ids=()
declare -a run_ids=()
for treatment in A B C; do
  for ((index = 1; index <= AGENT_RUNS; index++)); do
    output="$SMOKE_TMP/agent-${treatment}-${index}"
    case "$treatment" in
      A) args=("${args_a[@]}") ;;
      B) args=("${args_b[@]}") ;;
      C) args=("${args_c[@]}") ;;
    esac
    "$RUNNER" "${args[@]}" --model gpt-5.6-luna --effort low --output "$output"
    expected_workers=1
    [[ "$treatment" == A ]] && expected_workers=0
    jq -e --arg treatment "$treatment" --argjson workers "$expected_workers" \
      '.valid and .fresh_codex_home and .fresh_scratch and .treatment == $treatment
       and .worker_started_count == $workers and .worker_stopped_count == $workers' \
      "$output/status.json" >/dev/null
    jq -e --arg treatment "$treatment" \
      '.model == "gpt-5.6-luna" and .effort == "low" and .exit_code == 0 and .treatment == $treatment' \
      "$output/metrics.json" >/dev/null
    rg -q 'RUNNER_SMOKE_OK' "$output/answer.md"
    if [[ "$treatment" == A ]]; then
      jq -e '.tool_calls == 0' "$output/status.json" >/dev/null
      ! rg -q '"type": "tool_(started|call|result|stopped)"' "$output/events.jsonl"
    else
      jq -e '.tool_calls == 1' "$output/status.json" >/dev/null
      [[ "$(jq -r 'select(.type == "tool_call") | .operation' "$output/events.jsonl")" == scope ]]
    fi
    thread_id="$(jq -r 'select(.type == "thread.started") | .thread_id' "$output/agent.log" | tail -n 1)"
    [[ -n "$thread_id" && "$thread_id" != null ]]
    thread_ids+=("$thread_id")
    run_ids+=("$(jq -r .run_id "$output/status.json")")
  done
done

if (( AGENT_RUNS > 0 )); then
  expected=$((AGENT_RUNS * 3))
  [[ "$(printf '%s\n' "${thread_ids[@]}" | sort -u | wc -l)" -eq "$expected" ]]
  [[ "$(printf '%s\n' "${run_ids[@]}" | sort -u | wc -l)" -eq "$expected" ]]
fi

printf 'agent-runner treatment smoke: PASS shell_runs=15 agent_runs=%d-per-treatment\n' "$AGENT_RUNS"
