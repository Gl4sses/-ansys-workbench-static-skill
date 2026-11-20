# 配置驱动运行器

## 支持范围

当前求解器只支持一个实体、沿全局 X/Y/Z 的直杆、两端平面、一端完全固定、另一端轴向力矩。坐标必须核验为 mm；输入力矩单位为 `N m`。加载端是否增加导向由用户明确指定。`guide_loaded_end=true` 的实现限制三个平移和两个非轴向转动，轴向转动保持自由；其他自由度组合需要适配，不得用这个布尔量近似。

运行器使用 CPython，Mechanical 脚本由 ACT 队列执行。不要在系统 Python 直接运行 `mechanical_*.py` 或 `.wbjn`。已有用户工程锁定时，运行器停止；它不提供接管活动会话的适配器。

## 第一阶段：只导入并读取几何

从技能 `assets/configs/cad_import_example.json` 复制本次配置，填写 CAD、ASCII 工程名等真实路径。使用绝对路径；示例 `C:\cases\...` 不是可用数据。设置实际 `ANSYS_WB_EXE`，或在 JSON 写入实际 `workbench_exe`；JSON 不自动展开 `%VAR%` 或 shell 变量。

```powershell
$feaPython = 'C:\cae-tools\python\python.exe'
$feaSkill = 'C:\cae-tools\ansys-workbench-static'
$feaImportConfig = 'C:\cases\new-part\import.local.json'
& $feaPython "$feaSkill\scripts\ansys_fea_runner.py" --config $feaImportConfig --inspect-only
```

运行前必须完成 [前置检查](fea-workflow.md)。示例导入配置 `open_visible_after=false`，只关闭自己创建的导入会话，已有窗口保持原样。用户明确要求导入后查看 GUI 时，设置 `open_visible_after=true`；不能用 `--no-open` 违背该要求。随后工程将锁定，不能为了第二阶段擅自关闭用户的查看窗口，需会话适配或等用户正常保存关闭。

正常结果为退出码 **4** 和 `status=awaiting_user_input`：

- `<project>_geometry_review.json`：CAD/工程来源、几何和指纹。
- `<project>_simulation_questions.md`：当前几何及待询问信息。
- `<project>_simulation_draft.json`：`project_mode=reuse`、物理参数空值和未确认记录。

此时不设置材料、约束、载荷、网格或求解。读取实际几何后询问缺项并等待真实回答；不能根据候选面自动选支承。

## 第二阶段：填写已收到的工况

修改生成的草稿；`assets/configs/simple_torsion_example.json` 仅为字段参考，不是可运行示例。

| 字段 | 填写要求 |
| --- | --- |
| `material.*` | 牌号、状态、来源；E 用 GPa、强度用 MPa、密度用 kg/m³；材料 API 名为 ASCII，原牌号可在记录中保留 |
| `geometry.*` | 本次全局轴、已核验 mm、用户位置对应的两个当前面 ID |
| `boundary.*` | `fixed_support`、位置描述、用户明确的 `guide_loaded_end` |
| `load.*` | `moment`、`N m`、轴、正的 `value_nm` 和独立 `sign`（±1） |
| `analysis.*` | `static_structural`、一个实体、当前网格大小及明确的大变形布尔值 |
| `retry.*` | 本次允许的有限尝试；网格/子步备用列表默认空 |
| `user_input.*` | 真实回答记录和 CAD/几何/参数指纹 |

CAD 与几何指纹来自导入记录，不手造。所有已收到回答、单位换算和位置映射明确记录后，才能在代码中计算 `simulation_intake.input_digest(config)` 写入 `user_input.parameters_sha256` 并设置 `confirmed=true`。
这是助手记录对话的步骤，没有自动确认命令。指纹不是签名，不能防止人为伪造确认；用户回答本身仍是依据。
物理参数、CAD 或几何变化时，先解决变更的信息；数值设置变化也须符合本次预先记录的允许范围，不可直接更新指纹掩盖变更。

```powershell
$feaCaseConfig = 'C:\cases\new-part\part_simulation_draft.json'
& $feaPython "$feaSkill\scripts\ansys_fea_runner.py" --config $feaCaseConfig --preflight-only
& $feaPython "$feaSkill\scripts\ansys_fea_runner.py" --config $feaCaseConfig
```

只在预检 `ready=true`、输入核验通过后运行第二条。覆盖既有工程要求已经得到用户授权，且同时设置 `overwrite_project=true` 和 `--allow-overwrite`。`--prepare-only` 只准备运行文件，不启动软件；它不是忽略失败预检的手段。

## 有限恢复

默认最多 3 次 Mechanical 尝试（含首次），创建工程/打开 Mechanical 各最多 2 次；重试等待 5、10 秒，最多 30 秒。`retry.enabled=false` 禁用恢复。

| 状态 | 处理 |
| --- | --- |
| 明确临时许可证签出/服务错误 | 原参数退避重试；启动重试前确认旧受控进程退出、锁自然释放 |
| 终态网格失败 | 仅下一项 `retry.mesh_sizes_mm` 中针对当前几何指定的绝对尺寸 |
| 本次日志证实不收敛 | 仅下一项 `retry.solver_substeps`，不改变最终载荷、大变形或收敛标准 |
| 几何/材料/单位/约束/内存/API/未知错误 | 停止，保留诊断 |
| 响应未决、部分 JSON、ID 不匹配、等待预算用尽 | `pending`，保留原请求/会话，不重发 |
| 结果验证、保存、GUI 交付失败 | 修复交付阶段，不重复成功求解 |

可选子步格式为 `{"initial": ..., "minimum": ..., "maximum": ...}`，正整数且 `minimum <= initial <= maximum`。参数应由当前工况确定；属性/枚举不支持或读回不一致则停止。此分支离线测试通过，不代表真实 19.2 实机验证完成。

恢复只清理本次脚本拥有的 `MCP ` 对象。运行器的复用对象清理按此前缀实现，因此不能用于含有用户自建同前缀对象的未知工程；应选专属副本或补充明确归属记录。

## 诊断与 pending

运行目录默认在 `scripts/runtime/<run_id>`，可用 `ANSYS_FEA_RUNTIME_ROOT` 指定可写位置。每次运行都有独立 queue、done/abort、不可变请求快照、`execution.claim` 和工程控制器锁。

`retry_history.json` 记录阶段、请求及响应路径和恢复决策；`attempt_XX` 保存配置、进度、结果和失败诊断；`monitor.json` 每 30 秒记录进度及进程/累计 CPU 信息。日志尾部限大小且只取本次更新内容。

| 退出码 | 含义 |
| --- | --- |
| 0 | 求解及所请求的自动交付检查通过 |
| 1 | 终态错误或输入验证错误 |
| 2 | 预检阻塞（如工程锁） |
| 3 | 执行未决；不得另起求解 |
| 4 | 导入检查成功，等待用户工况 |

遇到 pending，读取 history 中同一 response/progress、`create_process.json`、`hold_process.json` 和原受控进程。重复执行原命令不是恢复功能；此版本没有跨进程自动续跑入口。
若稍后收到可信成功，按当前 run/attempt 重新验证结果后才发 done，并继续保存/GUI 核验；明确失败后 abort 只结束自己创建的持有会话，不保存。
这些标记必须来自已确认归属的 runtime_config；不能复制别的运行路径。
只有确认原控制器、持有进程及求解器都结束且工程未锁定，才可清理遗留 `.automation.lock`。不得删除 Workbench `.lock` 强行重试。

## 交付

核验当前 run/attempt、`UpToDate` / `Done`、有限且合理的数值、单位和独立安全系数后触发保存。
验证 `.wbpj` 和 `SYS.mechdb` 非空且时间戳足够新，再将结果文本复制到案例目录。
最后按用户要求可见打开并实际确认 GUI。运行器默认最终可见打开 Workbench；它不保证自动打开 Mechanical 的结果视图，用户要求该视图时还需打开并确认。

执行 [完整交付检查](fea-workflow.md)。成功重试不代替网格收敛或工程物理验证。
