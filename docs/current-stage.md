# 当前阶段：智能寻路 MVP + Farm P1 基础闭环

更新时间：2026-07-23

本文记录当前阶段目标、进度、已知缺口、验收标准和开发顺序。这里的内容会比 `AGENTS.md` 更频繁变化；稳定架构约束仍以 `AGENTS.md` 为准。

## 阶段目标

第一阶段主线仍是完成寻路模块。使用 `LLM_Node` 返回的模拟任务计划跑通完整闭环，暂不要求真实 LLM 生成可用计划。

本阶段的“智能寻路”不只是从起点走到终点，而是包括：

1. 根据模拟宏观计划完成跨地图路线分解。
2. 使用 SMAPI 导出的玩家位置、Warp 和障碍物状态进行局部 A* 寻路。
3. 动态避开不可破坏障碍，并在路径或状态变化后重新规划。
4. 识别可破坏障碍，选择正确工具和动作清除障碍，然后继续原路线。
5. 识别不可直接通行的门，走到交互位置、开门，并处理打烊或上锁反馈。
6. 仅在游戏状态确认到达目标地点后完成 `RouteTask`。
7. 遇到无路、工具缺失、体力不足、动作超时或门无法打开时安全停止，并暴露可恢复的失败原因。

在寻路闭环基础上，当前已经开始 Farm P1 基础能力：规划一片区域，按批处理阶段完成清障、锄地、播种和浇水。Farm 仍服务于“确定性技能执行”这条主线，不应扩展成完整农场日程规划。

## 当前运行模型

当前运行中存在两个相对独立的循环：

- State 通道：SMAPI Observer 高频导出游戏状态，Python Observer Client 更新 `PlayerContext.state`。这个通道不依赖行为树。
- 控制通道：`ValleyAgent` 每个 tick 先刷新 state，再轮询行为树节点。节点通过 `AgentBlackboard` 共享计划、进度和恢复信号。

当前顶层行为树：

```text
Selector
├── Sequence("Guard")
│   └── Defend_Node
├── Sequence("Route")
│   ├── OpenDoorNode
│   ├── SwitchToolNode
│   ├── ClearObstacleNode
│   └── RouteNode
├── Sequence("Chest")
│   └── ChestNode
├── Sequence("Farm")
│   ├── FarmResourceCheckNode
│   ├── SwitchToolNode
│   ├── ClearObstacleNode
│   ├── RefillWateringCanNode
│   └── FarmNode
└── Sequence("Think")
    └── LLM_Node
```

`Route`、`Chest`、`Farm` 和 `Think` 分支都是顶层 Selector 下的候选分支。`Think` 分支当前内部只有 `LLM_Node`，作为最后兜底：没有可执行计划时才生成模拟计划；有计划时让出控制权给前面的确定性节点。

`AgentBlackboard` 是跨节点通讯和调度状态中心，当前至少保存：

- `macro_plan`
- `current_step_index`
- `is_llm_thinking`
- `new_plan_received`
- `prompt`
- `require_open_door`
- `require_switch_tool`
- `require_clear_obstacle`
- `required_tool`
- `required_tool_owner`
- `clear_obstacle_owner`
- `clear_obstacle_tile`
- `clear_obstacle_type`
- `require_refill_watering_can`
- `refill_watering_can_owner`
- `refill_water_source_tile`
- `farm_resource_check_failed`
- `farm_missing_resources`
- `farm_resource_recovery_hint`

## 当前记忆与缓存模型

项目当前区分四层状态/缓存/记忆：

1. `Realtime State`：每帧事实，例如玩家位置、当前工具、`UsingTool`、`CanMove`。
2. `State Snapshot Cache`：性能缓存，例如 `obstacles`、`FarmTiles` 低频刷新；C# 没刷新时发 `null`，Python 复用上一份。
3. `MapKnowledgeCache`：当前运行期地图知识，例如 Farm 水源。后续可记录路上看见但暂不处理的觅食物、箱子和交互点。
4. `PersistentMemoryStore`：长期记忆预留接口，当前不实现、不调用；未来用于跨运行保存稳定线索。

当前水源不作为每帧 state 高频字段同步。Farm 需要补水时，`RefillWateringCanNode` 先查 `MapKnowledgeCache`，没有缓存时通过 C# `QUERY_WATER_SOURCES` 低频查询一次，并把结果写回缓存。

## 当前寻路与移动模型

当前移动控制已经从“Python 单次按键”调整为“Python 决策方向 + C# 持续保持方向”：

- Python `RouteNode` 缓存跨地图路线、当前 `tile_path` 和 `path_index`。
- `HardcodedStardewMap` 已移动到 `agent/action/map/map.py`，负责硬编码场景连通图和最少场景跳数候选路线枚举。
- RouteNode 会按候选路线评分选择跨场景路线：场景跳数优先；未知后续距离时优先当前第一跳传送门距离；已知距离时优先总距离。
- A* 只在必要时执行：初始路径为空、场景变化、路径过期、未来路径阻塞、清障/开门后重规划等。
- `MoveController` 每 tick 根据最新 `State`、玩家位置、`player_size=(48, 32)` 和目标 tile 输出当前移动方向。
- C# `StardewExecutor` 会持续按住最后一次 MOVE 命令对应的方向；Python 发送新 MOVE 用于更新方向，发送 `IDLE` 用于停止移动。
- 原地转向使用 `FACE_DIRECTION`，不要用 `MOVE_*` 充当转向脉冲；否则 C# 会持续移动并可能导致站位抖动。
- 中间路径点进入后立即推进，不强制回到格子中心；路径末端才做更严格的身体盒进入判断。
- Town 等大场景未来路径被阻挡时，优先启动后台 A*；旧路径继续执行，只有障碍已经接近才停下等待。
- 后台 A* 结果切换前需要对齐当前玩家位置，避免使用过期起点导致人物回头。
- 绝路、目标 warp 不存在、路径放弃或交给兜底规划前，RouteNode 必须先发送 `IDLE`，否则 C# 会继续保持旧方向。
- 可破坏障碍当前包括 `Stone`、`Twig`、`Weeds` 和策略允许后的普通树 `Tree0` ~ `Tree5`。普通树清理成本较高，需要通过清障策略层确认；`FruitTree0` ~ `FruitTree5` 和 `TreeStump` 暂视为不可自动清理障碍。
- 不允许斜向破坏障碍物。A* 不应斜向进入可破坏障碍格；RouteNode 和 ClearObstacleNode 只有在玩家位于目标上下左右相邻格时才触发/执行清障。

## 当前交互站位模型

交互前的短距离站位已从 FarmNode 中抽到动作层 `PositioningController`。统一输入是：

- `candidate_stand_tiles`：业务节点求解出的候选站位集合。
- `tool_target_tile`：需要工具目标或交互目标对准的地块，可为空。

`PositioningController` 负责：

1. 过滤地图外和阻挡格。
2. 从候选站位中规划最近可达路径。
3. 使用 `MoveController` 输出连续移动命令。
4. 到达候选站位后发送 `FACE_DIRECTION` 原地转向。
5. 只有当 `state.tool_target.tile == tool_target_tile` 时返回 `READY`。

FarmNode 当前已接入这套模型：它只负责选择未浇水作物，并把作物上下左右相邻格作为 `candidate_stand_tiles`、作物地块作为 `tool_target_tile`。后续箱子、树、NPC、商店柜台、门和清障都应优先复用这套模型，而不是在节点内部重复维护路径缓存和转向逻辑。

## 当前工具动作等待模型

使用工具是跨帧动作，必须区别三种状态：

1. C# Executor 接受命令：例如 `USE_TOOL` 返回 `SUCCESS`，只表示命令被发出。
2. 游戏动画进行中：SMAPI state 中 `UsingTool=True` 或 `CanMove=False`。
3. 工具动作已收招：上一轮动作观察到 `UsingTool=True` 后，又看到 `UsingTool=False` 且 `CanMove=True`。

当前 C# Observer 已同步 `UsingTool`、`CanMove`、`IsPlayerFree` 和 `CanPlayerMove`。C# Executor 在玩家忙碌时会对移动、转向、切工具、使用工具/物品返回 `BUSY`，避免 Python 在动画期间叠加输入。

Python 端原则：

- `ClearObstacleNode` 和 `FarmNode` 使用 `ToolActionTracker` 等待工具动作从开始到收招。
- C# 返回 `BUSY` 时，不增加尝试次数，不启动工具等待，保持节点 `RUNNING`。
- 工具收招后必须用最新 state 验证结果，例如障碍是否消失、地块是否成为 HoeDirt、作物是否 `IsWatered=True`。
- Farm P1 的 `WATER_TILES` 阶段把临时失败地块放入浇水重试队列。只要地块仍然 `HasCrop=True` 且 `IsWatered=False`，就不应因为一次站位卡顿或动作未命中直接永久跳过。
- 锄地、播种和清障阶段仍需要有限重试与明确失败原因，避免无限循环。

## 当前 Farm P1 模型

`FarmNode` 当前支持：

- `WATER`：自动选择未浇水作物，或按 `target_tiles` 给指定地块浇水。
- `PLANT`：规划区域后批量清障、锄地、播种。
- `PLANT_AND_WATER`：规划区域后批量清障、锄地、播种，最后批量浇水。

批处理阶段顺序：

```text
CLEAR_OBSTACLES -> HOE_TILES -> PLANT_SEEDS -> WATER_TILES -> DONE
```

当前障碍策略：

- 普通树 `Tree0` ~ `Tree5`：Farm 规划区域默认视为 Agent 已授权，可使用 Axe 清理，并在清理后继续锄地、播种、浇水。
- `FruitTree0` ~ `FruitTree5`、`TreeStump`：跳过该格，不纳入种植。
- `Grass`：使用 `Scythe`。
- `Weeds` / `Twig`：使用 `Axe`。
- `Stone`：使用 `Pickaxe`。

Farm 分支复用黑板中的工具切换和清障信号，通过 `required_tool_owner="Farm"` 和 `clear_obstacle_owner="Farm"` 区分调用来源。浇水阶段会维护 `_failed_water_tiles`、重试次数和重试时间，避免临时失败导致提前结束。

Farm 资源检查闭环：

1. `FarmResourceCheckNode` 在 FarmTask 执行前检查当前背包/工具栏 state。
2. `WATER` 至少要求 Watering Can，并验证水壶存在时有 `WaterLeft` / `WaterCapacity` state。
3. `PLANT` 至少要求 Hoe、目标种子，以及规划区域内清障所需工具。
4. `PLANT_AND_WATER` 同时要求 Hoe、Watering Can、目标种子和清障工具。
5. 若工具或种子不在背包/工具栏里，节点只判定“当前背包缺失”，不会直接猜测或操作箱子；它会安全 `IDLE`，把 `farm_missing_resources` 和 `farm_resource_recovery_hint` 写入 blackboard，并清空当前计划以触发恢复规划。
6. 未来箱子取物应由独立 Chest 节点根据这些缺口去查询箱子、移动到箱子旁并取回资源。

Farm 水壶补水闭环：

1. C# Observer 在 `Items` 中为 Watering Can 导出 `WaterLeft` 和 `WaterCapacity`。
2. FarmNode 准备浇水前检查当前水壶 `WaterLeft`；若 `WaterLeft <= 0`，发送 `IDLE` 并通过 blackboard 触发补水。
3. RefillWateringCanNode 读取/查询 Farm 水源，使用 `PositioningController` 站到水源上下左右相邻可达格并面向水源。
4. 节点发送 `USE_TOOL` 接水，使用 `ToolActionTracker` 等待收招，再通过下一帧水壶 state 验证 `WaterLeft > 0`。
5. 补水成功后清理 blackboard 标记，FarmNode 回到原浇水目标继续执行。

## 当前 Chest P0/P1 模型

`ChestNode` 当前支持指定箱子批量取物和批量存物：

```text
RouteTask("前往箱子所在场景") -> ChestTask("从指定 chest_tile 批量取 items")
RouteTask("前往箱子所在场景") -> ChestTask("向指定 chest_tile 批量存 items")
```

执行约束：

1. `ChestTask` 必须指定 `target_loc`、`chest_tile`、`chest_action` 和 `items`。`TAKE` 时，`items` 中每一项表示背包至少需要拥有的目标物品数量；如果背包里已经全部足够，节点会直接完成，不再强行开箱取物。`PUT` 时，`items` 表示尝试从背包存入箱子的物品清单；可堆叠物品不足请求数量时允许部分存入。旧的 `item_name` / `count` 单物品字段仍保持兼容，但新用例优先使用批量 `items`。
2. Python 端先发送 `QUERY_CHESTS` 校验箱子坐标；如果指定坐标不存在但当前场景只有一个箱子，会自动改用这个唯一箱子的真实坐标。
3. Python 端复用 `PositioningController`，将玩家移动到箱子上下左右相邻可达格，并通过 `FACE_DIRECTION` 面向箱子。
4. 取物前会额外确认玩家身体稳定进入相邻格，尽量靠近箱子，避免刚踩进邻格就交互导致无法打开箱子。
5. Python 端发送 `OPEN_CHEST` 打开箱子界面，并等待 0.5 秒让界面稳定。
6. Python 端根据 `chest_action` 发送 `TAKE_ITEMS_FROM_CHEST` 或 `PUT_ITEMS_TO_CHEST` 结构化命令，一次性批量转移，不使用鼠标，不拖拽 UI。
7. C# Executor 要求玩家位于当前场景、与箱子上下左右相邻、玩家没有处于工具动作状态；箱子菜单打开导致的 `CanMove=False` 不会阻止结构化取物。
8. C# 端在 `Chest.Items` 和 `Game1.player.Items` 之间批量转移物品，返回每个物品的 `transferred_count`、`status` 和 `reason`。
9. Python 端发送 `CLOSE_MENU` 关闭箱子界面，再等待下一帧背包 state：`TAKE` 验证数量增加，`PUT` 验证数量减少，验证通过后才推进 `current_step_index`。

当前暂不支持查询箱子内容、自动选择箱子，也暂未把 FarmResourceCheckNode 的缺资源恢复自动转成 ChestTask；这些内容记录在 `docs/next-development-plan.md`。

## 当前进度

| 能力 | 状态 | 当前说明 |
| --- | --- | --- |
| 模拟宏观计划 | 已有基础 | `LLM_Node` 异步返回硬编码 `RouteTask` |
| 跨地图规划 | 已有基础 | `HardcodedStardewMap` 已迁移到 `agent/action/map/map.py`，可枚举候选路线；RouteNode 按传送门距离缓存做路线评分 |
| SMAPI 结构化感知 | 已有基础 | 导出地点、位置、Warp、局部障碍物、`CurrentToolIndex`、`CurrentToolbarIndex` 和 `Items` |
| 局部 A* | 已有基础 | 支持格子路径、硬障碍、目标 Warp 和可破坏障碍代价；普通树按高成本清障候选处理；已限制斜向清障路径 |
| 路径缓存与局部跟随 | 已有基础 | RouteNode 缓存 `tile_path` / `path_index`，MoveController 负责连续移动方向 |
| 交互站位控制 | 基础接入 | `PositioningController` 已接入 FarmNode，统一处理候选站位、ToolTarget 对准和 FACE_DIRECTION 转向 |
| 工具动作等待 | 基础接入 | Observer 导出 `UsingTool`/`CanMove`，Executor 忙碌时返回 `BUSY`，Python 通过 `ToolActionTracker` 等待收招后验证 state |
| 动态避障与重规划 | 已有基础 | 支持偏航、未来路径阻塞检测和后台 A*，仍需系统化测试 |
| 开门 | 部分完成 | 已有 Route/OpenDoor 协作，需要补齐异步等待和结果验证 |
| 工具切换 | 基础接入 | `SwitchToolNode` 已接入 Route 分支，可基于背包 state 发送 Tab/槽位键切换 Axe/Pickaxe |
| 破坏障碍物 | 基础接入 | A* 可标记 Stone、Twig、Weeds；RouteNode 触发清障，SwitchToolNode 切工具，ClearObstacleNode 使用工具并验证障碍消失 |
| Farm 浇水 | 基础接入 | FarmNode 可选择未浇水作物，复用 PositioningController 站到相邻格、对准 ToolTarget 后使用水壶；P1 浇水阶段已有临时失败重试队列 |
| Farm 资源检查 | 基础接入 | FarmResourceCheckNode 在 FarmTask 前检查背包/工具栏中的工具、种子和水壶 state；缺资源时写入恢复上下文，不直接操作箱子 |
| Farm 水壶补水 | 基础接入 | 水壶没水时触发 RefillWateringCanNode，按需查询并缓存 Farm 水源，站到水源旁接水后继续浇水 |
| Chest P0 指定取物 | 基础接入 | ChestNode 可用 `QUERY_CHESTS` 校验/恢复唯一箱子坐标，站到箱子旁，调用 SMAPI `TAKE_ITEMS_FROM_CHEST` 结构化动作批量取物，并用背包 state 验证数量增加 |
| Chest P1 指定存物 | 基础接入 | ChestNode 可调用 SMAPI `PUT_ITEMS_TO_CHEST` 结构化动作批量存物，支持部分存入并用背包 state 验证数量减少 |
| 地图知识缓存 | 基础接入 | `MapKnowledgeCache` 已作为 PlayerContext 的运行期地图知识缓存；当前用于水源，采集物/箱子等机会记忆后续接入 |
| Farm P1 批处理 | 开发中 | 支持区域规划、清障、锄地、播种、浇水的阶段流水线，仍需更多游戏内测试和失败恢复 |
| C# 持续移动 | 已有基础 | Executor 保持最后 MOVE 方向，Python 需用新方向/IDLE 显式更新或停止 |
| 真实 LLM 规划 | 后续阶段 | 第一阶段继续使用 mock 计划 |
| 完整自主游玩 | 长期目标 | 还需要背包、时间、体力、菜单、NPC 等状态与技能 |

## 已有基础

- `LLM_Node` 可异步返回模拟 `RouteTask`。
- `HardcodedStardewMap` 可做跨地图候选路线枚举，RouteNode 可根据传送门距离选择路线。
- SMAPI Observer 可导出地点、玩家位置、Warp、局部障碍物和基础背包/工具栏状态。
- 本地 A* 支持格子路径、动态路径过期检测、偏航检测和重新计算。
- RouteNode 已缓存 `tile_path` 和 `path_index`，并通过 MoveController 做局部跟随。
- PositioningController 已抽象交互站位，FarmNode 已接入候选站位和工具目标地块模型。
- ToolActionTracker 已接入 ClearObstacleNode 和 FarmNode，用于等待工具动作收招。
- C# Executor 已支持保持最后移动方向，改善低频命令下的蠕动问题。
- RouteNode 失败路径已开始显式发送 `IDLE`，避免绝路后继续沿旧方向移动。
- Route/OpenDoor/SwitchTool/ClearObstacle 之间已有黑板标志协作。
- C# Executor 已支持基础移动、开门、关闭对话、切换工具和使用工具。
- C# Observer 已同步 `UsingTool`、`CanMove`、`IsPlayerFree` 和 `CanPlayerMove`，用于判断工具动画与玩家控制权。
- C# Observer 已在背包 `Items` 中导出水壶 `WaterLeft` / `WaterCapacity`。
- C# Executor 已支持 `QUERY_WATER_SOURCES`，可按需扫描指定场景 `Back` 层 `Water` tile 并返回水源坐标。
- `agent/memory/` 已加入运行期 `MapKnowledgeCache` 和长期记忆预留接口。

## 当前缺口

- `ValleyAgent.invoke(task)` 保存了原始任务，但尚未稳定注入 Planner Prompt；第一阶段可继续使用 mock 计划。
- `SwitchToolNode` 已有基础切工具流程，但仍需要更多游戏内验证和异常恢复策略。
- `ClearObstacleNode` 已能验证当前工具并使用工具，但体力检查、工具等级、背包掉落容量和失败恢复仍需完善。
- 工具动作等待已经接入，但仍需要更多真实场景验证：不同工具、不同动画长度、体力耗尽、命中失败和背包拾取等状态都可能影响结果判断。
- `PositioningController` 目前已接入 FarmNode 和 ChestNode；清障、NPC、商店柜台等交互还需要逐步迁移到同一模型。
- 普通树已纳入策略允许后的可清障目标；`FruitTree` 和 `TreeStump` 暂不纳入自动清障目标。
- `OpenDoorNode` 仍有异步路径使用 `time.sleep()`、结果验证不足等问题。
- `StardewExecutorClient.send_command()` 是阻塞式等待响应，缺少可靠超时和结构化 Action Result。
- Python 端当前仍会每 tick 重发当前移动方向；未来可优化为仅在方向变化、IDLE 或交互动作时发送，但必须保证安全停机语义不变。
- 高层场景连通图仍是硬编码数据，建筑入口和特殊路线需要持续校验；错误边会导致在当前场景查找不存在的目标 warp。
- SMAPI 快照仍缺少完成自主游玩需要的时间、金钱、体力、工具栏、背包、菜单、天气、NPC 和动作结果等状态。
- Farm P1 目前还是基础闭环，已加入基础资源检查，但仍缺少体力检查、背包容量检查、区域选择策略、作物阶段识别、失败后的二次规划和完整验收测试。
- Python 动作枚举比 C# Executor 实际支持的动作更多，两侧能力尚未完全对齐。
- `server/valley_server.py` 仍含旧 demo 逻辑，不要继续在 demo 路径上扩展正式能力。

## 第一阶段开发顺序

1. 保持 `LLM_Node` mock 计划稳定，确保黑板能够连续消费多个 `RouteTask`。
2. 继续校验硬编码场景连通图和 warp 目标名称，避免错误跨场景边导致目标 warp 不存在。
3. 继续验证 A* 障碍代价函数，区分不可通行、可绕行和可破坏障碍，并保持“不允许斜向清障”的路径约束。
4. 完善 `SwitchToolNode` 和 `ClearObstacleNode` 的游戏内验证、体力检查、工具等级和失败恢复。
5. 继续验证工具动作等待机制，确保 `UsingTool` / `CanMove` 和 state 结果验证足以覆盖清障、锄地、浇水。
6. 将清障、开门和后续 NPC/商店柜台等交互逐步迁移到 `PositioningController` 的候选站位 + 工具目标地块模型。
7. 强化玩家朝向、清障动作、超时与障碍消失验证。
8. 完善 Farm P1 的体力/背包容量检查、区域选择和失败恢复。
9. 完善 `OpenDoorNode` 的非阻塞状态机和门结果验证。
10. 增加确定性寻路/Farm 场景测试与游戏内端到端验收。

## 第一阶段验收标准

- `LLM_Node` 的模拟数据能稳定写入黑板并驱动多个连续 `RouteTask`。
- Agent 能跨至少两个地图完成导航。
- 固定障碍场景中，Agent 能绕开硬障碍并在动态阻塞后重新计算路径。
- 可破坏障碍挡住必要路径时，Agent 能完成“识别障碍 -> 切换正确工具 -> 执行动作 -> 验证障碍消失 -> 继续移动”。
- 工具动作必须通过 state 确认收招和结果变化，不能仅凭 Executor 返回 `SUCCESS` 判定完成。
- Farm P1 测试中，规划区域内可种植地块能完成“清障 -> 锄地 -> 播种 -> 浇水”；普通树会被清理，果树和 TreeStump 等不可处理地块会被明确跳过。
- 关闭但可进入的门能被打开；打烊或上锁能产生明确失败或恢复信号。
- 任务成功由最新 SMAPI 状态验证，不能仅以命令已发送或路径列表为空作为成功依据。
- 绝路、目标 warp 不存在或需要兜底恢复时，必须先发送 `IDLE`，人物不能继续保持旧方向移动。
- 核心场景具有可重复的测试记录，包括完成时间、重规划次数、动作次数和失败原因。

## 建议测试场景

- 无障碍跨地图导航。
- 路径中临时出现硬障碍，Agent 能重新规划绕行。
- 必经路径被石头、树枝或杂草挡住，Agent 能清除后继续。
- 规划一片 Farm 区域，包含 Grass、Weeds、Twig、Stone、普通树、果树和树桩，Agent 能清理可处理障碍、跳过果树/TreeStump、锄地、播种并浇水。
- 工具动作期间连续 tick 验证：Executor 返回 `BUSY` 时 Python 不叠加新动作，动作收招后再验证结果。
- 建筑门关闭但可进入，Agent 能开门并完成 Warp。
- 门打烊或上锁，Agent 能停止并提供明确失败原因。
- 完成连续多个 `RouteTask`，并由最终地点状态确认成功。

建议记录以下指标：

- 是否成功到达目标。
- 总耗时和动作数。
- A* 重规划次数。
- 清障和开门重试次数。
- 失败类型与最终游戏状态。

## 当前不做

- 不把购买、种田、完整日程规划作为第一阶段主线。
- 不把真实 LLM 规划质量作为第一阶段验收核心。
- 不把截图/VLM 作为第一阶段主要感知来源。
- 不继续扩展 `server/valley_server.py` 中的旧 demo 路径作为正式能力。
