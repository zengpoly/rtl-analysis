# Agent 使用入口

本分支提供 RTL 知识构建预览工作流，用中文回复。先判断目录：存在根目录 `bin/install`、`runtime-lock.json`、`vendor/python/` 或 `SHA256SUMS` 任一项时，按完全离线包处理，读取根目录 README，执行 `./bin/install`，使用 `./bin/rtl`。安装只使用包内文件；缺件、校验或加载失败时停止，向交付方取得完整知识构建预览包。保留用户已有工程和源码。

源码仓库先读 `quickstart.md`，使用 `./src/rtl-pack/bin/rtl`；安装器默认只使用本地缓存，允许联网时才显式使用 `--online`。Source code zip/tar 不含完整离线运行时。本次 GitHub 交付只有源码与标签。

## 使用约定

- 工程默认位于 `projects/<name>/`，`src/workspace/projects/<name>/` 同样支持。工程包含只读 `source/`、`rtl-project.json`、`runtime-query-build.json` 和可写 `analysis/`。
- 按实际构建文件确认 top、文件顺序、include、宏、参数与 single_unit；缺少关键配置时询问。
- 明确要求结构建库或恢复时，读取 `src/rtl-pack/skills/rtl-knowledge-build/SKILL.md`，按一次公共命令的机器回执处理 READY / PARTIAL / BLOCKED。
- 明确要求语义草稿或恢复时，读取 `src/rtl-pack/skills/rtl-build2-draft/SKILL.md`。先核对 Build1 身份与范围，按阶段保存，保留 unknown、未覆盖项和真实退出码。源码仓库命令映射以该 Skill 的“源码仓库入口”为准。
- 生成正式结构 Manual 或查看未审核草稿时，读取 `src/rtl-pack/skills/rtl-knowledge-manual/SKILL.md`。草稿预览留在 task；发布语义必须有适用的独立审核、批准和受控发布，不能把自检当批准。
- 回答 RTL 问题前读取 `src/rtl-pack/skills/rtl-query/SKILL.md`。已明确提供有效候选、Build 身份与 exact target 时，使用公共入口复用适用的已发布知识；没有可靠目标或候选时直接走源码 / Runtime，不猜路径或搜索任意旧知识库。按 DIRECT / CONTEXT / BYPASS 复用证据和补齐明确缺口。
- Build1 Fact 只证明结构；Build2 草稿保持 DRAFT_UNREVIEWED。答案引用源码或适用工具/知识证据，明确未知和分析范围。
- 用户 RTL、凭据、工具缓存和分析产物不提交 Git。

## 修改约定

先检查 Git 状态，保留已有改动。复用现有 RTL 工具和入口；不自行开发 parser、elaborator 或分析算法。修改后按范围运行测试、`git diff --check` 与 `./check`，只报告实际结果。提交说明用中文，推送到已配置的 GitHub 远程。
