# 下一步开发计划

更新时间：2026-07-23

本文专门记录接下来要开发的功能、优先级和暂缓事项。`docs/current-stage.md` 继续作为当前阶段事实、进度和验收标准；本文更偏任务队列和开发路线。

## 当前主线

短期主线仍是把“行为树 + SMAPI state + 确定性节点”的闭环做稳定。

当前重点不是扩展成完整农场经营 AI，而是把已有 Farm P1 基础闭环打磨成可复用、可验证的确定性技能：

```text
规划候选地块 -> 批量清障 -> 批量锄地 -> 批量播种 -> 批量浇水 -> 缺水补水 -> 结果验证
```

## P1 优先任务

### 1. Farm 资源检查

目标：在农业任务开始前或阶段推进前，提前判断任务是否具备执行条件，避免跑到中途才发现无法完成。

当前已基础接入 `FarmResourceCheckNode`，会检查：

- 背包中是否有目标种子。
- 种子数量是否足够完成 `count` 或规划区域。
- 是否拥有 Hoe、Watering Can，以及清障需要的 Axe、Pickaxe、Scythe。
- 水壶是否有 `WaterLeft` / `WaterCapacity` state。

暂未完成、后续继续补：

- 当前体力是否足够支持锄地、清障、浇水等操作。
- 背包空间是否可能被清障掉落物塞满。
- 工具或种子在箱子中时，由未来 Chest/取物节点根据资源缺口补计划并取回。

预期行为：

- 可恢复问题写入 blackboard，让后续 LLM 或规划器补计划，例如去买种子、从箱子取工具、回家睡觉。
- 局部问题尽量跳过单个 tile，不直接终止整个 FarmTask。
- 明确不可执行的问题应安全 `IDLE` 并输出结构化失败原因。

### 2. Farm 失败恢复细化

目标：把 Farm P1 中的失败区分为临时失败、局部失败和任务级失败。

需要细化：

- 站位失败：优先软恢复和重新规划站位，不要立刻把 tile 永久失败。
- 工具动作未命中：等待工具收招后，根据最新 state 决定是否重试。
- 锄地失败：如果 tile 仍可锄，应有限重试；如果不可锄，应跳过。
- 播种失败：如果缺种子或目标不可播种，应区分任务级失败和 tile 级跳过。
- 浇水失败：保留重试队列，只要 `HasCrop=True` 且 `IsWatered=False`，不要因为一次时序问题永久跳过。
- 补水失败：需要明确是找不到水源、无法到达水源、工具缺失，还是水壶 state 未刷新。

### 3. Farm 测试数据和验收场景

目标：让 Farm 的每次改动都能通过固定 mock 场景快速复现。

建议保留/补充这些测试任务：

- `FARM_P0_*`：指定地块浇水、自动浇水、缺水补水。
- `FARM_P1_1`：小区域种植并浇水，障碍较少。
- `FARM_P1_2`：大区域种植并浇水，包含 Grass、Weeds、Twig、Stone、Tree、TreeStump。
- 种子不足场景。
- 水壶没水场景。
- 区域内部分 tile 不可锄/不可种场景。
- 已有作物只浇水场景。

验收重点：

- 不重复锄同一个地块。
- 不往无作物地块浇水。
- 不在工具动作未收招时叠加新动作。
- 清障、锄地、播种、浇水都必须通过最新 state 验证结果。
- 出现失败时人物必须停止，不保留旧 MOVE 方向。

## P1 暂缓但需要记录的任务

### Chest / Inventory 后续能力

Chest P0 已完成最小闭环：指定 `chest_tile`，通过 `QUERY_CHESTS` 校验坐标，站到箱子旁，通过 SMAPI `TAKE_ITEMS_FROM_CHEST` 一次性批量取出指定物品清单，并用背包 state 验证数量增加。若指定坐标不存在但当前场景只有一个箱子，P0 会自动使用唯一箱子的真实坐标。

后续按以下顺序推进：

#### Chest P1：指定箱子存物

目标：

```text
站到指定 chest_tile 旁 -> PUT_TO_CHEST -> 验证背包数量减少
```

需要实现：

- C# Executor 新增 `PUT_TO_CHEST`。
- Python `ChestTask` 增加 `PUT`。
- `ChestNode` 支持把背包里的指定物品放入指定箱子。
- 处理背包没有物品、箱子满、部分成功和验证超时。

#### Chest P2：查询箱子内容与缓存

目标：

```text
QUERY_CHEST_CONTENT -> 写入 MapKnowledgeCache
```

需要实现：

- `QUERY_CHESTS` 已在 Chest P0 中基础接入，用于返回当前地点箱子坐标和基础信息；后续需要把结果写入缓存并处理过期。
- C# Executor 新增 `QUERY_CHEST_CONTENT`，返回指定箱子的 `Items` 摘要。
- Python 解析箱子查询结果。
- `MapKnowledgeCache` 增加箱子位置和箱子内容缓存。
- 取放成功后更新或失效对应缓存。

#### Chest P3：自动选择箱子

目标：

```text
ChestTask 不指定 chest_tile -> 查缓存/低频查询 -> 选择含目标物品且距离近的箱子
```

需要实现：

- `ChestTask.chest_tile` 允许为空。
- 优先从 `MapKnowledgeCache` 找含目标物品的箱子。
- 缓存缺失或疑似过期时调用 `QUERY_CHESTS` / `QUERY_CHEST_CONTENT`。
- 多个箱子都满足时，优先选择当前玩家到箱子的距离更短者。

#### Chest P4：Farm 缺资源恢复联动

目标：

```text
FarmResourceCheckNode 发现缺工具/种子
    -> LLM/Planner 生成 ChestTask
    -> ChestNode 取回资源
    -> 重新执行 FarmTask
```

短期可以先用 mock 数据验证：

```text
RouteTask("Farm") -> ChestTask("取防风草种子") -> FarmTask("种植并浇水")
```

长期由 Planner 根据 `blackboard.farm_missing_resources` 和 `farm_resource_recovery_hint` 自动补恢复计划。

### 区域规划策略

该任务先记录，暂不优先实现。

未来区域规划策略更适合接入 AI / Planner，由 Agent 根据目标、当前位置、农场布局、资源数量和长期收益给出规划区域。

未来可能的输入：

- 目标作物和数量。
- 当前季节、日期、天气。
- 当前背包种子数量。
- 玩家当前位置。
- 农场可耕地状态。
- 障碍物分布。
- 水源、箱子和建筑位置。

未来可能的输出：

- `area_origin`
- `area_width`
- `area_height`
- 或更灵活的 `target_tiles`
- 规划原因，例如“离玩家近、靠近水源、障碍少、形状整齐”

当前阶段仍允许 mock 数据或人工指定 `area_origin / area_width / area_height`，不要为了区域规划策略提前扩大 FarmNode 职责。

## P2 候选任务

### 1. Daily Water

目标：对已有作物执行每日浇水。

和当前 `WATER` 的区别：

- 更强调“今天未浇水的作物全部处理”。
- 不应该要求 seed_name。
- 需要支持水壶没水自动补水。
- 应跳过不需要浇水或已经浇水的地块。

### 2. Harvest

目标：收获成熟作物。

需要补充 state：

- 作物是否成熟。
- 作物名称或 ID。
- 是否可收获。
- 收获后是否保留作物，例如多次收获作物。

### 3. Replant

目标：收获后补种。

大致流程：

```text
收获成熟作物 -> 检查空地/已锄地 -> 切种子 -> 播种 -> 浇水
```

### 4. Farm 日程化

目标：从单个 FarmTask 升级到每日农场 routine。

可能流程：

```text
起床 -> 检查天气 -> 浇水/跳过雨天 -> 收获 -> 补种 -> 整理背包/箱子 -> 出门执行其他任务
```

该能力依赖更多 state，例如时间、天气、体力、金钱、箱子内容、背包容量和商店状态，暂不进入当前 P1。

## 跨模块依赖

Farm 后续开发依赖这些基础能力继续稳定：

- `PositioningController`：候选站位 + 工具目标地块。
- `ToolActionTracker`：等待工具动作开始、收招和超时。
- `SwitchToolNode`：根据 owner 和目标工具切换工具。
- `ClearObstacleNode`：Route/Farm 共用清障能力。
- `MapKnowledgeCache`：缓存水源、未来箱子/采集物等低频地图知识。
- SMAPI state：持续补齐体力、时间、天气、作物成熟、背包容量等结构化字段。

## 当前建议顺序

1. Chest P1：指定箱子存物。
2. Chest P2：查询箱子内容，并把箱子位置/内容接入 `MapKnowledgeCache`。
3. Chest P3：自动选择箱子。
4. Chest P4：Farm 缺资源恢复联动。
5. Farm 资源检查增强：体力、背包容量、箱子取物恢复计划。
6. Farm 失败恢复细化。
7. Farm mock 测试数据与验收日志整理。
8. Daily Water。
9. Harvest。
10. Replant。
11. AI / Planner 接入区域规划策略。
12. Farm 日程化。
