---
name: build-run-slang
description: Project Model 构建内部原子步骤；约束并核验 slang elaboration producer。
metadata:
  status: active
  visibility: internal
---

# slang producer

只由 MVP 构建父 Skill 调用。使用 spec 已声明的源码、top、include、宏和参数，让锁定 slang producer 提供 compiled AST、配置/elaboration、层级、实例/端口绑定和源码位置。

不得以单文件 CST、文件名或 `--ignore-unknown-modules` 冒充完整 elaboration。构建后在 summary 中核对 slang producer 为 complete，并确认相应 assertion 有 tool derivation 和 source/tool evidence；诊断或缺依赖如实进入 failed/partial coverage。
