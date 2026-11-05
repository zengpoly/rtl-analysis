# RTL Analysis Agent V1 离线安装

适用 Linux x86_64，已验证 Ubuntu 24.04 / glibc 2.39。内置 Python 3.12.14、PySlang 11.0.0、slang、Verible、YoWASP Yosys 及其依赖，无需系统 Python、pip 或联网下载。宿主仍需提供兼容的系统运行库、sh、tar 和 sha256sum；其他系统版本须现场验证。

## 安装与使用

**用 AI 安装：** 从交付方取得完整压缩包和可信校验值，转入目标机器并校验、解压。用已有的 Agent 打开整个解压目录，直接说 `please help me install`（或“帮我安装”）。Agent 按根目录 `AGENTS.md` 自动安装并验证示例工程，全程使用随包文件，无需联网。Agent 需能读取目录并执行本地命令。

本次 [GitHub 发布](https://github.com/zengpoly/rtl-analysis/releases)只有源码，尚无离线包附件。交付方按下节构建 **V1 离线安装包（Linux x86_64）** `rtl-analysis-agent-V1-linux-x86_64.tar.gz` 后再分发。页面自动提供的 **Source code (zip/tar.gz)** 只是源码归档；含有 `packaging/` 不代表含有运行时。完整包解压后必须有根目录 `bin/install`、`vendor/python/bin/python3`、`.local/downloads/`、`runtime-lock.json` 和 `SHA256SUMS`。缺少这些文件时先补齐完整包。

以下安装、工程配置与工具分析均可在完全无网络的机器执行，无需访问工具下载站点：

```sh
tar -xzf rtl-analysis-agent-V1-linux-x86_64.tar.gz
cd rtl-analysis-agent-V1
./bin/install
./bin/rtl --project projects/example doctor
```

`./bin/install` 会先校验包内 `SHA256SUMS`；缺件或校验失败时停止，无需另行下载校验文件。

用 VS Code 打开整个解压目录，让 Agent 读取 `AGENTS.md` 和 `src/rtl-pack/skills/rtl-query/SKILL.md`，分析 `projects/example`。**离线包使用 `./bin/rtl` 调用工具**。可直接问：“解释计数器的复位、使能和保持条件，引用源码行号。”模型客户端和本地/内网模型服务由现场提供。

自己的完整 RTL 源码放入 `projects/<工程名>/source/`，让 Agent 配置工程根目录的 `rtl-project.json`、`runtime-query-build.json` 并创建 `analysis/`。`src/workspace/projects/` 路径也支持。`AGENTS.md` 和 `quickstart.md` 会先按目录识别离线包，离线包以本页安装入口为准。

工具安装在 `.local/tools/`，原始安装文件在 `.local/downloads/`。重复安装会校验并复用工具，保留用户工程。`./bin/install` 强制离线；直接调用包内 `src/rtl-pack/scripts/install-tools` 也自动保持离线。缓存缺失或损坏会报告具体文件并停止，不尝试联网补齐；请从原始完整包恢复文件。源码分析期间只读，结果写入工程的 `analysis/`。安装不写系统目录，无需 sudo。

## 重新打包

V1 使用这一套 `packaging/`；完整压缩包另行分发，可作为 GitHub Release 附件上传，不提交 Git。提交源码后，使用本地已核验的缓存构建：

```sh
./packaging/build-offline --downloads .local/downloads \
  --python-cache /path/to/python-archive \
  --python-wheels /path/to/jinja-markupsafe-wheels \
  --output .derived/dist/V1
```

版本与 SHA256 固定在 `runtime-lock.json` 和源码工具清单中。包内保留 Python、Python wheel 和 slang/Verible 的许可证；工具来源、版本与下载摘要见 `manifest.json`，原始工具归档也随包保留。
