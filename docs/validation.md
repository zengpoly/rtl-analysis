# 发布前验证

本页记录本次源码整理的验证范围，不包含用户工程、模型答案或旧实验日志。

## V1

2026-11-05，在 Linux x86_64、Python 3.12.3 下使用锁定工具缓存、Jinja2 3.1.6 和 MarkupSafe 3.0.3 验证：

```sh
RTL_ANALYSIS_TOOL_ROOT="$PWD/.local/tools" \
RTL_ANALYSIS_PYSLANG_SITE="$PWD/.local/tools/pyslang/11.0.0/site-packages" \
./check
```

结果：377 项测试，361 项通过，16 项因未提供外部已审核 Q2 工程而跳过；产品文件、执行权限、Skill 元数据检查通过。`.local/` 中的工具和虚拟环境是验证用本地依赖，不提交 Git。

最小 `projects/example` 的 `doctor / syntax / lint / outline` 全部通过。源码校验值保持为 `415f9369d5f6536788810ed702773606d0f37411698558386fe7904aa14119b5`。

调整了一条过时的 Runtime 集成断言：按夹具逐项核对连接表达式实际引用的符号，常量 `1'b1` 的引用为空；保留精简存储省略空引用列表的现有格式。未修改分析实现。

公开内容检查覆盖待提交文件：凭据模式、私网地址、本机路径、历史任务引用、敏感文件名、用户工程、二进制文件和本地 Markdown 链接。第三方许可证原文保留。

未执行真实大工程、模型准确率评估、同事客户端验收或离线包重建；本次交付是源码、使用说明与版本标签。

## 知识构建预览版

同一环境与锁定依赖下运行本分支 `./check`：377 项测试，361 项通过、16 项因外部已审核 Q2 工程未提供而跳过。全部七个公共 Skill 与 domain manifest 所引用文件存在；源码 Build2 状态 helper 的帮助入口可执行。

实际执行 `projects/example` 的 `model knowledge-build --scope whole-design`：首次回执 READY，candidate 和 Manual 都 READY，编译 1 次、渲染 1 次；相同命令再次回执 READY，candidate 和 Manual 均复用，编译 0 次、渲染 0 次。示例源码 SHA256 不变。生成的候选、Manual 和分析状态只保存在本地忽略目录，不提交 Git。

本分支打包脚本调整为包含全部公共 Skills、模板和配置，通过语法与路径完整性检查；完整离线包尚未重建或验收。本次没有调用模型生成 Build2 语义草稿，Build2 的阶段保存、恢复与预览由产品夹具回归覆盖；这不等于真实工程的语义审核通过。
