---
name: build-seed-semantics
description: Project Model 构建内部原子步骤；载入有来源、置信度和复核状态的工程语义 seed。
metadata:
  status: active
  visibility: internal
---

# 工程语义 seed

只由 MVP 构建父 Skill 调用。只接受 spec 中显式声明、能引用 Project-relative source span 的语义 assertion；记录 `human` 或 `llm` derivation、confidence、status 和 evidence。

LLM 结论默认 `review-required`，不能覆盖或升级 tool/verified 事实。没有足够来源时不载入；冲突并存并标 `conflicted`。本原子只负责载入合同，不在构建时自由阅读全仓生成无边界解释。

Project 私有 predicate 必须先在 profile 声明通用主维度、说明和 `requires`。任一依赖 capability 不是 verified 时，seed 只能是 partial/review-required；发布校验拒绝把它标为 verified。该护栏不建立依赖图，也不自动推理新事实。
