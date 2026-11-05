# 最小 RTL 示例

`source/example.sv` 是一个 8 位计数器：低电平异步复位清零，时钟上升沿在 `en_i=1` 时加一，否则保持；溢出回到零。

已提供工程和编译配置。安装工具后，在仓库根目录执行：

```sh
./src/rtl-pack/bin/rtl --project projects/example doctor
./src/rtl-pack/bin/rtl --project projects/example syntax
./src/rtl-pack/bin/rtl --project projects/example lint
./src/rtl-pack/bin/rtl --project projects/example outline
```

可以让 Agent 读取 `src/rtl-pack/skills/rtl-query/SKILL.md`，分析这个工程的复位、使能和位宽，引用源码位置。结果写入 `analysis/`。

按根目录 `quickstart.md` 验证时，把 `source/` 中的文件复制到 `src/workspace/projects/my-design/source/`，再让 Agent 配置该工程；无需修改使用说明。

Git 仓库没有携带 PySlang wheel 或完整工具二进制。安装脚本默认使用本地缓存；允许联网时显式传 `--online`，按 `src/rtl-pack/config/toolchain.json` 下载并校验，下载缓存位于 `.local/downloads/`，安装结果位于 `.local/tools/`，两者均不提交 Git。无公网环境需事先准备这些依赖或完整离线包，以及兼容的 Python 3.12、系统库和可访问的模型服务；仅复制 Git 仓库不足以完成离线安装。
