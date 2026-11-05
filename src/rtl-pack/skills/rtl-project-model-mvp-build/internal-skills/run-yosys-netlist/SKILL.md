---
name: build-run-yosys-netlist
description: Project Model 构建内部原子步骤；约束并核验 Yosys netlist、寄存器与 Clock/Reset producer。
metadata:
  status: active
  visibility: internal
---

# Yosys netlist producer

只由 MVP 构建父 Skill 调用。使用 spec 固定的模块输入和锁定 Yosys 流程生产标准 cell/net 连接、数据/控制端口、寄存边界、clock/reset 极性和值及已证实的派生时钟关系。

不得从命名猜测时钟域、CDC、动态传播或周期数。构建后核对 yosys-netlist producer complete，寄存器 assertion 有 netlist/tool evidence；不兼容 SystemVerilog、黑盒或缺配置时降级，不制作 stub 绕过。
