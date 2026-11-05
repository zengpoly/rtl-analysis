# 知识构建预览包（Linux x86_64）

本分支装配 `rtl-analysis-knowledge-preview-1-linux-x86_64.tar.gz`，包含 Build1、Build2 draft、Manual、Query 与全部公共 Skills、模板和配置。宿主要求 Linux x86_64、兼容的系统运行库、sh、tar 与 sha256sum。模型客户端和服务由使用者提供。

本次 [GitHub 发布](https://github.com/zengpoly/rtl-analysis/releases)只有源码与标签，尚无完整离线包附件。**Source code (zip/tar.gz)** 不包含 Python 运行时或工具缓存。V1 默认查询包 `rtl-analysis-agent-V1-linux-x86_64.tar.gz` 不能代替本分支的知识包。

## 构建

在已提交且干净的知识分支目录，使用通过版本锁和 SHA256 核验的本地缓存：

```sh
./packaging/build-offline --downloads .local/downloads   --python-cache /path/to/python-archive   --python-wheels /path/to/jinja-markupsafe-wheels   --output .derived/dist/knowledge-preview-1
```

Python 与附加依赖锁在 `packaging/runtime-lock.json`，工具清单在 `src/rtl-pack/config/toolchain.json`。构建器不联网，缺件或摘要不匹配即停止。输出包和 sidecar SHA256 文件另行分发，不进入 Git。本次未重新构建或验收该完整包。

## 完全离线安装

先从交付方取得完整包及可信 SHA256，在一个新的父目录核验并解压：

```sh
sha256sum -c rtl-analysis-knowledge-preview-1-linux-x86_64.tar.gz.sha256
tar -xzf rtl-analysis-knowledge-preview-1-linux-x86_64.tar.gz
cd rtl-analysis-knowledge-preview-1
./bin/install
./bin/rtl --project projects/example doctor
```

包内必须有 `bin/install`、`vendor/python/bin/python3`、`.local/downloads/`、`runtime-lock.json` 和 `SHA256SUMS`。安装校验清单并强制离线；缺件、校验或动态库加载失败时停止，补齐完整包后重试。源码归档不能据此声称安装成功。

打开整个解压目录，让 Agent 读根目录 `AGENTS.md`、`quickstart.md` 和对应 Skill。离线包执行 CLI 用 `./bin/rtl`；运行状态 helper 使用 `./vendor/python/bin/python3 src/rtl-pack/skills/rtl-build2-draft/scripts/state.py ...`，以此替换 Skill 示例中的 `./bin/workspace __domain-python`。`<Skill>` 为 `src/rtl-pack/skills/rtl-build2-draft`，配置位于 `src/rtl-pack/config/`；无需创建 Workspace 包装器。

自己的工程放入 `projects/<name>/source/` 并配置工程根文件。Build1 按回执判断就绪；Build2 始终是未审核草稿，正式语义需要独立审核、批准和受控发布。分析期间源码只读，产物写入工程 `analysis/`，既有工程保留。
