---
name: rtl-question-factory
description: 当 RTL 工程师或 Agent 开发者需要从现有 Question Space 在真实 RTL 中找可验证问题，或把一个真实 seed 问题扩展为同类 sibling questions 时使用；不自动创建 hidden Golden 或宣称 benchmark 已冻结。
metadata:
  status: active
  version: "1.0"
  requires: 可读 RTL Source、Project/build identity、Workspace Question Space 与 questions helper
---

# RTL Question Factory

本 Skill 把工程问题变成可复核的 question candidate。问题先于 Tool；不得因为某个 primitive 方便就反向选题。
它由当前 VSCode Agent 直接读取和执行，不依赖独立模型 CLI。

仅写 `questions/inbox/` 或 `questions/generated/`。`capability/` 和 `sources/` 只读；临时文件只写 `state/tmp/`。

## 共同合同

1. 冻结 project/source/build identity，不把全仓文本搜索当成 executable RTL。
2. 先证明 Required RTL Phenomenon 确实存在，再写自然问题。
3. 每题必须有唯一或明确有界的真值、Source evidence 和工程价值。
4. 只在 deterministic obligation 适合时调用 `rtl-query`；Tool 不能证明意图或长因果链。
5. 生成者提出的 answer/evidence 只是 verification input，不构成独立验证。
6. 不在 Solver-visible metadata 中写 hidden answer、critical facts、旧答案或分数。

## 模式选择

### Space-driven generation

用户要求从 Question Space 为指定 RTL 生成代表性问题时：

1. 仅在用户提供分类题库时读取该题库并选择 slot；本包不再内置历史题库。未提供题库时使用工程师 seed question 路线，不猜测 slot。
2. 在 frozen build 中找与 slot 的 Required RTL Phenomenon 相符的真实对象。
3. 对每个 candidate 记录 category、mother slot、fact group、difficulty 和 evidence requirement。
4. 拒绝不存在、真值不唯一、依赖 README/testbench 泄漏或只是换 symbol 名的题。

### Seed-driven expansion

用户要求扩展 `questions/inbox/<seed>.json` 时：

1. 重新读 seed 和相关 RTL，归类 category、mother slot 与 fact group。
2. 在同一 frozen RTL 中找相同 relation 但不同 instance/signal/module 的 sibling phenomenon。
3. 生成 **3–10** 题；每题必须独立证明 phenomenon，不机械替换名称。
4. 把 `seed_id` 指回原问题，生成到 `questions/generated/<project>/<batch>/`。
5. 重复题、低价值题或无法验证题标 `REJECTED`，不为凑数保留。

## 生命周期

- `DRAFT`：问题文本或 metadata 未齐。
- `CANDIDATE`：工程问题已建档，尚未独立验证。
- `VERIFIED`：独立重读 Source 或 deterministic verification 已确认 phenomenon/真值/证据。
- `FROZEN`：只能由正式 benchmark/controller 流程冻结。
- `REJECTED`：不存在、重复、泄漏、低价值或无法唯一判定。

普通工程师新问题默认 `CANDIDATE`。本 Skill 可提供 verification record，但不得自行将自己生成的 candidate 升为 `FROZEN`。

## 完成条件

交付前对每个 JSON 运行 `bin/questions validate <file>`，然后运行 `bin/questions report`。最终报告区分 generated/verified/rejected，不把候选数当成质量。
