# AGENTS.md

## 项目事实源

修改本项目之前，先检查当前源码和本文件。`README.md` 面向开发者介绍项目，本文件用于约束 Agent 的开发行为；当历史文档、实验代码和当前实现冲突时，以当前源码和本文件为准。

当前项目入口文件是 `main.py`。

当前阶段、进度、验收标准和已知缺口见 `docs/current-stage.md`。这些内容会频繁变化，不应长期堆在本文件里。

## 项目目标

终极目标是开发一个能够自主玩《星露谷物语》的 AI Agent。Agent 应能理解自然语言目标，根据游戏状态规划任务，并通过确定性操作完成移动、交互、种植、采集、战斗、交易和日程管理等长期行为。

项目采用“行为树 + AI 决策 + SMAPI 结构化状态”的架构：

- 行为树负责实时控制、安全检查、节点轮询、执行顺序、超时、重试和状态验证。
- AI 模型负责高层意图理解、宏观计划生成和失败后的恢复建议。
- SMAPI 提供结构化游戏状态和动作执行能力。
- A\*、交互节点和后续技能负责确定性执行；不要让 LLM 直接参与每帧按键控制。

## 稳定架构约定

### State 是独立高频通道

游戏数据 `state` 来自 SMAPI Observer，经 Python Observer Client 进入 `PlayerContext.state`。它是高频更新的数据通道，不依赖行为树，也不应该被描述为行为树的下游产物。

行为树节点只读取最新 `context.state` 来判断当前帧应该做什么；状态采集本身应保持独立、连续、可被多个节点共享。

### 行为树是运行时轮询层

当前顶层行为树定义在 `agent/valley_agent.py`：

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

`ValleyAgent` 在主循环中先刷新 `PlayerContext.state`，再运行行为树。`Selector` 每个 tick 从左到右轮询子节点，遇到 `RUNNING` 或 `SUCCESS` 就停止本轮扫描。

因此，`Guard`、`Route`、`Chest`、`Farm` 和 `Think` 是顶层 Selector 下的同级候选分支。`Think` 分支是最后的兜底分支，当前内部只有 `LLM_Node`：只有前面的确定性节点没有可执行工作时，它才负责生成或补充宏观计划。

`Route` 分支内部的职责边界：

- `OpenDoorNode`：处理 RouteNode 发现的不可直接通行门，发送开门/关闭对话命令并暴露失败原因。
- `SwitchToolNode`：根据 `context.state` 中的 `CurrentToolIndex`、`CurrentToolbarIndex` 和 `Items` 切换到清障所需工具。
- `ClearObstacleNode`：在玩家位于障碍物上下左右相邻格时使用当前工具清理 Stone、Twig、Weeds，并验证障碍消失。
- `RouteNode`：选择跨场景路线、缓存 tile path、触发 A\*、驱动 MoveController、发现门/清障需求并写入 blackboard。

`Chest` 分支内部的职责边界：

- `ChestNode`：当前实现 Chest P0/P1/P2/P3，处理指定箱子批量取物、批量存物、交互式打开箱子建立内容缓存，以及 `TAKE` 不指定 `chest_tile` 时的场景内自动选箱。
- `ChestKnowledgeService`：不是行为树节点，只负责低频查询箱子位置、在箱子已打开后读取内容、写入 `MapKnowledgeCache`。它不负责推进角色移动，也不应被当成“隔空遍历箱子内容”的执行入口。

箱子和其他不可穿越对象由 SMAPI Observer 导出到 `Object` 障碍层；A* 会把 `Object` 作为硬障碍处理。ChestNode 只能站到目标箱子上下左右相邻格交互，不应把箱子所在 tile 当作可站立路径点。

Chest P0/P1 不使用鼠标或 UI 拖拽。C# Executor 可以直接操作 `Chest.Items` 和 `Game1.player.Items`，但必须保持游戏约束：玩家在当前场景、位于箱子上下左右相邻格、玩家不处于 `UsingTool` / `CanMove=False` 状态。`TAKE` 的 `ChestTask.items` 表示“背包至少需要拥有的物品清单”；若背包已有全部目标物品，ChestNode 应直接完成，不强行开箱取物。`PUT` 表示“把背包中匹配清单的物品尽量放入箱子”，可堆叠物品不足请求数量时允许部分存入并记录实际转移数量。

Chest P2/P3 的查询和自动选箱只在 `current_task.target_loc` 指定场景内进行；跨场景找箱子由 Planner/LLM 结合记忆生成 `RouteTask + ChestTask`，不要让 ChestNode 自己遍历世界。`SCAN` 只允许先低频查询当前场景箱子坐标，再逐个移动到箱子旁、打开、查看内容、写入缓存并关闭；`QUERY` 同样必须走到指定箱子旁打开查看；`TAKE` 且 `chest_tile=None` 时先查 `MapKnowledgeCache`，缓存缺失再按距离逐个打开当前场景箱子查看，找到满足需求的箱子后在已打开的箱子中取物。取放成功后应保守地把对应箱子内容缓存标记为过期，不在 Python 端硬改箱子堆叠数量。

未来 Farm 缺资源恢复应继续扩展 Chest 分支和 Planner，不要塞进 FarmNode。

`Farm` 分支内部的职责边界：

- `FarmResourceCheckNode`：在 FarmTask 开始前检查背包/工具栏中的必要工具、种子数量和水壶 state；缺资源时安全停止并把恢复上下文写入 blackboard。
- `SwitchToolNode`：根据 Farm 当前阶段切换 Hoe、种子、Watering Can 或清障工具。
- `ClearObstacleNode`：处理 FarmNode 规划区域中的 Grass、Weeds、Twig、Stone 等可清障碍。
- `RefillWateringCanNode`：当 FarmNode 发现水壶 `WaterLeft <= 0` 时，查询/读取地图知识缓存中的水源，移动到水源旁，面向水源并使用水壶补水。
- `FarmNode`：消费农业任务，求解农业交互所需的候选站位和工具目标地块，再交给动作层的 `PositioningController` 完成站位和转向。

Farm 资源检查只以当前 `context.state.inventory.Items` 为事实来源。若必要工具或种子不在背包/工具栏中，节点只能判定“当前背包缺失”，并把缺口写入 `farm_missing_resources` / `farm_resource_recovery_hint`；不要在该节点中猜测或直接操作箱子。未来箱子取物应由独立 Chest 节点读取箱子状态、移动到箱子旁并取回资源后，再恢复 FarmTask。

### Context 负责状态输入和动作输出

`PlayerContext` 是运行时上下文模块，内部提供两条主要链路：

- 状态输入链路：SMAPI `observer server` -> Python `observer client` -> `State` -> 行为树。
- 动作输出链路：行为树 -> `Command` -> Python `Executor client` -> SMAPI `Executor`。

`PlayerContext` 还持有运行期 `MapKnowledgeCache`。它用于保存低频地图知识和机会记忆，例如按需查询到的 Farm 水源；它不是每帧实时 state，也不是 blackboard 调度信号。

`ValleyAgent` 的 tick driver 每轮同时驱动 context 更新和行为树轮询。行为树不直接拥有 Observer/Executor 连接，而是通过 `PlayerContext` 读取最新状态、发送控制命令，并按需读取运行期地图知识缓存。

### 记忆与缓存分层

项目中“缓存/记忆”分为四层：

1. `Realtime State`：每帧游戏事实，例如位置、当前工具、`UsingTool`、`CanMove`。
2. `State Snapshot Cache`：性能缓存，例如 `obstacles`、`FarmTiles` 低频刷新；C# 没刷新时发 `null`，Python 复用上一份。
3. `MapKnowledgeCache`：当前运行期地图知识，例如水源、未来采集物、箱子或交互点。当前已用于 Farm 水源缓存。
4. `PersistentMemoryStore`：长期记忆预留接口，当前不实现、不调用；未来用于跨运行保存稳定线索。

不要把低变化地图知识塞进每帧 state。水源这类资源应优先走“按需查询 -> 写入 MapKnowledgeCache -> 后续复用”的模式。

### AgentBlackboard 是调度状态中心

`AgentBlackboard` 是跨节点通讯和系统调度状态中心，用于保存宏观计划、当前步骤、LLM 异步状态、节点间信号和恢复上下文。

精确边界：

- `ValleyAgent` 和 `Selector` 负责驱动 tick 与轮询节点。
- `AgentBlackboard` 负责保存调度状态和跨节点信号。
- 各行为树节点通过 blackboard 协作，但不应绕过黑板私自耦合彼此内部状态。

### 移动控制是 Python 决策 + C# 保持方向

当前移动控制分为两层：

- Python `RouteNode` / `MoveController` 根据最新 `context.state` 决定当前 tick 应该保持的移动方向。
- C# `StardewExecutor` 保存并持续按住最后一次 MOVE 命令对应的方向键。

因此，MOVE 命令的语义不是“只按一帧”，而是“更新 C# 端当前保持的移动方向”。`IDLE` 是显式停止持续移动的控制命令。

实现和修改寻路时必须遵守：

- 移动过程中允许 Python 每 tick 重发当前方向，以便及时覆盖 C# 端保持的方向。
- 后续若优化为“只在方向变化时发送”，必须保证 `IDLE`、开门、清障、失败、任务切换和连接异常仍能可靠清除 C# 端保持方向。
- RouteNode 任何 fatal failure、绝路停机、放弃当前路径或交给兜底规划前，都必须先发送 `StardewAction.IDLE`。

### 寻路是路径缓存 + 局部跟随

当前寻路不应每帧重新执行 A*。`RouteNode` 缓存 `tile_path` 和 `path_index`，A* 只在必要时触发，例如初始路径为空、场景变化、路径过期、未来路径阻塞、开门/清障后需要重新规划。

`MoveController` 负责局部移动跟随：

- 根据最新 `State` 和 `player_size` 判断人物身体与目标 tile 的关系。
- 中间路径点进入后立即推进，不为了回到格子中心而反向修正。
- 到达路径末端时才允许更严格地验证身体盒是否进入目标 tile。
- 不在推进 tile 时插入额外 `IDLE` 帧。

未来障碍触发重规划时，优先使用后台 A\*：旧路径仍可继续执行，只有障碍已经非常近时才停下等待新路径。后台路径切换前必须对齐当前玩家位置，避免切换到过期路径导致回头。

### 交互站位是候选站位 + 工具目标

对于浇水、砍树、开箱子、NPC 对话、商店柜台、开门和清障这类“先站到某处，再面向目标交互”的行为，通用输入应抽象为：

- `candidate_stand_tiles`：允许玩家站立的一组候选格。
- `tool_target_tile`：需要工具或交互目标对准的格，可为空。

动作层 `PositioningController` 负责从候选站位中选择可达站位、缓存站位路径、调用 `MoveController` 连续移动，并在到达后发送 `FACE_DIRECTION` 原地转向，直到 `state.tool_target.tile` 等于 `tool_target_tile`。业务节点只负责根据自身目标求解这两个输入，不应重复维护 `_tile_path`、`path_index` 或自行用 MOVE 命令模拟转向。

`MOVE_*` 表示 C# 端持续移动方向；转向必须使用 `FACE_DIRECTION`，不要用 `MOVE_*` 当作单帧转向脉冲。

### 工具动作必须等待收招并验证结果

使用工具是跨帧动作，不是单帧命令。`USE_TOOL` 或 `USE_ITEM` 返回 `SUCCESS` 只表示 C# Executor 接受并发出了命令，不代表游戏内结果已经发生。

SMAPI Observer 需要持续导出并同步以下状态：

- `UsingTool`：玩家当前是否正在使用工具。
- `CanMove`：玩家当前是否可以移动。
- `IsPlayerFree` / `CanPlayerMove`：SMAPI 上下文中的玩家自由状态，可作为辅助判断。

行为树节点应遵守：

- 发出挥斧、挥镐、锄地、浇水等 `USE_TOOL` 后，使用 `ToolActionTracker` 或等价跨帧状态机等待 `UsingTool=True` 被观察到，随后等待 `UsingTool=False` 且 `CanMove=True`。
- 只有工具动作收招后，才根据最新 `context.state` 验证结果，例如障碍消失、HoeDirt 出现、作物已浇水。
- C# Executor 返回 `BUSY` 时，Python 端不得增加动作尝试次数，也不得开启工具动作等待；应保持当前节点 `RUNNING` 并等待下一帧。
- 当 `UsingTool=True` 或 `CanMove=False` 且本节点没有正在追踪的动作时，节点应暂停推进，避免在上一轮动作未释放控制权时叠加新命令。
- 动作失败应区分永久失败和临时失败。Farm P1 浇水阶段的站位卡顿、动作未命中或短暂时序问题应进入浇水重试队列；锄地、播种、清障等阶段只有在明确不可执行或重试耗尽时才标记地块失败。
- 不要使用 `time.sleep()` 等待工具动画；应通过每 tick 的状态变化判断或使用非阻塞状态机。

### 状态驱动优先，时间等待只做保护

行为树节点的成功、失败和推进判断应优先依赖最新 `context.state`，例如位置、`ToolTarget`、`UsingTool`、`CanMove`、障碍是否消失、`HasHoeDirt`、`HasCrop`、`IsWatered`、`WaterLeft` 等真实游戏状态。

等待固定 tick 数或秒数不能作为主要业务判断，只能作为保护性机制：

- 短暂 grace window：命令刚发出后，允许 SMAPI state 有少量刷新延迟，避免立刻误判失败。
- 非阻塞节流：避免同一 tick 或极短时间内重复发送高风险动作命令。
- 超时兜底：当期望的 state 变化一直没有出现时，节点必须退出等待并进入恢复、重试或失败流程，不能永久 `RUNNING`。

因此，类似 `STATE_SETTLE_TICKS`、`*_VERIFY_DELAY_SECONDS`、`*_STUCK_TIMEOUT_SECONDS`、`*_RETRY_DELAY_SECONDS` 的参数只能用于“防误判”和“防死锁”，不应替代状态验证。若 state 已明确证明动作完成或目标已达成，应尽快推进；若 state 已明确证明动作不可执行，应尽快进入恢复或失败，而不是继续等待固定时间。

## Codex 必须显式读取的项目 Skills

项目内 Skill 位于 `skills/`。项目级 Skill 不一定会被 Codex 自动发现，因此不要依赖自动触发；符合以下条件时，必须显式打开并遵循对应 `SKILL.md`：

- 新增、修改、重构或审查代码：`skills/code-style/SKILL.md`
- 新增、修改或审查行为树节点：`skills/behavior-tree/SKILL.md`
- 修改 README、AGENTS 或 docs 文档：`skills/documentation/SKILL.md`

如果任务同时涉及多类 Skill，应按“文档 -> 编码 -> 行为树”的顺序读取与任务相关的 Skill；行为树专属契约以 `behavior-tree` Skill 为准。`SKILL.md` 引用的 `references/`、`assets/` 或模板应按其中的路由说明继续读取。用户明确要求的规则优先于 Skill。

## 文档维护边界

`README.md` 是项目门面，只保留项目目标、总体架构、当前能力摘要、运行方式和文档导航。不要在 README 中展开行为树节点状态机、SMAPI/Python 协议字段、工具动作等待、箱子/背包/菜单交互细节、临时 mock 数据或调试日志解释。

Agent 开发必须遵守的稳定工程契约应写入本文件；当前阶段目标、验收标准和已知缺口应写入 `docs/current-stage.md`；未来计划和暂不实现的设计应写入 `docs/next-development-plan.md`。更完整的判断规则见 `skills/documentation/SKILL.md`。

## 当前主要模块

- `main.py`：当前入口，加载环境、清理日志、初始化 `ValleyAgent` 并提交任务。
- `agent/valley_agent.py`：创建行为树、`PlayerContext` 和 `AgentBlackboard`，运行高频 tick 循环。
- `agent/behavior_tree/`：行为树节点、黑板、玩家上下文、规划兜底和寻路控制。
- `agent/memory/`：运行期地图知识缓存和长期记忆预留接口。
- `agent/action/map/map.py`：`HardcodedStardewMap`，维护硬编码场景连通图和最少场景跳数候选路线枚举。
- `agent/action/valley_action/AStar.py`：本地 A\* 寻路、路线动作标注和障碍代价函数。
- `agent/action/valley_action/clearance_policy.py`：清障策略层，判断障碍是否允许清理、所需工具和清障代价；未来可接入 Agent Skill/Planner 对普通树等高价值资源的保护策略。
- `agent/action/valley_action/move_controller.py`：根据缓存 tile path 和最新 state 输出连续移动方向。
- `agent/action/valley_action/positioning_controller.py`：通用交互站位控制，输入候选站位和工具目标地块，输出移动、转向或 READY 状态。
- `agent/action/valley_action/tool_targeting.py`：工具目标判断、`FACE_DIRECTION` 转向命令和 ToolTarget 日志格式化。
- `agent/behavior_tree/tool_action_tracker.py`：跨帧跟踪工具动作开始、收招和超时，供清障、锄地、浇水等节点复用。
- `agent/behavior_tree/refill_watering_can_node.py`：Farm 水壶补水节点，按需查询并缓存水源，复用站位控制和工具动作等待。
- `server/valley_server.py`：Python 侧 SMAPI Observer/Executor TCP 客户端和状态解析。
- `StardewMemoryExporter/`：SMAPI Mod，导出结构化状态并执行移动、开门、关闭对话和使用工具等命令。
- `skills/`：项目内 Codex Skills。若 Codex 无法自动发现，必须通过本文件显式说明。
- `docs/current-stage.md`：当前阶段目标、进度、缺口、验收和开发顺序。

## 开发原则

- 优先使用 SMAPI 结构化状态，不以截图/VLM 作为当前阶段的主要感知来源。
- 行为树是主控层。增加实时能力时优先新增或完善确定性节点，不把 `LLM_Node` 变成通用执行器。
- `LLM_Node` 只做最后兜底的宏观计划生成、补计划或恢复建议，不参与每帧移动控制。
- 每个任务和节点都应有前置条件、执行逻辑、成功判定、超时和恢复策略。
- 寻路需要区分硬障碍、可绕行障碍、可破坏障碍和交互式门。
- 普通树 `Tree0` ~ `Tree5` 属于“策略允许后可清理”的高成本障碍；当前 Route 和 Farm 规划区域默认允许砍普通树。`FruitTree0` ~ `FruitTree5` 和 `TreeStump` 暂不自动清理。
- 涉及普通树、未来高价值资源或长期收益的清障判断应通过 `clearance_policy` 一类策略层完成；执行节点只负责站位、切工具、使用工具和验证结果，不负责判断资源是否值得破坏。
- 清障必须验证工具可用、玩家位于上下左右相邻格、朝向正确，并在动作后从新状态确认障碍已经消失；当前不允许斜向破坏障碍物。
- 工具切换应优先读取 SMAPI state 中的 `CurrentToolIndex`、`CurrentToolbarIndex` 和 `Items`，不要在 Python 端硬猜当前工具。
- 工具动作必须等待 `UsingTool` / `CanMove` 状态确认收招，并在收招后验证游戏 state；不要把 Executor 的 `SUCCESS` 当作动作完成。
- 节点推进应状态驱动优先；固定 tick/秒数等待只能用于短暂防抖、节流和超时兜底，不要用经验等待替代 SMAPI state 验证。
- 水壶补水属于 Farm 分支的资源恢复能力。FarmNode 发现 `WaterLeft <= 0` 时应通过 blackboard 触发 `RefillWateringCanNode`，不要在 FarmNode 内部直接实现找水源、移动和补水。
- 水源坐标属于低频地图知识，优先通过 C# `QUERY_WATER_SOURCES` 按需查询并写入 `MapKnowledgeCache`；不要作为每帧 state 高频字段同步。
- 不重复实现寻路或动作逻辑；共享行为下沉到 A\*、动作层或复用节点。
- 交互类节点应优先复用 `PositioningController` 做站位与转向；节点只求解候选站位和工具目标地块，不在节点内部重复维护路径缓存。
- 不让 A\* 每帧重算；优先维护 RouteNode 的路径缓存、path_index 推进和 MoveController 局部跟随。
- 不把 C# Executor 的 MOVE 当成单帧脉冲；它会保持最后方向，失败和交互前必须显式 `IDLE`。
- 需要原地转向时使用 `StardewAction.FACE_DIRECTION`，不要发送 `MOVE_*` 伪装转向。
- Planner 输出和 Action Result 使用结构化数据，避免自由文本协议。
- 不在异步行为树路径中使用 `time.sleep()`；使用 `await asyncio.sleep()` 或跨帧状态机。
- 不静默吞掉失败；将失败原因写入黑板、任务结果或协议响应。

## 状态与协议命名

- C# 传输字段名属于协议契约，Python 必须按原始拼写和大小写读取。
- 新增 state 字段时，尽可能沿用 SMAPI／Stardew Valley API 原生属性名。
- Python 本地变量遵循 `snake_case`，必要时通过 Pydantic alias 映射传输字段。
- 不为统一风格直接批量重命名已有协议字段；迁移必须同步修改生产端、消费端和测试。

详细规范见 `skills/code-style/SKILL.md` 及其 references。

## 常用命令

```bash
./setup.sh
python main.py
./StardewMemoryExporter/setup.sh
git status --short
rg --files
rg "class |def |async def" agent server StardewMemoryExporter
```

`setup.sh` 使用 Conda 环境 `valley` 和 Python `3.13.5`，安装依赖后会直接运行 `main.py`。API Key 和 SMAPI 连接参数从 `.env` 加载，不要提交密钥。

## 测试与验证

当前尚无成熟自动化测试体系。根据改动范围选择最强可用验证：

- 纯 Python 逻辑：编译检查和聚焦单元测试。
- A\*、代价函数和跨地图规划：使用小型确定性地图与固定障碍布局测试。
- 行为树节点：用可控黑板、状态快照和假 Executor 验证状态转换。
- SMAPI 协议：同时验证 C# 序列化/响应和 Python 解析/超时。
- 游戏内行为：检查 `logs/`，确认移动、清障、开门和最终地点状态真实发生。

汇报完成时，必须说明运行了哪些检查、哪些结果已通过，以及哪些仍需要进入游戏实测。

## Git 与工作区安全

- 编辑前执行 `git status --short`。
- 不回滚或覆盖与当前任务无关的用户改动。
- 不删除生成资源、日志或实验文件，除非用户明确要求。
- 不提交 `.env`、日志、截图或本机专属部署路径，除非用户明确要求追踪。
