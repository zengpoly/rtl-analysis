---
name: build-validate-publish
description: Project Model 构建内部原子步骤；验证 candidate 并确认 current 原子发布结果。
metadata:
  status: active
  visibility: internal
---

# 验证与发布

只由 MVP 构建父 Skill 调用。保存构建命令结果后查询 summary/coverage，确认 model status complete、所有声明有 evidence、`foreign_key_check` 为空、`integrity_check` 为 ok、producer 状态与 spec 一致；required capability 不缺行，七维总体状态与细项汇总一致。

记录 model ID、content hash、文件大小及各维/能力 coverage。私有 assertion 的 `requires` 不完整时发布校验必须失败，调用方应将结论降为 partial 或补齐证据；该检查不是依赖推理。失败时比较构建前 current 身份，确认旧 complete 未变且 candidate 已清理；未满足任一条件不得报告发布成功。任务 report 只摘要结果，SQLite current 才是知识正本。
