# RTL 分析使用说明

开始前判断目录与网络条件：

- **完全离线包**：根目录存在 `bin/install`、`runtime-lock.json`、`vendor/python/` 或 `SHA256SUMS` 任一项时，先读根目录 `README.md`，执行 `./bin/install`，使用 `./bin/rtl`，工程放在 `projects/`。安装只使用随包文件，缺件或校验失败时停止并补齐完整包。
- **源码目录且机器完全离线**：GitHub 自动生成的 Source code zip/tar 不含 Python、工具缓存和 `SHA256SUMS`，不能直接离线安装。本次 GitHub 发布只有源码，尚无离线包附件；请向交付方取得按[打包说明](packaging/README.md)生成的 **V1 离线安装包（Linux x86_64）** `rtl-analysis-agent-V1-linux-x86_64.tar.gz`，转入离线机器后按包内 README 操作。`./bin/install` 会校验包内 `SHA256SUMS`。
- **已备齐本地缓存和 Python 环境的源码目录**：可使用下方源码流程。安装器默认只使用本地缓存；缺件即停止，提示取得完整离线包。
- **允许联网的源码目录**：先在 Python 3.12 虚拟环境中安装 `requirements.txt`，再显式执行 `./src/rtl-pack/scripts/install-tools --online` 获取并校验锁定工具。此步骤只适用于允许联网的机器。

以下源码流程需要 Linux x86_64、Python 3.12（`python3`）及对应 pip、`requirements.txt` 中的 Python 依赖、预先核验的 `.local/downloads/` 工具缓存，以及能读文件、执行命令的 Agent。源码归档本身不提供这些依赖；完全离线机器优先使用上面的完整离线包，无需系统 Python 或 pip。

允许联网的机器可先执行以下准备步骤；后续终端继续使用这个虚拟环境：

```sh
python3 -m venv .local/venv
. .local/venv/bin/activate
python3 -m pip install -r requirements.txt
./src/rtl-pack/scripts/install-tools --online
./src/rtl-pack/bin/rtl doctor
```

## 1. 用 AI 快速启动

**第一步：复制源码。** 用 VS Code 打开本仓库根目录，将待分析工程的完整源码和构建文件复制到下面的目录（`my-design` 换成实际工程名）：

```text
src/workspace/projects/my-design/source/
```

**第二步：让 Agent 安装和配置。** 直接发送以下内容：

```text
请用中文回复，阅读当前目录的 AGENTS.md、quickstart.md 和
src/rtl-pack/docs/mvp-toolchain.md，按现有安装脚本从本地缓存安装工具。
如果缺少 Python、缓存或文件校验失败，请停止并提示取得完整离线包。
待分析工程是 src/workspace/projects/my-design，源码已放在 source/。
请基于 src/rtl-pack/templates/rtl-project.json 和实际构建文件，
补齐工程配置 rtl-project.json、runtime-query-build.json，创建 analysis/，
确认顶层、文件顺序、include、宏和参数；不确定的设置先问我。
使用 ./src/rtl-pack/bin/rtl 检查工程，报告安装和配置是否成功。
然后读取 src/rtl-pack/skills/rtl-query/SKILL.md，后续使用源码和 Runtime
工具回答问题；这是源码仓库，CLI 始终使用 ./src/rtl-pack/bin/rtl。
不要修改 source，不构建或读取知识库。
```

**第三步：直接提问。** 安装和配置成功后，例如：

- “说明模块 xxx 的功能和接口。”
- “追踪信号 xxx 的赋值来源，列出源码位置。”
- “解释该模块的复位、使能和保持条件。”

分析结果可让 Agent 保存到该工程的 `analysis/` 目录。

## 2. 用命令安装

在仓库根目录执行（已由 Agent 安装成功时无需重复）：

```sh
./src/rtl-pack/scripts/install-tools --offline
./src/rtl-pack/bin/rtl doctor
```

工具安装到本仓库的 `.local/tools/`，无需 sudo。按上面的步骤放入源码、让 Agent 完成工程配置后，执行：

```sh
./src/rtl-pack/bin/rtl --project src/workspace/projects/my-design doctor
```

检查成功后即可提问；也可用以下命令检查配置中的入口文件：

```sh
./src/rtl-pack/bin/rtl --project src/workspace/projects/my-design syntax
./src/rtl-pack/bin/rtl --project src/workspace/projects/my-design lint
./src/rtl-pack/bin/rtl --project src/workspace/projects/my-design outline
```

完整离线包使用另一套目录：按包内 README 操作，安装命令为 `./bin/install`，待分析工程放在根目录 `projects/` 下。
