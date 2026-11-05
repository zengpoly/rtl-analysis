---
name: query-query-summary-entity
description: Project Model 查询内部原子步骤；查询模型概览或一个精确实体的出入关系。
metadata:
  status: active
  visibility: internal
---

# 概览与精确实体查询

summary 用于核对 model identity/status、producer、entity kind、predicate、coverage、integrity 和无 evidence 声明数。entity 必须使用精确 qualified name 或 entity ID，返回实体及 incoming/outgoing assertion；每条关系同时带可读 subject/object 与 evidence，不要求用户再拿 opaque ID 拼接端点。

同名无法唯一解析、空结果或 stale 状态如实返回。回答引用 assertion ID；需要独立 evidence artifact 时继续调用 evidence 原子，不从名字推断职责。
