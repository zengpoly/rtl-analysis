# Build2 草稿预览

这是当前有效 task 产物的只读投影，不接管 `manual-build --model`，不执行 RTL 推理或翻译，不改变知识发布合同。Workspace 的 README 第 6–8 节提供完整操作与英文 AI 提示；由本 Skill 按用户请求区分 candidate Manual 与 task 草稿预览。

```sh
./bin/rtl model manual-preview --project "projects/my-design" --task "projects/my-design/analysis/tasks/first" --output "artifacts/manual-preview-001"
```

Project/task 按 cwd 解析，output 相对指定 task 且必须是新目录。使用随包 Build2 公共 inspect，与 resume 相同地核对 Source/config/candidate/receipt/plan/Skill、map_anchor、阶段及依赖；退出 0 并不足够，identity_issues 或 stale 均拒绝。至少存在一块当前有效 draft；未开始块、缺少 review/combination 和 unknown 原样保留。manifest 绑定实际读取的原件和 SHA，生成前后复核输入稳定；出错不留下成功站点。

页面显式标记 `DRAFT_UNREVIEWED / NOT PUBLISHED`。review、self_checked、draft_self_checked 均不证明独立审核通过。实际实例与模块定义取自当前 structured detail 的独立字段，无关联则 unknown；参数 value/overridden/证据分别展示，不从等于默认值推断未覆盖。冻结计划是历史描述，和当前事实并列保留供作者核对冲突，不改计划、不判断自然语言真伪。

中文正文须在 evidence/draft/review 由作者写入。英文旧稿保持原文与语言限制提示，不能用渲染器翻译或直接改 HTML。每条 claim 可追溯至当前 stage 路径、数组下标和 SHA；原件、源码定位与自检细节折叠展示。HTML 转义、UTF-8、无外部资源。只服务生成站点，服务范围之外的引用仅显示定位。

旧 Skill identity 不兼容时，保留匹配旧安装完成恢复，或在新版创建新 task；不手改 state/hash，不加载 task 携带的代码。已有输出不覆盖，公共 revise 后输出另一个新目录。

## 作者生成或修订提示

```text
Reply in Simplified Chinese. Load the installed rtl-build2-draft Skill and
resume the exact task. Inspect current identity, valid stages and dependencies.
Use complete relevant source constructs and current structured configuration.
Write semantic prose in Simplified Chinese in the actual stage artifacts.
Use public save for new stages, or public revise with a reason for incorrect
existing stages; preserve history and rebuild affected downstream stages.
Keep conditions, evidence, target checks and unknowns consistent. Do not edit
state/hashes, frozen plans or generated HTML. Run manual-preview into a new
task-relative directory only when current artifacts validate. Report missing
review/combination and keep DRAFT_UNREVIEWED / NOT PUBLISHED explicit.
```
