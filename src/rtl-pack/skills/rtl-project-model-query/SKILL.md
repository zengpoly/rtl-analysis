---
name: rtl-project-model-query
description: 当用户需要快速检索或展示已有 SQLite RTL Project Model 中的实体、结构路径、FSM、Clock/Reset、覆盖、工程语义或证据时使用；只读查询并输出适合阅读与复用的结果。
metadata:
  status: active
  mode: legacy
  requires: legacy precomputed analysis/model/rtl-project-model.sqlite 与现有 rtl model query CLI
---

# 查询与展示 RTL Project Model

> Legacy / requires-precomputed-model：本入口保留兼容已有 Project Model；新的 Source + Runtime 分析默认使用 `rtl-query`。

本 Skill 的目标是“搜得快、看得懂”。事实只来自 `analysis/model/rtl-project-model.sqlite`；完整 `result-v1` JSON 保存到 task artifact，用户答案按问题选择 Markdown 表格、Mermaid 图或简洁分层说明。

面向用户的问题写法、命令和展示样例以 [Workspace 查询手册](../../templates/workspace/README.md) 为准；修改视图或输出约定时必须同步该手册。

每次调用本父 Skill 时，由编排 Agent 在被查询 Project 内建立 `analysis/tasks/<task-id>/`，并把下层 CLI 的 stdout/stderr 捕获为 `request.json`、`executions.jsonl`、`artifacts/` 和 `report.md`。裸 `rtl model query` 本身保持只读，只向 stdout 返回 JSON，不隐式创建 task。这些运行日志只用于审计、复现和改进 Query Skill，后续查询不得把日志当事实输入。

`report.md` 应保存用户问题、实际使用的视图、结果或未知、耗时、展示方式、证据/表达缺口，以及可复用的 Query Skill 优化建议。完整试用报告仍归这个 Query task；修改 Skill 时引用本日志，保留变更决策与回归结果。

## 内置原子 Skills

先读取路由，再只读取问题所需原子，最后按需读取展示原子：

1. [问题路由](internal-skills/route-question/SKILL.md)
2. [实体快速检索](internal-skills/search-entities/SKILL.md)
3. [概览与实体](internal-skills/query-summary-entity/SKILL.md)
4. [连接路径](internal-skills/query-path/SKILL.md)
5. [FSM](internal-skills/query-fsm/SKILL.md)
6. [Clock/Reset](internal-skills/query-clock-reset/SKILL.md)
7. [覆盖与缺口](internal-skills/query-coverage/SKILL.md)
8. [语义与证据](internal-skills/query-semantics-evidence/SKILL.md)
9. [结果展示](internal-skills/render-result/SKILL.md)

## 固定查询面

Legacy v2 只允许九个只读视图：`summary`、`search`、`entity`、`path`、`fsm`、`clock-reset`、`coverage`、`semantics`、`evidence`。不开放任意 SQL。

```sh
rtl model query --project <project-dir> --view summary
rtl model query --project <project-dir> --view search --text <literal> [--kind <kind>] [--limit <1..100>]
rtl model query --project <project-dir> --view entity --name <qualified-name>
rtl model query --project <project-dir> --view path --start <qualified-name> --end <qualified-name> --max-depth <1..32>
# 名称本身跨 kind 重复时，改用 --start-id <entity-id> --end-id <entity-id>
rtl model query --project <project-dir> --view fsm --name <qualified-name>
rtl model query --project <project-dir> --view clock-reset --name <qualified-name-fragment>
rtl model query --project <project-dir> --view coverage
rtl model query --project <project-dir> --view semantics
rtl model query --project <project-dir> --view evidence --assertion-id <id>
```

Knowledge Contract v0.1 Prototype candidate 另提供一个最小 `context` view。调用者必须先给出精确 entity ID，
再从 Contract 登记的 query role 中选择至少一个 role；CLI 不理解自然语言、不回答问题，也不扫描源码补事实。
检索固定为 1–2 跳，返回记录上限为 1–200，不能把它扩展为任意 traversal、Query Engine 或 Graph Engine。

```sh
rtl model query \
  --project <project-dir> \
  --model analysis/model/<candidate>.sqlite \
  --view context \
  --entity-id <exact-entity-id> \
  --role <contract-query-role> [--role <contract-query-role>] \
  --max-depth <1|2> --limit <1..200>
```

Context Pack 分开返回 deterministic Facts、AI Knowledge、AI→Fact support、source/provenance evidence 与明确的
boundary/unsupported；它不会把 AI Knowledge 提升成 deterministic Fact。Prototype candidate 不承担本轮 legacy
九 View 兼容要求。

`search` 是 SQLite 内的 literal 名称检索，按 exact、prefix、contains 稳定排序，可按 kind 过滤，并强制限制 1—100 条。查询不得扫描源码或 tasks；当前 OpenC910 MVP 的代表性查询目标为单次低于 1 秒。

## 回答与展示合同

1. 必要时先查 summary/coverage，确认模型 complete、identity、integrity、producer、七维总体状态和 required capability 细项；名称不确定先 search，再用唯一 qualified name 进入精确视图；若 path 端点的 qualified name 跨 kind 重复，使用搜索结果中的 entity ID。
2. 用户从正在阅读的代码发问时，从其提供的 snippet、模块和标识符提取搜索词；先展示歧义候选，再组合 entity/path/FSM/Clock-Reset/evidence。只有文件行号而没有可识别对象时返回当前 MVP 不支持反向行号检索。
3. 开头直接给结论；实体集合、coverage、FSM、Clock/Reset 和 evidence 优先用 Markdown 表格；路径与 FSM 关系适合时用 Mermaid，并在图后保留 assertion/evidence 对照表。
4. 明确标记 `code-context`、`model/tool-verified`、`semantic/review-required`。用户片段中的布尔条件不能冒充模型已结构化的条件映射；模型关系仍以 assertion 为粒度给出 derivation、status、confidence 和 evidence locator/span。
5. 完整 JSON 始终进入 artifact，不把展示文本变成第二知识模型。`not-found` 只表示当前模型在给定 scope、关系和深度内没有结果；不能扩大成硬件上不存在。
6. `partial`、`unsupported`、`failed`、`conflicted`、`review-required` 或 `stale` 必须原样呈现。维度总体状态不能隐藏 capability 细项；Project 私有结论的 `requires` 有任一项不是 verified 时，不得把该结论回答成 verified。当前 MVP 范围缺知识时转交 `rtl-project-model-mvp-build`；更大范围说明需要未来独立 Build Skill。
7. 不把静态关系描述成动态可达、固定周期、CDC 或签核证明。
8. 收尾时在 Query task 的 `report.md` 记录“对 Query Skill 的优化反馈”；运行日志是反馈来源，不是下一次回答的知识来源。
