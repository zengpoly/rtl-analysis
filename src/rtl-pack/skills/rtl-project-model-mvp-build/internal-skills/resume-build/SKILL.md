---
name: build-resume-build
description: RTL Project Model MVP Build 的内置原子步骤；校验状态身份并从最近有效 checkpoint 恢复。
metadata:
  status: active
  visibility: internal
---

# 恢复 MVP 构建

只由 `rtl-project-model-mvp-build` 调用。读取整体与 scope state，逐项比较 Project、source revision、config/spec hash、domain pack、toolchain 和 schema。身份不符时将旧 state 标 `stale`，不得复用 artifact 或完成计数。

身份相同且已完成 checkpoint 的 artifact/证据仍存在时可以跳过；状态为 `in_progress` 或 `interrupted` 的 producer 因无原生 checkpoint，从该 scope 的 producer 阶段重跑。恢复前把 `next_action` 和本次决定写入 executions，恢复后立即原子更新两个 state。
