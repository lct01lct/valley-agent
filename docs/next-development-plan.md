# 下一步开发计划

更新时间：2026-07-24

本文专门记录接下来要开发的功能、优先级和暂缓事项。`docs/current-stage.md` 继续作为当前阶段事实、进度和验收标准；本文更偏任务队列和开发路线。

## 当前主线

短期主线仍是把“行为树 + SMAPI state + 确定性节点”的闭环做稳定。

Farm P1 当前先暂停继续扩展。已有 Farm 模块已经能证明“确定性农业技能 + Chest 资源恢复 + 工具归还”这条链路成立；后续资源管理、背包容量、体力消耗和复杂恢复更适合放到 Mining 模块中验证，因为采矿场景会更密集地触发这些问题。

下一阶段 Mining 的主线不是先做完整采矿收益最大化，而是先跑通矿洞核心循环：

```text
进入矿洞 -> 找到下一层 -> 第二层开始处理战斗风险 -> 再做资源采集与资源管理
```

因此开发优先级调整为：先完成“找到下一层”，再接 Defend，最后把体力、背包、箱子恢复和目标资源采集逐步叠上去。

Farm 当前保留为可复用的基础农业技能：

```text
规划候选地块 -> 批量清障 -> 批量锄地 -> 批量播种 -> 批量浇水 -> 缺水补水 -> 结果验证
```

## Farm 暂停边界与未来待办

### 当前暂停边界

Farm 当前先停在“P1 基础闭环可用”的边界：

- 保留 `WATER`：自动选择未浇水作物，或按指定 `target_tiles` 浇水。
- 保留 `PLANT`：规划区域后批量清障、锄地、播种。
- 保留 `PLANT_AND_WATER`：规划区域后批量清障、锄地、播种，最后浇水。
- 保留缺水补水：水壶没水时前往 Farm 水源接水。
- 保留基础资源检查：检查 Hoe、Watering Can、种子和清障工具是否在背包/工具栏。
- 保留 Chest 恢复 mock：缺工具/种子时可生成 ChestTask 取回资源，并在 Farm 完成后归还借出的工具。

暂时不继续在 Farm 模块里扩展完整资源管理。体力、背包容量、复杂箱子搜索、资源不足恢复和工具归还失败等问题，后续优先在 Mining 模块中实现和验证，再把稳定下来的通用能力回流给 Farm。

### 未来 Farm 需要继续做的内容

这些任务先记录，后续当 Mining 的资源管理底座稳定后再回来做：

1. Farm 资源检查增强：
   - 检查当前体力是否足够支持锄地、清障、播种、浇水。
   - 检查背包容量，避免清障掉落物或收获物导致背包满。
   - 区分“资源当前不在背包”“箱子也没有资源”“资源在其他场景箱子里”等恢复分支。
   - 处理工具借出后归还失败、工具箱不存在、箱子满等情况。
2. Farm 失败恢复细化：
   - 站位失败时优先重新选择候选站位，不立即永久跳过 tile。
   - 工具动作未命中时等待收招后按最新 state 决定重试。
   - 锄地、播种、浇水分别区分 tile 级失败和任务级失败。
   - 补水失败时区分找不到水源、无法到达水源、工具缺失、水壶 state 未刷新。
3. Farm 验收场景整理：
   - 固定 mock 覆盖 `FARM_P0_*`、`FARM_P1_1`、`FARM_P1_2`、`FARM_P1_3`。
   - 增加种子不足、水壶没水、已有作物只浇水、区域内不可锄/不可种等场景。
   - 记录完成时间、动作次数、失败 tile、重试次数和最终状态。
4. Farm P2 候选能力：
   - `Daily Water`：每天给所有需要浇水的作物浇水，雨天跳过。
   - `Harvest`：识别成熟作物并收获。
   - `Replant`：收获后补种并浇水。
   - Farm 日程化：起床后根据天气、作物、体力和背包状态安排农场 routine。
5. 区域规划策略：
   - 暂不在 FarmNode 内硬编码复杂规划。
   - 未来由 AI / Planner 根据农场布局、当前位置、水源、箱子、障碍、作物目标和长期收益给出 `target_tiles` 或 `area_origin / area_width / area_height`。

## Mining 开发路线

### Mining P0：找到下一层

目标：在矿洞第一层完成“找到并进入下一层”的最小闭环。第一层默认没有怪物，因此 P0 不处理战斗。

当前状态：已基础实现 `MiningTask`、`MiningResourceCheckNode`、`MineNode`、`INTERACT_TILE`、`MineLevel`、`Ladders`、`MiningNodes` 和 `MineEntrances` 协议，下一步需要进入游戏实测并根据日志校正矿洞入口/梯子识别。

推荐任务：

```text
RouteTask("Mine")
-> MiningTask(mine_action="FIND_NEXT_LEVEL", target_mine_level=2)
```

P0 行为流程：

```text
确认当前在 Mine 且 MineLevel == 1
-> 查询当前层是否已有 Ladder / Stairs
-> 如果已有下一层入口，移动到入口并进入
-> 如果没有入口，选择附近 Stone / MiningNode
-> 切 Pickaxe
-> 站到目标上下左右相邻格并面向目标
-> 挥镐并等待工具动作收招
-> 验证 Stone / MiningNode 消失
-> 重新查询 Ladder / Stairs
-> 找到入口后进入下一层
-> 验证 MineLevel == 2
```

P0 需要补齐或确认的 state / action：

- `MineLevel`：当前矿洞层数。
- `Ladders` / `Stairs`：当前层可进入下一层的入口坐标。
- `MiningNodes` 或可复用的结构化障碍信息：至少能识别可破坏 Stone。
- 当前工具、背包 `Items`、`UsingTool`、`CanMove`、`ToolTarget`。
- 进入下一层动作：优先设计通用 `INTERACT_TILE`，短期也可封装专用 `ENTER_LADDER`，但必须通过最新 state 验证层数变化。

P0 验收标准：

- 能进入矿洞第一层。
- 能读取并验证 `MineLevel == 1`。
- 能识别已有下一层入口。
- 没有入口时能打碎 Stone / MiningNode 并重新检查入口。
- 能进入下一层，并通过 state 验证 `MineLevel == 2`。
- 失败、超时或出现 P0 不处理的怪物时，必须发送 `IDLE` 并给出明确失败原因。

### Mining P1：第二层开始接入 Defend

目标：进入第二层后，开始处理怪物导致的测试不稳定问题。Defend 是 Guard 分支，不消费 MiningTask，应抢占 Mining/Route/Farm。

Defend 策略先分两类：

- `AVOID`：躲避战斗，优先保证生存并继续 Mining。
- `FIGHT`：怪物近身或阻挡任务时，攻击以保证安全。

P1 行为流程：

```text
Mining 进入 MineLevel >= 2
-> DefendNode 读取 monsters / player health
-> 威胁不在范围内：Defend 返回 FAILURE，Mining 继续
-> 怪物接近但未贴脸：选择远离怪物的安全 tile
-> 怪物贴脸或无法躲避：切武器，面向怪物，攻击
-> 威胁解除后恢复 Mining
```

需要补齐或确认的 state：

- `Monsters`：怪物名称、位置/tile、Health、是否死亡。
- 玩家 `Health`。
- 当前武器/工具栏信息。
- 可通行邻格或局部障碍信息。

P1 验收标准：

- 没有怪物时不干扰 Mining。
- 怪物进入威胁范围时能暂停 Mining。
- 近身威胁能攻击或击退。
- 低血或非必要战斗时优先躲避。
- 威胁解除后 Mining 可以继续。

### Mining P2：基础采矿与资源节点选择

目标：从“找下一层”扩展到“打碎指定数量资源节点”。

推荐任务：

```text
MiningTask(mine_action="BREAK_ROCKS", count=5)
MiningTask(mine_action="COLLECT_RESOURCE", target_resource_types=["Copper Ore"], count=10)
```

P2 重点：

- 抽出 Mining 目标选择策略：优先选择最近、可达、可破坏的资源节点。
- 支持普通 Stone、Ore、Gem Node、Container 等类型逐步扩展。
- 复用 `PositioningController` 做候选站位与 ToolTarget 对准。
- 复用 `ToolActionTracker` 等待挥镐收招。
- 通过最新 state 验证节点消失或目标资源数量增加。

### Mining P3：资源管理底座

目标：在 Mining 中验证通用资源管理能力，然后再回流 Farm。

P3 需要实现：

- 体力检查：体力不足时停止采矿或触发恢复/撤退。
- 背包容量检查：背包满时停止采矿或触发整理/回箱子。
- 工具检查：缺 Pickaxe 时触发 Chest 恢复；借出工具任务结束后归还。
- 掉落/拾取验证：打碎节点后验证目标资源是否进入背包，或记录需要移动拾取的掉落物。
- 任务级安全停机：失败、资源不足、背包满、体力低时都要发送 `IDLE`。

### Mining P4：楼层策略与目标资源采集

目标：不只进入下一层，而是围绕目标资源、楼层和时间做决策。

后续能力：

- 指定目标层数，例如进入 5 层、10 层。
- 根据资源类型选择楼层，例如铜矿、铁矿、煤矿。
- 找不到楼梯时继续打石头。
- 找到楼梯后判断是否下楼或继续采当前层资源。
- 低体力、低血量、时间过晚时撤退。

### Mining P5：长期记忆与机会资源

目标：把 Mining 中遇到但暂不处理的资源纳入机会记忆。

后续能力：

- 记录路上看见但未采集的矿石、宝石、箱子、怪物密集区域。
- 将矿洞箱子、入口、危险区域写入 `MapKnowledgeCache` 或未来 `PersistentMemoryStore`。
- 让 Planner 能基于记忆生成下一次 Mining 路线。

### Mining P6：收益型采矿与日程联动

目标：从确定性技能升级成更完整的采矿 routine。

后续能力：

- 根据时间、体力、背包、目标资源和危险程度决定继续深入或撤退。
- 结合 Farm/Chest/Route：出门前取工具，结束后归还工具和整理资源。
- 结合未来交易/制作系统：采集目标服务于制作、升级工具或赚钱。

## 已暂缓的 Farm P1 优先任务

以下内容原本属于 P1 优先任务，但现在统一暂缓，等待 Mining 模块验证资源管理底座后再回流。

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
- 工具或种子在箱子中时，当前 mock Planner 已可根据资源缺口补 `ChestTask` 并取回；后续需要升级成真实 Planner 策略。

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

Chest P0/P1 已完成最小闭环：指定 `chest_tile`，通过 `QUERY_CHESTS` 校验坐标，站到箱子旁，通过 SMAPI `TAKE_ITEMS_FROM_CHEST` 一次性批量取出指定物品清单，或通过 `PUT_ITEMS_TO_CHEST` 一次性批量存入指定物品清单，并用背包 state 验证数量变化。若指定坐标不存在但当前场景只有一个箱子，会自动使用唯一箱子的真实坐标。

后续按以下顺序推进：

#### Chest P1：指定箱子存物

当前已完成基础接入：

```text
站到指定 chest_tile 旁 -> PUT_ITEMS_TO_CHEST -> CLOSE_MENU -> 验证背包数量减少
```

已实现：

- C# Executor 新增 `PUT_ITEMS_TO_CHEST`。
- Python `ChestTask.chest_action` 支持 `PUT`。
- `ChestNode` 支持把背包里的指定物品批量放入指定箱子。
- 处理背包没有物品、箱子满、部分成功和验证超时。

后续增强：

- 对箱子满、部分存入和关键工具缺失做更细粒度恢复计划。
- 结合 Chest P2 的箱子内容缓存，在存取成功后失效或更新缓存。

#### Chest P2：查询箱子内容与缓存

目标：

```text
走到箱子旁 -> 打开箱子 -> QUERY_CHEST_CONTENT -> 写入 MapKnowledgeCache -> 关闭箱子
```

当前已基础接入：

- `QUERY_CHESTS` 返回当前地点箱子坐标和基础信息，并写入 `MapKnowledgeCache` 箱子位置缓存。
- C# Executor 新增 `QUERY_CHEST_CONTENT`，用于箱子打开后返回指定箱子的 `Items` 摘要。
- Python 会先移动到箱子旁、打开箱子、等待界面稳定，再读取箱子内容并写入 `MapKnowledgeCache`。
- 取放成功后将对应箱子内容缓存标记为过期。
- `MapKnowledgeCache` 已预留箱子语义标签接口，用于未来记录“这个箱子打算存什么”，但当前不参与决策。

#### Chest P3：自动选择箱子

目标：

```text
ChestTask 不指定 chest_tile -> 查缓存 -> 缓存缺失时逐个打开箱子查看 -> 选择含目标物品且距离近的箱子
```

当前已基础接入：

- `ChestTask.chest_tile` 允许为空。
- `TAKE` 不指定 `chest_tile` 时，优先从 `MapKnowledgeCache` 找含目标物品的箱子。
- 缓存缺失时只用 `QUERY_CHESTS` 获取当前 `target_loc` 场景内箱子坐标，然后按距离走到候选箱子旁、打开查看、缓存内容；已知新鲜且不匹配当前取物需求的箱子会跳过，避免重复打开。
- 找到满足目标物品的箱子后停止查看，并在当前打开的箱子中取物。

后续增强：

- 自动存物策略：根据语义标签、已有同类物品、空位和距离选择存物箱。
- 跨场景搜索策略：由 Planner/LLM 基于记忆生成候选场景，不放入 ChestNode。
- 箱子内容查询可接入长期记忆，并在新一天、移动箱子、破坏箱子等事件中处理失效。

#### Chest P4：Farm 缺资源恢复联动

目标：

```text
FarmResourceCheckNode 发现缺工具/种子
    -> LLM/Planner 生成 ChestTask
    -> ChestNode 取回资源
    -> 重新执行 FarmTask
```

当前已基础接入 mock 恢复闭环：

```text
FarmResourceCheckNode 记录缺失资源和原始 FarmTask
-> LLM_Node mock 生成 RouteTask("Farm") + 多个 ChestTask(TAKE, chest_tile=None) + 原 FarmTask
-> 工具合并成一组取物任务，种子等堆叠物拆成独立取物任务
-> ChestNode 找到满足当前取物任务的箱子后取物，不继续翻看其余箱子；已知新鲜且不含目标资源的箱子会被缓存过滤
-> FarmTask 重新执行
-> FarmTask 完成后，ChestNode 根据 borrowed_chest_items 把借来的工具放回原箱子
```

当前 mock 测试重点：

- `FARM_P1_3`：以 `(43, 15)` 为 `area_origin` 规划 `7x7` 防风草种植并浇水。
- 背包缺工具或种子时，应补箱子取物计划；箱子有目标资源后立即停止继续翻看，已知不匹配的箱子不应重复打开。
- 农业任务结束后，应归还借出的工具；种子等消耗品不归还。

后续增强：

- 由真实 Planner 根据 `blackboard.farm_missing_resources`、`farm_missing_chest_items` 和 `farm_resource_recovery_hint` 自动补恢复计划。
- 支持跨场景候选箱子搜索，由 Planner 基于记忆决定先去哪一个场景。
- 接入 `ChestSemanticMemory` 的工具箱/种子箱标签，让语义记忆影响候选搜索顺序；语义记忆只做推荐，不替代真实开箱验证或结构化存取。
- 处理箱子也缺资源、多个箱子分散存放资源、背包空间不足、工具归还失败等恢复分支。

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

## Farm P2 候选任务（暂缓）

以下能力仍然属于 Farm 未来路线，但当前不进入下一阶段优先开发。等 Mining 模块把体力、背包容量、资源收集、资源恢复和撤退策略验证充分后，再回到 Farm 上实现。

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

1. Mining P0 设计：确认 `MiningTask`、`MineNode`、`MiningResourceCheckNode`、mock 数据和验收标准。
2. SMAPI state/action 补齐：`MineLevel`、`Ladders/Stairs`、`MiningNodes`、进入下一层动作。
3. Mining P0 实现：第一层找到下一层；没有入口时打 Stone 直到出现入口；验证进入第二层。
4. Defend P0/P1 接入：第二层开始识别怪物，先做躲避/近身攻击，保证 Mining 测试安全。
5. Mining P2 基础采矿：打碎指定数量 Stone / MiningNode，验证节点消失。
6. Mining P3 资源管理：体力、背包容量、Pickaxe 缺失恢复、掉落/拾取验证、工具归还。
7. Mining P4 楼层策略：目标层数、是否下楼、是否继续采当前层。
8. Mining P5 记忆接入：记录矿洞中遇到的资源、箱子和危险区域。
9. 将 Mining 中稳定的资源管理和失败恢复能力回流 Farm。
10. 再恢复 Farm 未来任务：Daily Water、Harvest、Replant、区域规划策略和 Farm 日程化。
