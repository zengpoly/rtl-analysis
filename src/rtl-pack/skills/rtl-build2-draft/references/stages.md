# 计划、阶段与组合输入格式

## 区域增量 v2

本节仅供选对象后的计划/start；Query、块内阶段和组合在到达当步后再读。新区域任务用 `schema: "rtl-analysis-agent/build2-plan-v2"`，其余 v1 必填字段、ID 正则 `[a-z][a-z0-9-]{0,63}`、直接 `covers` 和无环 `depends_on` 不变。父计划 `object`/`boundary` 描述**预定的有限区域集合**；`block.object` 必须是公开地图中的 exact `entity_id`，`configuration`/`boundary` 说明实际适用配置及局部边界。同配置同 candidate Scope 内追加工作，不扩大编译闭包或 Store Scope。可处理但尚未安排的项列为无 owner 的 target，`resume.targets[id]` 显示 `pending`；`uncovered` 只描述确实在本任务范围外的项目。

用[有界读取示例](bounded-reading.md)从 Project 根、receipt 的 `manual.path` 与自选 exact ID 确定性生成引用，不手拼路径。显示用的 manifest-relative 路径与保存用的 Project-relative ref 不同。

每个 v2 block 必填 `map_anchor: {"manifest": <ref>, "detail": <ref>}`。两个 ref 均为下文的 `{base,path,sha256}`，相对 `project` 指 Project 根、相对 `task` 指当前 task 根，路径必须留在该根内。manifest 是 Build1 公开 `rtl-analysis-agent/manual-v6-build1-manifest-v1` JSON；其 `candidate_sha256`、`build_sha256`、`scope_id` 必须匹配当前 receipt/identity。detail 是其 `structured_detail_map[block.object]` 指向的实际文件，schema 为 `rtl-analysis-agent/manual-v6-structured-detail-v1`，`scope_id` 与 `object.entity_id` 匹配，`page_kind` 为 `ACTUAL_INSTANCE` 或 `MODULE_DEFINITION`，`semantic` 必须为 `NOT_BUILT`。map 中的路径相对 manifest 所在目录；detail ref 解析出的实际文件须是同一文件。哈希、路径、对象或身份不符即拒绝；详情变化只使实际引用它的块失效，共享 manifest 变化影响所有引用它的块。机械验证不证明描述文字或 RTL 语义正确；以 frozen Build 身份、detail 实际参数与有限 public context/Source 为依据，`parameters_truncated` 或参数缺失时保留 unknown。

下面是**无答案结构示例**。把 `__...__` 全部换成当前 disposable Project 的真实 exact ID、相对路径与文件 SHA；示例本身不能直接作为 RTL 结论。A 两项可共享同一 evidence/draft，B 是尚未分配的下一知识项。manifest/detail 可位于 Project 或 task，只要路径基准和 SHA 匹配。

<!-- example:plan-v2 -->
```json
{
  "schema": "rtl-analysis-agent/build2-plan-v2", "maturity": "DRAFT_UNREVIEWED",
  "target": "bounded neighborhood questions", "object": "__EXACT_A__ and adjacent __EXACT_B__",
  "configuration": "frozen project configuration", "boundary": "A and adjacent B only",
  "uncovered": [],
  "targets": [
    {"id": "a-entry", "question": "entry condition in A"},
    {"id": "a-exit", "question": "exit condition in A"},
    {"id": "b-link", "question": "relationship to adjacent B"}
  ],
  "blocks": [{
    "id": "a-main", "goal": "A entry and exit", "object": "__EXACT_A__",
    "configuration": "frozen actual parameters", "boundary": "A only",
    "covers": ["a-entry", "a-exit"], "depends_on": [],
    "map_anchor": {
      "manifest": {"base": "project", "path": "__MANIFEST_RELATIVE_TO_PROJECT__", "sha256": "__MANIFEST_SHA256__"},
      "detail": {"base": "project", "path": "__DETAIL_A_RELATIVE_TO_PROJECT__", "sha256": "__DETAIL_A_SHA256__"}
    }
  }]
}
```

`resume.targets` 每项为 `{status, owners}`。没有直接 owner 为 `pending`；owner 阶段不全/无效或任一 owner 对该项 `open` 为 `open`；所有直接 owner 的有效 review 都自检时才为 `self_checked`。依赖不转移覆盖权。任何 pending/open、未知/范围外项或未通过的六项组合检查都使 `completion=open`，即使某块 review 已保存也一样。`self_checked` 只代表作者自检，maturity 始终 `DRAFT_UNREVIEWED`。

## 追加计划

仅在当前授权边界内追加时读此节。追加时写**完整新计划** `complete-new-plan.json`，旧 targets/blocks 的内容和顺序、顶层 schema/maturity/target/object/configuration/boundary 原样保留，只在数组尾追加至少一个 target 或 block。旧 pending target 可由新块承接，同对象可添新 target/block，跨区关系块可依赖旧块；依赖须存在且无环。`uncovered` 只能保留原项并追加，不能在本轮把它静默搬入 targets。调用 `./bin/workspace __domain-python <Skill>/scripts/state.py extend-plan --task <task> --plan <task>/complete-new-plan.json --reason '<非空理由>'`。旧阶段/refs 有效且身份匹配才可追加；旧组合因计划变更 stale 不单独阻止追加。新计划在 `plans/<sha256>.json` 不可变保存，`skill.state.plan_path`/`plan_sha256` 原子切换为唯一当前计划；无 `plan_path` 的旧状态读 `plan.json`。提交前中断时按旧 state 恢复，未引用的新计划可留在目录中。旧组合原件、阶段、修订记录和 extension 前后 SHA/理由保留。不得手工改指针、覆盖计划或改 hash 伪造有效性。v1/legacy 按旧合同继续运行，`extend-plan` 不自动迁移 v1，也不能通过更换 Skill 文件 hash 迁移身份。

## 计划先行

`input-plan.json` 是 Agent 自行编写的有界计划；v1 `start --plan` 将其正规化复制到 `plan.json` 并绑定 SHA。字段：`schema: "rtl-analysis-agent/build2-plan-v1"`、`maturity: "DRAFT_UNREVIEWED"`、非空 `target/object/configuration/boundary`、`uncovered` 字符串数组、非空 `targets` 与 `blocks` 数组。`targets` 每项是 `{ "id": "安全短名", "question": "完整目标中的待答问题" }`；每块是 `{ "id": "安全短名", "goal": "局部问题", "object": "精确对象", "configuration": "适用配置", "boundary": "分析边界", "covers": ["目标 id"], "depends_on": ["上游块 id"] }`。目标与块 ID 均须完全匹配 `[a-z][a-z0-9-]{0,63}`：小写字母开头，之后最多 63 个小写字母、数字或连字符；下划线、大写和空格均不合法。全部目标和未启动块都应列出；目前确实无法覆盖的问题写 `uncovered`，不要默默省略。`covers` 只列该块**直接负责**的问题；依赖不会自动获得上游的覆盖权。依赖须无环；块 ID 不随执行顺序改变。

下面是无特定 RTL 答案的最小计划。把对象、配置、问题和边界替换为当前 Project 的实际值，再保存为 task 的 `input-plan.json`：

<!-- example:plan -->
```json
{
  "schema": "rtl-analysis-agent/build2-plan-v1",
  "maturity": "DRAFT_UNREVIEWED",
  "target": "bounded unit behavior",
  "object": "unit",
  "configuration": "frozen project configuration",
  "boundary": "unit only",
  "uncovered": [],
  "targets": [
    {"id": "entry", "question": "entry condition"},
    {"id": "clear", "question": "clear condition"}
  ],
  "blocks": [
    {"id": "entry-path", "goal": "entry condition", "object": "unit", "configuration": "frozen",
     "boundary": "entry path", "covers": ["entry"], "depends_on": []},
    {"id": "clear-path", "goal": "clear condition", "object": "unit", "configuration": "frozen",
     "boundary": "clear path", "covers": ["clear"], "depends_on": ["entry-path"]}
  ]
}
```

`resume.status=ready` 只表示身份、已记录阶段和依赖可复用；它不等于整体完成。`completion=open` 表示计划块、组合、冲突、未知或未覆盖范围仍未闭合。仅当全部块有效、每个目标有直接 owner、无计划外未覆盖/未知/冲突、六项组合检查均 pass 时才报告 `completion=draft_self_checked`；它仍不是独立语义审核或发布。已创建块的 review 全部保存也不会清空整个目标。`plan.not_started_blocks`、`plan.uncovered_targets`、`remaining_scope` 保留下一会话所需的范围。

## 块内阶段

`scripts/state.py` 只处理 task 文件/身份，不解析 RTL、不调用模型、不写 candidate。所有路径参数均按当前 cwd 解析；先核对 Workspace 根，用 `project=projects/<name>`、`task="$project/analysis/tasks/<task>"` 推导，或用实际绝对路径。裸 task 名与 Project-relative `analysis/tasks/<task>` 不能直接从 Workspace 根使用；不要反复手抄长绝对路径。失败读 stderr 的 `reason`，确认具体输入再修正。

`save` 先写并 fsync 阶段产物，再原子替换并 fsync `skill.state`；state 记录产物 SHA、冻结 Source/config/build/candidate/Skill/receipt/plan 身份、目标/剩余范围、依赖、未知、预算和下一动作。预算耗尽时可在当前有效阶段的 `save` 命令附 `--budget-turns 0 --stop-reason <原因>`。任何失败都不应被报告为完成。引用必须是现存 Project/task 文件的 SHA；缺失引用不可编造。state 是索引，公共输出、Source 与草稿文件才是原件。

三个输入都是 JSON object，均含 `stage`（`evidence` / `draft` / `review`）、`maturity: "DRAFT_UNREVIEWED"`、非空 `references`、`unknowns` 和 `next_action`。`references` 中每项为 `{ "base": "project"|"task", "path": "相对路径", "sha256": "原件 SHA-256" }`；保存器核对路径、实际文件与哈希。不要把已填答案放进交给独立试验模型的空白输入。

阶段 JSON 可含额外结构化字段，但不得包含 `PUBLISHED`、`REVIEWED` 或伪造的 READY 判定。保存命令原子复制输入到 `blocks/<id>/<stage>.json`；后续只通过该正式产物读取。计划块第一次保存 evidence 时，所有上游必须有有效 review，helper 固定当时的 review SHA；上游尚未完成则等待。上游后来修订时，下游依赖变 stale：先完成上游，再对下游已记录的 evidence 执行 `revise --reason`，helper 才重新绑定当前上游 review SHA，随后重做本块 draft/review；不可直接 `save` 或手改 state。

按当前阶段读下面一个子节；三个 JSON 都是同一 `entry-path` 块的空白结构。替换路径/真实 SHA 与观察，不把占位文字当事实，也不重复抄入上下阶段全文。

### evidence

非空 `observations` 写 public Fact ID、有效配置、Source locator 或明确 unknown；至少引用保存于 task 的公共 Query 原件。观察须注明原件中的实际字段路径和值，分别记录缺字段、空值、未读取、截断；宿主工具事件与领域 JSON 不混用，不能用不存在的字段描述空返回。保留 `truncated`、unsupported 与声明/构造缺口；无需此时加载 draft/review 格式。

<!-- example:evidence -->
```json
{
  "stage": "evidence", "maturity": "DRAFT_UNREVIEWED",
  "references": [{"base": "task", "path": "public-context.json", "sha256": "__CONTEXT_SHA256__"}],
  "observations": ["replace with cited public Fact and Source observation"],
  "unknowns": ["replace with unresolved evidence gap"],
  "next_action": "draft this block from verified evidence"
}
```

### draft

非空 `claims` 写必要条件、优先级/例外、Fact/Source 依据及推断边界，引用本块 evidence 与必要 Source；无法闭合写 unknown。开始写 claim 时即按下节位宽/条件要求核对，避免先写无条件结论。

<!-- example:draft -->
```json
{
  "stage": "draft", "maturity": "DRAFT_UNREVIEWED",
  "references": [{"base": "task", "path": "blocks/entry-path/evidence.json", "sha256": "__EVIDENCE_SHA256__"}],
  "claims": ["replace with bounded claim, conditions, priority and citation"],
  "unknowns": ["replace with unresolved semantic gap"],
  "next_action": "self-check this block"
}
```

### review

涉及位向量、编码、常量或透传的关键表达式，在现有 checks/note 留下短推导：原式（含切片/常量尺寸）、适用参数、操作数有效宽度/符号、必要中间值、赋值扩展/截断后的结果。按实际声明和表达式上下文核算，不用指令格式宽度代替信号宽度，不把切片或无尺寸常量默认为完整数学整数。现有公开证据无法确认时保持 open，不猜值或编写新分析器。claim、conditions、derivation 必须约束同一输入集合；具体输入结论不能写成条件不足的全称结论。对固定数值/输出声明，尝试保持已列条件、改变未约束相关位的反例；对关键时序 claim，逐个真正更新状态的过程写清前提、正常更新和覆盖更新，再保持已声明条件改变未约束控制输入/同时条件。不能只换数据值就称无反例；不同状态过程分别核对，未经证明的上游排除条件不得默认为真。引用须定位实际声明与完整构造。反例成立就修正结论或补足必要条件，不能仅凭原输入正确通过自检。

非空 `checks`，逐项核对配置、Fact/Source 引用、结论/未知、依赖和剩余目标；引用本块 `draft.json`。每条 claim 先与完整构造逐字对照再定 `self_checked`/`open`：枚举各输入条件单独有效与**多个条件同时有效**的组合，核对同一过程内实际执行的分支优先级与赋值顺序：同次执行中，无延时的阻塞赋值写同一变量/重叠位时，后执行的写入覆盖先前值；非阻塞赋值在执行时求值、在调度的 NBA 时点更新，须核对更新时点与顺序。跨过程、多驱动、时钟/复位及混合赋值不能按源码先后判胜负。结合实际配置与完整时序条件核对结论；引用 SHA 匹配、身份一致或结论复述都不构成行为核对，条件不明或未对照完整构造的项保持 `open`。这只是作者自检，maturity 保持 `DRAFT_UNREVIEWED`。

v2 review 除公共字段外必填 `target_checks`；keys **恰等于**本块 `covers`，状态只接受 `self_checked`/`open`，每项 `note` 非空，仍须引用真实 evidence/draft。下面片段对应「区域增量 v2」的 a-main 块；加入通用 review object 时须按当前块的 covers 替换 keys：

<!-- example:target-checks -->
```json
"target_checks": {
  "a-entry": {"status": "self_checked", "note": "cite actual public Fact and Source; explain applicability"},
  "a-exit": {"status": "open", "note": "source condition still missing"}
}
```

<!-- example:review -->
```json
{
  "stage": "review", "maturity": "DRAFT_UNREVIEWED",
  "references": [{"base": "task", "path": "blocks/entry-path/draft.json", "sha256": "__DRAFT_SHA256__"}],
  "checks": ["replace with configuration, evidence, dependency and gap check"],
  "unknowns": ["replace with remaining issue"],
  "next_action": "continue the planned blocks"
}
```

### 修订

已记录阶段需要修正时，用 `revise --task <task> --block <id> --stage evidence|draft|review --input <修订 JSON> --reason '<具体原因>'`。helper 将被修订阶段及所有已记录的后续阶段各自的原字节、原记录存入 `blocks/<id>/history/` 与 revision，再清除后续阶段索引；缺失/损坏原件的 observed/recorded 差异会明示。旧依赖和组合随之失效。先重做受影响阶段及下游；下游 evidence 修订时才重绑新上游 review SHA，保留无关有效块。组合本身更新用 `save-combination ... --reason '<具体原因>'`；旧组合归档。不要删旧证据、改哈希或直接编辑 state 来解锁。

修订 evidence 时逐项标明旧观察哪些保留、变更或仍未知，核对实际JSON字段和完整构造，不能只修行号。

已记录阶段只能用 `revise --reason` 修正；被修订阶段及其**已记录的后续阶段**先按各自 SHA 归档原字节和原记录，然后移除后续索引。每个 revision 记录理由、影响阶段、旧依赖及重绑时的新依赖。旧原件缺失则保留原记录并标记 archive 为 null；字节损坏则归档实际观察字节并同时记录 observed/recorded SHA 差异，不能称旧记录原字节已保全。归档失败时拒绝修订，旧索引与固定路径原件保持不变。旧依赖块与组合失效；无关块保持可用。

## 组合自检

`input-combination.json` 为 object，含 `schema: "rtl-analysis-agent/build2-combination-v1"`、`maturity: "DRAFT_UNREVIEWED"`、`blocks`（每个计划块 ID → 当前 review SHA）、`coverage`（**每个**计划目标 ID → 直接负责它的块 ID 数组）、`references`（Project/task 原件 SHA 引用）、`conflicts`、`uncovered`、`unknowns` 字符串数组及非空 `next_action`。`coverage[target]` 的每个块都必须在 `plan.blocks[block].covers` **直接列出**该 target；上游依赖关系不授予下游覆盖权。某问题尚无可确认 owner 时填空数组并在 `uncovered` 和 `coverage_unknowns` 标 open，不要借用依赖块代填。`checks` 必须有六项：`object_configuration`、`interface`、`priority_defaults`、`timing`、`duplicate_conflict`、`coverage_unknowns`；每项为 `{ "status": "pass"|"open"|"conflict", "note": "具体自检依据或缺口" }`。`pass` 仅表示作者自检有依据，不代表独立语义审核。

`blocks` 的 SHA 从当前 `skill.state.blocks.<id>.stages.review.sha256` 取得，`references` 引用对应 review 文件及必要 public 输出。以下示例中的 `__...SHA256__` 是占位符，须替换为当前文件的真实 SHA；示例不包含语义答案：

<!-- example:combination -->
```json
{
  "schema": "rtl-analysis-agent/build2-combination-v1",
  "maturity": "DRAFT_UNREVIEWED",
  "blocks": {"entry-path": "__ENTRY_REVIEW_SHA256__", "clear-path": "__CLEAR_REVIEW_SHA256__"},
  "coverage": {"entry": ["entry-path"], "clear": ["clear-path"]},
  "checks": {
    "object_configuration": {"status": "open", "note": "verify actual parameters"},
    "interface": {"status": "open", "note": "verify boundary"},
    "priority_defaults": {"status": "open", "note": "verify priority"},
    "timing": {"status": "open", "note": "verify cycle applicability"},
    "duplicate_conflict": {"status": "open", "note": "compare block claims"},
    "coverage_unknowns": {"status": "open", "note": "review unknowns"}
  },
  "conflicts": [], "uncovered": [], "unknowns": ["semantic self-check pending"],
  "references": [
    {"base": "task", "path": "blocks/entry-path/review.json", "sha256": "__ENTRY_REVIEW_SHA256__"},
    {"base": "task", "path": "blocks/clear-path/review.json", "sha256": "__CLEAR_REVIEW_SHA256__"}
  ],
  "next_action": "verify each check against public evidence"
}
```

块结论相互冲突时同时保留原件，在 `conflicts` 与相应检查中写明；接口、默认/优先级、时序、覆盖或 Source 证据不足时写 `open` 和 `unknowns/uncovered`。`priority_defaults` 与 `duplicate_conflict` 的 `pass` 须基于多条件同时有效组合与「块内阶段」所述赋值/时序适用范围的逐字核对，不以块间引用一致代替行为一致。helper 只校验结构、SHA、计划目标和块版本；不能从文本自动判断 RTL 结论真假。`save-combination` 可保存未闭合组合，报告 `completion=open`。只有各块有效、六项自检标为 pass、没有冲突/未知/未覆盖时，报告 `draft_self_checked`；仍始终是 `DRAFT_UNREVIEWED`。
