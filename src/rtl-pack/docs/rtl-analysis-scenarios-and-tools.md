# RTL 分析能力与工具

本页描述当前产品入口与边界。工具精确版本、来源和 SHA256 以 [toolchain.json](../config/toolchain.json) 为准；安装和工程合同见 [mvp-toolchain.md](mvp-toolchain.md)。分支默认流程见根目录的使用说明与版本说明。

## 当前入口

| 场景 | 入口 | 工具 / 产物 | 能力边界 |
|---|---|---|---|
| 工具与工程检查 | `rtl doctor`、`rtl --project <dir> doctor` | 锁定工具版本、源码身份和配置检查 | 不能代替完整工程功能验证 |
| 单文件语法检查 | `rtl --project <dir> syntax` | slang 诊断 | 不承接多文件 elaboration |
| 单文件规范检查 | `rtl --project <dir> lint` | Verible 规则、位置与诊断 | 风格问题不等于功能错误 |
| 单文件轮廓 | `rtl --project <dir> outline` | slang CST、模块与端口 | 不展开完整实例层次 |
| 单文件结构与连接 | `structure`、`connectivity` | Yosys / YoWASP 的原生产物 | legacy 有界入口，不能证明时序或跨模块语义 |
| 多文件 Runtime 查询 | `rtl --project <dir> query --build <build.json>` | PySlang 11.0.0；JSONL `scope / inspect / bindings / uses` | 同任务编译一次；明确 top、文件顺序、include、宏和参数；任务结束释放 Compilation |
| 结构知识 Build1 | `rtl model knowledge-build --project <dir> --scope whole-design` | compiler-grounded candidate、状态回执和 Manual | 支持 whole-design 与明确 actual-root；结构事实不能证明行为语义 |
| 语义草稿 Build2 | `rtl-build2-draft` Skill 与其公开状态 helper | 分阶段 evidence、draft、review、组合自检与恢复状态 | `DRAFT_UNREVIEWED`；自检不等于独立审核或正式发布 |
| 知识辅助查询 | `rtl model query --view module-knowledge ...` | 已发布且身份匹配的知识投影 | 确切对象、Build 与适用性必须匹配；缺口回到源码 / Runtime |
| 派生 Manual | `rtl model manual-build ...` | 只读映射生成 HTML、structured detail、manifest | HTML 不是知识正本；正式语义须通过受控发布核验 |
| 草稿预览 | `rtl model manual-preview ...` | task 内派生静态页面 | 保留未审核标记，不写入正式知识或 Manual |
| legacy Project Model | `rtl model build/query/export/import` | SQLite `rtlpm-v2` 与有界 producer/query | 显式选择预计算路线时才使用；不自动进入默认 Runtime 查询 |

完整参数以 `rtl --help`、`rtl model --help` 与各子命令 `--help` 为准。V1 默认不调用 Build1/Build2 或读取知识；知识分支按明确目标使用这些入口。

## 分析原则

局部问题先读取足够的完整源码构造；依赖实例、有效参数、类型或绑定的事实使用 Runtime。Compiler Fact、RTL Source Evidence 和模型推理分别标明；对复位、优先级、更新条件和覆盖路径保留必要上下文。工具结果、哈希一致或文件齐全都不能代替行为正确性证明。

Knowledge 只有当前有效、范围适用且有足够证据时才能关闭对应子问题。`DIRECT / CONTEXT / BYPASS` 是适用性路由；存在缺口时只补明确缺口，不把部分材料描述为完整覆盖。没有候选或可靠 exact target 时直接走源码 / Runtime。

工程源码保持只读；配置、工具、来源身份或知识失效时停止受影响步骤并报告。分析产物保存到工程 `analysis/`，用户工程和凭据不进入 Git。

## 验证范围

`./check` 检查产品文件、执行权限、Skill 元数据并运行产品回归。测试使用轻量夹具，不携带大工程源码、模型答案、旧实验日志或预先审核的知识库。

仿真、Formal、CDC/RDC、PPA、完整工程覆盖、模型准确率提升及目标机器部署需要各自的输入、工具与实际验证，本仓库的回归通过不代表这些能力已经验收。模型客户端及模型服务由使用者提供。
