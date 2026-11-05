# Agent Sandbox Runner

`tools/agent-runner` provides one reusable single-Agent Docker wrapper for DEV and benchmark runs. It has three explicit treatments and is not a scheduler or a hardened hostile-code platform.

Build the local images once:

```sh
AGENT_RUNNER_PROXY=http://127.0.0.1:7890 ./tools/agent-runner/run --build-images
```

This creates `rtl-analysis-agent-runner:0.3.0-a`, `0.3.0-b`, and `0.3.0-c`. Normal qualification and benchmark runs reuse those images; rebuild only after image inputs change or the local images are removed. `AGENT_RUNNER_PROXY` is optional when Docker can reach package sources directly and is never stored in an image.

The treatments are:

| Treatment | Source | deterministic Tool/runtime | frozen Skill |
|---|---:|---:|---:|
| A | yes | physically absent | absent |
| B | yes | present | absent |
| C | yes | present | read-only mount |

Run B without a Skill:

```sh
./tools/agent-runner/run \
  --treatment B \
  --source /path/to/source \
  --prompt /path/question.md \
  --build /path/build.json \
  --model gpt-5.6-luna \
  --effort low \
  --output /tmp/agent-run-B
```

For C, change `--treatment C` and add `--skill src/rtl-pack/skills/rtl-query`. A omits `--skill` and uses the source-only image. A and B reject `--skill`; C requires it. Prompts follow the same isolation: A does not mention Tool or Skill, B only declares the available Tool/runtime, and C adds only the Skill instruction.

All treatments share the same source/question/build/model/effort and isolation contract. The host creates a per-run source snapshot excluding `.git`, then mounts source and input read-only. Scratch, `/tmp`, and `CODEX_HOME` are fresh tmpfs; output is the only persistent writable mount. The author repository, Git history, hidden assets, and prior runs are not mounted. Docker uses `--read-only`, `--network none`, dropped capabilities, and `no-new-privileges`. A narrow host-proxy socket carries only the Codex control plane.

Authentication is never baked into the image. `run` copies only the current `CODEX_HOME/auth.json` (or `AGENT_RUNNER_AUTH_FILE`) into a private temporary file, mounts it read-only, and the entrypoint copies it into the fresh home. Config, sessions, skills, and tokens are not logged or mounted.

Every completed run writes `answer.md`, raw Codex JSONL `agent.log`, real runner/tool `events.jsonl`, `metrics.json`, and `status.json`. Token usage is retained only when Codex emits authoritative `turn.completed.usage`; otherwise it is `unavailable`. Existing non-empty output is refused unless `--force` is explicit.

For B and C, the entrypoint owns exactly one task-local PySlang 11.0.0 runtime. Agents never start or manage the runtime, JSONL transport, sockets, or worker PIDs. The stable `rtlq` commands adapt the existing `scope/inspect/bindings/uses` primitives:

```sh
rtlq scope <target> [name] [--kind KIND] [--limit 50]
rtlq inspect <target>
rtlq bindings <instance> [--port PORT] [--limit 50]
rtlq drivers <signal> [--limit 50]
rtlq reads <scope> [--limit 50]
rtlq knowledge <module> --format readable
rtlq result <result-ref> --format json|readable
```

With reviewed knowledge ON, one `knowledge` call writes a structured JSON result and a readable Markdown view under the run output. Both are derived from the same response and bound to the run, request, Build, and candidate. The readable view distinguishes section, direct-child, and additional-requirement gaps. `result` rereads those files without another database query and preserves the saved `DIRECT`, `CONTEXT`, or `BYPASS` route and reasons; `status=ok` means only that the file read succeeded. Use `knowledge --format json` for existing JSON consumers. With knowledge OFF, the candidate is not mounted and no result directory is created.

`--knowledge on --knowledge-target MODULE` uses Controller prefetch by default. After the runtime is ready and before the Agent starts, the Controller performs one reviewed-knowledge query. `DIRECT` and `CONTEXT` deliver the readable result and run-local result reference once; `BYPASS` delivers only a short route/reason. The Runner owns only routing and delivery facts; the mounted `rtl-query` Skill is the sole owner of route consumption, gap filling, evidence reuse, and stop policy. If knowledge is requested without a reliable target, the runner records `target-unresolved` and does not mount/access the candidate. The historical Agent-selected behavior remains available only through explicit `--knowledge-delivery available`. Source and live Tool remain available in every route. Prefetch events use `actor=controller, phase=prefetch`, while subsequent Agent calls use `actor=agent, phase=analysis`, so Controller delivery is not reported as Agent routing. `--knowledge off` keeps the unmounted behavior. For prefetched DIRECT/CONTEXT Agent runs, the runner also records a literal `PASS / MISSING / ALTERED` check of expected qualified direct-instance identities; it never rewrites the answer.

Run five shell isolation probes per treatment plus three Luna fixture smokes per treatment with `./tools/agent-runner/tests/smoke.sh`. Use `--skip-build` to reuse existing images and `--agent-runs 0` for the model-free 15-probe subset. These fixtures are not Accuracy Core benchmark items.
