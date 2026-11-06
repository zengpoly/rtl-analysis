# PySlang 查询分类与 Python 使用指南

先按问题选择下面的查询类别，再从已定位对象调用对应 API。一个问题可组合多个类别，也可以一次查询就结束；分类不是固定执行顺序。接口依据锁定的 PySlang 11.0.0，调用时以当前对象的类型、`dir()` 和 `help()` 为准。

## 查询分类：问题 → 对象 → API → 信息

以下 `ast.*`、`analysis.*`、`syntax.*` 分别位于 `pyslang` 的同名模块。表中列出各类常用入口；完整原生库均可访问，不限于表内名称。

| 查询类别 | 问题 / 想取得的信息 | 从哪里开始 | 主要 API 与返回内容 |
|---|---|---|---|
| 1. 设计与层级 | 有哪些顶层、模块定义、包、子实例？ | `compilation`、`root`、已定位实例的 `body` | `root.topInstances`；`Compilation.getDefinitions / getPackages / getCompilationUnits`；遍历 `InstanceBodySymbol` 取得直接成员，筛选 `InstanceSymbol` 取得子实例。定义不等于实际实例。 |
| 2. 定位节点 | 问题里的名称对应哪个实例、信号或过程？ | 已确认的 scope | `Scope.find(name)` 找直接成员；`Scope.lookupName(path)` 解析名称；`Symbol.visit(callback)` 遍历目标子树。返回后核对 `kind / name / hierarchicalPath`；未找到先报告当前范围，不猜路径。 |
| 3. 实例与端口 | 实例属于哪个模块？端口实际接到什么？ | `InstanceSymbol` | `definition.name`、`body.portList`、`portConnections`；每条连接读 `port.name / direction` 和 `expression`。表达式可能为空、拼接、切片或转换；保留其类型和源码证据。 |
| 4. 参数与 generate | 当前实例的有效参数是什么？展开了哪个分支/循环？ | 实例 `body` | `InstanceBodySymbol.parameters` → `ParameterSymbol.value`；`GenerateBlockSymbol.isUninstantiated / conditionExpression / arrayIndex`；`GenerateBlockArraySymbol.entries`。使用当前 elaboration 的有效值，区别定义默认值与失活分支。 |
| 5. 类型与位宽 | 信号多少位、有无符号、数组/结构体是什么形状？ | `ValueSymbol.type` 或 `Expression.type` | `Type.bitWidth / isSigned / isFourState / canonicalType`；有固定范围时读 `fixedRange`；数组读 `arrayElementType`，关联数组可读 `associativeIndexType`。先判类型，再访问适用属性。 |
| 6. 表达式 | 运算符是什么？操作数引用谁？有没有切片/转换？ | 连接、赋值或条件的 `Expression` | `kind / type / sourceRange / getSymbolReference()`；按实际子类读 `BinaryExpression.op / left / right`、`UnaryExpression.op / operand`、`ConversionExpression.operand`、选择/拼接表达式的属性。`str(expr)` 不等于 RTL 原文。 |
| 7. 赋值语句 | 谁被赋值？左右值是什么？是阻塞还是非阻塞？ | continuous assignment 或过程体 | `ContinuousAssignSymbol.assignment`；过程体 `visit` 找 `AssignmentExpression`，读 `left / right / op / isCompound / isNonBlocking`。是否在某分支执行还需第 8、9 类信息。 |
| 8. 条件与循环 | if/case 的分支是什么？循环的条件和循环体是什么？ | 相关过程或函数的语句树 | `ConditionalStatement.conditions / ifTrue / ifFalse`；`CaseStatement.expr / items / defaultCase`；`ForLoopStatement.initializers / stopExpr / steps / body`。保留语句嵌套和执行顺序；仅列条件不能证明完整优先级。 |
| 9. 过程与事件 | always_ff / always_comb / initial 在什么事件下执行？ | `ProceduralBlockSymbol` | `procedureKind / body`；`TimedStatement.timing / stmt`；`EventListControl.events`；`SignalEventControl.edge / expr / iffCondition`。显式边沿、隐式敏感集合和动态事件要区分。 |
| 10. 函数与任务 | 调了哪个函数/任务？实参、形参、函数体是什么？ | scope 中的 `SubroutineSymbol` 或表达式树 | `SubroutineSymbol.arguments / body / returnType / subroutineKind`；`CallExpression.subroutine / arguments / isSystemCall`。系统调用先确认种类，不假定存在用户函数体。 |
| 11. 驱动与读取 | 信号由谁写？过程读了哪些符号/位范围？ | `get_analysis()`、已定位信号/实例体 | `AnalysisManager.getDrivers(symbol)` → `ValueDriver.kind / source / containingSymbol / bounds / sourceRange`；`getAnalyzedScope(body).procedures` → `AnalyzedProcedure.readSet / timingControls`；`ReadRange.symbol / bitRange`。它们是原生分析事实，不自动等于传递依赖或位等价。 |
| 12. 控制流遍历 | 按原生控制流访问赋值、条件、循环和合并点？ | 已定位过程/函数 | `analysis.FlowAnalysis(symbol)`；设置 `onAssignment / onConditionalBegin / onCaseBegin / onLoopBegin / onBranchMerge` 等回调，再 `run(stmt)`。回调签名先 `help()`；使用原生遍历记录所需信息，不自行实现时序求解器。 |
| 13. 断言与覆盖 | 有哪些断言、对应时钟、覆盖 bin 定义？ | 已定位的 scope / 符号 | `AnalysisManager.getAnalyzedAssertions(symbol)` → `AnalyzedAssertion.astNode / root / semanticLeadingClock`，必要时 `getClock(expr)`；语法/AST 中读取 `CoverageBinSymbol.values` 等定义。结构读取不能证明断言成立或仿真覆盖率。 |
| 14. 常量求值 | 当前配置下参数、常量表达式的值是什么？ | 参数或已绑定表达式 | `ParameterSymbol.value`；`Expression.constant`；需要上下文时 `Expression.eval(EvalContext)`；`ConstantValue.value` 与 `SVInt.bitWidth / hasUnknown`。保留原始位宽、符号及 x/z；运行期信号不能按常量猜值。 |
| 15. 源码与宏位置 | 事实来自哪行源码？宏/include 来自哪里？ | 节点或 `sourceRange` | 优先 `evidence(node)` / `source_evidence(range)`；原生 `SourceManager.getFileName / getLineNumber / getColumnNumber / getSourceText`，以及 `getOriginalLoc / getExpansionLoc / getMacroName / getIncludedFrom`。`getSourceText` 参数是 BufferID；避免返回整文件。 |
| 16. 语法树与 Token | 需要原始声明写法、Token 或未展开语法？ | `compilation.getSyntaxTrees()` 或节点 `syntax` | `SyntaxTree.root`、`SyntaxNode.visit / getFirstToken / getLastToken`、`Token.kind / valueText / location`。CST 描述源码语法，当前实例的有效配置仍看 elaborated AST。 |
| 17. 诊断与配置 | 为什么编译/分析失败？正在用什么配置？ | ready 回执、`compilation`、分析器 | `Compilation.getParseDiagnostics / getSemanticDiagnostics / getAllDiagnostics / options`；`AnalysisManager.getDiagnostics()`。配置身份用 ready 的 `compilation_identity`；诊断保留 code、位置与严重性，不能以无错误证明功能正确。 |

选好类别后，只查询回答所缺的信息。例如“属于哪个模块”用 2→3 即可；“端口怎么接”继续读取连接；“寄存器何时更新”通常需要 7＋8＋9，必要时用 11 消歧驱动。已经取得的事实、对象和证据直接复用。

## 调用入口

Portable 向正在运行的 `rtl … query --build …` 进程发送一行 JSON：

```json
{"id":"tops","op":"python","code":"result = [x.hierarchicalPath for x in root.topInstances]"}
```

Runner 使用 `rtlq python query.py`；`rtlq python -` 从 stdin 读取。文件路径只由客户端读取，代码在已有 worker 执行。每个请求计一次 query，同任务 compilation_count 保持 1。

## 可直接使用的对象

| 名称 | 用途 |
|---|---|
| `pyslang` | 完整原生库，包括 ast、syntax、analysis 等模块；没有 API 名称白名单 |
| `compilation` / `root` | 当前冻结配置的 Compilation / RootSymbol |
| `source_manager` | 源码文本、位置与宏映射 |
| `get_analysis()` | 延迟创建并复用原生 AnalysisManager |
| `evidence(node)` | 节点的源码位置、内容哈希、配置身份；可用 `include_excerpt=True` |
| `source_evidence(range)` | SourceRange / SourceLocation 的同类证据 |

`result` 接受 JSON 标量、列表、对象。将枚举转成 `str()`，将原生对象转成需要的属性；保留未知位的 SV 数值原文。打印和 `help()` 的内容在 `stdout`，错误打印在 `stderr`，不会混入 JSONL 协议。需要的方法未知时可用 `result = dir(type(node))` 或 `help(type(node))`。

## 按类别编写查询

下面示例针对一个已知小设计：顶层 `query_top`，源码里有子实例 `u_leaf`。名称来自示例输入，不是所有工程的默认路径。A 取得顶层，B 保留 `leaf`；之后 C—H 是按问题选用的独立查询，不要求全部执行。每个代码块可单独作为一次 `python` 请求，结果见响应的 `result`。

### A. 设计入口：取得顶层身份（类别 1）

```python
result = [{"path": x.hierarchicalPath, "module": x.definition.name}
          for x in root.topInstances]
```

返回顶层的准确路径与模块名。从实际返回中选择目标；不因为列出顶层就扫描所有后代。

### B. 定位实例：确认所属模块（类别 2、3）

```python
top = root.lookupName("query_top")  # 使用 A 已返回的路径
leaf = top.body.find("u_leaf")     # 使用题目/源码确认的名称
result = ({"found": False, "name": "u_leaf"} if leaf is None else
          {"found": True, "path": leaf.hierarchicalPath,
           "module": leaf.definition.name, "evidence": evidence(leaf)})
```

`found=false` 时先解决定位，不继续使用 `leaf`。找到后，这份输出已经能回答所属模块；后面的查询只补其它要求。

### C. 端口实际连接（类别 3、6）

```python
result = []
for connection in leaf.portConnections:
    expr = connection.expression
    result.append({"port": connection.port.name,
                   "direction": str(connection.port.direction),
                   "expression_kind": str(expr.kind) if expr is not None else None,
                   "actual": source_evidence(expr.sourceRange, include_excerpt=True)
                             if expr is not None else None})
```

返回本实例的直接端口连接；复杂连接按表达式类型再取操作数，不把一个连接直接解释成跨多层的数据路径。

### D. 有效参数、位宽与 generate（类别 4、5）

```python
signal = leaf.body.find("data_q")
typ = signal.type
result = {
    "parameters": [{"name": p.name, "value": str(p.value)}
                   for p in leaf.body.parameters],
    "signal": {"path": signal.hierarchicalPath, "type": str(typ),
               "bit_width": typ.bitWidth, "signed": typ.isSigned,
               "range": str(typ.fixedRange) if typ.hasFixedRange else None},
    "generate": [{"path": n.hierarchicalPath,
                  "uninstantiated": n.isUninstantiated}
                 for n in top.body if isinstance(n, pyslang.ast.GenerateBlockSymbol)]}
```

这是该 build 下的有效参数和类型。generate 数组使用 `GenerateBlockArraySymbol.entries` 逐项读取；保留索引及分支是否实例化。

### E. 赋值与条件：阻塞/NBA、if 分支（类别 7、8）

```python
statements = []
def collect_statements(node):
    if isinstance(node, pyslang.ast.AssignmentExpression):
        statements.append({"kind": "assignment", "nonblocking": node.isNonBlocking,
                           "left": source_evidence(node.left.sourceRange, include_excerpt=True),
                           "right": source_evidence(node.right.sourceRange, include_excerpt=True)})
    elif isinstance(node, pyslang.ast.ConditionalStatement):
        statements.append({"kind": "if", "conditions": len(node.conditions),
                           "has_else": node.ifFalse is not None,
                           "evidence": evidence(node, include_excerpt=True)})
leaf.body.visit(collect_statements)
result = statements
```

返回赋值方式与条件构造。若问题要求 reset/enable/hold 或分支优先级，继续沿语句树读取具体条件、分支和顺序，结合完整相关源码判断；扁平列表本身不是完整时序结论。

### F. 过程与时钟事件（类别 9）

```python
result = []
for block in leaf.body:
    if not isinstance(block, pyslang.ast.ProceduralBlockSymbol):
        continue
    row = {"procedure": str(block.procedureKind), "evidence": evidence(block)}
    if isinstance(block.body, pyslang.ast.TimedStatement):
        timing = block.body.timing
        events = timing.events if isinstance(timing, pyslang.ast.EventListControl) else [timing]
        row["events"] = [{"edge": str(e.edge),
                          "signal": source_evidence(e.expr.sourceRange, include_excerpt=True)}
                         for e in events if isinstance(e, pyslang.ast.SignalEventControl)]
    result.append(row)
```

本例读取最外层显式事件；过程内部还可嵌套事件等待，不能据此声称已列出全部事件。always_comb 的隐式读集合使用下一类原生分析。

### G. 驱动和过程读集合（类别 11）

```python
analysis = get_analysis()
signal = leaf.body.find("data_q")
result = {
    "drivers": [{"kind": str(d.kind), "source": str(d.source), "bounds": list(d.bounds),
                 "evidence": source_evidence(d.sourceRange)}
                for d in analysis.getDrivers(signal)],
    "procedures": [{"evidence": evidence(p.analyzedSymbol),
                    "reads": [{"path": r.symbol.hierarchicalPath, "bits": list(r.bitRange)}
                              for r in p.readSet]}
                   for p in analysis.getAnalyzedScope(leaf.body).procedures]}
```

共享 AnalysisManager 只初始化一次。读集合是原生分析记录的符号/位范围；时钟与复位事件另看 timingControls / sensitivityList，不能把“未出现在 readSet”解释为完全无关。

### H. 源码、语法与 API 发现（类别 15、16、17）

```python
result = {"source": evidence(leaf, include_excerpt=True),
          "syntax_kind": str(leaf.syntax.kind) if leaf.syntax is not None else None,
          "available_members": [x for x in dir(type(leaf)) if not x.startswith("_")]}
```

需要精确签名时，在同会话执行 `help(pyslang.ast.Scope.lookupName)`，查看 `stdout`。其它类别也遵循相同方式：先确认原生对象类型，再读表中字段；先定位相关子树，再 `visit()`，避免默认输出整个 AST。原生接口的来源和用法可查 [Slang v11.0 官方 Python 文档](https://github.com/MikePopoloski/slang/blob/v11.0/pyslang/docs/pages/index.rst)；安装目录中 `pyslang/*.pyi` 提供当前绑定的类、成员和签名。

变量、函数和原生对象在同任务内保留；每次请求前只重置 `result`。失败前已执行的赋值也可能保留，纠错时重新初始化受影响的临时列表。关闭会话后全部释放。

## 完整性与执行边界

- 原生对象访问成功不代表回答完整。结合问题检查访问范围、过滤条件、证据与状态；推断与实际读取的原生属性分开标注。
- 请求总大小上限 65536 字节；响应上限 512000 字节。过大结果返回 `partial / truncated`；收窄查询，不把被省略的数据当空集。
- stdout、stderr 各保留最多 16384 UTF-8 字节，截断有单独标志。结构化结果仍以 `result` 为准。
- Python 检查点有 30 秒预算；长时间不返回 Python 的原生调用由宿主进程时限控制。代码按 worker 的系统权限运行，这不是 Python 安全沙箱。
- 运行异常、语法错误、非 JSON 结果返回 `query_error` 和错误行；`SystemExit` 不关闭会话。源码/配置变化在调用前后检查，发现变化即 `stale_input`，后续查询不再执行。
- 查询代码读取现有编译对象；保持 Source、配置和 Compilation 不变。任务仍复用原生 compiler，不编写新的 RTL parser、elaborator 或仿真器。
