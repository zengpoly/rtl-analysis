---
name: query-query-fsm
description: Project Model 查询内部原子步骤；查询 FSM 状态、转移、复位状态和控制输入。
metadata:
  status: active
  visibility: internal
---

# FSM 结构查询

使用完整 FSM qualified name；未指定时可列出模型内全部 FSM。报告状态/编码、transition、reset state 和 control input；`transition_assertions` 提供每个 current-state → transition → next-state 的可读端点与 evidence。

Yosys/KISS2 的 transition evidence 可能只定位 FSM state cell，而不能定位每个 RTL 分支；这时条件位串是 `model/tool-verified`，人工映射到布尔条件和精确分支行必须标 `code-context`。静态提取不证明所有转移动态可达；缺失、非法状态未覆盖或 producer partial 时明确未知，不用源码印象补齐。
