---
name: query-query-semantics-evidence
description: Project Model 查询内部原子步骤；查询工程语义及任一 assertion 的来源证据。
metadata:
  status: active
  visibility: internal
---

# 工程语义与证据查询

semantics 返回 human/llm 工程语义 assertion；逐条报告 derivation、confidence、status 和 evidence。`review-required` 不升级为 verified，冲突结论并列呈现。

evidence 使用精确 assertion ID，返回 producer、Project-relative locator/span、artifact/tool identity。证据缺失视为模型完整性问题并停止引用该结论；不能从历史任务日志补造 evidence。
