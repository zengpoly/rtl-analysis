# 领域包与分析 Workspace

`src/rtl-pack/` 保存能力包研发正本；开发机仅 main checkout 的 `<repo>/src/workspace/` 是 canonical runtime Workspace，对应容器的 `/workspace/`。linked Git worktree 不另建自己的 runtime Workspace；`src/workspace/` 整体被 Git 忽略，但其中的 Project 是持久资产。
所有用户工程均使用`projects/<name>/{source/,rtl-project.json,analysis/}`。OpenC910只是研发样例；
领域包不携带它、不设置默认用户工程。局域网工程可以使用不同名称、源码、revision和entry。
0.8.0 的默认事实分析入口是 `rtl-query`：一个任务内用 PySlang compile/elaborate 一次，再查询带源码证据的 native facts；不要求 Project Model。0.9.0 增加正交的 `rtl-question-factory`。0.10.0 将 `rtl-query` 升至v1.2并按build dependence选择Source/Runtime/Mixed路线；0.11.0升级至v1.3，加入exact-label/truncation-stop、SV literal消费与固定回答/耗时合同。v1.5 增加可选、身份严格匹配且绑定独立审核制品的预构建模块知识门；v1.6 在同一 SQLite 与相同安全判定上增加默认 `answer` 视图和精确引用的 `audit` 按需展开，旧 `full` 视图保留兼容。v1.7 只把已发布的实例连接薄投影到 answer，并让充分知识关闭相应 Query obligation；v1.8 让 Runner 的同一次查询交付可读材料和 run-local structured 原件，后续整理按 result reference 回读而不重复查询；v1.9 把有 reliable target 的 Runner Knowledge-ON 首次路由交给 Controller 确定性 prefetch，Skill 只消费 route/material 并补明确缺口，`available` 仅保留显式历史模式。worker和四个primitive保持不变。0.4.0—0.7.0 的 MVP Build/Query 继续作为 legacy/precomputed-model 能力保留，不删除其 SQLite 或原子 Skill。下面给出标准装配和运行步骤。

## 生成运行快照

在研发仓根执行。依赖现有 `jq`、`tar`、`find`、`sha256sum` 和基本文件命令。
`src/rtl-pack/config/domain-pack.json` 是公开载荷白名单，路径相对能力包根；`skills` 中包含 `rtl-query`、`rtl-knowledge-build`、`rtl-build2-draft`、`rtl-knowledge-manual`、`rtl-question-factory` 和两个带内嵌 atoms 的 legacy 父目录。其他研发 Skills 和用户数据不进入快照。变更白名单或 Skill 后重新生成，不直接编辑生成物。
公开 CLI 的必需模块也须逐项列在 `files`；当前 v0.2 Query 和公共 Source 路径映射分别需要 `lib/rtl_query_v02.py`、`lib/rtl_source_path.py`。`tests/test_domain_pack_assembly.py` 从此白名单生成全新快照并启动 CLI，避免研发源码目录掩盖载荷遗漏。

```bash
set -euo pipefail
rtl_pack=src/rtl-pack
mkdir -p .local/domain-snapshots
rtl_snapshot="$(mktemp -d .local/domain-snapshots/snapshot.XXXXXX)"
jq -r '.files[], (.skills[] | "skills/" + .)' "$rtl_pack/config/domain-pack.json" > "$rtl_snapshot/payload-paths.txt"
tar -C "$rtl_pack" -cf - -T "$rtl_snapshot/payload-paths.txt" | tar -xf - -C "$rtl_snapshot"
(
  cd "$rtl_snapshot"
  find . -type f ! -name receipt.sha256 -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > receipt.sha256
  sha256sum --check --quiet receipt.sha256
)
chmod -R a-w "$rtl_snapshot"
test ! -e .local/domain || test -L .local/domain
ln -sfnT "domain-snapshots/$(basename "$rtl_snapshot")" .local/domain
```

每次产生一份独立只读快照；`.local/domain`仅是当前版本指针，内容身份取自`receipt.sha256`的SHA-256。
保留旧快照供进行中的任务核验与回退，不自动清理；已撤下的Skill不会残留在新快照中。
receipt覆盖白名单、工具锁、运行文件和模板；上游工具安装本体另由既有`.local/tools/receipt.json`核验。
文件权限是防误写措施，不是针对同一OS用户的安全隔离。

在仓库根核验当前快照：

```bash
(cd .local/domain && sha256sum --check --quiet receipt.sha256)
sha256sum .local/domain/receipt.sha256
cmp src/rtl-pack/config/domain-pack.json .local/domain/config/domain-pack.json
cmp src/rtl-pack/config/toolchain.json .local/domain/config/toolchain.json
```

正本与快照是否同步，还应对比白名单内 Skill/文件内容，例如 `diff -qr src/rtl-pack/skills/rtl-query .local/domain/skills/rtl-query`；`.rtl-skills` 顶层应包含默认 `rtl-query` 及两个 legacy 入口，只有 legacy 父目录含 `internal-skills/`。
receipt验证快照自身完整性，不能仅凭它通过就声称正本没有新改动。

## 配置本地视图

仍在 main checkout 的研发仓根执行。以下路径是专用生成入口；已有同名普通目录/文件时先核对归属，不能覆盖用户文件。不要在 linked worktree 下运行这组命令来创建第二份 runtime Workspace。

```bash
mkdir -p src/workspace/projects src/workspace/.agents src/workspace/.claude
ln -sfnT ../../.local/domain src/workspace/.rtl-domain
ln -sfnT .rtl-domain/skills src/workspace/.rtl-skills
ln -sfnT ../.rtl-skills src/workspace/.agents/skills
ln -sfnT ../.rtl-skills src/workspace/.claude/skills
ln -sfnT .rtl-domain/templates/workspace/AGENTS.md src/workspace/AGENTS.md
ln -sfnT .rtl-domain/templates/workspace/AGENTS.md src/workspace/CLAUDE.md
```

本地仅配置当前Workspace，不安装用户全局Skills。Codex从项目`.agents/skills`读取技能；
链接存在、宿主实际发现和实际调用应分别验证。[官方技能发现说明](https://learn.chatgpt.com/docs/build-skills)
Claude入口按现有`.claude/skills`约定配置，其真实发现/调用与Codex分开记证据。
从Workspace根启动宿主；不要在嵌套的`source/`仓根启动后假定一定继承外层入口。
容器由IAS提供自身工具根、PATH和临时目录配置，不复制本地绝对路径或用户数据。

## 运行分析

IAS容器打开`/workspace`时，镜像已经配置工具根、PATH、临时目录和缓存。不要运行研发工作树的相对路径
初始化，也不要覆盖现有`RTL_ANALYSIS_TOOL_ROOT`；直接验证并分析：

```bash
test "$RTL_ANALYSIS_TOOL_ROOT" = /opt/rtl-analysis-agent/.local/tools
command -v rtl
rtl --project projects/<真实项目目录> doctor
rtl --project projects/<真实项目目录> query --build <build-identity.json>
rtl model query --project projects/<真实项目目录> --view summary
```

只有在本研发仓的 `src/workspace/` 目录本地演练时，才设置以下仓内相对环境：

```bash
rtl_workspace="$(pwd -P)"
export PATH="$(realpath .rtl-domain)/bin:$PATH"
export RTL_ANALYSIS_TOOL_ROOT="$(realpath ../../.local/tools)"
export RTL_ANALYSIS_TMPDIR="$rtl_workspace/.rtl-tmp"
export YOWASP_CACHE_DIR="$(realpath ../../.local/cache/yowasp)"
export PYTHONDONTWRITEBYTECODE=1
mkdir -p "$RTL_ANALYSIS_TMPDIR"
command -v rtl
rtl --project projects/demo doctor
rtl --project projects/demo query --build projects/demo/runtime-query-build.json
rtl model query --project projects/demo --view summary
```

`demo`应替换为用户实际选择的工程。领域快照只支持显式`--project`；省略选择器的旧研发基准命令仍在仓根运行。
上述环境也应传给从该Shell启动的Agent；不同终端需重新配置。工具未安装时在研发仓根使用原有
`src/rtl-pack/scripts/install-tools` 和 `src/rtl-pack/bin/rtl doctor`，不往只读快照内安装。

## 接入或替换工程

用标准目录/复制命令和模板建立新工程；source必须是真实目录，不能是绕过合同的软链接。
接入用户已有源码前确认目标和版本，保留未提交内容；不得通过清理改动满足Gate。

```text
projects/<name>/
  source/                    该工程的只读分析输入
  rtl-project.json            从能力包 templates/rtl-project.json 复制并填写
  runtime-query-build.json    该工程需要 Runtime build identity 时使用
  analysis/
    model/                    当前 Knowledge candidate / Project Model
    tasks/<task>/             该 RTL Project 的 build/query/semantic 实验、草稿与证据
      artifacts/manual/       该任务尚未发布的 Manual projection
    manual/releases/<id>/     已审核、确定性重渲染的静态 Manual release
    manual/current            仅正式发布后存在，指向 releases/<id>
```

未发布的 AI Draft、validation 或 prototype Manual 只留在所属 `analysis/tasks/<task>/artifacts/manual/`，可通过 Workspace `web/previews/` 的相对链接查看；不得放进 `analysis/manual/releases/` 或 `web/manuals/`。已有 OpenC910 历史 task/Manual 原位保留，新任务按上述布局执行。

同事看的正式 Manual 只能从冻结的 candidate、template 和 semantic/build profile 确定性重新渲染至 `analysis/manual/releases/<release-id>/`，审核后才切换 `current`；不能直接把 task Manual 移作 release。release manifest 至少应追溯 Project、Source revision、candidate SHA、scope ID、template/page type version、semantic/build profile identity 和 rendered artifact identity；已存在的 manifest 字段复用，缺项在未来发布任务补齐，不在此建立发布系统。`web/manuals/<project>` 只可指向已发布的 `analysis/manual/current`，是静态服务别名，不是第二 Knowledge Store。

修改模板的`name`、`entry`和`revision`即可选择其他工程。`entry`相对source；使用Git固定输入时填写
完整revision并保持工作树干净，普通目录使用`revision: null`且在任务中记录内容指纹。
固定`source`、`analysis`字段及`project-v1`schema不变。不支持的filelist/宏/Top等由适用的现成上游CLI和
原生配置处理，不能向该合同塞入新字段。替换工程不需要修改Skill或CLI源码。

OpenC910 研发示例使用 `src/workspace/projects/openc910/`，其中 source、项目合同和 analysis 是一个整体。
本地 Gate 和 `src/rtl-pack/config/targets.json` 直接使用该 Project 路径，不再创建 Workspace 根级 OpenC910 兼容链接。新工程不需要修改能力包或设置根级源码链接。

### 用可信 tar 搬入或搬出项目

项目搬运不再是公开 RTL 知识 Skill；需要时按 Workspace 管理说明组合已有的 `tar`、`sha256sum`、`jq` 和 `rtl doctor`：

- 新源码tar解压到新项目的`source/`，创建既有`project-v1`合同和空`analysis/`；
- 完整项目导出时整体打包`projects/<name>/`，源码、合同与昂贵中间结果一起保留；
- 同事在自己的私有Workspace解压成新目录，保留包内项目身份和证据，再核对有效性。

这不是新的包格式、文件服务或安全解包器，只接受本人或可信同事的`.tar.gz/.tgz`，不覆盖已有项目。
完整项目仍以 source、合同、model 和 tasks 日志整体搬运；搬运不是知识生成或查询，不增加第三个领域 Skill。

## 构建与查询日志

使用 legacy Build/Query 时，把 `.rtl-domain/templates/task/` 复制到新的 `analysis/tasks/<task-id>/` 并创建 `artifacts/`，不覆盖已有任务。Runtime `rtl-query` 默认直接保持一个 task-owned worker，并按宿主运行记录保存必要 request/result；它不创建 model 或跨任务状态。快照文件只读，复制后只对新任务目录恢复用户写权限。

- `request.json` 固定 task ID、build/query 操作、Project、current 相对路径、原问题或 spec 输入及状态；
- `executions.jsonl` 每次实际调用追加命令、开始/结束、退出码、结果和 artifact 引用；失败与重试保留独立记录；
- `artifacts/` 保存实际 `result-v1` JSON、必要 stderr 和输入 hash，不复制源码或秘密；
- `report.md` 摘要模型身份、assertion/evidence、coverage、未知和下一步。

tasks 是可审计运行日志，不参与查询求值。跨会话恢复时重新核对 Project、current model ID/content hash、spec/参数和 artifact；日志身份不匹配时重跑相关 build/query。长任务没有独立后台状态承诺，应拆成有界 producer 后由 build Skill 写入同一 SQLite。

## 发布与验证范围

对应主表见`rtl-analysis-scenarios-and-tools.md`；不要把框架模板或现有CLI冒烟写成A/B解释效果已验证。
本地框架的可复核入口和证据位于研发Run；本地PASS不代表容器持久化、Claude或双用户Gate已经通过。

## 可选图示渲染（原生程序适配）

报告优先保留Markdown的Mermaid代码块。需要离线可读图片时使用固定的上游Mermaid CLI，
版本及依赖闭包见 `src/rtl-pack/config/rendering/package.json` 和同目录 `package-lock.json`（MIT；依赖各自许可证随包提供）。
不修改该工具或增加RTL CLI子命令。图源是正本，SVG/PNG及引用它们的Markdown是派生物。

在用户可写的渲染目录复制上述两份文件后运行`PUPPETEER_SKIP_DOWNLOAD=true npm ci --no-audit --no-fund`。
需已有兼容Chrome/Chromium；用上游Puppeteer JSON配置的`executablePath`指定当前环境浏览器，
环境选项由部署方提供，不把本机路径写进共享配置。浏览器版本及实际配置在任务调用记录登记。
无浏览器或离线依赖不完整时保留待渲染状态，不能声称图片已经可用。

使用该目录原生入口`node_modules/.bin/mmdc -p <browser-config.json> -i <report.md> -o <report.rendered.md> -e svg -j 1`。
需要PNG时另选输出文件名及`-e png`；不要覆盖报告正本。保留源码证据编号、生成命令与工具版本，
检查图中文字、箭头、条件、裁切与报告相对链接。原生DOT图仍保留DOT，只有实际采用Graphviz时才锁定其版本并记录渲染证据。
