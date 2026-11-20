# ANSYS Workbench Static Skill

把真实操作中积累的 ANSYS Workbench / Mechanical 静力仿真经验整理为可安装的 Codex skill，重点支持 Windows + ANSYS 19.2（v192）。内容包括 CAD 检查、工况询问、材料 XML、ACT 文件队列、超时恢复、结果核验和工程保存。

原创内容采用 [MIT License](LICENSE)。附带 ACT 插件来自 [CAE-Agent-Hub](https://github.com/Cai-aa/CAE-Agent-Hub)，保留其 MIT 许可，见 [第三方说明](THIRD_PARTY_NOTICES.md)。

## 当前能力

| 内容 | 状态 |
| --- | --- |
| 物块与杆件的静力分析操作经验、19.2 故障处理 | 已整理为技能及参考文档 |
| CAD 导入、几何检查、用户输入记录 | 附带实现；先检查几何再收集输入 |
| 单实体、全局轴对齐直杆、一端固定、另一端施加轴向力矩 | 附带配置驱动运行器 |
| 其他力/压力、弯曲杆、多实体、接触等 | 可按流程适配；未提供通用求解器 |
| 离线回归、文档/配置/打包检查、GitHub Actions | 附带，无需 ANSYS 许可证 |
| 此发行包的新机器实机验收 | 尚未执行，按[验收清单](docs/RELEASE_CHECKLIST.md)记录 |

过去物块和扭杆案例的成功，不等于这个重新整理的发行包已在另一台机器完成验证。可核对的历史结果与限制见 [验证记录](skills/ansys-workbench-static/references/validation.md)。

## 安装

将 `skills/ansys-workbench-static` 整个文件夹复制到 `$CODEX_HOME/skills/`，未设置 `CODEX_HOME` 时使用 `%USERPROFILE%\.codex\skills\`。不要只复制 `SKILL.md`。例如，在仓库根目录执行：

```powershell
$feaSkillsHome = if ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME 'skills' } else { Join-Path $env:USERPROFILE '.codex\skills' }
$feaSkillDestination = Join-Path $feaSkillsHome 'ansys-workbench-static'
if (Test-Path -LiteralPath $feaSkillDestination) { throw '技能目录已存在，请比较版本后再更新。' }
New-Item -ItemType Directory -Path $feaSkillsHome -Force | Out-Null
Copy-Item -LiteralPath '.\skills\ansys-workbench-static' -Destination $feaSkillDestination -Recurse
```

技能默认允许自动发现，也可显式调用 `$ansys-workbench-static`。本仓库只提供文件，不会自动修改用户 Codex 配置、安装 ACT 或启动 ANSYS。安装 ANSYS/ACT/MCP 的具体步骤及脱敏配置见 [环境配置](skills/ansys-workbench-static/references/environment.md) 和 [examples](examples/)。

运行器需要 Windows、CPython 3.10+、可用的 ANSYS 19.2 和已加载的 ACT 队列插件。运行器只使用 Python 标准库；MCP server 是单独安装的可选交互接口，不是运行器的 Python 依赖。Mechanical 内脚本使用 ANSYS 自带 IronPython，不可直接用系统 Python 执行。

## 使用

示例请求：

> 使用 $ansys-workbench-static 导入我的 STEP。先检查实际几何并询问缺少的材料、支承和载荷条件，再完成适用的静力分析，保存工程并保持结果窗口可见。

先把 `assets/configs/cad_import_example.json` 复制到本次案例目录，填写本机 CAD/工程路径，并设置 `ANSYS_WB_EXE` 或配置中的 `workbench_exe`。然后按照 [运行器说明](skills/ansys-workbench-static/references/runner.md) 执行导入和后续分析。

关键规则：新 CAD 不能直接套用旧面编号、材料、网格或载荷；未收集完整工况不得划网格或求解；短等待超时不得重启求解；已有用户会话必须保留。

## 验证与发布

在仓库根目录使用 CPython 3.10+：

```powershell
python -m pip install -r requirements-dev.txt
python tools/validate_repo.py
python -m unittest discover -s skills/ansys-workbench-static/scripts/tests -v
python -m unittest discover -s tools/tests -v
python tools/build_release.py --output ../ansys-workbench-static-skill-release.zip
```

这些命令不会启动 ANSYS。验证器检查技能元数据、相对文档链接、配置空值、语法、插件哈希和常见私有配置/产物。打包器只收录明确允许的源码和文档；它还会为可单独安装的技能带入 MIT 和第三方许可。验证通过不能代替对新模型的几何、工况和精度核验。

发布时新建 GitHub 仓库，上传本目录内容或解压发行 ZIP 后上传；不要上传整个仿真工作区。先执行 [发布清单](docs/RELEASE_CHECKLIST.md)。仓库 URL/作者账号由发布者自行设置；此包不预填个人身份，也不执行推送。
