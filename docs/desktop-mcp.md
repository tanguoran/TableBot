# 大肥鱼桌面 MCP

Unity 是场景与执行结果的权威来源。WSL 运行 Python Agent、Ollama 和 MCP 服务；Windows Unity 通过本机 HTTP 与桥接层交换数据。无需新增 Python 依赖。

## 直接使用现有本地模型

1. Unity 打开 `desktop/ds_desktop/Assets/Scenes/WhaleDesk.unity`，点击 Play。
2. 在项目根目录的 PowerShell 运行 `./wsl.ps1`（已有 CLI 需重启一次以加载新代码）。
3. 在 Unity Game 视图底部输入“走到笔记本电脑旁边”，点击发送或按 Ctrl + Enter。回复显示在角色头顶，可滚动阅读或点击 × 收起。终端也能继续对话，两个入口共享同一会话。Unity 聊天使用本机端口 18766。
4. 自主行为默认开启；`/auto status` 查看当前与后续动作。聊天后安静 30 秒会自动恢复。

CLI 会启动 MCP stdio 子进程，通过 `tools/list` 发现桌面工具，再把 schema 提供给 Ollama。MCP 服务没有发现独立桥接服务时，会在自己进程中启动桥接线程。Unity 约每 0.2 秒交换一次快照与命令；后台运行已开启。

可选配置（`.env` 不填时也使用这些默认值）：

```ini
JARVIS_DESKTOP_ENABLED=true
JARVIS_DESKTOP_BRIDGE_URL=http://127.0.0.1:18765
```

设为 `false` 可以只使用原有聊天工具。Unity 尚未运行或快照超过 3 秒没有更新时，工具会明确返回离线；不会把旧位置当成实时状态。

Unity 的 Esc 始终可以直接停止行动。当前 CLI 在一次回复期间同步等待，不能在同一个输入提示中同时输入另一条指令；另一个 MCP 连接可以并发调用 `cancel_move`。

## 连接其他 MCP 客户端

使用根目录 `desktop-mcp.example.json` 的 `mcpServers.whale-desktop` 配置。它是普通 MCP stdio 服务，支持 `initialize`、`notifications/initialized`、`ping`、`tools/list`、`tools/call`；工具结果同时提供 `content` 和 `structuredContent`。

命令等价于：

```powershell
wsl.exe --distribution Ubuntu --cd 'C:\Users\user\Documents\个人资料\持续学习\代码\TableBot' --exec python3 -m jarvis.desktop_mcp
```

不要手工在该进程的 stdout 打印调试信息；stdout 只承载 MCP JSON-RPC 消息。参考 [MCP stdio 规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) 与 [工具规范](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)。

多个 MCP 客户端共用一个 Unity 场景时，建议先独立运行桥接服务：

```powershell
./wsl.ps1 -Action desktop-bridge
```

之后每个 MCP 进程会连接这个桥接服务。独立桥接服务用 Ctrl+C 退出。如果由某个 MCP 进程临时托管桥接，该进程退出也会关闭它；其他客户端需重连。

## 大肥鱼状态与物件状态分离

`DesktopCharacterState` 挂在 `大肥鱼 - Action Root`，输出专用 `character`：

- `character_id`、名称、实际位置、朝向、体型半径与高度。
- `navigation_ready`、`movement_state`、实际速度。
- `traits` 保存 Inspector 中的好奇倾向和休息阈值。`simulated_needs` 在本次 Play 内更新：移动和跳跃消耗精力，坐姿加快恢复；空闲时探索需求上升，移动或关注不同物件会降低它。两项均使用 0–1 范围。`rest_suggested` 在精力低于阈值时开启，恢复到阈值以上 0.2 后解除。
- `idle_seconds` 表示身体连续空闲的游戏秒数，`body_action_ready` 表示当前可以开始新的身体动作。
- `animation_state`、`current_action`、`action_id`、是否有目标及目标坐标、最近错误。
- `requested_target`、`target_adjusted`：最近寻路请求的原始目标及是否因桌外目标而修正。
- `posture`（standing / sitting / airborne）、`action_phase`（执行阶段）、`vertical_offset`（跳跃视觉高度）、`visual_position`。
- `control_revision`：鼠标/Esc 接管计数。顶层 `control` 提供 MCP 控制版本与最近动作 ID，供调度器识别其他连接的操作。

大肥鱼不使用 `DesktopWorldObject`，也不会出现在普通 `objects` 列表。物件筛选永远保留完整 `character`。不会编造饥饿值、心情或尚未实现的内部状态。

`DesktopWorldObject` 挂在实际需要感知的物件根节点。目前已标记 12 个逻辑物件；桌面、墙壁、示范人偶等不逐个暴露。右侧两本叠放的书作为一个书堆。物件启用时注册、禁用时注销，不为取状态反复扫描所有场景节点。

启用真实桌面映射后，运行时隐藏占位书本/便签并生成文件实体；`objects` 只包含当前页的可见实体，`desktop_files` 提供完整目录和分页/同步状态。文件实体附带 `file` 元数据，路径由 Windows 绑定层保管。详见 [桌面文件同步](desktop-files.md)。

Inspector 属性：

| 属性 | 说明 |
|---|---|
| Object Id | 稳定且唯一的 ID；复制物件后要改成不同 ID |
| Display Name / Object Type | 模型可理解的名称和类型 |
| Tags | 筛选标签 |
| Include In State | 是否暴露给模型；取消不影响物理避障 |
| Can Approach | 桌内物件是否提供接近站位；桌外物件自动提供最近可达的桌面站位 |
| Approach Points | 可选的命名站位 Transform；留空自动生成前后左右站位 |

物件的中心位置、朝向、世界包围盒实时计算；`is_obstacle` 来自独立的 `DesktopObstacle`。自动站位根据角色半径留出间距，并由 NavMesh 检查可达性。暴露实际实现的接近和观察能力。

手动站位的名称可用 front/back/left/right；其他名称可由 `nearest` 自动选择。front 是世界 -Z，back 是 +Z，left 是 -X，right 是 +X。

## 工具

通用物品属性、实时状态与 `interact_object` 接口见 [物品交互](object-interactions.md)。已接入台灯真实灯光开关与用户请求的外部文件打开。

| 工具 | 参数 | 结果 |
|---|---|---|
| `get_desktop_state` | 可选 types、tags、max_distance | 实时 character + 筛选后的 objects + 桌面边界 |
| `set_expression` | emotion（happy、angry、sad、relaxed、neutral），可选 intensity（0–1，默认 0.7）、duration_seconds（0.5–20，默认 3）、request_id | Unity 接受后立即返回，表情与身体动作并行，持续时间结束后淡回自然状态 |
| `move_to` | x、z；可选 heading_degrees、timeout_seconds、request_id | 走到目标并转到指定朝向后，才返回 arrived |
| `approach_object` | object_id；可选 side、heading_degrees、timeout_seconds、request_id | 默认到达后朝向物体；指定角度可覆盖 |
| `cancel_move` | 可选 request_id | Unity 停止后返回 succeeded；被打断的动作返回 cancelled |
| `sit_down` | 可选 heading_degrees、timeout_seconds、request_id | 播放坐下过渡，完成后保持坐姿 |
| `stand_up` | 可选 timeout_seconds、request_id | 播放起立过渡后站立 |
| `observe` | 可选 object_id、heading_degrees、duration_seconds（1–20）、timeout_seconds、request_id | 朝向目标并注视；无目标时左右观察，可坐着执行 |
| `look_at` | 必选 target_type（camera/object/position）；物件需 object_id，坐标需 x/y/z；可选 turn_body、duration_seconds（1–20，默认5秒）、timeout_seconds、request_id | 通用头眼注视，允许原地转身或保持身体朝向，保持站姿/坐姿，不走路 |
| `jump` | 可选 heading_degrees、height（0.05–0.18米）、timeout_seconds、request_id | 原地起跳、腾空、落地；不支持越障 |

types 匹配任意给定类型，tags 必须全部匹配；max_distance 是物体中心到角色的 XZ 平面距离（米）。只筛选 objects。

大肥鱼在 Unity 中自动眨眼。可以说“开心地笑三秒”或“恢复自然表情”。实时的 `character.expression` 提供情绪名、剩余秒数、四种情绪的权重和眨眼权重。自主规划可以在 `propose_plan` 顶层附带 `expression` 对象，与同一计划的身体动作一起执行。

所有坐标是 Unity 世界坐标、单位米，+X 向右，+Z 远离观察者。Y 由桌面寻路设置确定，模型不提供途经点。请求参数严格校验，拒绝非有限坐标和未知参数。

桌外目标由 **Unity** 自动回退：在当前已考虑角色半径和障碍物的桌面 NavMesh 上，选择离原始目标最近、且能从角色当前位置完整走到的点。按目标距离选择，跳过被障碍物隔断的不可达区域；不会走到地板、家具顶面或墙上。此机制适用于 `move_to`、鼠标移动，以及 `approach_object` 的桌外站位。墙面任务板、时钟等桌外物件会自动暴露 `approach_object`，其站位 `side=nearest`，最终默认面向物件；桌外物件请求的其他 side 也回退到这个可达点。

移动结果包含 `requested_destination`（原始目标）、`destination`（实际终点）和 `destination_adjusted`（是否回退）。`state=arrived` 只表示实际抵达修正后的桌面位置；模型应说“走到桌面上靠近时钟的位置”，不能说走上墙。物件站位也返回 `requested_position` 和 `destination_adjusted`。桌内被占用/不可达的目标仍会失败；非法坐标、未就绪或没有可达桌面时返回失败原因，不会尝试越界。

`heading_degrees` 是世界 Y 轴角度：0 朝 +Z，90 朝 +X，180 朝 -Z（当前镜头/用户），270 或 -90 朝 -X。移动的完成条件包括角度误差小于 0.5°。工具结果包含实际 `heading_degrees` 和 `posture`。

定向注视统一用 `look_at`，可以说“看着我”“看向台灯”“身体别动，只转头看笔筒”“看向坐标 x=0、y=1.2、z=-0.5”。镜头、物件和世界坐标共用 `DesktopGazeController` 的目标解析、平滑转头、VRM 眼睛跟随及角度限制。`observe` 保留为观察/左右环视行为；带物件目标时也复用同一套注视控制。

```json
{"target_type":"camera","duration_seconds":3}
{"target_type":"object","object_id":"lamp_01","turn_body":false,"duration_seconds":3}
{"target_type":"position","x":0,"y":1.2,"z":-0.5,"turn_body":true}
```

`turn_body` 默认 true：原地转身面向目标，随后跟随移动的目标。设为 false 时保持身体朝向，只转头和眼睛；仍受头部角度限制，背后目标可能无法对准。物件必须出现在实时 `objects` 中；被删除、隐藏或翻页移除时动作失败。坐标是世界 XYZ，Y 表示注视高度，不使用寻路的桌面高度替代。目标类型只能选一种，坐标必须完整且有限。镜头模式不会开启摄像头；正交镜头使用平行视线方向。

`character.gaze` 返回 mode（camera/object/position/look_around/none）、target_object_id、tracking、limited、目标位置、头部角度与眼睛追踪可用状态；物件筛选不会移除它。`limited=true` 表示目标超出头部范围，`tracking=false` 表示当前无法追踪，不能据此声称完全对准。动作返回的 `gaze` 保留本次注视结束时的目标与状态；时限结束或取消后，实时注视恢复默认行为。旧 `look_at_camera` 调用仍兼容，但不再出现在模型工具列表中。

`DesktopGazeController` 已安装在角色行动根节点：默认轻微看向镜头，走路时降低转头幅度，显式注视物件/坐标时临时让出默认镜头目标。可在 Inspector 关闭 `Look At Camera When Idle` 或调整转头范围与跟随速度。

更新后需重启旧聊天 CLI，让工具列表加载新动作。若独立运行桌面桥接服务，也需重启该服务。Unity 在 Play 中重编译后若 `navigation_ready=false`，退出再进入一次 Play。

例如：`move_to({"x":0.5,"z":-0.45,"heading_degrees":180})`。不传角度的 move_to 保持走路结束时的朝向。

坐姿来自已导入的 Quaternius `Sitting_Enter / Sitting_Idle_Loop / Sitting_Exit`；走路和跳跃会先起立。跳跃使用 Jump_Start / Jump_Loop / Jump_Land，寻路根节点留在桌面，视觉节点沿弧线升降，取消时恢复落地。观察是转身和头部注视，不代表读取文件、识别屏幕内容。动画控制器可在 `Assets/WhaleDesk/Animation/WhaleLocomotion.controller` 查看八个状态。

## 自主行为

CLI 支持 `/auto on`、`/auto pause`、`/auto resume`、`/auto off` 和 `/auto status`，默认开启。`python3 -m jarvis --no-autonomous` 可本次关闭。共享上下文和自动恢复规则见 [说明](character-context.md)。

调度器每 0.5 秒检查执行结果与控制变化；两次规划至少间隔 10 秒。一次执行一个动作，最多保留两个后续步骤。模型只提交 `propose_plan`：继续、替换后续步骤，或等待 10–30 秒；不能自行打断正在执行的动作。`wait` 是调度器的可取消等待，不是 Unity/MCP 动作。

模型根据统一人设、近期对话和实际经历选择动作，已移除固定“观察→等待”的启动要求。没有当前动作或后续队列时，不能提交空的 continue。上一轮规划校验反馈会随下次快照提供给模型，避免持续重复无效计划。

规划线程和动作线程独立，执行较长动作时仍可更新后续安排。同一时间最多一个规划推理；输入包括新快照、当前动作、后续队列、最近八次结果和共享角色上下文。聊天和规划使用同一份人设与近期对话。计划经过参数、对象能力和队列长度校验，场景与用户控制变化会使旧推理结果失效。

用户提交普通对话/图片时先为聊天让出控制并停止自己的动作，回复结束后安静 30 秒自动恢复；新的对话会重置计时。明确说“停下”或使用 `/auto pause`、`/auto off` 后保持暂停，需主动恢复。Unity 鼠标/Esc 或检测到其他 MCP 控制也会保持暂停。让出控制时清空后续队列，恢复后从共享上下文重新规划。

聊天和自主规划共用推理锁，等待中的聊天优先。自主规划单次最多 768 token、20 秒，使用关闭思考的独立配置；已有聊天配置及思考开关保持原值。已进入 Ollama 的自主请求需返回或超时后才能交给聊天，不能立即中断模型服务器推理。

自主模式需 CLI 进程持续运行。退出 CLI 会停止自主行为，动作与队列不写入个人记忆。

同一时刻只执行一个动作；忙碌时新动作被拒绝，取消请求走独立通道。鼠标改目标会取消原 MCP 动作；鼠标/键盘控制仍可用。动作有超时和场景会话 ID；退出/重新进入 Play 后旧命令不能在新场景重放。复用 request_id 只能重试同一动作，近期完成结果缓存 256 条，不能把它当成永久存档。

当前场景只有一个角色，沿带体型余量的 NavMesh 路线绕过 carving 障碍；关闭了会在狭窄桌沿与该路线互相干扰的额外局部避障转向。叠放的书共用一个障碍范围。

## 验证与排查

另开 PowerShell 运行 `./monitor.ps1` 可监控聊天与自主规划的模型输出、工具执行结果、计划采用/拒绝/失效原因。更新后的 CLI 需要重启；详见 [模型监控](model-monitor.md)。

```powershell
./wsl.ps1 -Action test
./wsl.ps1 -Action desktop-smoke
# 同时验证真实 Ollama Agent 发出动作：
wsl.exe --distribution Ubuntu --cd 'C:\Users\user\Documents\个人资料\持续学习\代码\TableBot' --exec python3 -u -m jarvis.desktop_smoke --model
# 45秒真实自主规划验收，结束自动关闭：
wsl.exe --distribution Ubuntu --cd 'C:\Users\user\Documents\个人资料\持续学习\代码\TableBot' --exec python3 -u -m jarvis.autonomy_smoke --seconds 45
```

端到端测试需要 Unity 保持 Play，期间会让大肥鱼移动。覆盖角色/物件分离、类型/标签筛选、坐标移动、物体接近、并发取消、桌外目标回退、最终朝向、持续坐姿、坐姿观察、起立、跳跃/取消后落地；`--model` 另外调用当前选中的真实聊天模型。

Unity 的 `WhaleDeskDestinationTests` 还可通过 `Temp/WhaleDeskDestination.request` 写入 `test` 运行（需 Play）；报告位于 `Temp/WhaleDeskDestination.report.json`。该测试覆盖多方向/极远目标、最近可达点对比、墙面物件、实际到达及朝向，以及临时障碍分隔桌面后的连通区域选择，完成后移除测试障碍并返回原位置。

如果工具提示离线，确认 Play 未暂停、MCP/桥接进程仍在运行、Unity `07 - Local Model MCP` 的 Bridge Url 与 Python 配置一致。当前连接已按 Windows Unity → WSL 本机转发设计；不需要将 Ollama 或桥接端口开放到局域网。

更新到本版后需要重启旧 CLI/MCP 和独立的 desktop-bridge 进程；协议为 v2，旧桥接会明确提示重启。

菜单 `Whale Desk > Install Desktop MCP Integration` 可重新挂载集成；已经标记物件的 Inspector 自定义值会保留。场景重建也会安装集成。连接错误可在 `DesktopBridgeClient.LastConnectionError` 中查看。
