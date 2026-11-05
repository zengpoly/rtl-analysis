# Project Model 任务报告

说明本次是 `build` 还是 `query`，填写 Project、模型身份、输入问题或 spec、实际状态。未填写的模板不代表任务完成。

## 结论与证据

每条结论引用 SQLite assertion/evidence 及 `artifacts/<call-id>/` 中的实际查询结果。将 tool/human/llm、verified/partial/unsupported 和未知分开。`report.md` 是运行日志摘要，不是知识正本；后续查询必须重新读取 current SQLite。

## 局限与下一步

记录 coverage 缺口、失败 producer、未完成步骤和下一次 build/query 动作。工具中断、失败或未运行不得写成成功。

## 对 Build / Query Skill 的优化反馈

记录本次真实运行暴露的效率、恢复、查询、展示或证据问题，以及可复用的改进建议；没有发现问题时明确写“无”。这些反馈可被研发任务引用，但不是 SQLite 知识，也不直接改变 Skill。
