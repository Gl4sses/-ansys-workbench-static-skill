---
name: ansys-workbench-static
description: 在 Windows 上通过 ANSYS Workbench/Mechanical 和 ACT 文件队列导入已有 CAD、检查几何、收集工况并执行静力结构分析。包含 ANSYS 19.2 材料 XML、会话保留、长任务监控、安全系数和保存验证经验，以及单实体轴向扭转运行器。用于物块、杆件等已有模型的静力任务；不用于 CFD、电磁分析或从零绘制 CAD。
license: MIT
---

# ANSYS Workbench 静力结构分析

先证明当前几何和工况一致，再修改模型。历史成功案例证明了接口可用，不能提供新工况的物理输入。

## 执行前阅读

在启动、关闭或脚本控制 Workbench/Mechanical 前，完整阅读
[工作流程与交付检查](references/fea-workflow.md)。若当前项目另有
`AGENTS.md` 或 `ANSYS_FEA_AUTOMATION_GUIDE.md`，同时完整阅读并遵守。
首次安装、路径/队列异常时读 [环境配置](references/environment.md)；
19.2 脚本读 [兼容接口与故障处理](references/ansys-192.md)。

## 关键约束

- 保留用户已有的 Workbench/Mechanical 会话。默认复用；附带运行器无法接管锁定工程，不能为运行它关闭用户窗口或删除工程锁。
- 新 CAD 导入后，先枚举实际实体、尺寸、顶点和面，再收集材料牌号、热处理/供货状态及数据来源，支承位置/类型/自由度，载荷位置/类型/大小/单位/方向，以及静力、大变形、接触等分析条件。记录本次已提供的信息，只询问缺项；“左端”“向下”等必须映射到当前几何与坐标系。
- 输入不完整或位置有歧义时停在导入/询问阶段；不得设置材料、边界、网格或求解。不能从模板、旧面 ID、旧材料强度、旧轴向补值。重试也不能改变物理输入。
- 19.2 使用显式导入和经过独立 Library 验证的 Engineering Data XML；禁用 `model.Materials.Import(path)`，不得在活动材料库试验 `NewMaterial/Clone`。
- 网格/求解短超时是进行中；继续观察同一 request_id、response 和进度。未收到可信终态时不重复提交，不以 CPU 安静判断失败。
- 只有验证求解状态、当前运行结果、独立安全系数、`.wbpj` 与 `SYS.mechdb` 保存证据及用户要求的 GUI 状态后，才能宣布完成。

## 选择实现

**任意新 CAD：先检查，再收集工况。** 只导入入口：

```powershell
& $feaPython "$feaSkill\scripts\ansys_fea_runner.py" --config $feaImportConfig --inspect-only
```

这些变量须指向已核验的本机解释器、技能目录和本次配置；模板在
`assets/configs/cad_import_example.json`。该阶段不施加物理设置；正常结束返回
`awaiting_user_input`（退出码 4），并生成几何记录、问题清单和未确认配置草稿。
照顾用户已提供的信息后提问，等待回答。

**单实体、沿全局轴的直杆、一端完全固定、另一端轴向力矩：**
读 [运行器使用说明](references/runner.md)，核对平面端部、坐标单位及加载端是否导向，
填充生成的草稿。收到真实回答后记录 `confirmation_note` 和参数指纹；
`confirmed=true` 是回答记录，不是自动批准，也不要求完整回答后再审批一轮。
先 `--preflight-only`，通过后求解。模板所有物理参数保持空值，不能直接运行。

**物块受力、弯曲杆、多实体、非轴对齐、接触或其他支承/载荷：**
使用相同检查和交付流程，按本次工况适配脚本或已连接的 Mechanical 工具。
参考 [19.2 接口](references/ansys-192.md)。附带求解器仅支持上述扭转条件，
不得用扭转载荷替代用户要求，也不得跳过输入/几何检查。未实现的路径须如实说明，
实现并验证后再声称支持；模态、屈曲和疲劳需各自流程。

## 恢复与交付

运行器为每次运行隔离队列、配置快照、请求占用和完成标记。只有明确临时许可证错误、
终态网格失败或不收敛可进入有上限的恢复；网格和子步只用本次记录的备用设置。
`pending`（退出码 3）必须保留请求和会话，按 [运行器说明](references/runner.md) 处理。
保存或 GUI 交付失败不能触发重新求解。只清理本次自动化拥有的对象。

使用 [工作流程](references/fea-workflow.md) 中的完整交付清单，报告物理输入、实际网格、
应力/变形及单位、原生和未截断安全系数、保存路径和 GUI 状态；有重试则记录实际参数。
区分“求解成功”“工程已保存”“GUI 已确认”和“工程精度已验证”。
现有证据、测试边界和待完成的实机验收见 [验证记录](references/validation.md)。
