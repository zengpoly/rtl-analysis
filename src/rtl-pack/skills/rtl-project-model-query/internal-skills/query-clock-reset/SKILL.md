---
name: query-query-clock-reset
description: Project Model 查询内部原子步骤；查询寄存器及其 Clock/Reset 和派生时钟关系。
metadata:
  status: active
  visibility: internal
---

# Clock/Reset 静态查询

可用模块/寄存器 qualified name 片段限制范围；返回 register、Q、clock、reset、可读 edge/active/mode、原始极性、复位值，以及从所选寄存器时钟向上的 derived-from 链。带 `--name` 时不得混入其他模块的无关时钟链；assertion 同时带可读端点和 evidence。

只说明静态网表事实。coverage partial 时不推断 CDC、安全释放、门控质量、时序收敛或真实波形；这些缺口应返回给构建流程增加相应 producer。
