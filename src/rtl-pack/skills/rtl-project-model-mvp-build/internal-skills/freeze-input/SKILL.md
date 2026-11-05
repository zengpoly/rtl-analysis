---
name: build-freeze-input
description: Project Model 构建内部原子步骤；冻结 Project、源码、spec、工具和输出身份。
metadata:
  status: active
  visibility: internal
---

# 冻结构建输入

只由 `rtl-project-model-mvp-build` 调用。核对 `rtl-project.json`、source 边界、可选 revision/clean 状态、spec 路径与 SHA-256、top/filelist/include/宏/参数、领域包及锁定工具版本。

把输入身份写入本次 request 和首条 execution；路径使用 Project-relative 坐标。输入缺失、逃逸、revision 不符或 spec 未显式列出必要文件时停止，不猜配置、不修改源码。固定 current 为 `analysis/model/rtl-project-model.sqlite`，并记录构建前 current 的 model ID/content hash（存在时）。
