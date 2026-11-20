# 第三方来源与许可

`skills/ansys-workbench-static/assets/workbench-act/` 中的四个插件文件来自
[Cai-aa/CAE-Agent-Hub](https://github.com/Cai-aa/CAE-Agent-Hub/tree/main/MCP/Ansys/Workbench%20MCP/workbench_plugin)，
版权声明为 `Copyright (c) 2026 Thompson Labs`，MIT 原文保存在该目录的 `LICENSE`。
这里保留项目中使用过的兼容版本，含日志异常不中断队列及 `chr(0)` 清理 NUL 的本地修正。
文件来源、未固定上游提交的事实和发布时哈希见同目录 `provenance.json`。
这些哈希只标识本包内容，不能证明上游某个提交或 installed ACT 与它一致。

ACT 原作者信息、GUID 和许可均保留。未再分发第三方 MCP server、Python 虚拟环境或整套 CAE-Agent-Hub；
如需 MCP server，请从上游安装并记录实际使用的版本。原工作区兼容运行器和回归测试整理到技能的 `scripts/`。

ANSYS 安装、许可证、商标和求解器归其权利人所有，不受本仓库 MIT 许可覆盖。
本包不包含 CAD、用户工程、材料数据库、商业软件二进制或求解结果数据库。
