# RTL 知识构建预览设计

`src/rtl-pack/` 保存 CLI、Skills、配置与模板；`projects/<name>/` 保存只读 `source/`、工程配置和可写 `analysis/`。

Build1 以明确 Build identity 与 whole-design / actual-root 范围构建 compiler-grounded candidate。candidate 保存结构 Fact 与源码证据，Manual 从它生成 HTML 和 structured detail，不形成第二知识正本。

Build2 消费匹配的回执、结构地图和公开 Query，按计划将阶段原件和状态保存到 `analysis/tasks/<task>/`。身份、引用或依赖漂移时停止受影响步骤；有效阶段可核验后恢复。草稿不写回正式知识，作者自检不代表独立审核。

查询复用适用的已发布 Knowledge；DIRECT / CONTEXT / BYPASS 表达适用性与缺口。Runtime 使用任务内 PySlang Compilation，提供 `scope / inspect / bindings / uses`。源码、Compiler Fact 与受审语义分别标明。

产品路径和工具合同见[工具链](../src/rtl-pack/docs/mvp-toolchain.md)，具体使用入口见[使用说明](../quickstart.md)。本分支保留 V1 的通用修复，通过入口与打包范围启用知识工作流。
