# Windows / ANSYS 19.2 环境配置

## 组成与版本边界

| 组件 | 用途 | 要求/记录 |
| --- | --- | --- |
| ANSYS Workbench / Mechanical | GUI、几何、网格与求解 | 历史环境为 19.2 / v192，需自备许可证 |
| CPython | 外部主控与可选 MCP server | 本运行器 3.10+，仅标准库 |
| IronPython | Mechanical 内脚本 | 随 19.2 提供，按 Python 2 兼容方式编写 |
| ACT 文件队列插件 | 在 Mechanical 主上下文执行请求 | 本技能 assets 中保存已使用源码副本 |
| Workbench MCP server | 客户端交互工具 | 可选；与 ACT 插件、技能分别安装 |

历史安装记录：CPython 3.12、fastmcp 2.14.7、mcp 1.29.0、python-dotenv 1.2.2、ansys-mechanical-core 0.13.0。这是当时环境记录，不是完整依赖锁。ANSYS 19.2 不应使用要求 2024 R1+ 的 PyMechanical CLI `--exit` 路径。

## 本机配置

在当前任务 shell 设置变量，用实际安装路径替换下面示例：

```powershell
$env:ANSYS_WB_EXE = 'C:\Program Files\ANSYS Inc\v192\Framework\bin\Win64\RunWB2.exe'
$env:WORKBENCH_MCP_ROOT = 'C:\cae-tools\WorkbenchMCP'
$env:WORKBENCH_MCP_QUEUE_ROOT = 'C:\cae-runs\interactive-queue'
$env:WORKBENCH_MCP_AUTO_START_SOCKET = '0'
$env:WORKBENCH_MCP_AUTO_START_QUEUE = '1'
$env:WORKBENCH_MCP_QUEUE_INTERVAL_MS = '1500'
$env:ANSYS_FEA_RUNTIME_ROOT = 'C:\cae-runs\runtime'
```

变量必须在 Workbench/Mechanical 启动前可继承。MCP 客户端配置里的 `env` 不会自动改变已经打开的 Mechanical 环境。运行器启动自己的会话时会覆盖队列根为本次唯一目录；不能让其他会话指向它。

主控不会自动加载 MCP server 目录的 `.env`；直接运行时应在当前 shell 设置变量或填写配置。独立运行器不依赖正在运行的 MCP server，ACT 插件仍是必需的。

`ANSYS_FEA_RUNTIME_ROOT` 用于把临时队列/日志移到可写 ASCII 目录；默认是技能的 `scripts/runtime/`。19.2 journal 文件自身也必须有可用 ASCII 路径。若安装的技能路径含中文且无可用短路径，把技能源码复制到可写 ASCII 目录后执行，不依靠运行日志目录解决 journal 编码。

## 安装 ACT

完整读取 [工作流程](fea-workflow.md)，确认现有进程及会话。不要通过关闭用户窗口让安装立即生效；已打开的会话可等下一次正常启动后再验证。

从技能 `assets/workbench-act/` 安装以下文件（保留原有同名插件备份，比较 SHA-256，不盲目覆盖）：

```text
%APPDATA%\Ansys\v192\ACT\extensions\WorkbenchMCP.xml
%APPDATA%\Ansys\v192\ACT\extensions\WorkbenchMCP\main.py
%APPDATA%\Ansys\v192\ACT\extensions\WorkbenchMCP\mechanical_queue_processor.py
%APPDATA%\Ansys\v192\ACT\extensions\WorkbenchMCP\mechanical_socket_timer_v7.py
```

保留插件 `LICENSE`。确认目标产品确为 v192；不要把示例目录直接用于其他版本。首次正常打开 Mechanical 后确认插件已加载、工具栏存在，自动队列已启动。`provenance.json` 可用于检查文件复制完整性；它不证明 ACT 已加载。

## 可选：安装 MCP server

上游为 [CAE-Agent-Hub / Workbench MCP](https://github.com/Cai-aa/CAE-Agent-Hub/tree/main/MCP/Ansys/Workbench%20MCP)。在上游目录创建虚拟环境，按上游当前文档安装，例如 `python -m pip install -e .`；需要新版 CLI 工具才安装 `[mechanical]`。入口是 `server.py`。

GitHub clone 因网络失败时可以使用官方源码 ZIP，但应记录快照日期/提交（若可确认）和哈希。不能把 ZIP 快照称为完整 git clone，也不能将当前 main 当作历史已验证提交。此技能未固定上游提交，附带插件有独立来源和哈希。

客户端配置模板在仓库 `examples/codex_config.example.toml` 与 `examples/workbench.env.example`。安装后的技能不含仓库 examples 时，可照下述字段配置：stdio server `ansys-workbench` 的 `command` 指向 MCP venv Python，`args` 指向 server.py，`cwd` 为 MCP 根目录，`env` 提供实际 ANSYS 路径、MCP 根和队列参数。只合并该段，保留现有配置，不复制私有 provider/token。

## 最小验证

1. Python 解释器存在，MCP server（若使用）可通过 stdio 启动，tool discovery 和 `workbench_detect_tool` 返回实际安装信息；不要硬编码工具总数。
2. 正常打开一个有几何的测试工程，验证 Mechanical 工具栏、当前工程目录及分析数量；检测到可执行文件不能证明 Mechanical 就绪。
3. 使用确认属于该会话的队列执行一次只读 `print("OK")` / 状态查询，记录 request_id、响应和 stdout。不要把请求发给不确定的共享队列。
4. 核验中文路径、插件日志和 UTF-8 请求能处理；记录启动环境、队列隔离和保存行为。

若没有实际收到 Mechanical 响应，状态只能是“环境检测通过，ACT 实机联调未完成”。沙箱缓存权限、网络或 GUI 启动权限错误，应按环境权限流程处理，不更换工具绕过拒绝。
