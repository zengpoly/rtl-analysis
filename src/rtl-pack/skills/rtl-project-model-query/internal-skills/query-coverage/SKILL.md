---
name: query-query-coverage
description: Project Model 查询内部原子步骤；解释 6+1 维度的 verified、partial、unsupported、failed 或 stale 状态。
metadata:
  status: active
  visibility: internal
---

# Coverage 与缺口查询

读取 coverage 视图的七维总体记录和 `scope × dimension × capability` 细项，按 scope、dimension、status、producer 和 reason 汇总。总体 `verified` 只在该 scope/dimension 的 required capability 全部 verified 时有效；缺行或混合状态为 partial，`unsupported/failed/stale` 不能隐藏。

回答缺少的输入、producer 或验证类别；当前有界 MVP 可重新调用 MVP Build，更大范围转交未来独立的全代码 Build Skill。coverage 是能力声明，不是单条设计结论。
