---
name: rtl-knowledge-build
description: 为一个 RTL Project 的明确 Analysis Scope 构建或恢复 compiler-grounded Build1 Knowledge candidate，并生成当前 deterministic Manual；依据机器状态完成或停止。
metadata:
  status: active
  mode: build1
  requires: 有效 rtl-project.json、Project-relative runtime-query-build.json、锁定 PySlang 与 public rtl CLI
---

# Build1 RTL Knowledge

源码仓库根使用 `./src/rtl-pack/bin/rtl` 替换下方 `rtl`；完整离线包使用 `./bin/rtl`。参数和机器状态规则相同。

## When to use

用户要求从RTL Project建立、恢复或验证Build1结构知识与当前Manual时使用。它只构建compiler-grounded结构世界；不生成AI semantic、Behavior、coverage或Query答案。

## Inputs

只解析：

- RTL Project目录；
- Analysis Scope：`whole-design`，或一个明确的`actual-root` exact elaborated path。

module-definition、multi-root、source-only当前没有正式producer。遇到这些模式仍调用public command取得`BLOCKED`，不要伪装支持、挑fake root或自行降级。

## One public command

Project与Scope已经明确时，读完本Skill后直接执行一次下列命令；不要先跑`find`、`help`、`--version`、`doctor`或内部preflight。机器结果本身就是唯一preflight与completion status。

Whole Design：

```sh
rtl model knowledge-build --project <project-dir> --scope whole-design
```

Exact actual root：

```sh
rtl model knowledge-build --project <project-dir> --scope actual-root --target-path <exact-path>
```

只有用户明确指定非默认制品位置时才添加`--candidate`或`--manual`；一旦指定，第一次调用就必须原样带上，禁止先试默认路径。不要读取或修改 SQLite，不调用内部Python，不自己串producer/renderer，也不要从长日志推断完成状态。

## Result and stop contract

只按`rtl-analysis-agent/build1-status-v1`顶层`status`处理：

- `READY`：candidate和Manual均已验证；报告scope、identity、path、SHA、counts、limitations与timing。tool-side timing只包含`candidate_validation`、`manual_validation`、`build`、`render`和`total`；模型controller latency不属于product result。
- `PARTIAL`：candidate已READY但Manual未READY；报告`reason_code`和`safe_next_action`。Manual不存在或上次render失败时，相同命令只会render，不会重建candidate；`MANUAL_INCOMPLETE`表示已有derived Manual缺文件或路径不安全，命令不会覆盖它，须选择新Manual路径，或审核后删除/移动损坏Manual再原命令render。
- `BLOCKED`：立即STOP并原样报告`reason_code`、`human_message`、`safe_next_action`；不得猜hierarchy、改SQL、patch SQLite或换scope掩盖问题。

确认`heavy_actions.compile_build_count <= 1`且`render_count <= 1`。已有matching candidate/manual时两者应为0。不要为“再确认一次”重跑成功的heavy action。

## Mandatory STOP cases

包括：scope不能唯一解析或producer不支持；build/source identity mismatch或stale；candidate integrity失败；mandatory skeleton truncation或producer budget exhausted；Manual声明文件缺失、escape、symlink或非regular file；需要新RTL算法、G52/G53变更、模型读取/修改SQLite或猜测hierarchy。

## Resume and evidence

总是以完全相同的Project、Scope、build、candidate和Manual参数重试可恢复失败。candidate已READY而render失败时，不删除或重建candidate。只有machine result里的path/SHA/scope/build identity/counts和telemetry可作为完成证据。

Manual只是Build1成功后的deterministic serving step；本Skill不拥有template、页面布局、section顺序或视觉设计。

## Public handoff to Build2 draft

后续语义草稿由发行的 `rtl-build2-draft` 接手。向它交付完整 machine result：`status`、`candidate.path/sha256/reused`、`build_identity`、`analysis_scope/scope_id`、`heavy_actions` 与 `reason_code`。只有 candidate 自身 READY 且身份匹配才可用；`PARTIAL` 不等于 Manual READY。消费者通过 public `rtl model query --view context` 读取 exact entity 的有限 Fact 和 Source Evidence，不使用 legacy MVP view、不直接读 SQLite。Build1 不替 Build2 判定语义或发布知识。

保存完整 result 后直接交接，不重复读取已经读过的主 Skill。`--project` 可为绝对路径；后续 `--model`（或 `RTL_CONTEXT_MODEL`）必须原样取 `candidate.path` 的 Project-relative 值，不能在前面拼 Project。地图的 `structured_detail_map` 以 exact entity ID 为 key，详情路径相对 manifest 目录；字段投影与非空/唯一检查见 [Build2 有界读取](../rtl-build2-draft/references/bounded-reading.md)。
