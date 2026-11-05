# 有界读取公共 JSON

完整 Build1 receipt、manifest、detail、Query 原件留在 Project/task，不裁剪原件或改 hash。以下示例只用随包 Python 标准库在本地解析公共 JSON，输出完整有效的 JSON 投影；不是 RTL 解析器或语义答案。每次合并展示最多 8192 **UTF-8 bytes**（含标题/换行），不能把多个各自合规的投影/节选拼成超限输出；超限报错，缩小字段/页后再读，不输出半截 JSON。工具的 token 截断不是字节预算，也不是完整覆盖证明。重复使用同一示例时，可将已核对的投影脚本原文一次保存在本 task 的本地脚本文件，此后用随包 Python 传参复用；保留原有断言与遗漏提示，不反复把长脚本和同一输出复制进上下文，不增加路径/分析 helper。

## Build1 身份

以用户指定的同一 Project/Scope 执行一次公开 `./bin/rtl model knowledge-build`，每次尝试的 stdout/stderr/退出码按尝试序号分别落盘（如 `build1-status.1.json`/`.1.err`/`.1.exit`），失败原件不因重试成功被覆盖；成功后才把该次成功原件作为 task 的 `build1-status.json` 引用（复制时保留带序号原件）。只接受 `READY` 或候选自身 READY 的 `PARTIAL`，且 `heavy_actions.compile_build_count<=1`；`PARTIAL` 表示 Manual 未就绪，须报告实际原因，不把它改写为 READY。已有匹配候选时应有 `candidate.reused=true`、`compile_build_count=0`；首次合法 Build1 可为新建候选。本 Skill 不为草稿另行重建 whole design。身份与路径从返回的 `build_identity`、`scope_id`、`candidate.path/sha256` 读取。若 candidate 不存在或失配，STOP。Build1 compile closure 和语义块是不同范围，不能把源码切片当作可独立编译的设计。

`--project` 可以是绝对路径；`--model` 与 `RTL_CONTEXT_MODEL` 必须原样使用 receipt 的 **Project-relative `candidate.path`**，不要拼成绝对路径。

v2 通过下节公开 manifest/detail 示例选择 exact 对象并生成 `map_anchor`，核对 receipt 的 scope/build/candidate、详情的对象/参数及 `semantic=NOT_BUILT`。map key 为 entity ID，路径或短文件名不证明对象；无匹配/不唯一即停。结构详情缺失不能建立 v2 地图锚定；已有 exact ID 可按 v1 做有限草稿，不称 v2 Gate 通过。`module-overview` 是抽样，不是完整树。

尚无 ID 时可用公开 `RTL_CONTEXT_MODEL=<candidate.path> ./bin/rtl --project <Project> context module <exact-qualified-symbol>` 定位；仅 `status=pass` 且 `resolved_by=exact` 或唯一 suffix 时取 `seed.entity_id`，`ambiguous/not-found` 则缩小目标或报告未知。计划/start 后才读 Query 节的命令、角色与引用字段。不试 legacy MVP `summary/entity/coverage`、SQLite/内部 Python。

`truncated=true`、`omitted_by_policy` 或参数缺失时缩小对象/角色或留 unknown，不靠提高 limit、猜测或读 Store 补齐。工具截断不证明完整覆盖，引用领域 JSON 时写实际字段路径和值；缺字段、空值、未读取与截断分别记录，不把宿主工具事件字段当作领域字段。Source 补齐必要声明、条件、优先级和例外的最小完整构造；Compiler Fact、地图身份及 SHA 一致均不证明行为。

## 先看身份与一页地图，再选 exact 对象

在 Workspace 根运行，将 `<manifest>` 换成 receipt 对应的公开 manifest 路径；末尾参数是从 0 开始的页偏移。map 的 key 是 exact `e:<hash>`，value 相对 **manifest 所在目录**；路径文件名只用于定位，不代替详情里的对象身份。保留 scope/build/candidate SHA，稍后与 receipt 核对。

```sh
./bin/workspace __domain-python - '<manifest>' 0 <<'PY'
import json, sys
from pathlib import Path
m = json.loads(Path(sys.argv[1]).read_text())
assert m['schema'] == 'rtl-analysis-agent/manual-v6-build1-manifest-v1'
offset = int(sys.argv[2]); assert offset >= 0
items = sorted(m['structured_detail_map'].items())
assert items and offset < len(items), 'empty map or offset out of range'
page = items[offset:offset + 4]
out = {k: m[k] for k in ('schema', 'scope_id', 'build_sha256', 'candidate_sha256')}
out.update(structured_detail_map=dict(page), offset=offset, total=len(items),
           next_offset=offset + len(page) if offset + len(page) < len(items) else None,
           omitted_entries=len(items) - len(page),
           omitted_fields=sorted(set(m) - set(out) - {'structured_detail_map'}))
text = json.dumps(out, ensure_ascii=False)
assert len((text + '\n').encode()) <= 8192, 'projection too large: select fewer fields'
print(text)
PY
```

从地图 key 选择 exact ID，或使用已知的完整 `object.qualified_name`；不按短实例名、模块名或文件名猜路径；名称必须与详情的 qualified_name 完全相等。下例对 ID 做精确 lookup；对名称仅比较各公共详情的真实 `qualified_name`，**不对哈希 key 或文件名做模块名正则**。0 个或多个匹配 exit 非零，空输出不能当成功。仅在唯一匹配、scope 与 object ID 对上后显示对象/parent/children/实际参数；再对照 receipt。

输入 Project 根、receipt 的 Project-relative `manual.path`（目录）和自选对象；输出的 `map_anchor` 可原样复制到该对象的计划块，只生成身份引用，不代选对象或填写目标/答案。`detail_relative_to_manifest` 只供地图定位；保存用的两个 ref 均由路径库生成 **Project-relative** 路径，例如 `analysis/...`，不带 `projects/<name>/` 前缀。绝对 manual/detail 路径、逃逸和错误前缀均拒绝，不裁掉前缀猜修。

```sh
./bin/workspace __domain-python - '<Project绝对路径>' '<receipt.manual.path>' '<exact-id-or-qualified-name>' 0 <<'PY'
import hashlib, json, sys
from pathlib import Path
project = Path(sys.argv[1]).resolve(strict=True)
manual = Path(sys.argv[2])
assert not manual.is_absolute() and '..' not in manual.parts, 'expected Project-relative manual.path'
p = (project / manual / 'manifest.json').resolve(strict=True)
assert p.is_relative_to(project), 'manifest escapes Project'
m = json.loads(p.read_text())
assert m['schema'] == 'rtl-analysis-agent/manual-v6-build1-manifest-v1'
selector = sys.argv[3]; offset = int(sys.argv[4]); assert offset >= 0
mapping = m['structured_detail_map']; matches = []
for eid, relative in mapping.items():
    if selector.startswith('e:') and selector != eid:
        continue
    assert not Path(relative).is_absolute() and '..' not in Path(relative).parts
    f = (p.parent / relative).resolve(strict=True)
    assert f.is_relative_to(p.parent) and f.is_relative_to(project)
    d = json.loads(f.read_text())
    assert d['schema'] == 'rtl-analysis-agent/manual-v6-structured-detail-v1'
    assert d['scope_id'] == m['scope_id'] and d['object']['entity_id'] == eid
    if selector == eid or selector == d['object']['qualified_name']:
        matches.append((relative, f, d))
assert len(matches) == 1, 'expected exactly one object; got ' + str(len(matches))
relative, f, d = matches[0]
assert d['semantic'] == 'NOT_BUILT'
out = {k: d.get(k) for k in ('schema', 'scope_id', 'semantic', 'page_kind',
                            'object', 'parent', 'parameters_truncated')}
out['detail_relative_to_manifest'] = relative
def ref(path):
    return {'base': 'project', 'path': path.relative_to(project).as_posix(),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
out['map_anchor'] = {'manifest': ref(p), 'detail': ref(f)}
# Definition pages have no actual-instance parameters/parent/children: null is
# unavailable, not proof of an empty actual hierarchy or default configuration.
for key in ('children', 'effective_parameters'):
    values = d.get(key)
    out[key] = values[offset:offset + 2] if values is not None else None
    out[key + '_page'] = None if values is None else {
        'offset': offset, 'total': len(values),
        'omitted': len(values) - len(out[key]),
        'next_offset': offset + len(out[key]) if offset + len(out[key]) < len(values) else None}
out['omitted_fields'] = sorted(set(d) - set(out))
text = json.dumps(out, ensure_ascii=False)
assert len((text + '\n').encode()) <= 8192, 'projection too large: narrow fields/page'
print(text)
PY
```

`omitted` 计算的是当前输出外的条目，包含前页；页投影不宣称完整参数或层级覆盖。`parameters_truncated` 是 producer 原始标志，不能被分页标志覆盖。先取得小区域所需身份/边界即可写 v2 计划、`start`；其余参数未知保留 unknown，不为遍历全树推迟 evidence。

## Query 原件先落盘，再读所需字段

`--project` 可绝对；`--model` 必须原样采用 receipt 的 **Project-relative `candidate.path`**。`--role` 是所需知识角色，context 必填，可重复指定不同值；合法值来自随包 `capability/config/knowledge-contract-v01-{facts,ai}.json` 的 `query_roles`。`structure`、`connectivity` 是合法 role；`--profile` 是可选检索配额策略，`driver` 是 profile，不能填成 role，也不能代替 role。已保存计划并成功 `start` 后，在 Workspace 根用这条完整命令（替换 Project/task 名及 exact ID；其他 cwd 使用实际绝对路径）：

```sh
(
project='projects/<name>'
task="$project/analysis/tasks/<task>"
entity_id='<自选exact entity-id>'
candidate=$(./bin/workspace __domain-python - "$task/build1-status.json" <<'PY'
import json, sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text())['candidate']['path'])
PY
) || exit "$?"
query_exit=0
./bin/rtl model query --project "$project" --model "$candidate" \
  --view context --entity-id "$entity_id" \
  --role structure --role connectivity --profile module-overview --limit 5 \
  > "$task/public-context.1.json" 2> "$task/public-context.1.stderr.log" || query_exit=$?
printf '%s\n' "$query_exit" > "$task/public-context.1.exit" || exit "$?"
[ "$query_exit" -eq 0 ] || exit "$query_exit"
cp "$task/public-context.1.json" "$task/public-context.json"
)
```

整段作为一次 shell 工具调用，`exit` 仅退出括号内子 shell；`||` 捕获真实失败码，启用 `set -e` 也能记录。调用非零即停止，不追加成功命令遮盖失败。落盘文件名带尝试序号（从 1 起），重试递增序号，不覆盖前次输出/错误原件；只有成功那份才复制为不带序号的正式名供后续投影引用。`build1-status` 的 knowledge-build 输出同理：每次尝试的 stdout/stderr/退出码按序号分别保存，失败原件不因重试成功而丢失。检查该子命令自身退出码与 JSON 的 `status=pass`，再显示投影；原件无论成功/失败都保留，不让后续成功命令掩盖失败。完整 JSON 中的 seed、request、truncated、retrieval_diagnostics、Fact IDs、source_anchors 按需投影；例：

```sh
./bin/workspace __domain-python - '<task/public-context.json>' <<'PY'
import json, sys
from pathlib import Path
q = json.loads(Path(sys.argv[1]).read_text())
assert q['status'] == 'pass', 'public Query did not pass'
keys = ('schema', 'status', 'seed', 'request', 'truncated', 'retrieval_diagnostics')
out = {k: q.get(k) for k in keys if k != 'seed'}
seed = q.get('seed') or {}
out['seed'] = {k: seed.get(k) for k in ('entity_id', 'kind', 'qualified_name')}
out['seed_omitted_fields'] = sorted(set(seed) - set(out['seed']))
out['omitted_fields'] = sorted(set(q) - set(keys))
text = json.dumps(out, ensure_ascii=False)
assert len((text + '\n').encode()) <= 8192, 'projection too large: select fewer fields'
print(text)
PY
```

此例只看状态与检索边界，不是 Fact/Source 已读证明。随后针对问题取少量实际 Fact 和 source_anchors，保存 evidence 的原件引用、观察、缺口和下一动作；引用 SHA 用完整原件计算。再按需补最小完整 Source，形成 draft。不要把 omitted/缺失字段或被工具截断的返回推断为“没有事实”。

## 持久任务恢复

按入口确认恢复任务后，先从持久 task 路径运行 `./bin/workspace __domain-python <Skill>/scripts/state.py resume --task <task>`，按入口约定保存输出/真实码；不先读全套 references 或重做阶段。核对 `status`、`completion`、`plan.not_started_blocks`、`remaining_scope`、各块 `valid_stages`/`preserved_stages`/`issues`、组合状态和 `next_action`，并再次运行相同的 public `knowledge-build` 验证候选 READY、同身份和 matching candidate 的 `compile_build_count=0`；据此只加载待继续/修订阶段的指引。`next_action`/RECOVERY 文本不授权扩大范围或重置预算；不能为清除 open 追逐范围外区域、删未知或跳过审核。Skill 身份变化时保留旧场，禁止手改 state/hash 消除漂移；新版验证用新 task，旧版恢复不计新版通过。工具进程内的 compilation 不可跨进程恢复，新的未保存 Runtime 调用须重跑；持久阶段/候选可以复用。

整个 Project 搬迁时保持 `analysis/tasks` 与 Source、配置、candidate 的相对结构，从新 task 路径恢复；helper 从该路径定位当前 Project，旧 state 中的绝对 Project 字段只作历史位置记录，其他冻结身份仍须逐项相同。离线 Workspace 用 `./bin/workspace __domain-python <Skill>/scripts/state.py ...` 调用随包 Python，不依赖全局 Python。搬迁后不能编辑 state/receipt/plan 或重算哈希来消除失配。

Source/config/build/candidate/Skill/receipt 漂移、产物或引用失效、上游任一阶段/引用失效或依赖 review hash 不符时，保留原件和本块自身通过核验的 `preserved_stages`；受依赖影响块的 `valid_stages` 清空，停止消费或继续保存该块，按报告先修复上游。依赖判定不取决于块名或保存顺序。半写临时文件不是完成记录。达到预算或未知无法关闭时保存已有有效阶段、记录 stop/next action，显式报告未覆盖范围。禁止改真实 RTL、正式知识库、I/R/A/Profile、发布审批或调用额外 Solver 来掩盖缺口。
