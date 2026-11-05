This is a cheap Agent Sandbox Runner smoke, not a benchmark question.

Perform all of these observable actions:
1. Read `/workspace/skills/SKILL.md`.
2. Read `/workspace/source/design.sv`.
3. Call exactly once: `rtlq scope query_top --limit 2`.
4. In the final answer, report the returned status and compilation_count, then end with the literal marker `RUNNER_SMOKE_OK`.

Do not start a runtime process yourself and do not make any other rtlq call.
