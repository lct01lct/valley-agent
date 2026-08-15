# 当前阶段：智能寻路 MVP + Farm P1 基础闭环

更新时间：2026-07-24

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

Farm P1 当前暂停继续扩展资源管理细节。已有 Farm 模块保留为基础农业技能；体力、背包容量、资源缺失恢复、复杂箱子搜索和工具归还失败等更适合在 Mining 模块中集中验证，后续再把稳定下来的通用能力回流 Farm。

当前 Mining 模块已经从“找到下一层”推进到“机会资源锚点 + 工具后处理 + 掉落物拾取 + Defend P1 最小版”。下一步重点不是完整采矿收益最大化，而是在矿井二层以后验证怪物干扰下的稳定性；随后再做体力、背包容量、资源采集和长期记忆。

## 当前运行模型

当前运行中存在两个相对独立的循环：

- State 通道：SMAPI Observer 高频导出游戏状态，Python Observer Client 更新 `PlayerContext.state`。这个通道不依赖行为树。
- 控制通道：`ValleyAgent` 每个 tick 先刷新 state，再轮询行为树节点。节点通过 `AgentBlackboard` 共享计划、进度和恢复信号。

当前顶层行为树：

```text
Selector
├── Sequence("Guard")
│   ├── UiGuardNode
│   ├── SwitchToolNode
│   └── Defend_Node
├── Sequence("CollectLoot")
│   └── CollectLootNode
├── Sequence("InventoryRecovery")
│   └── InventoryRecoveryNode
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
├── Sequence("Mining")
│   ├── MiningResourceCheckNode
│   ├── SwitchToolNode
│   └── MineNode
└── Sequence("Think")
    └── LLM_Node
```

`CollectLoot`、`InventoryRecovery`、`Route`、`Chest`、`Farm`、`Mining` 和 `Think` 分支都是顶层 Selector 下的候选分支。`CollectLoot` 在工具动作后消费 blackboard 中的近距离掉落物请求，只捡可达目标，不为拾取触发清障；当背包满且仍有真实掉落物无法接收时，它暴露恢复请求并让出控制权。`InventoryRecovery` 负责当前场景内的任务感知型背包整理，优先存箱，必要时丢弃任务无关物品。`Think` 分支当前内部只有 `LLM_Node`，作为最后兜底：没有可执行计划时才生成模拟计划；有计划时让出控制权给前面的确定性节点。

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
- `farm_missing_chest_items`
- `farm_resource_recovery_hint`
- `farm_recovery_task`
- `borrowed_chest_items`

## 当前记忆与缓存模型

项目当前区分四层状态/缓存/记忆：

1. `Realtime State`：每帧事实，例如玩家位置、当前工具、`UsingTool`、`CanMove`。
2. `State Snapshot Cache`：性能缓存，例如 `obstacles`、`FarmTiles` 低频刷新；C# 没刷新时发 `null`，Python 复用上一份。
3. `MapKnowledgeCache`：当前运行期地图知识，例如 Farm 水源、箱子位置、箱子内容快照和箱子语义记忆。后续可记录路上看见但暂不处理的觅食物和交互点。
4. `PersistentMemoryStore`：长期记忆预留接口，当前不实现、不调用；未来用于跨运行保存稳定线索。

当前水源不作为每帧 state 高频字段同步。Farm 需要补水时，`RefillWateringCanNode` 先查 `MapKnowledgeCache`，没有缓存时通过 C# `QUERY_WATER_SOURCES` 低频查询一次，并把结果写回缓存。

`Debris` 是工具动作后可能快速变化的动态掉落物事实，当前由 C# Observer 轻量高频同步到 Python `state.debris`。它用于工具动作后处理层判断目标附近是否出现掉落物；近距离可达掉落物已由 `ToolAftermathService` 登记，并交给 `CollectLootNode` / `LootPolicyService` 处理低成本局部贪心拾取、延迟拾取、磁吸覆盖和拾取验证。当前 `CollectLootNode` 已收敛背包满场景：可堆叠掉落物继续拾取；可堆叠目标已尽量拾取后仍有真实掉落物无法接收时，暴露背包恢复请求并交给 `InventoryRecoveryNode`；普通树掉落物使用更宽的磁吸候选站位集合，降低明明可磁吸却被误判不可达的概率。

当前 Debris state 已按“只同步真实可拾取物品”收紧：纯视觉碎屑、身份不完整的 Debris 和 `Weeds/(O)0` 不进入 Python state。C# `DebrisStateScanner` 已补充读取 `Debris.itemId.Value`，矿石/硬木等 `source=OBJECT` 掉落物即使没有 `debris.item` / `chunk.item`，也可以解析出真实物品身份；已通过日志确认 Copper Ore / 铜矿石 `(O)378` 可以从 C# 传到 Python，并触发 `CollectLootNode`。

箱子相关知识分为三类：`ChestContentKnowledge` 是打开箱子后得到的内容事实；`ChestSemanticMemory` 是“工具箱/种子箱”等用途倾向，只能作为候选推荐；`borrowed_chest_items` 是本轮任务级借用账本，用来把借出的工具还回原箱子。

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
- 可破坏障碍当前包括 `Stone`、`Twig`、`Weeds` 和策略允许后的普通树 `Tree0` ~ `Tree5`。普通树清理成本较高，需要通过清障策略层确认；砍普通树时同一目标地块残留的普通树桩会继续清理，独立规划目标中的 `TreeStump` 仍暂视为不可自动清理障碍。
- 箱子、机器和其他普通地图对象由 SMAPI Observer 进入 `Object` 层；A* 将 `Object` 作为硬障碍处理。ChestNode 需要交互箱子时，只允许站到箱子上下左右相邻格，不会把箱子 tile 纳入可站立路径。
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
5. 对普通工具目标，只有当 `state.tool_target.tile == tool_target_tile` 时返回 `READY`。
6. 对矿洞入口、梯子、箱子等 ActionTile / 交互对象，`ToolTarget` 对准只说明方向正确，不代表交互按钮一定可触发；还必须确认人物在相邻格内足够贴近交互范围。

交互站位控制已支持未来路径阻塞检查。Mining 远距离站位路径如果因局部 state 后续发现 `Wall`、`MiningNode`、`Object`、木箱/木桶等障碍，会让缓存路径失效并重新 A\*。

FarmNode 当前已接入这套模型：它只负责选择未浇水作物，并把作物上下左右相邻格作为 `candidate_stand_tiles`、作物地块作为 `tool_target_tile`。后续箱子、树、NPC、商店柜台、门和清障都应优先复用这套模型，而不是在节点内部重复维护路径缓存和转向逻辑。

Mining P0 实测经验：从 Y 轴进入矿洞入口/梯子时，人物可能因碰撞停在理想贴近点前 1~2 像素。贴近判断应使用 X/Y 轴统一的小范围像素容忍区，避免持续向入口/梯子方向 MOVE；但不能仅因 `ToolTarget` 已对准就跳过贴近判断。

## 当前工具动作等待模型

使用工具是跨帧动作，必须区别三种状态：

1. C# Executor 接受命令：例如 `USE_TOOL` 返回 `SUCCESS`，只表示命令被发出。
2. 游戏动画进行中：SMAPI state 中 `UsingTool=True` 或 `CanMove=False`。
3. 工具动作已收招：上一轮动作观察到 `UsingTool=True` 后，又看到 `UsingTool=False` 且 `CanMove=True`。

当前 C# Observer 已同步 `UsingTool`、`CanMove`、`IsPlayerFree` 和 `CanPlayerMove`。C# Executor 在玩家忙碌时会对移动、转向、切工具、使用工具/物品返回 `BUSY`，避免 Python 在动画期间叠加输入。

当前 C# Observer 也已同步当前场景 `Debris` 快照，Python 端解析为 `state.debris`。`ToolAftermathService` 会在工具收招后记录目标附近的掉落物 tile，作为后续拾取策略的输入。

Python 端原则：

- `ClearObstacleNode` 和 `FarmNode` 使用 `ToolActionTracker` 等待工具动作从开始到收招。
- C# 返回 `BUSY` 时，不增加尝试次数，不启动工具等待，保持节点 `RUNNING`。
- 工具收招后必须用最新 state 验证结果和副作用，例如障碍是否消失、范围内障碍是否减少、地块是否成为 HoeDirt、作物是否 `IsWatered=True`、掉落物是否出现、梯子是否出现或是否出现阻塞 UI。
- 当前 `ToolAftermathService` 的工具效果等待窗口为 `1.0s`。它不是固定等待时间：如果 state 已经证明目标完成或有效副作用发生，节点应立即推进；只有工具收招后 1 秒内完全没有观察到预期效果或副作用，才进入超时、重试或失败判断。
- 范围工具当前按“精确目标 + 副作用”双层判断。Scythe / 剑清理 Grass / Weeds 时，如果目标格没有立刻消失，但作用范围内预期障碍减少，或目标附近出现可拾取掉落物，也视为本次工具动作已经有效，避免因为要求目标格一次命中而原地等待。
- 破坏类目标当前按“目标消失后才登记掉落物”处理。以铜矿为例，前几次挥镐后 MiningNode 仍存在时只视为受击或视觉碎屑阶段，不登记掉落物；最后一次挥镐后目标从 state 消失，才扫描目标附近可拾取 `Debris` 并登记给 `CollectLootNode`。
- Mining 破石时即使工具动画期间刷新了梯子，也不能直接抢先进入 Ladder 阶段；必须先完成当前 Pickaxe 动作收招、统一后处理和掉落物登记，再决定拾取或下梯子。
- MineNode 交互梯子前会先结算当前层拾取需求：包括正在执行的 `require_collect_loot`、延迟拾取队列，以及最近一次工具目标附近短窗口内出现的可拾取掉落物。
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
- `FruitTree0` ~ `FruitTree5`、独立规划目标中的 `TreeStump`：跳过该格，不纳入种植；普通树砍倒后同一地块残留的树桩会作为当前清树动作的一部分继续砍完。
- `Grass`：使用 `Scythe`。
- `Weeds` / `Twig`：使用 `Axe`。
- `Stone`：使用 `Pickaxe`。

Farm 分支复用黑板中的工具切换和清障信号，通过 `required_tool_owner="Farm"` 和 `clear_obstacle_owner="Farm"` 区分调用来源。浇水阶段会维护 `_failed_water_tiles`、重试次数和重试时间，避免临时失败导致提前结束。

Farm 资源检查闭环：

1. `FarmResourceCheckNode` 在 FarmTask 执行前检查当前背包/工具栏 state。
2. `WATER` 至少要求 Watering Can，并验证水壶存在时有 `WaterLeft` / `WaterCapacity` state。
3. `PLANT` 至少要求 Hoe、目标种子，以及规划区域内清障所需工具。
4. `PLANT_AND_WATER` 同时要求 Hoe、Watering Can、目标种子和清障工具。
5. 若工具或种子不在背包/工具栏里，节点只判定“当前背包缺失”，不会直接猜测或操作箱子；它会安全 `IDLE`，把 `farm_missing_resources`、可转成 ChestTask 的 `farm_missing_chest_items`、`farm_resource_recovery_hint` 和原始 `farm_recovery_task` 写入 blackboard，并清空当前计划以触发恢复规划。
6. 当前 mock Planner 可把这些缺口转成 `RouteTask + 多个 ChestTask(TAKE, chest_tile=None) + 原 FarmTask + ChestTask(PUT, chest_tile=None)`：工具会合并成一组取物任务，种子等堆叠物按物品拆成独立取物任务，避免错误要求“同一个箱子同时拥有所有缺失资源”。ChestNode 会先查缓存，缓存缺失时按需打开当前场景箱子查看；已知新鲜且不满足当前取物需求的箱子会跳过，找到满足当前取物任务的箱子后立即取物，不继续翻看其他箱子。Farm 完成后，借出的工具按 `borrowed_chest_items` 记录归还到原箱子；种子默认不归还。

Farm 水壶补水闭环：

1. C# Observer 在 `Items` 中为 Watering Can 导出 `WaterLeft` 和 `WaterCapacity`。
2. FarmNode 准备浇水前检查当前水壶 `WaterLeft`；若 `WaterLeft <= 0`，发送 `IDLE` 并通过 blackboard 触发补水。
3. RefillWateringCanNode 读取/查询 Farm 水源，使用 `PositioningController` 站到水源上下左右相邻可达格并面向水源。
4. 节点发送 `USE_TOOL` 接水，使用 `ToolActionTracker` 等待收招，再通过下一帧水壶 state 验证 `WaterLeft > 0`。
5. 补水成功后清理 blackboard 标记，FarmNode 回到原浇水目标继续执行。

## 当前 Chest P0/P1/P2/P3 模型

`ChestNode` 当前支持指定箱子批量取物、批量存物、交互式打开箱子建立内容缓存，以及在当前 `target_loc` 内自动选择满足取物需求的箱子：

```text
RouteTask("前往箱子所在场景") -> ChestTask("从指定 chest_tile 批量取 items")
RouteTask("前往箱子所在场景") -> ChestTask("向指定 chest_tile 批量存 items")
RouteTask("前往箱子所在场景") -> ChestTask("SCAN 当前场景箱子，逐个打开查看并缓存")
RouteTask("前往箱子所在场景") -> ChestTask("TAKE 且 chest_tile=None，自动选箱取物")
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

Chest P2/P3 约定：

1. `SCAN` 会先低频查询 `target_loc` 当前场景中的箱子坐标，然后逐个移动到箱子旁、打开箱子、查看内容、写入 `MapKnowledgeCache` 并关闭箱子；不要通过底层代码直接遍历所有箱子内容。
2. `QUERY` 用于打开查看指定箱子内容并写入缓存。
3. `TAKE` 允许 `chest_tile=None`；此时 `ChestNode` 先查当前运行期缓存。缓存没有正命中时，只在 `target_loc` 当前场景内按需打开候选箱子查看：未知箱子优先，已知新鲜且不满足当前物品需求的箱子会跳过，过期且不匹配的箱子只作为兜底候选。找到满足目标物品的箱子后停止，并在当前打开的箱子中取物。
4. 跨场景找箱子不属于 ChestNode 职责；应由 Planner/LLM 结合记忆生成 `RouteTask + ChestTask`。
5. 取放成功后，Python 会保守地把对应箱子内容缓存标记为过期，后续需要时重新查询。
6. 对 Farm 资源恢复中借出的工具，`ChestNode` 会记录来源箱子；后续 `PUT chest_tile=None` 优先使用该借用记录解析归还箱子。没有借用记录时，未来可用 `tool_chest` 语义记忆作为兜底推荐，但仍必须执行真实存物动作。

当前已把 FarmResourceCheckNode 的缺资源恢复基础接入 mock Planner；真实 Planner 和更复杂的跨场景恢复仍记录在 `docs/next-development-plan.md`。

## 当前进度

| 能力 | 状态 | 当前说明 |
| --- | --- | --- |
| 模拟宏观计划 | 已有基础 | `LLM_Node` 异步返回硬编码 `RouteTask` |
| 跨地图规划 | 已有基础 | `HardcodedStardewMap` 已迁移到 `agent/action/map/map.py`，可枚举候选路线；RouteNode 按传送门距离缓存做路线评分 |
| SMAPI 结构化感知 | 已有基础 | 导出地点、位置、Warp、局部障碍物、`CurrentToolIndex`、`CurrentToolbarIndex` 和 `Items` |
| 局部 A* | 已有基础 | 支持格子路径、硬障碍、目标 Warp 和可破坏障碍代价；`Object` 层已作为硬障碍，因此箱子/机器等对象不会被当成可行走 tile；普通树按高成本清障候选处理；已限制斜向清障路径 |
| 路径缓存与局部跟随 | 已有基础 | RouteNode 缓存 `tile_path` / `path_index`，MoveController 负责连续移动方向 |
| 交互站位控制 | 基础接入 | `PositioningController` 已接入 FarmNode/ChestNode/MiningNode，统一处理候选站位、ToolTarget 对准和 FACE_DIRECTION 转向；缓存路径会根据最新局部 state 的未来阻塞检查失效并重新规划 |
| 工具动作等待 | 基础接入 | Observer 导出 `UsingTool`/`CanMove`，Executor 忙碌时返回 `BUSY`，Python 通过 `ToolActionTracker` 等待收招后验证 state |
| 工具动作后处理 | 基础底座接入 | `ToolAftermathService` 已用于 Mining / ClearObstacle 的目标变化、梯子查询、范围副作用、阻塞 UI 和掉落物观察；工具效果等待窗口为 `1.0s`，仅在完全没有观察到预期效果或有效副作用时作为超时兜底；C# Debris state 已收紧为真实可拾取物品，并支持从 `Debris.itemId.Value` 解析 Copper Ore `(O)378` 等 `source=OBJECT` 掉落物；近距离可达掉落物已通过 `CollectLootNode` / `LootPolicyService` 支持低成本局部贪心拾取、延迟拾取、磁吸覆盖判断、树木掉落物宽候选站位和拾取验证；背包满且不可接收时交给 InventoryRecovery；Mining 下梯子前会先结算当前层已登记/延迟/最近工具来源附近的拾取需求 |
| 动态避障与重规划 | 已有基础 | 支持偏航、未来路径阻塞检测和后台 A*，仍需系统化测试 |
| 开门 | 部分完成 | 已有 Route/OpenDoor 协作，需要补齐异步等待和结果验证 |
| 工具切换 | 基础接入 | `SwitchToolNode` 已接入 Route 分支，可基于背包 state 发送 Tab/槽位键切换 Axe/Pickaxe |
| 破坏障碍物 | 基础接入 | A* 可标记 Stone、Twig、Weeds；RouteNode 触发清障，SwitchToolNode 切工具，ClearObstacleNode 使用工具并验证障碍消失 |
| Farm 浇水 | 基础接入 | FarmNode 可选择未浇水作物，复用 PositioningController 站到相邻格、对准 ToolTarget 后使用水壶；P1 浇水阶段已有临时失败重试队列 |
| Farm 资源检查/恢复 | 基础接入 | FarmResourceCheckNode 在 FarmTask 前检查背包/工具栏中的工具、种子和水壶 state；缺资源时写入恢复上下文和原始 FarmTask，mock Planner 可补 ChestTask 取回资源后继续 Farm |
| Farm 水壶补水 | 基础接入 | 水壶没水时触发 RefillWateringCanNode，按需查询并缓存 Farm 水源，站到水源旁接水后继续浇水 |
| Chest P0 指定取物 | 基础接入 | ChestNode 可用 `QUERY_CHESTS` 校验/恢复唯一箱子坐标，站到箱子旁，调用 SMAPI `TAKE_ITEMS_FROM_CHEST` 结构化动作批量取物，并用背包 state 验证数量增加 |
| Chest P1 指定存物 | 基础接入 | ChestNode 可调用 SMAPI `PUT_ITEMS_TO_CHEST` 结构化动作批量存物，支持部分存入并用背包 state 验证数量减少 |
| Chest P2/P3 箱子知识 | 基础接入 | 支持打开箱子后 `QUERY_CHEST_CONTENT` 写入缓存、`SCAN` 逐箱交互式查看，以及 `TAKE` 不指定 chest_tile 时基于缓存/按需查看自动选箱；自动取物会跳过已知新鲜且不匹配的箱子 |
| 地图知识缓存 | 基础接入 | `MapKnowledgeCache` 已作为 PlayerContext 的运行期地图知识缓存；当前用于水源和箱子位置/内容，采集物等机会记忆后续接入 |
| Farm P1 批处理 | 基础接入，暂停扩展 | 支持区域规划、清障、锄地、播种、浇水的阶段流水线；后续资源管理和复杂失败恢复先转 Mining 模块验证 |
| Mining P0/P2 基础循环 | 基础接入，持续实测 | 已新增 MiningTask、MiningResourceCheckNode、MineNode、矿洞 state/action 协议和 mock 数据；当前已围绕找梯子、机会资源、工具后处理和掉落物拾取持续迭代 |
| Mining 目标抽象 | 第一版接入 | 已新增 `MineTarget` / `MineTargetSelector`，当前用于统一建模 Ladder、MineEntrance、Stone、MiningNode、Collectible 和 BreakableContainer；C# Observer 已新增 `MineCollectibles` 与 `MineBreakableContainers` 快照，MineNode 心跳日志会输出对应数量 |
| Mining 目标决策迁移 | 第一轮完成，待游戏内验证 | 已新增纯决策 `MiningTargetResolver` / `MiningTargetDecision`，梯子、价值资源锚点、资源通路石头、普通破石和探索石头选择已从 MineNode 迁入 Resolver；MineNode 继续负责站位、工具动作、交互、后处理和结果验收 |
| Mining 价值资源锚点 | 基础接入 | `collect_opportunity_resources=True` 时，通过 `MiningOpportunityPolicy` 按资源价值、近距离资源奖励、额外路径成本、通路破石成本、动作成本和预留风险成本评分；未出现梯子时高分资源会影响挖石方向，已出现梯子时只处理相对直接下楼仍然值得的路径附近/低成本资源，不再使用固定次数上限 |
| Defend P1 / Mining 战术层最小版 | 最小版接入，第一轮验证通过 | Guard 分支已启用 `SwitchToolNode(owner="Guard")` 和 `Defend_Node`；Mining 已启用怪物战术判断，使用 `MonsterThreatEvaluator`、`CombatTacticalResolver` 和 `WeaponSelector` 处理怪物贴脸、堵路、暂缓目标和风险阻塞；怪物威胁解除后会在安全条件下登记附近可拾取掉落物并交给 `CollectLootNode` |
| Inventory P0/P1 背包风险与恢复 | P1 第一版接入，待游戏内验证 | C# Observer 已补充背包容量和最大堆叠信息；Python 已新增背包风险判断层，支持 OK / LOW_SPACE / FULL_CAN_STACK / FULL_BLOCKED；CollectLoot 拾取前会判断目标是否可进入背包，背包满但目标可堆叠时继续拾取；可堆叠目标已尽量拾取后仍有真实掉落物无法接收时，InventoryRecovery 会根据当前任务保留工具/任务物品/预期继续产生的可堆叠掉落物，优先把任务无关物品存入当前场景最近箱子，没有箱子时通过 `DISCARD_INVENTORY_ITEM` 丢弃任务无关物品并短期忽略自己丢出的 Debris |
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
- 普通树已纳入策略允许后的可清障目标；普通树砍倒后同一地块残留的树桩会继续清理；`FruitTree` 和独立 `TreeStump` 暂不纳入自动清障目标。
- `OpenDoorNode` 仍有异步路径使用 `time.sleep()`、结果验证不足等问题。
- `StardewExecutorClient.send_command()` 是阻塞式等待响应，缺少可靠超时和结构化 Action Result。
- Python 端当前仍会每 tick 重发当前移动方向；未来可优化为仅在方向变化、IDLE 或交互动作时发送，但必须保证安全停机语义不变。
- 高层场景连通图仍是硬编码数据，建筑入口和特殊路线需要持续校验；错误边会导致在当前场景查找不存在的目标 warp。
- SMAPI 快照仍缺少完成自主游玩需要的时间、金钱、体力、工具栏、背包、菜单、天气、NPC 和动作结果等状态。
- Farm P1 目前还是基础闭环，已加入基础资源检查，但仍缺少体力检查、背包容量检查、区域选择策略、作物阶段识别、失败后的二次规划和完整验收测试。
- InventoryRecovery P1 已接入第一版任务感知型背包整理，但仍需要游戏内验证：背包满时应先捡完可堆叠掉落物，再把任务无关物品存入当前场景最近箱子；没有箱子时才丢弃任务无关物品，并避免重新捡回主动丢弃物。
- Python 动作枚举比 C# Executor 实际支持的动作更多，两侧能力尚未完全对齐。
- `server/valley_server.py` 仍含旧 demo 逻辑，不要继续在 demo 路径上扩展正式能力。

## 第一阶段开发顺序

1. 游戏内验证 Mining P0：确认 `TASK_MOCK_DATA["MINING_P0_1"]` 能从 Mine 入口进入第一层，找到/挖出梯子并进入第二层。
2. 若 P0 实测失败，优先检查 `logs/mining_node_debug.log` 中的 `MineLevel`、`MineEntrances`、`Ladders`、`MiningNodes` 和站位日志。
3. 当前 `MineTarget`、`MiningOpportunityPolicy`、`ToolAftermathService`、`CollectLootNode` 和 `LootPolicyService` 已形成 Mining 基础底座；机会资源选择和掉落物拾取后续只按日志做增量修补，不再作为下一阶段主线。
4. 继续实测 Defend P1 / Mining 战术层最小版：确认无怪物时不抢占 Mining、怪物贴脸时切武器攻击、怪物堵住梯子/目标路径时不再左右抽搐，怪物死亡/消失后能在安全条件下拾取掉落物。
5. 根据 `logs/defend_node_debug.log`、`logs/collect_loot_debug.log` 和 `logs/mining_node_debug.log` 调整威胁阈值、堵路判断、目标暂缓策略和战斗后拾取范围。
6. 扩展 Mining 基础采矿验收：确认 Stone / MiningNode / Collectible / BreakableContainer / Ladder 在当前 MineTarget 抽象下能稳定执行和验证。
7. 游戏内验证 InventoryRecovery P1：背包满时，先确认可堆叠掉落物已被 CollectLoot 尽量拾取，再验证任务无关物品会优先存入当前场景最近箱子；没有箱子时才丢弃任务无关物品，并且不会立刻捡回自己丢出的 Debris。
8. 继续维护 Route/A*、SwitchToolNode、ClearObstacleNode 和 ToolActionTracker，保证 Mining/Farm/Route 共用底座稳定。
9. 在 Mining 中继续实现资源管理底座：体力检查、Pickaxe 缺失恢复、工具借用归还和失败恢复。
10. 将 Mining 中验证稳定的资源检查和失败恢复能力回流 Farm。
11. 增加确定性 Route/Farm/Mining 场景测试与游戏内端到端验收。

## 第一阶段验收标准

- `LLM_Node` 的模拟数据能稳定写入黑板并驱动多个连续 `RouteTask`。
- Agent 能跨至少两个地图完成导航。
- 固定障碍场景中，Agent 能绕开硬障碍并在动态阻塞后重新计算路径。
- 可破坏障碍挡住必要路径时，Agent 能完成“识别障碍 -> 切换正确工具 -> 执行动作 -> 验证障碍消失 -> 继续移动”。
- 工具动作必须通过 state 确认收招和结果变化，不能仅凭 Executor 返回 `SUCCESS` 判定完成。
- Farm P1 测试中，规划区域内可种植地块能完成“清障 -> 锄地 -> 播种 -> 浇水”；普通树会被清理并连同残留树桩处理完后统一拾取掉落物，果树和独立 TreeStump 等不可处理地块会被明确跳过。
- 关闭但可进入的门能被打开；打烊或上锁能产生明确失败或恢复信号。
- 任务成功由最新 SMAPI 状态验证，不能仅以命令已发送或路径列表为空作为成功依据。
- 绝路、目标 warp 不存在或需要兜底恢复时，必须先发送 `IDLE`，人物不能继续保持旧方向移动。
- 核心场景具有可重复的测试记录，包括完成时间、重规划次数、动作次数和失败原因。

## 建议测试场景

- 无障碍跨地图导航。
- 路径中临时出现硬障碍，Agent 能重新规划绕行。
- 必经路径被石头、树枝或杂草挡住，Agent 能清除后继续。
- Mining P0 入口交互：从 Mine 大厅走到矿井入口旁，足够贴近后交互进入第一层；不能只因 `ToolTarget` 对准就提前交互。
- Mining P0 天然梯子：进入某层后若已有天然梯子，优先走到梯子旁并进入下一层；梯子在局部视野外时，应先稳定接近，不应左右抽搐或反复换中继点。
- Mining P0 挖石出梯子：没有梯子时打碎 Stone / MiningNode，等待工具收招后只查询被破坏 tile 是否生成梯子；若生成梯子，应立即切回梯子目标。
- Mining P0 破石同时出梯子和掉落物：必须先完成 Pickaxe 收招和 `ToolAftermathService` 后处理，登记并拾取当前层掉落物，再交互梯子进入下一层。
- Mining P0/P2 破坏资源矿点：多次挥镐的资源矿点只有在目标从 `MiningNodes` state 中消失后才登记掉落物；目标仍存在时不得把 `CHUNKS` 等视觉碎屑当作可拾取物。
- Mining P0/P2 价值资源锚点：当 10 格内看到石英、地晶、木箱/木桶或资源矿点等价值目标时，应优先挖通向该目标方向的石头；若打通过程中出现梯子，先完成已锁定资源目标，再前往梯子。
- Mining P0 交互边界：梯子/矿井入口目标 tile 不应被当成可站立 tile；玩家必须站在上下左右相邻格，并足够贴近交互边缘。
- Mining P0 状态切层：进入下一层后必须重置上一层的目标、接近点、破石计数和临时路径，不能沿用过期状态。
- 规划一片 Farm 区域，包含 Grass、Weeds、Twig、Stone、普通树、果树和独立树桩，Agent 能清理可处理障碍、普通树连同残留树桩处理完后再拾取掉落物、跳过果树/独立 TreeStump、锄地、播种并浇水。
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
