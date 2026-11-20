# ANSYS 19.2 兼容接口与故障处理

这里的接口来自已有工程操作记录。只对 19.2 已观察路径提供依据，不保证其他版本相同。实际调用前仍须读工作流程并核对当前工况。

## Mechanical 队列的显式导入

```python
import clr
clr.AddReference("Ans.Core")
clr.AddReference("Ans.EngineeringData")
clr.AddReference("Ans.EngineeringData.Library")
clr.AddReference("Ans.EngineeringData.Core")
clr.AddReference("Ans.EngineeringData.MatMLReaderWriter")
from Ansys.Core.Units import Quantity
from Ansys.ACT.Interfaces.Common import SelectionTypeEnum
from Ansys.ACT.Automation.Mechanical.Enums import LoadDefineBy
from Ansys.EngineeringData import Library
from Ansys.EngineeringData.Core import MatMLXmlTextReader

project = ExtAPI.DataModel.Project
model = project.Model
```

不依赖队列隐式提供 `Model`、`Quantity`、枚举，也不使用本机 19.2 不支持的新版 `Ansys.Mechanical.DataModel.Enums`。

## Engineering Data XML

```python
# material_xml_path 和 material_name 均来自本次已核验配置。
validation_library = Library()
MatMLXmlTextReader().Load(material_xml_path, validation_library.GetMaterials(), "", False, None)
items = list(validation_library.Materials)
if len(items) != 1 or unicode(items[0].DisplayName) != material_name:
    raise RuntimeError("Material XML validation failed")

library = project.EngineeringDataLibrary
names = [unicode(item.DisplayName) for item in list(library.Materials)]
if material_name not in names:
    MatMLXmlTextReader().Load(material_xml_path, library.GetMaterials(), "", False, None)
    model.RefreshMaterials()
# 分配前还须核验本次材料关键性能值、单位、数据来源和热处理状态。
```

19.2 的 `model.Materials.Import` 接收 DataModelObject，不是文件路径接口。`NewMaterial/Clone` 的探测曾覆盖源材料；不在活动工程试验。`GetMaterialByName` 曾返回 None，应枚举 DisplayName。运行器由本次参数生成 XML，并给材料名加短指纹，降低同名旧材料误复用风险；名字后缀不能代替必要的属性核验。

## 支承和分量力（物块等工况的适配示意）

这段是 API 模式，不是独立运行器：所有变量必须由当前几何及完整用户回答提供，且输入核验已经完成。运行器的 schema 当前不支持 force，不能只改模板中的类型绕过检查。

```python
def current_face_selection(face_id):
    selection = ExtAPI.SelectionManager.CreateSelectionInfo(SelectionTypeEnum.GeometryEntities)
    selection.Ids = [int(face_id)]
    return selection

support = analysis.AddFixedSupport()
support.Name = automation_owned_support_name
support.Location = current_face_selection(current_fixed_face_id)
force = analysis.AddForce()
force.Name = automation_owned_load_name
force.Location = current_face_selection(current_loaded_face_id)
force.DefineBy = LoadDefineBy.Components
for axis, value_n in zip(("X", "Y", "Z"), confirmed_force_components_n):
    getattr(force, axis + "Component").Output.DiscreteValues = [Quantity("%.15g [N]" % value_n)]
```

面集、分布载荷、远程载荷、局部坐标系和非完全固定支承需要对应适配。对象归属需按当前运行跟踪；仅凭前缀不能证明旧对象可删除。

## 安全系数

先确认当前等效应力显示/计算单位为 MPa，且单材料延性屈服判据适用，再用当前 `yield_strength_mpa`：

```python
uncapped = solution.AddUserDefinedResult()
uncapped.Name = automation_owned_result_name
uncapped.Expression = "%.15g/seqv" % yield_strength_mpa
uncapped.EvaluateAllResults()
if unicode(uncapped.GetQuantityName()) != u"Dimensionless":
    raise RuntimeError("Safety factor must be dimensionless")
```

观察到原生结果封顶为 15 时报告 `>=15（显示封顶）`，不能把 15 当成实际极限。独立核对 `σy/σeq,max`；材料、单位变化后重建表达式。原生结果 `[MPa]` 显示异常不能作为无量纲证明。

## 已遇问题与处理

| 表现 | 判断与处理 |
| --- | --- |
| 已有窗口消失 | 检查是否用了 `-B -R` 并让 journal 返回；不触碰用户会话，受控会话保存后用 `-F` 可见打开 |
| journal 退出，无 stdout 或阶段日志 | 核查 `-R` 参数本身仍是中文长路径；8.3 路径必须保持到实际进程调用，不只改 journal 内容 |
| `model.Edit()` 后 Mechanical 消失 | journal 已返回；用持有循环等本次 done/abort 标记，不能用墙钟截止强制退出 |
| Workbench 已启动，队列无响应 | 等待 Mechanical opened 阶段、核查 ACT 加载和队列；Workbench 主窗不代表队列已就绪，不反复发求解或 ping |
| Unicode/日志异常使队列停住 | 附带插件让日志失败不中断；UTF-8 读取，`chr(0)` 去 NUL。安装后核对插件哈希并执行只读实测 |
| 同名材料仍用旧强度 | 比较实际属性与参数指纹；不只根据名称跳过验证 |
| 另一个窗口处理了请求 | 队列被共享；每次受控运行隔离环境及目录，确认归属后才提交 |
| mesh/solve 短等待超时 | 属于进行中；观察同一 response、进度和求解器，不重复求解 |
| 成功结果出现，工程未保存 | 控制器验证后才写 done；本次容器 Exit 后 Workbench Save，检查两种数据库时间戳 |
| 子步属性/枚举不可用 | 停止并记录，不在活动工程猜新版命名空间；可选子步恢复尚需代表工况实机验证 |

新增发现须记录版本、最小触发条件、错误、经过验证的恢复与证据，再更新本文件；没有验证的方案明确标记待验证，不能升格为固定操作。
