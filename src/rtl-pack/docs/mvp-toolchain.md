# 本地工具包与适配合同

本合同按锁定工具合同维护可重复、只读的RTL分析闭环；已覆盖第一阶段源码诊断与第二阶段局部结构分析，
不代表完整工具选型或完整OpenC910覆盖已经结束。

## 锁定工具

| 工具 | 固定版本 | 许可证 | 当前适配能力 | 本地文件 |
|---|---|---|---|---|
| slang | `v11.0` | MIT | 单文件语法检查、CST模块轮廓；Project Model 的 compiled AST/elaboration、层级和端口绑定 | `bin/slang` |
| PySlang | `11.0.0` | MIT | 默认 Runtime Query 的 task-local Compilation、AST/elaboration、symbol/type、binding、driver/read/timing facts | `site-packages/pyslang`、`bin/pyslang-version` |
| Verible | `v0.0-4163-g6cce8f19` | Apache-2.0 | 单文件默认规则lint | `bin/verible-verilog-lint`、`bin/verible-verilog-syntax` |
| Yosys / YoWASP | `v0.67` / `0.67.0.0.post1190` | ISC | legacy 单文件结构摘要和 Project Model；不进入新的 Runtime Query 路线 | `bin/yosys`和锁定Python wheels |

下载 URL、上游资产 SHA-256 和精确载荷在 `src/rtl-pack/config/toolchain.json` 中。`src/rtl-pack/scripts/install-tools` 只把校验通过的
上游二进制安装到被Git忽略的`.local/tools/<tool>/<version>/`，并写当前内容哈希receipt；不编译、补丁或
修改工具源码，不需要sudo，也不写系统目录。网络不稳定时下载使用断点续传，已校验的缓存不会重复下载。

安装器默认只使用本地缓存（也可显式传 `--offline`）。完全离线包使用根目录 `README.md` 和 `./bin/install`，CLI 用 `./bin/rtl`；安装器自动识别包目录，只校验和安装本地缓存，缺件即停止。GitHub Source code zip/tar 归档不含完整离线运行时，需先取得 V1 完整离线安装包。

```sh
./src/rtl-pack/scripts/install-tools --offline
./src/rtl-pack/bin/rtl doctor
```

维护者更新缓存时，可在用户明确允许联网的源码环境显式使用 `--online` 启用下载；这不是离线安装步骤，离线包会拒绝该选项。

## 稳定CLI合同

以下既有原子命令保持兼容。0.8.0 增加默认 `rtl query` JSONL worker；它只投影锁定 PySlang native facts，不使用 Yosys、SQLite 或 Project Model。0.4.0—0.7.0 的 Project Model 子命令继续作为 legacy 路线保留：

```sh
./src/rtl-pack/bin/rtl doctor
./src/rtl-pack/bin/rtl syntax --file C910_RTL_FACTORY/gen_rtl/rtu/rtl/ct_rtu_encode_8.v
./src/rtl-pack/bin/rtl lint --file C910_RTL_FACTORY/gen_rtl/rtu/rtl/ct_rtu_encode_8.v
./src/rtl-pack/bin/rtl outline --file C910_RTL_FACTORY/gen_rtl/rtu/rtl/ct_rtu_encode_8.v
./src/rtl-pack/bin/rtl structure --file C910_RTL_FACTORY/gen_rtl/rtu/rtl/ct_rtu_encode_8.v
./src/rtl-pack/bin/rtl connectivity --file C910_RTL_FACTORY/gen_rtl/rtu/rtl/ct_rtu_encode_8.v --signal x_num
./src/rtl-pack/bin/rtl --project projects/example query --build projects/example/runtime-query-build.json
```

裸 `doctor` 仅检查锁定工具，不要求预置 RTL 工程；带 `--project` 或 `--target` 时同时检查所选源码。其他无选择器入口继续使用 `src/rtl-pack/config/targets.json` 中的研发基准。Coder 或其他用户环境使用
项目选择器，默认读取项目配置中的`entry`，也可显式选择同一`source/`内的另一个单文件：

```sh
./src/rtl-pack/bin/rtl --project /workspace/projects/demo doctor
./src/rtl-pack/bin/rtl --project /workspace/projects/demo outline
./src/rtl-pack/bin/rtl --project /workspace/projects/demo syntax --file rtl/top.sv
```

项目固定布局和配置如下：

```text
projects/demo/
├── source/             # 源码；可以是普通目录，或clean精确Git worktree
├── analysis/           # 用户显式重定向保存的中间/最终结果
└── rtl-project.json
```

```json
{
  "schema": "rtl-analysis-agent/project-v1",
  "name": "demo",
  "source": "source",
  "analysis": "analysis",
  "revision": null,
  "entry": "rtl/top.sv"
}
```

`source`与`analysis`首版必须使用上述固定相对目录，`entry`只能位于`source/`内。Runtime build identity 另以 `runtime-query-build-v1` 明确给出 ordered files/filelists、include、define、top、single-unit、source-relative environment 和可选 top parameters；所有路径必须留在 `source/`。`revision`为`null`时结果
明确报告`unversioned`，不伪装成已验证版本；填入40位小写Git SHA时，CLI要求`source/`是该精确revision的
clean worktree。项目根改名或搬到另一绝对路径不会进入JSON结果。既有原子分析仍是单文件能力并只写标准输出；Project Model 只处理 spec 显式列出的有界多文件 scope，在 `analysis/model/rtl-project-model.sqlite` 原子发布 current，并可显式导出/回灌规范化 JSONL。它不自动发现完整工程配置，也不定义源码归档格式。

领域仓本地默认使用`.local/tmp`保存单次命令临时文件。只读镜像装配必须显式设置
`RTL_ANALYSIS_TMPDIR`到容器内绝对、非符号链接的可写临时目录；CLI不会把该绝对路径写入结果，目录中的
临时文件仍在命令结束时清理。这个入口只承载可丢弃临时数据，不替代项目`analysis/`或YoWASP cache。

文件路径必须相对所选Target根目录，适配器先验证固定revision和干净工作树。结果统一使用
`rtl-analysis-agent/result-v1`：退出码`0`表示通过，`1`表示分析完成但发现问题，`2`表示适配器、边界或
环境错误。legacy 诊断/连接最多返回100条，源码文件最大5 MiB；Runtime Query diagnostics 返回前20条和完整计数，primitive 结果硬上限100。普通短命令最多30秒，Yosys版本检查和结构命令
最多90秒，以容纳2 CPU容器中实测约56秒的首次架构缓存生成。legacy 单文件命令仍没有多文件配置；新的
Runtime Query 通过显式 build identity 支持 PySlang 多文件/filelist/include/define/elaboration，但不自动发现工程配置。Project Model 的多文件输入仍由其 legacy spec 给出。

## 独立升级与未来挂载

升级单个工具时，只更新它在manifest中的版本、官方资产和SHA-256，新版本会进入新的版本目录。必须先用
同一个OpenC910样本通过`toolchain-smoke`和`mvp`，再切换锁定版本；旧目录可在验收结束前保留用于回退。
不要靠覆盖现有版本目录完成升级。

装配IAS时，工具包保持相同的`<tool>/<version>/...`布局，只读安装后通过
`RTL_ANALYSIS_TOOL_ROOT`指向挂载根。skills和适配器跟随本领域包发布，code-server Runtime不需要重新
安装或拥有这些工具，也不依赖Web AI仓库。

当前本地Yosys执行载体是与上游`v0.67`对应的YoWASP WebAssembly包，用来避免下载约741 MB的完整OSS
CAD Suite或在缺少构建依赖的目标主机修改/编译上游源码。首次生成机器码缓存实测约24秒、峰值约
2.2 GB、缓存约207 MB；缓存后结构命令约0.4秒、约140 MB。因此正式IAS Tool Pack优先使用原生Yosys，
YoWASP保留为本地便携适配与合同验证载体。替换执行载体不能改变`result-v1`，并必须通过相同Gate。

Yosys分析前，适配器把选中的输入复制到忽略的临时目录，仅向WebAssembly挂载该临时目录；OpenC910目标
目录不暴露给工具写入。完整Yosys JSON也只存在于临时目录，Agent只接收规范后的有界摘要。
