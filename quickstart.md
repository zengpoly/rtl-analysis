# 知识构建预览版使用说明

## 0. 识别源码与离线包

- **完全离线包**：根目录有 `bin/install`、`runtime-lock.json`、`vendor/python/` 或 `SHA256SUMS` 时，先读根目录 README，执行 `./bin/install`；安装成功后使用 `./bin/rtl`，工程放在 `projects/`。安装只使用包内文件，缺件或校验失败即停止。
- **源码归档且机器完全离线**：Source code zip/tar 不含运行时和缓存。本次 GitHub 尚无离线包附件；请交付方按 `packaging/README.md` 构建并提供 `rtl-analysis-knowledge-preview-1-linux-x86_64.tar.gz`，不能用 V1 默认源码查询包替代。
- **源码目录**：需要 Linux x86_64、Python 3.12、Python 依赖和锁定工具。下面命令从仓库根执行。

允许联网的机器：

```sh
python3 -m venv .local/venv
. .local/venv/bin/activate
python3 -m pip install -r requirements.txt
./src/rtl-pack/scripts/install-tools --online
./src/rtl-pack/bin/rtl doctor
```

工具缓存已完整且核验通过时，用 `./src/rtl-pack/scripts/install-tools --offline`。Python 依赖也须已备齐；缺件时先补齐完整包，不在离线机器尝试下载。新的终端应重新激活虚拟环境。

## 1. 准备工程

内置 `projects/example` 可以直接试用。自己的完整 RTL 和构建文件放到 `projects/<name>/source/`，让 Agent 按实际文件配置工程根目录的 `rtl-project.json`、`runtime-query-build.json` 并创建 `analysis/`。不确定的 top、文件顺序、include、宏或参数先确认。

```sh
./src/rtl-pack/bin/rtl --project projects/example doctor
```

源码始终只读；结构知识、Manual、草稿和运行记录放到工程 `analysis/`。模型客户端和服务由使用者提供。

## 2. Build1：结构知识与 Manual

先让 Agent 读取 `src/rtl-pack/skills/rtl-knowledge-build/SKILL.md`。明确工程和范围后调用一次：

```sh
./src/rtl-pack/bin/rtl model knowledge-build   --project projects/example --scope whole-design
```

实际工程使用 `--scope actual-root --target-path <exact-elaborated-path>` 选择真实实例根。不要从模块短名猜实例路径，也不要将源码切片伪装成完整可编译设计。

只有机器回执 `READY` 才能称候选与 Manual 都已就绪；`PARTIAL` 要报告候选和 Manual 的各自状态；`BLOCKED` 立即按原因停止。匹配候选的重复命令应复用候选，不重复编译。默认产物为 `analysis/model/rtl-knowledge-build1.sqlite` 与 `analysis/manual-v6-build1/`；准确路径、Build 身份与 SHA 以回执为准。结构 Manual 的语义保持 `NOT_BUILT`。

## 3. Build2：有界语义草稿

给 Agent 明确范围和任务目录，例如：

```text
请阅读 AGENTS.md、quickstart.md 和
src/rtl-pack/skills/rtl-build2-draft/SKILL.md。
工程是 projects/example，任务目录由你在该工程 analysis/tasks/ 下新建。
使用匹配的 Build1 回执与公开结构地图，为计数器的复位、使能、保持和溢出行为
制定有界计划，按 evidence、draft、review 阶段保存语义草稿，保留未知和源码证据。
按 Skill 的源码仓库入口执行，不修改 source 或正式知识，不把草稿或自检写成已审核发布。
```

Build2 Skill 的 portable Workspace 示例通过以下映射适配源码目录；不需要自行新建包装脚本：

| Workspace 示例 | 当前源码仓库 |
|---|---|
| `./bin/rtl` 或 `rtl` | `./src/rtl-pack/bin/rtl` |
| `./bin/workspace __domain-python` | 已激活虚拟环境中的 `python3` |
| `<Skill>` | `src/rtl-pack/skills/rtl-build2-draft` |
| `capability/config/` | `src/rtl-pack/config/` |

可用 `python3 src/rtl-pack/skills/rtl-build2-draft/scripts/state.py --help` 查看公开状态 helper。计划、确切对象和身份必须来自实际回执与公开地图；具体 start / save / revise / resume 参数按 Skill 逐步读取，不猜 JSON 或修改 state/hash。

恢复时先调用该 helper 的 `resume --task <实际task路径>`，保留输出和真实退出码，再按返回的状态处理有效阶段、失效依赖和剩余范围。跨会话恢复不代表跨进程保留 Compilation。

## 4. 查看草稿与知识查询

需要草稿页面时，读取 `rtl-knowledge-manual` Skill 后调用：

```sh
./src/rtl-pack/bin/rtl model manual-preview   --project projects/example   --task projects/example/analysis/tasks/<实际task>   --output preview
```

task 必须实际存在并含有效 draft；输出相对该 task 且必须是新目录。页面保留 `DRAFT_UNREVIEWED / NOT PUBLISHED`，不写入正式 candidate。

普通源码问题可直接使用 `rtl-query` Skill，无需先建库。显式提供已发布候选、当前 Build SHA 与可靠 exact target 后，公共知识查询入口为：

```sh
./src/rtl-pack/bin/rtl model query   --project projects/<name>   --model <Project-relative-candidate.sqlite>   --view module-knowledge --target-name <exact-target>   --build-sha256 <current-build-sha256>
```

`--model` 采用已验证回执的工程相对路径。需要特定语义 obligation 或实际实例时，按 Skill 与候选合同提供精确参数。Build1-only 候选不等于完整语义知识，Build2 task 草稿不自动进入该查询入口。缺失、过期、部分覆盖或未知对象时保留缺口并回到源码 / Runtime。

`tools/agent-runner/` 还提供显式 Knowledge ON/OFF 的控制器入口，详见其 README；此处不要求 Docker 或额外模型调用。
