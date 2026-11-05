# RTL 知识构建预览实现

- `rtl_analysis_cli.py`：工程、工具和公开 Build / Query / Manual 子命令。
- `rtl_build1.py`、`rtl_module_knowledge.py`：结构候选构建、验证与复用。
- `rtl_knowledge_prototype.py`、`rtl_query_v02.py`：知识合同、Store 和只读知识查询。
- `rtl_manual_v6.py`：candidate 的 deterministic Manual 映射；`rtl_build2_preview.py`：受身份与阶段核验的草稿预览。
- `skills/rtl-build2-draft/`：有界草稿工作流、阶段规则和公开状态 helper。
- `rtl_query_runtime.py`：任务内 PySlang 查询；`tests/` 保存产品回归。

上述文件位于 `src/rtl-pack/` 下；分析库位于 `lib/`，状态 helper 位于相应 Skill 的 `scripts/`。源码仓库和离线包共用这些实现；本分支的包装脚本包含全部公共 Skills、模板和说明，不再只打包 V1 默认查询 Skill。

`./check` 验证文件、执行权限、Skill 元数据与回归。它不调用模型，不代替目标机器、真实工程或语义发布审核。实际验证见[发布前验证](validation.md)。
