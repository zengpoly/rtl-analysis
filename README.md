# RTL Analysis · 知识构建预览版

当前分支为 **knowledge-build**：提供 Build1 结构知识、Build2 语义草稿、派生 Manual 和知识辅助查询。Build2 产物保持 `DRAFT_UNREVIEWED`；自检通过不等于独立审核或正式发布。

## 两个版本

| 版本 | 分支 / 标签 | 工作方式 |
|---|---|---|
| V1 | [`main`](https://github.com/zengpoly/rtl-analysis/tree/main) / `v1.0.0` | 直接读源码，按需使用任务内 Runtime；默认不读知识库 |
| 知识构建预览版 | [`knowledge-build`](https://github.com/zengpoly/rtl-analysis/tree/knowledge-build) / `knowledge-preview-1` | 显式 Build1 / Build2；查询复用适用的已发布知识，缺口回到源码 / Runtime |

两个分支共享现有能力实现，通过 Agent 使用入口、文档和打包范围区分工作流。V1 源码仍保留知识实现，知识分支保留 V1 的路径识别和离线安装修复。具体边界见[版本说明](docs/versions.md)。

## 开始使用

```sh
git clone --branch knowledge-build https://github.com/zengpoly/rtl-analysis.git
cd rtl-analysis
```

按[使用说明](quickstart.md)准备 Python 3.12 环境、`requirements.txt` 依赖与锁定工具，先试用 [projects/example](projects/example/README.md)。Agent 根据 [AGENTS.md](AGENTS.md) 选择 Build1、Build2、Manual 或查询步骤；不要把未审核草稿当作正式知识。

**Source code (zip/tar.gz) 只是源码归档。** 本次 [GitHub 发布](https://github.com/zengpoly/rtl-analysis/releases)只有源码与标签，尚无离线包附件。需要 **知识构建预览包（Linux x86_64）** 时，由交付方按[打包说明](packaging/README.md)构建并提供 `rtl-analysis-knowledge-preview-1-linux-x86_64.tar.gz`；V1 离线包只提供默认源码查询技能，不能代替知识版完整包。

## 目录与验证

- `src/rtl-pack/`：CLI、工具配置、Build / Query / Manual Skills、模板与能力说明。
- `projects/example/`：最小工程；其他用户源码和分析结果不进入 Git。
- `packaging/`：本分支的知识构建预览包装配脚本及第三方许可证。
- `tests/`：产品回归；`tools/agent-runner/`：Agent 运行辅助工具。
- `docs/`：需求、设计、实现、版本说明与[实际检查结果](docs/validation.md)。

运行 `./check` 验证产品结构与回归。模型客户端、模型服务与授权由使用者提供；仓库不携带预先审核的知识库或大工程源码。
