---
name: query-route-question
description: Project Model 查询内部原子步骤；把自然语言问题缩成固定视图和有界参数。
metadata:
  status: active
  visibility: internal
---

# 查询路由

只由 `rtl-project-model-query` 调用。先确认 Project、current 和问题范围，再把问题映射到一个或少量固定视图；记录原问题、视图、qualified name、起止点、深度或 assertion ID。用户贴出 MVP 代码时提取其中的模块、端口、signal、state 和 register 名称作为 search 候选，但不把 snippet 当成 SQLite assertion。

模型健康/有什么用 summary，名称不确定先用 search，单对象关系用 entity，跨对象连接用 path，状态机用 fsm，寄存与时钟复位用 clock-reset，能力缺口用 coverage，工程解释用 semantics，来源用 evidence。只有文件行号而无可识别对象时说明当前没有反向行号视图；问题超出这些视图时返回 unsupported 并说明所缺 producer，不生成自由 SQL。
