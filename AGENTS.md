# Agent 使用入口

用户说“please help me install”“please help mu install”或“帮我安装”时，直接执行下面对应目录的安装流程。对于完整离线包，在根目录运行 `./bin/install`，成功后运行 `./bin/rtl --project projects/example doctor` 和 `./bin/rtl --project projects/example syntax`；全部退出码为 0 且检查通过才报告安装成功。用户无需先提供自己的 RTL 工程。模型客户端与服务由使用者提供，安装只使用包内文件，禁止访问外部站点或在线包管理器；缺件或失败时停止并报告具体原因，由用户从联网机器重新转入完整包。保留已有工程和源码。

先判断当前目录：根目录存在 `bin/install`、`runtime-lock.json`、`vendor/python/` 或 `SHA256SUMS` 任一项时，按完全离线包处理，先读根目录 `README.md` 的离线安装说明，安装用 `./bin/install`，CLI 用 `./bin/rtl`，工程放在 `projects/`。缺件或校验失败时停止，提示用户转入完整离线包。

其余目录是源码仓库（包括 GitHub 自动生成的 Source code zip/tar 归档），先读 `quickstart.md` 判断环境；完全离线机器需要完整离线包，源码归档无法直接离线安装。安装器默认只使用本地缓存，缺件即停止并提示取得完整离线包。安装与工具合同见 `src/rtl-pack/docs/mvp-toolchain.md`，分析前读取 `src/rtl-pack/skills/rtl-query/SKILL.md`。

## 使用约定

- 用中文回复。当前流程仅使用源码与 Runtime 工具，不构建或读取知识库。
- 源码仓库 CLI 为 `./src/rtl-pack/bin/rtl`，安装脚本为 `./src/rtl-pack/scripts/install-tools`；离线包使用上面的 `bin/` 入口。
- 待分析工程可位于 `projects/<name>/`；已发出的使用说明采用的 `src/workspace/projects/<name>/` 同样支持。`source/` 是只读源码，工程根保存配置，`analysis/` 保存结果与任务。内置 `projects/example` 可直接试用。
- 按实际构建文件确认顶层、文件顺序、include、宏、参数和 single_unit；缺少关键配置时询问，不猜测。
- 回答引用源码或工具证据，明确未知与分析范围。不将工具输出当作完整功能证明。
- 私有源码、模型凭据、工具安装目录和分析产物不提交 Git。

## 修改约定

- 修改前查看 Git 状态，保留用户已有改动，仅修改授权范围。
- 优先复用现有工具和入口，不自行开发 RTL parser、elaborator 或分析算法。
- 按修改范围运行相关测试与 `git diff --check`；只报告实际验证结果。
- `./check` 运行当前产品检查与回归；失败时如实报告，不跳过断言伪造通过。
- 提交说明使用中文，推送使用已配置的 GitHub 远程。
