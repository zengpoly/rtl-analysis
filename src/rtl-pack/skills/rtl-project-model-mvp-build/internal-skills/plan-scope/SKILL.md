---
name: build-plan-scope
description: RTL Project Model MVP Build 的内置原子步骤；把明确 MVP 目标冻结成有界 scope 与可判定完成条件。
metadata:
  status: active
  visibility: internal
---

# 规划 MVP scope

只由 `rtl-project-model-mvp-build` 调用。为每个 scope 固定唯一 ID、source 文件、top、配置、6+1 目标维度、通用 required capabilities、producer 和验收项，并计算整体 `total_scopes`。当前 MVP 通常只有一个 scope；不得用文件总量猜测“全工程进度”。

Project 私有声明仅放在 profile，条目只含 `id`、`dimension`、`description`、`requires`；`requires` 必须引用通用 capability。不得把项目术语新增为顶层维度或核心表。

范围不明确、超出已验证 producer 能力或需要合并多个模型时停止并报告边界；更大范围交给未来独立 Build Skill。
