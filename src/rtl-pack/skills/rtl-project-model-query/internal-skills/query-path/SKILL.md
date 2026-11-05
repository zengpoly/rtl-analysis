---
name: query-query-path
description: Project Model 查询内部原子步骤；查询两个唯一实体之间的有界最短结构路径。
metadata:
  status: active
  visibility: internal
---

# 有界连接路径查询

start/end 可各用唯一 qualified name，或在同名 port/signal 无法唯一解析时各用 entity ID；同一端点不能同时给 name 和 ID。深度为 1—32，默认 8。返回的每一跳必须保留 predicate、assertion ID 和 evidence。

`not-found` 只代表当前模型、当前关系集合与深度没有路径；不能声称物理上绝对不连接。路径是结构关系，不自动证明事件传播、周期、协议或动态可达性。
