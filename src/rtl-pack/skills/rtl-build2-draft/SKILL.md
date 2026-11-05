---
name: rtl-build2-draft
description: 从已匹配的 Build1 candidate 生成有界、带证据的 RTL 语义草稿块，分阶段保存并在新会话核验后恢复；不发布知识。
metadata:
  status: candidate
  mode: build2-draft
  requires: public rtl CLI、READY Build1 receipt、task-local writable analysis/tasks
---

# Build2 草稿块

从匹配的 Build1 candidate 为用户指定 Project/范围生成带证据的小块草稿；先列 targets、直接 covers、依赖和未启动块。同块可共享 Facts/Source。产物始终是 `DRAFT_UNREVIEWED`；self_checked、文件齐全或 helper 成功不等于独立审核、READY、知识发布或 Manual 发布。

## 路径与读取约定

在目标 Workspace 根调用自己的 `./bin/rtl`、`./bin/workspace __domain-python`；`<Skill>` 为 `capability/skills/rtl-build2-draft`。外部 cwd 用实际 Workspace/Skill 绝对路径；Source Git 核验在 Project/source。task 位于 Project/analysis/tasks；所有路径按当前 cwd 解析，勿从 Workspace 根传裸 task 名或 Project-relative task。每次调用重新推导路径，不假定 shell 变量跨调用保留。`--model`/`RTL_CONTEXT_MODEL` 原样用 receipt 的 Project-relative candidate.path。

本入口可单次读取；下面 references 按标题取当步段落/子节，勿整读或提前加载后续格式。**每次合并展示≤8192 UTF-8字节（含标题/换行）**；单节也先核算，超限按完整段落/代码块分次读取。JSON 按字段/页投影，保留遗漏/截断提示及完整原件，不截字符串或静默丢字段。相关声明与完整构造不可省略，必要时分次读齐。工具参数、重复脚本与作者输出也会累积；这不是模型token硬上限。已读材料/脚本可复用，保存有效阶段后可按预算停止。

## 新任务

1. 读 rtl-knowledge-build 及 [Build1 身份](references/bounded-reading.md#build1-身份)，取得同 Project/Scope 的 READY candidate/receipt；PARTIAL、compile计数和匹配复用按该节核对。身份失配或 candidate 缺失即停，不为草稿重建whole design。
2. 分次读 [对象地图](references/bounded-reading.md#先看身份与一页地图再选-exact-对象)和 [区域v2计划](references/stages.md#区域增量-v2)。exact ID、真实参数和 map_anchor 来自公开原件；文件名不证明对象。旧v1任务用「计划先行」。写完整 input-plan.json 后 start：

```sh
(
project='projects/<name>'
task="$project/analysis/tasks/<task>"
start_exit=0
./bin/workspace __domain-python <Skill>/scripts/state.py start \
  --project "$project" --task "$task" \
  --receipt "$task/build1-status.json" \
  --plan "$task/input-plan.json" --budget-turns <剩余轮次> \
  > "$task/start.1.out" \
  2> "$task/start.1.err" || start_exit=$?
printf '%s\n' "$start_exit" > "$task/start.1.exit" || exit "$?"
exit "$start_exit"
)
```

整段为一次shell调用；括号限定exit只退出子shell。立即捕获真实码，stdout/stderr/.exit按尝试号保留；不得追加成功命令或用管道末码掩盖失败。返回0且生成skill.state才算start成功。非零先停，保留原计划/原件，按reason核实并修正输入后仅**一次**重试，将三个`.1.`改为`.2.`；仍失败即停，禁止第三次尝试。Build1、Query、save/revise/save-combination、resume同样留证；echo不代替.exit文件。

3. start成功后才读 [Query落盘/投影](references/bounded-reading.md#query-原件先落盘再读所需字段)，再读 stages [块内阶段公共字段](references/stages.md#块内阶段)与 [evidence](references/stages.md#evidence)，尽快保存有限公共输出、必要Source和缺口。
4. 到达draft/review才取对应子节：[draft](references/stages.md#draft)、[review](references/stages.md#review)。关键推导须留下原式、有效宽度/符号、必要中间值和赋值结果；时序claim逐更新过程核对前提、正常与覆盖更新。按review要求验证未约束的数据/控制输入反例，依据不足保持open。只用已有checks/note，不自研分析器或评分器。
5. 全部块review有效、本轮需要且预算允许时才读 [组合自检](references/stages.md#组合自检)。依赖不转移直接covers；任一直接owner仍open的目标仍open。未知/冲突与uncovered如实保留，不靠扩大范围或清空未知凑完成。

保存入口：`./bin/workspace __domain-python <Skill>/scripts/state.py save --task <task> --block <id> --stage evidence|draft|review --input <JSON> --budget-turns <剩余轮次>`。参数/引用和原子保存规则在块内阶段；预算耗尽可附`--budget-turns 0 --stop-reason <原因>`。修订已存阶段只用 [revise/history](references/stages.md#修订)，获准在冻结边界内追加才读 [extend-plan](references/stages.md#追加计划)。

## 恢复任务

读本入口后直接从持久task调用 `./bin/workspace __domain-python <Skill>/scripts/state.py resume --task <task>`，按上方约定保存原件/真实码；再读 [持久任务恢复](references/bounded-reading.md#持久任务恢复)核对身份、有效/失效阶段、组合与剩余范围。只加载待继续/修订子节，不先读所有创建示例；按同身份knowledge-build核验候选复用，不重做有效阶段。

next_action/RECOVERY不授予扩大范围或重置预算；Skill变更保留旧场，新版验证用新task，禁止改state/hash消除漂移。新会话必须由宿主实际建立，公开resume只证明持久恢复，不证明上下文已清空；普通逐阶段使用无需每轮新会话。

Source/config/build/candidate/Skill/receipt或引用漂移即停止受影响操作，保留原件和可核实阶段，按恢复节处理；搬迁不改身份。禁止改真实RTL、正式知识库、I/R/A/Profile或发布审批，禁止另调Solver补缺口。报告实际有效阶段、未覆盖项和停止原因，不能把作者自检写成内容已批准。
