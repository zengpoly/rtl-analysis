---
name: build-run-yosys-fsm
description: Project Model 构建内部原子步骤；约束并核验 Yosys FSM/KISS2 producer。
metadata:
  status: active
  visibility: internal
---

# Yosys FSM producer

只由 MVP 构建父 Skill 调用。对 spec 明确登记的 FSM 运行锁定的 Yosys FSM 提取/导出流程，载入状态、编码、reset state、transition 和 control input。

普通条件链不能自动命名为 FSM；KISS2 编码必须与源码/网表对象对账。构建后核对 yosys-fsm producer complete，状态与转移计数、reset 编码及 evidence 齐全；无法提取时写 failed/unsupported，不手工编造转移。
