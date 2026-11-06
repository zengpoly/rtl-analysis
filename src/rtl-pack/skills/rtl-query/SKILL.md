---
name: rtl-query
description: 当用户需要在冻结 SystemVerilog/Verilog build identity 下分析 elaboration、instance hierarchy、port binding、symbol/type、signal writer、时序过程或跨模块功能流时使用；以最少 task-local PySlang 查询关闭确定性事实，再读最小完整源码并综合语义。
metadata:
  status: active
  version: "1.14"
  requires: 有效 rtl-project.json、runtime-query-build-v1 build identity、锁定 PySlang 11.0.0 与 rtl query CLI
---

# Runtime RTL Query

本 Skill 是 Source + Runtime 路线的默认入口。正式主流程只有一条：解析请求 → 接收 Query Entry 已确定的 knowledge route/material 或 OFF/no-target 事实 → 关闭已覆盖事项 → 只补真实缺口 → 组织答案与证据 → 结束。不得为了“使用工具”启动 runtime，也不得强迫工具回答功能意图、长因果链或完整时序语义。显式 legacy/diagnostic `available` 是后述单独例外，不属于正式主流程。

## 0. Query Entry 交付的预构建模块知识

Agent Runner 的正式 Knowledge-ON 入口由 Controller 拥有首次路由：调用方已给出可靠 exact target 时，Controller 在 Agent 开始前确定性执行一次现有 knowledge adapter，并在 `BEGIN/END REVIEWED PREBUILT KNOWLEDGE` 边界中交付 route、reviewed material 或短回退理由。看到该边界即表示 Knowledge Gate 已完成；不得重复执行相同 `rtlq knowledge`。需要重排已返回字段时，只按交付的 run-local `result_ref` 执行 `rtlq result <result_ref> --format json|readable`，该回读不再访问 candidate。

Controller 声明知识 OFF 时，或 Knowledge-ON 但没有可靠 target 而记录 `target-unresolved`时，跳过知识发现、candidate 打开和旧 result 读取，直接使用后续共同 Source/Tool 路线。不从任意自然语言猜 target，也不在 Source 调查定位模块后自动二次进入知识门。公共命令仍保留 `knowledge-disabled` 短路作为误调用保护。

Agent Runner 只在显式 legacy/diagnostic `available` delivery 中允许 Agent 主动做一次 `rtlq knowledge <module> --format readable`，用于历史实验复现；它不是正式 ON 默认。Runner 内没有 `./bin/rtl`。**Portable Workspace** 保留已验证的 `./bin/rtl` 入口和 candidate 发布流程；已明确发布 candidate 时使用 `./bin/rtl model query --project <project-dir> --model <project-relative-candidate> --view module-knowledge --target-name <module> --build-sha256 <current-build-sha256>`，未提供经核验路径和 Build 身份时不搜索猜测。Portable 只同步“复用已覆盖事项、只补明确缺口”的消费原则，不强行套用 Runner 的交付命令。

Portable v0.2 Q2 在同一个公共入口附加一个或多个精确 `--obligation module-q2.*@2`；已批准的组合候选也可请求精确 `fifo.*@1`，Stage1-only 或未批准候选不能由此产生语义答案。有多个实际实例时还须提供 `--actual-id <exact-id>`；需要问单一实例的定义归属时用 `--request-kind deterministic-instance-of`，不附语义 obligation。不得从自然语言臆造 obligation 或 actual ID。v0.2 返回 `coverage.requested/ready/partial/missing_obligations`、逐项 gaps、受审 typed fields、`source_evidence_ids` 与 workspace-relative evidence span。`PARTIAL` 即使已发布且有正文，也只能是 `CONTEXT`；只关闭该正文实际支持的子命题，保留整个 obligation 的未验证部分。`DIRECT` 只有当前、完整、适用且足够的确定性 Fact，或以后另行受审达到 READY 的语义覆盖。知识 OFF 在模型路径解析前短路。v0.2 目前只支持 `answer`；旧 `full/audit` 与四章节参数属于 v0.1 路径，不能用它们读取 v0.2。返回的数量是 queried/returned，不是 used/cited；最终引用须由实际答案记录。

v0.2 的 `review.status=VERIFIED` 与 `review.evidence_id/input_sha256/artifact_sha256` 是经发布审计绑定的独立审核状态；`support_kind=source-only` 表示只由 Source 支持，`scope-fact-and-source` 表示 Compiler Fact 只证明 scope/实例关系，均不代表有行为 content Fact，不能据此说“待审核”。`coverage.partial_obligations/gaps` 另说明覆盖未闭合；回答须分别表述“该 claim 已审核可引用”“所请求的完整行为尚未验证”以及具体缺口。Stage1 `DIRECT` 的 `knowledge=[]` 仅表示没有 AI semantic claim，仍须明确引用返回的 `support_facts` 和 Source Evidence，不能说没有预构建知识。若 Source 子问题确在 packet 外，才针对该子问题读取最小完整 construct。

知识 adapter 只接受 exact module entity、当前 build identity、请求章节、`direct` 子实例身份集合、request kind 和附加要求，并返回：

- `DIRECT`：每个请求章节的 coverage 绑定具体 knowledge IDs/digest、必需 claims、独立审核制品 hash、Fact support 与可引用 source evidence；Build/对象/direct 子实例**身份集合**、request kind 和附加要求完全匹配。从本次结果组织答案并停止，不重复查询、重读同一源证据或重做 live Tool 分析。
- `CONTEXT`：实际返回了有用的已核验语义/Fact/定位材料，但有明确缺章节、缺子实例、request kind 或附加要求缺口。保留并复用已满足事项，只为每个 missing obligation 选择最小 Source/Runtime 动作；若返回记录为零且没有辅助 Fact/定位材料，它必须是 `BYPASS`，不是空 `CONTEXT`。
- `BYPASS`：关闭、未覆盖、对象不存在、Build 失效或无法可靠定位。立即走普通 Source/Runtime；失效内容不得支持结论。

四个章节 ready 不是 DIRECT 的充分条件。新增仲裁优先级、完整时序、性能保证、递归后代或其它 candidate 未声明的要求必须 CONTEXT/BYPASS。不得用数量相等代替 direct 子实例身份集合一致，也不得把读取知识自动记成模型正确消费。

Runner 的 readable 结果把已发布知识正文、对象/实例/定义映射、发布 Fact 中的直接实例连接、关键源码锚点、限制和 route/coverage 放在一起；structured 原件保留完整 ID 和精确回查入口。二者来自同一次响应。连接表只证明已发布的 immediate binding expression；`actual_truncated`、缺行或不完整表达式不能被说成位等价或完整功能。明确的审计请求或证据冲突才使用 structured 原件中的 exact `audit_ref` 发起 `--view audit --audit-ref <exact-ref>`；不得猜测、截断或模糊匹配 ID，也不得默认展开 `full` 或重审全部内容。

把用户要求拆成已由交付材料支持的子结论和仍待查的具体子问题。`DIRECT` 且某项由适用的 verified knowledge 或其 deterministic support Fact、有效源码锚点充分提供时，将该项标 `CLOSED_KNOWLEDGE`，直接组织回答；这不是编译器对 AI 语义的证明。`CONTEXT` 先逐项区分 claim 已证明的子命题与未闭合的子问题；同一 Source span 内可以同时有已审角色和未审状态条件，不能因区间重叠跳过后者。对未闭合项写 `缺什么 → 已有输出是否足够 → 定位什么构造/依赖 → 怎样算补齐`，先复用本次 Query、Runtime、配置和 Source 输出。不足时精确定位符号，再读含默认赋值、条件优先级、有效参数与必要跨模块绑定的最小完整 construct；已有区间不含必要条件时允许补读并说明子问题。定位搜索不附带跨多个无关构造的大范围上下文。真实因果链所需内容必须补齐，不设任意行数上限。`BYPASS` 不关闭；冲突、失效、截断或新增要求只重新打开受影响事项。

已交付的 exact instance identity、module/instance name、数字结构事实和 binding expression 必须从 readable/structured truth **逐字复制**，不缩写、重命名或根据模式改写。最终回答前对用户明确要求的 exact identity 集合做逐项对照；发现 missing/altered 时报告而不猜测修补。本 Skill 不授权用第二个模型或隐式改写器“纠正”答案。

仅在存在适用的 deterministic obligation 时启动 runtime；同一冻结 build identity 在任务内复用一个 query worker，compile/elaborate 一次，任务结束关闭。不得跨任务保留 compiler state。通过 `python` 查询直接调用 PySlang 原生 API；`scope / inspect / bindings / uses` 保留为便利操作。

## 1. 先选路线，不先启动 runtime

知识门未关闭的事项再选以下一路；选择依据是事实是否依赖 active build，而不是问题来自哪个项目或样例：

- `SOURCE_FAST_PATH`：答案由一个局部 executable construct 唯一确定，且不随 hierarchy、define/include、parameter override、generate 或 instance-specific type/range 改变。典型包括显式 assignment/concat/width、reset polarity/value、enable/hold、完整单个 process/case 的局部语义。读取最小但完整 construct，闭合后回答，不建立 runtime session。
- `RUNTIME_QUERY_PATH`：答案依赖 frozen build 的 active hierarchy、effective parameter、active generate、instance-specific type/range、direct port binding 或必须先消歧的 writer/location。使用 exact Tool fact，Source 只补 Tool 明确不支持的语义。
- `MIXED_PATH`：用户要求语义/时序/功能结论，同时存在会改变该结论的 build-dependent deterministic obligation。先用 Tool 关闭必要身份或边界，再读取对应完整 Source；不能把“可能有帮助”当成 obligation。

边界不清时先问：如果不做 elaboration，局部 Source 是否已经能得到唯一答案？答案为是则使用 `SOURCE_FAST_PATH`。不要为局部事实寻找一个 elaborated instance 来证明 module Source 本身。

在 Portable Workspace 中只使用公共入口 `./bin/rtl`；在 Agent Runner 中只使用 `rtlq`。不要为辨认环境试执行另一环境的命令。不得探测系统 Python、PATH、PySlang安装目录、capability内部脚本或tool root，不使用 shell-specific `VAR=value cmd`。Portable 公共入口失败时最多运行一次 `./bin/doctor` 并报告 `RUNTIME_UNAVAILABLE`；不得自行修环境或创建启动脚本。

## 2. 只为未完成事项写最小 Task Card

`SOURCE_FAST_PATH` 只需一行写明 required fact、完整 Source construct 与 stop；不得为了模板制造 hierarchy obligation。`RUNTIME_QUERY_PATH` 或 `MIXED_PATH` 才按需写：

```text
Scenario: transaction/event/configuration/lane；未知项显式为 unknown
Start: 已知对象、事件或待消歧 locator
Stop: 最后一个必须回答的 observable boundary
Assumptions: clock/reset/handshake/runtime-evidence 条件
Completeness: lookup / selected path / every boundary / bounded branches
Required claims: 用户要求的可裁决结论
Deterministic obligations: hierarchy/type/parameter/binding/writer/location 等
Semantic obligations: intent/temporal/causal/root-cause/flow explanation 等
Query budget: 每个 query 必须关闭一个 deterministic obligation，或为它取得新的定位证据；越少越好
```

复杂请求才为每项 obligation 维护 `PENDING / CLOSED_KNOWLEDGE / CLOSED_TOOL / CLOSED_SOURCE / UNRESOLVED`。简单问题不强制填写长 ledger。`CLOSED_KNOWLEDGE` 要记录对应已发布 claim 或 deterministic Fact、适用身份与具体 source anchor；route 返回、记录数或 candidate 挂载本身不能关闭事项。Scenario 或 stop point 会改变 required claims 时，先澄清，不能静默选择一条路径。

只填写适用于当前问题的字段；单点 lookup 用一行 obligation 即可。transaction、handshake、valid/payload/sideband 只在目标协议确实包含这些概念时跟踪。

## 3. Tool applicability gate

优先查询的事实：exact hierarchy/instance、effective parameter、active generate、instance-specific symbol/type/range、direct port binding、必须消歧的 writer/reference 和 source location。

主要交给局部 Source + LLM 的内容：完整功能意图、长因果说明、完整 temporal semantics、root-cause reasoning、端到端 functional flow、effective guard/priority/next-state。

Semantic 问题只选少量会改变答案、且能闭合边界的 build-dependent deterministic obligations。若没有这样的 obligation，保持 `SOURCE_FAST_PATH`，不为“使用工具”而查询。

## 4. 建立唯一 runtime session

build identity 必须冻结 files/filelists、top、file order、single-unit、include、define、路径环境和可选 top parameter。Portable Workspace 启动：

```sh
./bin/rtl --project <project-dir> query --build <project-dir>/<runtime-query-build.json>
```

进程先输出一行 `status=ready`，其 `operations` 包含 `python`；随后 stdin/stdout 每行一个 JSON object。保持进程到任务结束，EOF 后释放状态。以下 discovery 查询取得顶层身份：

```json
{"id":"tops","op":"python","code":"result = [x.hierarchicalPath for x in root.topInstances]"}
```

Agent Runner 已由宿主建立会话；把查询代码写入任务临时文件，调用 `rtlq python <query.py>`，或以 `rtlq python -` 从 stdin 读取代码。这两个入口都在已有 worker 中执行；不在外部 Python 中重建 Compilation。

读取结果后，在同一会话发送下一条 query；可继续使用上次保存的原生对象或返回的 exact hierarchical path。不得在同一预写 batch 中引用尚未返回的 identity；只有 identity 已知且彼此独立的查询才可预先批量发送。

便利操作的 `limit` 默认 50，合法范围 `1..100`。Python 查询自己限定对象范围和返回字段；查询计数不等于代码内部原生 API 调用次数。compile failure 后只做有界 build/source 检查；不补 stub、删文件或猜宏。

此命令从任何调用 CWD 都以 Workspace root 解析相对路径，并使用 bundled runtime。不要改用裸 `rtl`、`python` 或 `capability/` 内部入口。

## 5. 选择原生 API，执行 Python 查询

每次只关闭当前缺少的事实：问题 → 选择 API、生成代码 → Agent 在 Runtime 执行 → 读取结果 → 继续查询或回答。读取类型、表达式运算符、条件分支、赋值或原生分析信息时，直接使用 `pyslang` 与现有 `compilation / root`；不限于四种便利操作。

- 把所需的 JSON 值赋给 `result`；原生对象可保存在变量中供后续查询使用，不能直接放入 JSON。`result` 每次重置为 null，其他变量和函数保留到任务结束。
- 已知名称先用原生 `find / lookupName` 或既有 `scope` 精确定位；复用返回的对象，避免扫描整个设计。需要方法名、类型属性或签名时，在当前会话用 `dir / help` 查询，不猜 API。
- 用 `evidence(node)` 或 `source_evidence(range)` 附上源码证据；`get_analysis()` 按需取得共享 AnalysisManager。`ok / completeness=executed` 只表示代码执行成功，不能替代范围完整性与证据判断。代码中自行计算的推断仍须单独说明。
- 查询保持 RTL 源码和编译配置不变，读取现有设计对象；不用查询代码重建或改写 Compilation。返回 `query_error` 时读 `error_type / line / message` 后按有依据的一次修正规则处理；失败不会回滚已经执行的 Python 语句。`stale_input` 后停止使用整个会话。

首次使用 Python 查询前，必须读 [查询分类与使用指南](references/python-query.md)，按“问题 → 对象 → API → 返回信息”选择类别。指南覆盖设计层级、定位、端口、参数/generate、类型、表达式、赋值、条件、事件、函数、驱动、控制流、断言、常量、源码、语法及诊断，并提供可执行示例。后续遇到新类别或不确定 API 用法时回查对应部分。宿主负责 worker 的执行环境和进程时限。

### 便利操作的固定返回范围

| `op` | 最小输入 | 能证明什么 | 不能证明什么 |
|---|---|---|---|
| `scope` | 已知 `path`，可选 literal `name/kind/limit` | direct members、active generate、child instance、exact handle、definition/source | 功能重要性、递归 project map |
| `inspect` | returned exact `path` 或 `handle` | native kind/type、effective parameter、procedure kind/timing source | semantic summary、effective guard、next-state/reset/priority proof |
| `bindings` | returned exact child `instance`/handle，可选 exact `port` | immediate formal/direction/internal endpoint、actual/internal expression 与 evidence | transitive connectivity、bit equivalence、完整 operator/conversion tree、protocol meaning |
| `uses` | returned exact `path`/handle；`mode=drivers` 或 `procedure_reads` | native drivers/ranges、procedure kind、event controls/read set、analysis diagnostics | blocking/NBA operator、general fanout、dependency cone、interprocedural effect、temporal proof |

表中的限制仅指这四种固定返回，不限制 `python` 访问相应原生 API。保留原始状态：`ok / query_error / ambiguous / not_found / unsupported / partial / compile_failed / stale_input`。`not_found` 只表示当前 build/scope 未返回；`partial` 或 `truncated` 绝不是完整空集。

## 6. 永远不发明 elaborated path

合法 query identity 只能来自：

1. 用户或 build identity 给出的 anchor，且 runtime 已成功解析；
2. `status=ready` 或 query result 返回的 exact path/handle；
3. 在已知 scope 中通过原生 API 或 `scope` 便利操作按名称/类型有界发现后返回的唯一 identity。

源码中的相似名称只是 locator，不是 elaborated identity。不得拼接 `parent.child` 猜路径，也不得从 ambiguous candidates 任选一个继续递归。

无 exact symbol 时，对当前未解析 locator 做有界 literal source search 取得候选 spelling；获得新定位证据后再在已知 scope 内用原生 API 或 `scope` 消歧，不重复没有新增信息的搜索。优先级为：exact returned identity → local scope discovery → one evidence-backed parent correction → minimal Source fallback。若 runtime 没返回 exact identity，停止路径递归。

## 7. 每项 obligation 的最小查询与重试

对一个 deterministic obligation：

1. 发一个 primary query；
2. `ok` 且结果完整：消费结果并关闭 obligation；
3. 参数/API 使用错误，或能由已返回 candidate、handle、parent、filter 修正的 `query_error/ambiguous/partial/not_found`，共只允许一次有依据的 corrected retry；
4. 没有修正依据、`unsupported`，或 corrected retry 后仍未闭合：立即读最小完整 Source 或标 `UNRESOLVED`。

不得连续猜路径，不得以不同 primitive 对同一失败身份盲试。一次 primary + 一次有证据的 corrected retry 是硬预算；相同 obligation 的第二次结果没有新 evidence 时终止 query 路线。该预算只约束失败查询的修正；成功取得新定位证据后的必要 discovery 或下一跳不是重试，但仍须对应 obligation。

## 8. Narrow before broad

顺序固定为 exact entity → local scope → immediate parent/child boundary → 必要时更宽 discovery。默认不 dump top 或大 scope。

用户问题或 Source 已给出 exact label / child / generate block 名称时，必须 `literal name filter first`；不能先枚举整个 scope 再从输出中找它。filter 是 literal exact match，不把 module type、instance basename 和 hierarchical path 混为同一字段。

如果 broad scope `truncated`，不要提高 limit 重跑；立即改用 literal `name/kind` 收窄。不得调用 `get_terminal_output`、滚动 UI 或复制完整日志来补取同一个宽结果；这些动作没有关闭新的 obligation。UI 截断也不是创建 parser script、使用无关 UI 自动化或扩大 scope 的理由。若无法说明 query 将关闭哪个 obligation 或取得什么新的定位证据，就不执行该 query。

## 9. 通用调查策略

### Elaboration / configuration

从 frozen build identity 和已解析 instance 开始，分别核对 definition default、override/effective parameter、active generate/index、instance-specific type/range 和 immediate binding。当前配置下的可达性或常量断言必须沿 effective parameter 与 active binding 追到驱动，不能由 definition default、局部 `if` 或注释直接推出。题设输入与该固定配置冲突时，先回答当前 build 的事实，再把局部条件分支明确标为反事实；不能将其当成此 build 实际会等待/发生的行为。不要在脑内执行 generate。比较配置需要独立 compilation identity；单会话不能证明 counterfactual hardware。

### Every-boundary binding chain

先把 start、stop 及题目要求的每个 boundary 变成 ledger 项。每跳只用 returned identity 查询一次 immediate binding，并记录 parent、child、formal、actual、internal endpoint、direction、slice/concat/tie-off 和 evidence。local continuation 使用 native driver 或完整 assignment。遇到分支必须按题目 completeness 显式展开或标 unresolved；不能用 hierarchy shortest path 冒充 connectivity，也不能折叠 wrapper。

### Sequential / temporal state

已知局部 register/process 且完整 Source 能唯一给出 reset、enable、hold、blocking/NBA 或 branch priority 时，使用 `SOURCE_FAST_PATH`。需要 frozen build 消歧时，用 Python 查询原生 driver、process、条件与赋值对象，或用 `uses` 定位，再补齐尚未覆盖的完整 Source。目标存在 transaction 时按协议需要跟踪 valid、payload 和 sideband；不要构造微型 simulator。

### Cross-boundary functional flow

先按用户问题列出必须闭合的 lifecycle stages 和身份连续性，而不是先枚举 module。request 与 response 分开追踪，用真实 tag/ID/entry/handshake 等机制连接；名称相似不构成链。每个 requested stage 至少有一个具体边界和 source anchor。原生查询返回当前对象、连接、表达式与分析事实；功能解释与异常分支由模型结合必要 Source 综合。

## 10. Evidence 复用与冲突纪律

Tool 返回 exact entity、relation、source span 和 configuration identity，且状态为完整、evidence 有效、无冲突的 `ok` 时，默认直接作为该 deterministic fact 的 evidence；不要再次打开同一 source 复核。已审核知识和其发布的 deterministic support Fact 若已关闭要求，同样直接复用，不再执行后续通用 Source 调查章节。只为未闭合 obligation、证据缺失/冲突、复杂 expression 或 semantic obligation 读取必要 Source。`stale_input` 时立即停止消费该 session 的全部结果并报告输入漂移；`compile_failed` 或其它无法形成有效 evidence 的状态只作 limitation，不能冒充事实。

每个 material claim 标成 `prebuilt-semantic / prebuilt-fact / tool-supported / source-derived / inference / unresolved`。公共 Query evidence 的 `path` 是 Project-relative 路径；外部 Source 明示 `path_root=external-source` 且 `path` 为实际绝对路径。`locator` 始终是 Source-root-relative 原始身份，不直接当 Project 链接。引用使用已校验的 `path:line-range`，不能只写 basename；没有确定可点击跳转目标时输出该真实文本，不拼接占位 URL。注释和名称不是 executable fact；二者与 active elaboration 或完整 executable construct 冲突时，以后者为准并说明冲突。不得为 compiler-inserted conversion 制造 source line。

## 11. SystemVerilog numeric literal 消费纪律

Tool 或 Source 返回的 numeric `raw literal` 必须原样保留，再决定是否给出人类数值。sized based literal 按 `<size>'[s]<base><digits>` 解释：base 只可能是 `'b / 'o / 'd / 'h`，不能把 digits 的文本外观直接当十进制。转换前同时核对 width、`signedness`、前导符号以及 `x / z / ?` unknown digits；含 unknown、超出可靠归一范围或signed语义不明确时，只报告raw literal和限制，不猜十进制。

若同时展示raw和归一数值，两者必须做一致性检查；冲突时该claim保持`UNRESOLVED`，不得宣称complete。这个规则同样适用于parameter、enum、range、mask和binding expression，不依赖具体工程或信号名。

## 12. Source fast path / fallback 必须最小但完整

Fast path 先记录 required fact、要读取的 construct 和 stop；fallback 另记录未闭合 obligation 与 tool status。只读关闭该项所需的最小完整 assignment、process、instance declaration 或 include/override chain；不能只截掉 `else` 的几行，也不能重做已由 tool 关闭的事实。

链路问题只扩展当前 frontier；时序问题读取完整 relevant process；语义问题读取覆盖 start→stop 所需的最少相邻 constructs。Source 仍无法闭合时报告 unknown，不用大范围源码叙事填空。

## 13. Stop condition

每次查询或 Source fallback 后更新 ledger。只有以下条件同时成立才可宣称 complete：

- 所有 required obligations 是 `CLOSED_KNOWLEDGE`、`CLOSED_TOOL` 或 `CLOSED_SOURCE`；
- start、stop、要求的中间 boundary 和身份连续性均已核对；
- `partial/truncated/unsupported` 已转为明确 limitation，而不是静默忽略；
- material claims 都有 evidence，推断与事实分开。

若某项无法闭合，答案应局部完整并显式 `UNRESOLVED`。required obligations 已关闭后立即停止；`DIRECT` 内容充分时直接进入最终呈现，不执行后续通用调查。不得复查由同一 definition/effective configuration 已经覆盖的 sibling，不顺手扩展相邻模块、无关状态、下游行为或架构常识。

禁止自动 end-to-end flow、FSM extraction/reachability、general bit provenance、normalized next-state/hold/reset/priority、simulation/scheduling、CDC/RDC、STA、formal、configuration matrix 或 exhaustive change impact。不得调用 synthesis/netlist backend，也不得让工具直接生成自然语言最终答案。

## 14. Final answer contract

默认使用下列短结构，不把Task Card、搜索过程、终端日志或未被用户要求的相邻实现细节搬进最终回答：

```text
## 结论
<直接回答required claims；raw literal与归一值必须一致>

## 证据
- [prebuilt-semantic / prebuilt-fact] <已发布结论或事实> — <claim/fact ref；workspace-relative path:line-range>
- [tool-supported] <fact> — <exact identity/status/evidence>
- [source-derived] <fact> — <workspace-relative path:line-range>

## 限制/未知
<仅在存在 unresolved、partial、truncated、冲突或关键假设时显示>

## 耗时
- 总耗时：<authoritative controller wall time | UNAVAILABLE>
- Tool：<authoritative duration/calls | UNAVAILABLE>

## 本次执行摘要
- 对象/范围：<module/scope>
- 实际路径：<DIRECT / CONTEXT + gap fill / BYPASS + Source/Tool>
- 知识：<returned records 和 route；实际引用的 claim/fact refs 或 UNAVAILABLE>
- 补充：<明确 missing obligation → Source/Tool 动作；没有则写“无”>
- 停止/回退：<telemetry reason>
- 轨迹：<event reference | UNAVAILABLE>
```

`结论`与`证据`必选；`限制/未知`没有内容时省略；`耗时`必选。模块理解请求必须附上简短`本次执行摘要`，区分接口 `DIRECT` 与实际是否发生补充分析；`returned` 不得写成“已复用”，无事件证据不能事后替源码读取编造缺口。导航或最终引用无权威事件时写 `UNAVAILABLE`。只能消费 controller 或 telemetry 明确提供的时间/调用数，普通 Chat 没有权威记录时写 `UNAVAILABLE`。不得估算、根据 UI steps 反推、为取得耗时额外执行计时命令，也不得把“很快”当指标。`knowledge_query_ms` 只是知识接口内部耗时，不得冒充端到端 wall。
