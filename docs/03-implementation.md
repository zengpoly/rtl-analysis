# RTL 分析实现与验证

- `src/rtl-pack/bin/rtl`：命令行入口。
- `src/rtl-pack/lib/rtl_analysis_cli.py`：工程选择、工具检查和单文件命令。
- `src/rtl-pack/lib/rtl_query_runtime.py`：任务内 PySlang 查询。
- `src/rtl-pack/scripts/install-tools`：按版本锁和 SHA256 安装工具。
- `tests/`：当前产品回归；`./check` 检查文件、执行权限、Skill 元数据并运行测试。

裸 `rtl doctor` 检查工具；指定 `--project` 或 `--target` 时同时检查源码身份。根目录识别不依赖研发资料目录。

最小工程见 [example](../projects/example/README.md)。测试保留当前产品回归，不携带旧研发任务、退休原型和固定实验题库。

全仓检查不安装工具、不调用模型，也不代替目标机器与客户端的实际验收。
