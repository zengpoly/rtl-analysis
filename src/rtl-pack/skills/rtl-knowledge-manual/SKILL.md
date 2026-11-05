---
name: rtl-knowledge-manual
description: 将一个现有 RTL Knowledge candidate 通过 deterministic、read-only Mapping 投影为静态 Manual，并依据公共命令结果完成或停止。
metadata:
  status: active
  mode: mapping-v1
  requires: 有效 RTL Project、Project-relative Knowledge candidate 与新的 Manual output path
---

# RTL Knowledge Manual

源码仓库根使用 `./src/rtl-pack/bin/rtl` 替换下方 `rtl`；完整离线包使用 `./bin/rtl`。

## When to use

用户要求从现有Knowledge candidate生成或重新生成Manual时使用。candidate自己提供build identity与Analysis Scope；不要让用户或Agent重复猜scope。

用户明确要求查看 task 的未审核 Build2 草稿时，先读 [Build2 草稿预览](references/build2-preview.md)，使用 `rtl model manual-preview --project <Project> --task <task> --output <new-task-relative-directory>`。Project/task 按 cwd 解析，输出相对 task 且必须新目录；随包 helper 核验当前有效阶段、身份与依赖，至少有有效 draft。保留 `DRAFT_UNREVIEWED / NOT PUBLISHED`、缺失审核/组合与 unknown；中文正文来自作者有效草稿，英文旧稿不自动翻译。修正内容必须先公共 revise 留历史，不让模型自写或修改 HTML 掩盖错误。

三个层级不可混用：Build1 candidate 结构投影走下面的 `manual-build`，语义保持 `NOT_BUILT`；适用合同下已发布语义走原正式选项与发布核验；task 草稿只走 `manual-preview`，不是知识发布或独立审核通过。预览入口不会把 task 内容补入 candidate 或正式 Manual。

## Inputs and one public command

只解析RTL Project、Project-relative candidate、Project-relative新Manual目录，以及可选source viewer模式。执行一次：

```sh
rtl model manual-build --project <project-dir> --model <candidate.sqlite> --output <manual-dir> --source-viewer <locator|full>
```

正式 module-q2 页面必须明确添加 `--formal-q2`。该选项只接收建库时固定真实 Profile、已完成 12 目标整包且通过受控 writer 发布的 v0.2 candidate；缺少任一批准批次、审核绑定或 Source 时命令拒绝，不从 task-local draft JSON 补内容。没有正式批准时保持 Build1 入口与 `NOT_BUILT`。

不要读取或修改candidate、写SQL、调用内部Python、生成template或让模型生成HTML。Mapping correctness由同一个deterministic public command决定；本Skill只是薄入口。

## What it maps and returns

命令把candidate中的Knowledge truth映射为typed PageView、静态HTML、structured detail与唯一`manifest.json`。结果报告candidate identity、scope ID与Analysis Scope presentation、Manual path、page/detail counts、source serving mode、timing与limitations。

Mapping是read-only：candidate仍是唯一Knowledge truth，HTML/JSON只是可删除重建的projection。Build1也复用同一个mapper，不通过另一个Agent Skill增加模型轮次。

`--formal-q2` 由同一只读 SQLite snapshot 读取 backbone、CURRENT claim/coverage、Fact/Source 与 review，并核对 `publication_bundle` 记录的所有批次、12 项处置和额外行。HTML、per-Definition structured detail 与 manifest 使用同一份 semantic projection；已审核 PARTIAL 保持 PARTIAL。locator/full 两种模式都在渲染前核验 Source；full Source 页面消费已核验字节。

当前受控 Profile 为 v2；interface 的 signal_refs 必须是属于 exact actual、由 Stage1 Fact/Evidence 绑定的 PORT entity，不接收自由信号字符串。正式 Module PageView 的概览、职责、接口、控制、流程原栏目承载结构化字段；每项显示 Source-only/Fact 种类、审核状态、适用性与边界，技术摘要折叠。历史 v1 Profile/I 与旧 candidate 不晋升为 v2。每个发布批次对应一个 exact target，同一 obligation/applicability 切片的多条 claim 由一条 CURRENT coverage 关联；12 个内容目标不意味着 12 批或 12 条 coverage。本仓库不携带预先批准的语义 candidate；实际语义必须以使用者提供的当前审核、批准和发布记录核验。

## Provenance and grounding

页面用简体中文展示导航、结构说明与证据边界，稳定区分 AI 经审核语义、Slang 编译器事实和 RTL 源码证据。RTL 名称、表达式、机器枚举/字段、ID/路径/SHA及用户知识正文保留原文；作者自检不能称为审核通过。Build1的AI semantic必须保持`NOT_BUILT`，不得补写假解释。未来任何VERIFIED、REVIEWED或factual semantic内容都必须明确supported by至少一个Compiler Fact或Source Evidence。

每个展示的deterministic Fact必须直接列出只属于该Fact的Source Evidence path/span；locator模式显示安全定位文本，full模式才链接对应Source page fragment。页面级Source inventory不能替代逐Fact grounding。

`locator`只发布可核验source locator；`full`才生成完整source页面。两种模式都不得越过candidate声明的source boundary。

## STOP cases

输出目录已存在、candidate/source identity不安全或过期、manifest失败、scope或build identity不成立时立即STOP并原样报告。不要覆盖已有Manual，不要修补Knowledge Store，也不要换renderer、猜scope或绕过Source Evidence。
