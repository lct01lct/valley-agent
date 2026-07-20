# 当前阶段：智能寻路 MVP

更新时间：2026-07-20

本文记录当前阶段目标、进度、已知缺口、验收标准和开发顺序。这里的内容会比 `AGENTS.md` 更频繁变化；稳定架构约束仍以 `AGENTS.md` 为准。

## 阶段目标

第一阶段只聚焦完成寻路模块。使用 `LLM_Node` 返回的模拟 `RouteTask` 计划跑通完整闭环，暂不要求真实 LLM 生成可用计划。

本阶段的“智能寻路”不只是从起点走到终点，而是包括：

1. 根据模拟宏观计划完成跨地图路线分解。
2. 使用 SMAPI 导出的玩家位置、Warp 和障碍物状态进行局部 A* 寻路。
3. 动态避开不可破坏障碍，并在路径或状态变化后重新规划。
4. 识别可破坏障碍，选择正确工具和动作清除障碍，然后继续原路线。
5. 识别不可直接通行的门，走到交互位置、开门，并处理打烊或上锁反馈。
6. 仅在游戏状态确认到达目标地点后完成 `RouteTask`。
7. 遇到无路、工具缺失、体力不足、动作超时或门无法打开时安全停止，并暴露可恢复的失败原因。

第一阶段完成前，不要把主要精力扩展到购买、种田或完整日程规划。可以为后续能力保留结构，但当前开发和测试应优先服务寻路闭环。

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
│   ├── ClearObstacleNode
│   └── RouteNode
└── Sequence("Think")
    └── LLM_Node
```

`Route` 分支和 `Think` 分支都是顶层 Selector 下的候选分支。`Think` 分支当前内部只有 `LLM_Node`，作为最后兜底：没有可执行计划时才生成模拟计划；有计划时让出控制权给前面的确定性节点。

`AgentBlackboard` 是跨节点通讯和调度状态中心，当前至少保存：

- `macro_plan`
- `current_step_index`
- `is_llm_thinking`
- `new_plan_received`
- `prompt`
- `require_open_door`
- `require_clear_obstacle`

## 当前寻路与移动模型

当前移动控制已经从“Python 单次按键”调整为“Python 决策方向 + C# 持续保持方向”：

- Python `RouteNode` 缓存跨地图路线、当前 `tile_path` 和 `path_index`。
- A* 只在必要时执行：初始路径为空、场景变化、路径过期、未来路径阻塞、清障/开门后重规划等。
- `MoveController` 每 tick 根据最新 `State`、玩家位置、`player_size=(48, 32)` 和目标 tile 输出当前移动方向。
- C# `StardewExecutor` 会持续按住最后一次 MOVE 命令对应的方向；Python 发送新 MOVE 用于更新方向，发送 `IDLE` 用于停止移动。
- 中间路径点进入后立即推进，不强制回到格子中心；路径末端才做更严格的身体盒进入判断。
- Town 等大场景未来路径被阻挡时，优先启动后台 A*；旧路径继续执行，只有障碍已经接近才停下等待。
- 后台 A* 结果切换前需要对齐当前玩家位置，避免使用过期起点导致人物回头。
- 绝路、目标 warp 不存在、路径放弃或交给兜底规划前，RouteNode 必须先发送 `IDLE`，否则 C# 会继续保持旧方向。

## 当前进度

| 能力 | 状态 | 当前说明 |
| --- | --- | --- |
| 模拟宏观计划 | 已有基础 | `LLM_Node` 异步返回硬编码 `RouteTask` |
| 跨地图规划 | 已有基础 | `HardcodedStardewMap` 使用 BFS 生成地点链路 |
| SMAPI 结构化感知 | 已有基础 | 导出地点、位置、Warp 和局部障碍物 |
| 局部 A* | 已有基础 | 支持格子路径、硬障碍和目标 Warp |
| 路径缓存与局部跟随 | 已有基础 | RouteNode 缓存 `tile_path` / `path_index`，MoveController 负责连续移动方向 |
| 动态避障与重规划 | 已有基础 | 支持偏航、未来路径阻塞检测和后台 A*，仍需系统化测试 |
| 开门 | 部分完成 | 已有 Route/OpenDoor 协作，需要补齐异步等待和结果验证 |
| 破坏障碍物 | 基础接入 | A* 可标记石头、树枝、杂草等必要清障点，`ClearObstacleNode` 已接入 Route 分支；工具选择和游戏内验证仍需完善 |
| C# 持续移动 | 已有基础 | Executor 保持最后 MOVE 方向，Python 需用新方向/IDLE 显式更新或停止 |
| 真实 LLM 规划 | 后续阶段 | 第一阶段继续使用 mock 计划 |
| 完整自主游玩 | 长期目标 | 还需要背包、时间、体力、菜单、NPC 等状态与技能 |

## 已有基础

- `LLM_Node` 可异步返回模拟 `RouteTask`。
- `HardcodedStardewMap` 可做跨地图 BFS 路线分解。
- SMAPI Observer 可导出地点、玩家位置、Warp 和局部障碍物。
- 本地 A* 支持格子路径、动态路径过期检测、偏航检测和重新计算。
- RouteNode 已缓存 `tile_path` 和 `path_index`，并通过 MoveController 做局部跟随。
- C# Executor 已支持保持最后移动方向，改善低频命令下的蠕动问题。
- RouteNode 失败路径已开始显式发送 `IDLE`，避免绝路后继续沿旧方向移动。
- Route/OpenDoor/ClearObstacle 之间已有黑板标志协作。
- C# Executor 已支持基础移动、开门、关闭对话和使用工具。

## 当前缺口

- `ValleyAgent.invoke(task)` 保存了原始任务，但尚未稳定注入 Planner Prompt；第一阶段可继续使用 mock 计划。
- `ClearObstacleNode` 当前复用现有 `USE_TOOL` 协议，只能使用当前手持工具；还没有工具选择、体力检查和工具栏状态验证。
- 树木、石头、树枝、杂草等可破坏物的工具、体力和背包约束尚未形成完整决策。
- `OpenDoorNode` 仍有异步路径使用 `time.sleep()`、结果验证不足等问题。
- `StardewExecutorClient.send_command()` 是阻塞式等待响应，缺少可靠超时和结构化 Action Result。
- Python 端当前仍会每 tick 重发当前移动方向；未来可优化为仅在方向变化、IDLE 或交互动作时发送，但必须保证安全停机语义不变。
- 高层场景连通图仍是硬编码数据，建筑入口和特殊路线需要持续校验；错误边会导致在当前场景查找不存在的目标 warp。
- SMAPI 快照仍缺少完成自主游玩需要的时间、金钱、体力、工具栏、背包、菜单、天气、NPC 和动作结果等状态。
- Python 动作枚举比 C# Executor 实际支持的动作更多，两侧能力尚未完全对齐。
- `server/valley_server.py` 仍含旧 demo 逻辑，不要继续在 demo 路径上扩展正式能力。

## 第一阶段开发顺序

1. 保持 `LLM_Node` mock 计划稳定，确保黑板能够连续消费多个 `RouteTask`。
2. 继续校验硬编码场景连通图和 warp 目标名称，避免错误跨场景边导致目标 warp 不存在。
3. 为 A* 正式接入障碍代价函数，区分不可通行、可绕行和可破坏障碍。
4. 完善 `ClearObstacleNode` 的工具选择、体力检查和更多可安全处理的障碍类型。
5. 强化玩家朝向、清障动作、超时与障碍消失验证。
6. 完善 `OpenDoorNode` 的非阻塞状态机和门结果验证。
7. 增加确定性寻路场景测试与游戏内端到端验收。

## 第一阶段验收标准

- `LLM_Node` 的模拟数据能稳定写入黑板并驱动多个连续 `RouteTask`。
- Agent 能跨至少两个地图完成导航。
- 固定障碍场景中，Agent 能绕开硬障碍并在动态阻塞后重新计算路径。
- 可破坏障碍挡住必要路径时，Agent 能完成“识别障碍 -> 切换正确工具 -> 执行动作 -> 验证障碍消失 -> 继续移动”。
- 关闭但可进入的门能被打开；打烊或上锁能产生明确失败或恢复信号。
- 任务成功由最新 SMAPI 状态验证，不能仅以命令已发送或路径列表为空作为成功依据。
- 绝路、目标 warp 不存在或需要兜底恢复时，必须先发送 `IDLE`，人物不能继续保持旧方向移动。
- 核心场景具有可重复的测试记录，包括完成时间、重规划次数、动作次数和失败原因。

## 建议测试场景

- 无障碍跨地图导航。
- 路径中临时出现硬障碍，Agent 能重新规划绕行。
- 必经路径被石头、树枝或杂草挡住，Agent 能清除后继续。
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
