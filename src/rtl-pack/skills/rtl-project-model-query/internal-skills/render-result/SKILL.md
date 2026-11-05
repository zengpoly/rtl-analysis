---
name: query-render-result
description: RTL Project Model Query 的内置原子步骤；把只读 JSON 结果展示成简洁结论、Markdown 表格和必要的 Mermaid 图。
metadata:
  status: active
  visibility: internal
---

# 展示查询结果

先给直接答案，再按视图组织：search/entity 用实体与关系表；coverage 先给 scope × dimension 总体状态，再列相关 capability 细项；FSM 用状态/转移表，关系复杂时附 Mermaid stateDiagram-v2；path 用 Mermaid flowchart 并附 assertion/evidence 表；Clock/Reset 和 evidence 用来源表。

代码发起的问题先标出 `code-context`，SQLite assertion 标为 `model/tool-verified`，工程解释标为 `semantic/review-required`。FSM 条件的人类命名若只由代码分支映射而非 producer 直接给出，也必须标 `code-context`，不能用 FSM cell evidence 冒充精确 branch span。不得省略 partial/unsupported/conflicted/stale，不把空结果画成“无连接”。完整 JSON 保存为 artifact；Markdown/Mermaid 只是同一结果的视图。输出样式与 `templates/workspace/README.md` 查询手册保持一致。
