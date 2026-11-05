---
name: query-search-entities
description: RTL Project Model Query 的内置原子步骤；在 SQLite 中有界、稳定地检索实体名称。
metadata:
  status: active
  visibility: internal
---

# 快速检索实体

名称不完整时使用 `search`，传入非空 literal text、可选精确 kind 和 1—100 limit。结果按 exact、prefix、contains，再按 qualified name/kind/ID 稳定排序。

只查询 current SQLite，不扫描源码或 tasks，不接受正则或任意 SQL。选择候选后展示 kind、qualified name、entity ID 和 match；进入 entity 等查询时可用 ID，path 名称跨 kind 重复时必须改用端点 ID，不能默选歧义项。
