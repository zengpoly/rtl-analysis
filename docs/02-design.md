# RTL 分析设计

`src/rtl-pack/` 保存 CLI、工具版本锁、Skills 和模板；`projects/` 保存待分析工程，内置 `projects/example`。已发出的说明采用的 `src/workspace/projects/` 路径继续支持。

每个工程包含只读的 `source/`、`rtl-project.json`、`runtime-query-build.json` 和可写的 `analysis/`。用户工程和分析产物默认不进入 Git。

Agent 使用 `rtl-query` Skill：局部问题直接读取完整源码构造；依赖实例、参数、类型或绑定的事实使用 PySlang Runtime。Runtime 提供 `scope / inspect / bindings / uses`，通过 JSONL 在一次编译后处理查询，任务结束释放状态。

工具安装缓存位于 `.local/downloads/`，安装结果位于 `.local/tools/`，均由版本和校验清单约束。打包使用同一能力源码；旧研发题库与实验记录不再作为输入。

源码和工具合同见 [工具链](../src/rtl-pack/docs/mvp-toolchain.md)。
