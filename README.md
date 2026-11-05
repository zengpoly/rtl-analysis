# RTL Analysis

面向 RTL 工程的 Agent 分析工具，结合源码、锁定版本的开源工具和可追溯证据回答工程问题。当前分支为 **V1：源码 + Runtime 查询**，默认不构建或读取知识库。

## 两个版本

| 版本 | 分支 / 标签 | 工作方式 |
|---|---|---|
| V1 | [`main`](https://github.com/zengpoly/rtl-analysis/tree/main) / `v1.0.0` | Agent 直接读源码，按需使用任务内 PySlang Runtime 查询 |
| 知识构建预览版 | [`knowledge-build`](https://github.com/zengpoly/rtl-analysis/tree/knowledge-build) / `knowledge-preview-1` | Build1 结构知识、Build2 语义草稿、Manual 与知识辅助查询 |

两个版本共用能力源码，V1 源码仍保留知识构建实现；版本区别是默认使用流程和打包入口，并非 V1 已删除全部 Build1/Build2 代码。知识版的 Build2 草稿不等于已审核、已发布知识，具体范围见[版本说明](docs/versions.md)。

## 开始使用 V1

```sh
git clone https://github.com/zengpoly/rtl-analysis.git
cd rtl-analysis
```

阅读[使用说明](quickstart.md)，让能读取文件、执行命令的 Agent 按 [AGENTS.md](AGENTS.md) 安装和配置。仓库提供最小工程 [projects/example](projects/example/README.md)。模型客户端、模型服务和授权由使用者提供。

源码运行需要 Linux x86_64、Python 3.12、`requirements.txt` 中的依赖及锁定工具。默认安装只使用本地缓存；允许联网时先按使用说明建立虚拟环境、安装 Python 依赖，再显式执行 `./src/rtl-pack/scripts/install-tools --online` 获取工具。完全离线环境需要完整离线包。

**Source code (zip/tar.gz) 只是源码归档。** 本次 [GitHub 发布](https://github.com/zengpoly/rtl-analysis/releases)仅提供源码与版本标签，尚未提供离线包附件。需要 **V1 离线安装包（Linux x86_64）** 时，由交付方按[打包说明](packaging/README.md)构建并提供 `rtl-analysis-agent-V1-linux-x86_64.tar.gz`，不能将源码归档当作完整离线包安装。

## 目录

- `src/rtl-pack/`：CLI、锁定工具配置、Skills、模板和能力说明。
- `projects/example/`：最小示例；其他用户工程和分析产物不进入 Git。
- `packaging/`：V1 离线包装配脚本及第三方许可证。
- `tests/`：产品回归测试。
- `tools/agent-runner/`：Agent 运行辅助工具。
- `docs/`：需求、设计、实现与版本说明。

运行 `./check` 检查产品结构与回归测试。真实 RTL 源码在分析期间保持只读，结果保存到工程的 `analysis/`。本仓库仅保留产品源码和必要文档。
