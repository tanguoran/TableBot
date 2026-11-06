# 物品属性、状态和交互

所有挂有 `DesktopWorldObject` 的感知物品都通过 `get_desktop_state.objects` 提供以下字段。角色继续使用独立的 `character` 状态。

- `properties`：只读语义属性，默认包含 category，可在 Inspector 添加用途、材质、偏好描述等。
- `state`：实时状态，默认包含 active、can_interact。组件适配器直接从功能组件读取状态。
- `interactions`：操作清单，包含 operation、description、available、unavailable_reason、allow_autonomous。
- 原有的坐标、朝向、包围盒、tags、capabilities、approach_points 和 file 元数据保持可用。

属性与状态均为 ObjectValue 数组。每项以 key 标识，按 type 读取 string 对应的 text、boolean 对应的 boolean、number 对应的 number，数值可附 unit。其他值字段忽略。自定义 key 请保持唯一，不要覆盖组件提供的 key。

## 调用

先读取实时快照，再调用 MCP 工具，例如：

```json
{"name":"interact_object","arguments":{"object_id":"lamp_01","operation":"turn_off"}}
```

等待 `state=succeeded`，结果的 `object_state` 是执行后的实际物品快照。无效 ID、隐藏物品、未提供或不可用的操作会失败。交互使用现有动作通道，忙碌时等待完成；需要靠近时先调用 approach_object，本版开关本身没有距离限制或按开关手势动画。request_id 可用于重试去重。

## 已接入功能

- 台灯：运行时为 objectType=lamp 的物品自动添加 DesktopLampInteraction。默认只绑定自身子物体中的 Light，不会关掉房间灯。turn_on / turn_off 修改真实 Light.enabled，state 包含 is_on、mixed、light_count、enabled_light_count。可手动挂组件并在 lights 中指定灯光，包括单独放置的灯光对象。此版不切换灯泡材质的自发光，也不控制实体硬件。
- 桌面文件：DesktopFileInteraction 接到已有 OpenExternal，检查文件身份与存在性后交由 Windows 默认应用打开。模型入口支持文件夹及常见文档、图片、音视频扩展名，脚本和可执行文件不提供可用操作。仅用户主动请求时使用，自主规划拒绝 open_external。打开不等于读取正文，测试不启动用户的真实文档应用。
- 普通摆件：具有统一属性与状态，暂无功能适配器的物品 interactions 为空。

## 增加真实功能

为物品挂一个继承 DesktopObjectBehaviour 的组件，覆盖 Properties、State、Interactions、Execute。State 从真实功能读取，Execute 调用明确的业务方法并返回成功或错误。DesktopWorldObject 自动收集同物体上已启用的适配器，无需给每种物品另写 MCP。禁止通过模型字符串反射任意 Unity 方法或直接改任意字段。

Inspector 的 properties / states 可编辑语义数据；动态功能应由适配器读回，不应只改一个状态字符串冒充操作成功。运行时变化本版不会跨 Play 会话保存。

更新后重启 JARVIS/MCP 以加载协议 v4，再进入 Unity Play。台灯和文件适配器会自动挂载，无需重新安装场景。
