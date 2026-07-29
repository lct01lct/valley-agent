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

### Mining / Defend 战术层长期方案

当前状态：暂缓实现。该部分涉及角色决策架构升级，包括怪物风险、绕路、机会目标、冲层/刷矿/刷怪 profile 和多目标 utility 评分，改动范围较大。短期先不把它作为 Mining P0 和工具后处理通用化的前置条件；只保留设计方向和关键约束，等 Mining 基础动作、工具后处理、掉落物和资源目标稳定后再集中开发。

推荐采用“LLM / Planner 做战略，Utility AI 做战术，行为树做执行”的组合架构。

```text
LLM / Planner
-> 生成宏观任务和 TacticalProfile
-> Utility Tactical Resolver 根据当前 state 计算最值得做的战术目标
-> Behavior Tree 执行确定性动作并验证结果
```

这套方案适合《星露谷物语》这类多目标场景：冲层、刷矿、刷怪、采集、撤退和顺手机会目标经常互相冲突，不能只靠固定 `if/else`，也不应让 LLM 每 tick 直接判断按键。

#### 目标分层

战术层需要把目标分成三类：

- `Primary Objective`：主目标，决定当前任务是否成功，例如冲到目标层、收集指定矿石、击杀指定怪物。
- `Blocking Objective`：阻挡主目标的问题，例如怪物堵住梯子、石头挡住通路、障碍物阻挡交互站位。
- `Opportunity Objective`：顺手目标，例如路线旁边的矿石、宝石、低风险怪物或未来可记忆的资源点。

例如“冲层但顺手挖容易拿到的矿石”不是 `RUSH_LEVEL` 和 `RESOURCE_FARM` 二选一，而是：

```text
Primary Objective: RUSH_LEVEL
Blocking Policy: HANDLE_IF_BLOCKING
Opportunity Policy: EASY_ORE_ONLY
```

机会目标必须有预算，否则 Agent 容易从“冲层”退化成“看到什么都想做”：

- 最大绕路格数。
- 最大机会动作次数。
- 最大额外耗时。
- 最大风险分数。
- 是否只处理当前路径附近目标。

#### TacticalProfile

未来 AI / Planner 不应该直接返回每帧动作，而应返回宏观任务和战术倾向参数，例如：

```python
TacticalProfile(
    primary_objective="RUSH_LEVEL",
    risk_tolerance=0.4,
    opportunity_policy=OpportunityPolicy(
        enabled=True,
        targets=["Ore Node", "Gem Node"],
        max_detour_tiles=2,
        max_actions=3,
        max_extra_seconds=6.0,
        max_risk_score=0.2,
        only_if_on_path=True,
    ),
)
```

不同任务通过参数改变倾向，而不是复制多套节点：

- 冲层：下楼优先；怪物只有挡路或贴脸才处理；机会矿石只允许低风险、低绕路。
- 刷矿：目标资源优先；梯子可以作为机会目标；允许更大绕路和更多机会动作。
- 刷怪：怪物是主目标；资源采集降级为机会目标。
- 撤退：生存和离开矿洞优先；非必要战斗和采矿全部降级。

#### Utility Tactical Resolver

战术层应抽象成 `Utility Tactical Resolver`，输入当前任务、TacticalProfile、游戏 state、候选目标、怪物威胁、地形和资源状态，输出结构化决策：

```python
TacticalDecision(
    decision_type="CONTINUE_PRIMARY"
    | "BREAK_BLOCKING_STONE"
    | "COLLECT_OPPORTUNITY"
    | "ENGAGE_THREAT"
    | "REROUTE"
    | "DEFER_OBJECTIVE"
    | "RETREAT",
    reason="铜矿在当前路线旁 1 格，风险低，机会预算充足",
)
```

第一版评分可以保持简单：

```text
score = 主目标收益
      + 阻挡解除收益
      + 机会收益
      - 距离成本
      - 绕路成本
      - 风险成本
      - 时间成本
      - 资源成本
      - 机会预算消耗
```

示例：

- 冲层时梯子已出现：`进入梯子` 分数最高。
- 怪物堵住梯子：`ENGAGE_THREAT` 或 `REROUTE` 取决于风险和绕路成本。
- 路线旁 1 格有铜矿且无怪物：`COLLECT_OPPORTUNITY` 可以短暂抢占。
- 铜矿离路线 8 格：绕路成本超过预算，继续主目标。
- 低血或时间过晚：`RETREAT` 分数上升。

#### 与行为树的职责边界

- LLM / Planner：生成宏观任务、TacticalProfile 和失败后的补计划。
- Tactical Resolver：决定当前 tick 最值得推进的战术目标；不直接按键。
- DefendNode：只执行战斗动作，例如切武器、接近、面向、攻击和验证威胁解除；不要自己决定长期是否恋战。
- MineNode：只执行采矿/下楼/破石等确定性目标；遇到怪物、机会矿石或目标冲突时询问战术层。
- Behavior Tree：继续负责节点优先级、跨节点抢占、动作发送、状态验证、失败重试和安全 `IDLE`。

#### 推荐落地顺序

1. 在 Mining 内先实现最小 `TacticalProfile` 和 `TacticalDecision` 数据结构。
2. 把现有 Mining / Defend 中散落的怪物风险、换石头、绕路和贴脸攻击判断收束到 `MiningTacticalResolver`。
3. 先支持三个 profile：
   - `RUSH_LEVEL`：冲层，允许低成本机会矿石。
   - `RESOURCE_FARM`：刷矿，允许更高机会预算。
   - `COMBAT_FARM`：刷怪，主动选择目标怪物。
4. 先只让机会目标支持矿石 / 宝石节点，不急着接入所有采集物。
5. 当 Mining 稳定后，再把通用战术层扩展给 Route、Farm、Chest 和未来交易/制作模块。

### 工具动作后处理通用方案

目标：把挖矿、清障、砍树、锄地、浇水、打木箱等“使用工具后可能产生副作用”的问题抽成通用底座，而不是只在 MiningNode 内补丁式处理。

这类问题不只存在于 Mining：

- Mining 挥镐可能打碎 Stone / MiningNode，生成 Ladder、矿石、晶球或掉落物。
- Route / Farm 清障打碎 Stone、砍树、清 Twig / Weeds 时，也可能产生掉落物。
- Farm 锄地、浇水、清障会遇到工具动作收招、结果验证、背包容量和掉落物拾取问题。
- 打木箱/木桶需要用武器或工具破坏，并处理掉落物。
- 挖到晶球或触发提示框时，阻塞菜单可能暂停后续所有动作。

推荐边界：

```text
业务节点（MineNode / FarmNode / ClearObstacleNode / 未来 BreakContainerNode）
-> 发送 USE_TOOL / USE_ITEM / ATTACK
-> ToolActionTracker 等待 UsingTool / CanMove 完成
-> ToolAftermathService 处理工具动作后的副作用
-> 业务节点继续用最新 state 验证自己的目标结果
```

不要把这些能力合并成一个巨大 `UseToolNode`。原因是不同业务的成功判定不同：

- 挖石头：Stone / MiningNode 消失，或目标 tile 出现 Ladder。
- 砍树：Tree 阶段变化、倒下或消失。
- 锄地：目标地块出现 HoeDirt。
- 浇水：目标作物或地块变为 watered。
- 打木箱/木桶：容器消失并可能产生掉落物。

通用层只处理“工具动作之后系统层面的副作用”，业务节点仍负责判断“我的目标是否完成”。

#### UiGuardNode

优先实现一个高优先级通用节点，用于分类并处理非预期阻塞 UI。

建议挂载位置：

```text
Selector
├── Sequence("Guard")
│   ├── UiGuardNode
│   └── DefendNode
├── Route
├── Chest
├── Farm
├── Mining
└── Think
```

职责：

- 读取 C# Observer 暴露的菜单/阻塞状态。
- 如果存在晶球提示、拾取提示、打烊、上锁或其他会阻塞玩家行动的 DialogueBox，先分类为结构化 UI 事件，再按策略关闭。
- 只处理“解除阻塞”和“事件归一化”，不消费业务任务，不判断采矿、清障、Farm、NPC 或商店目标是否完成。
- 关闭后把 `ActionFeedbackEvent` 写入 blackboard；若是打烊/上锁等业务失败事件，则触发重新规划，若只是晶球提示等普通阻塞事件，则让原任务下一 tick 继续。

短期验收：

- 挖矿过程中触发晶球提示后，Agent 能自动关闭提示并继续当前 MiningTask。
- Route / Farm 清障触发类似提示时，也不会卡住行为树。
- 没有菜单时节点快速返回 `FAILURE`，不干扰其他分支；遇到 NPC、商店、箱子等业务节点登记的 `InteractionSession` 时，不误关预期 UI。

#### ToolAftermathService

当前状态：已新增最小版轻量服务层，不是一开始就做复杂节点。当前接入 Mining 和 ClearObstacle，用于工具收招后观察阻塞 UI、目标地块变化，以及 Mining 可选的破石后梯子查询。C# Observer 已同步当前场景 `Debris`，Python `state.debris` 已可被 `ToolAftermathService` 用于记录目标附近掉落物。`CollectLootNode` 已支持工具动作后的近距离可达掉落物自动拾取；采集物分类、批量拾取策略和更复杂的 aftermath checker 仍待后续扩展。

职责：

- 在工具动作收招后读取最新 state。
- 检查是否有阻塞菜单，需要时写入 blackboard 或直接让 `UiGuardNode` 抢占。
- 检查是否产生新掉落物、新采集物或关键结果，例如 Ladder。
- 生成结构化 aftermath 结果，供业务节点决定下一步。

当前工具效果等待策略：

- 工具动作先由 `ToolActionTracker` 观察 `UsingTool=True`，再等待 `UsingTool=False` 且 `CanMove=True`，确认动作收招。
- 收招后进入 `ToolAftermathService`，用业务节点提供的 effect checker / side effect checker 验证最新 state。
- `1.0s` 工具效果等待窗口只用于“完全没有观察到预期效果或有效副作用”的保护性超时，不是每次工具动作后的固定停顿。
- 对 Scythe / 剑等范围工具，预期目标没有一次性完全清掉时，只要范围内预期障碍减少，或目标附近出现可拾取掉落物，就应视为本次工具动作有效；随后由业务节点决定继续补刀、切换目标或进入拾取。
- Mining 已补充一个短期保护：破石时如果工具动画期间刷新梯子，仍先完成工具收招和 aftermath；交互梯子前先处理当前层已登记、延迟或最近工具来源附近的可拾取掉落物，避免切层丢失。
- 后续扩展到 BreakContainer、采集物、矿石和树木时，也应遵守同一原则：通用层观察副作用，业务节点解释目标是否完成，不用固定 sleep 替代 state 验证。

示例结构：

```python
ToolAftermathResult(
    has_blocking_menu=True,
    new_ladder_tile=Tile(12, 20),
    loot_tiles=[Tile(13, 20), Tile(14, 20)],
    should_collect_loot=True,
)
```

短期只需要支持 Mining / ClearObstacle 的最小信息；不要一开始就追求完整物品分类。

#### 掉落物与拾取策略

掉落物管理建议分成两层：

1. `LootAwarenessService`：基于 `state.debris` 识别当前场景掉落物、位置和可拾取状态。
2. `CollectLootNode`：根据 blackboard 中的拾取请求移动并捡起；当前只捡可达目标，不为拾取触发清障，树掉落物允许部分跳过。

拾取策略不要写死在节点里，应由业务模块或战术层决定：

- Mining 冲层：只捡路径旁、距离很近、低风险的掉落物；主目标仍是下楼。
- Mining 刷矿：可批量挖完一片区域后统一拾取。
- Route 清障：只顺路拾取，不为掉落物绕远路。
- Farm 清障：可在清完规划区域后批量拾取木材、石头、纤维等。
- Combat / Defend：战斗结束且安全后再拾取。

短期建议先实现两种模式：

- `IMMEDIATE`：工具动作后立刻尝试拾取附近掉落物。
- `BATCH`：业务阶段结束后统一拾取已记录的掉落物。

后续需要继续增强拾取验证：如果 C# Debris 能稳定导出 `QualifiedItemId` / 物品名，则 Python 可以用“背包数量增加”确认堆叠物或新物品已拾取；如果只有泛化 `RESOURCE` / `OBJECT`，仍需要保留掉落物消失、位置移动和短窗口动态观察作为兜底。

#### Mining 目标类型扩展

Mining P2 不应只理解 Stone / MiningNode。当前已新增第一版结构化 `MineTarget` / `MineTargetSelector`，并接入 state 中的 Ladder、MineEntrance、Stone、MiningNode、Collectible 和 BreakableContainer。后续需要继续把矿井目标选择和执行策略纳入同一抽象：

```text
MineTarget
├── Collectible      徒手拾取：地晶、石英、火水晶等
├── MiningNode       镐子挖：铜矿、铁矿、宝石矿等
├── Stone            镐子挖：普通石头
├── BreakableBox     武器打破：木箱 / 木桶
└── Ladder           交互：进入下一层
```

不同目标绑定不同执行方式：

- `Collectible`：移动到可拾取范围，使用交互或直接走近拾取，并验证物品进入背包或地图目标消失。
- `MiningNode` / `Stone`：切 Pickaxe，站到相邻格，面向目标，挥镐，等待收招，验证目标消失或资源变化。
- `BreakableBox`：切武器，站到相邻格，面向目标，攻击，等待动作结束，验证容器消失并处理掉落物。
- `Ladder`：站到可交互位置，足够贴近并交互，验证 `MineLevel` 变化。

目标选择由 Mining 目标选择器或战术层决定，不应散落在 MineNode 的各个分支里。

当前第一版落地边界：

- `Ladder` / `MineEntrance`：来自 `state.ladders` / `state.mine_entrances`，用于交互目标选择。
- `Stone` / `MiningNode`：来自 `state.layers["Stone"]` 和 `state.mining_nodes`，用于破石候选选择。
- `Collectible`：来自 `state.mine_collectibles`，用于后续走近/交互拾取地晶、石英、火水晶等徒手采集物。
- `BreakableContainer`：来自 `state.mine_breakable_containers`，用于后续切武器打破木箱/木桶并处理掉落物。
- MineNode 仍保留现有执行状态机，避免在目标抽象第一步就重写完整 Mining 流程。

#### 推荐讨论和开发顺序

后续按以下顺序逐项商讨和实现：

1. 先设计 C# Observer 如何暴露 `MenuState`，以及 Python 如何把 DialogueBox 文本分类为结构化 UI 事件。
2. 实现 `UiGuardNode`，优先解决晶球提示框导致的全局卡顿，同时保留打烊/上锁触发重新规划的语义。
3. 设计 C# Observer 如何暴露矿井采集物、矿石节点、木箱/木桶和掉落物。
4. 抽象 `MineTarget` 与 Mining 目标选择器，先支持 Collectible / MiningNode / Stone / BreakableBox / Ladder 的基础分类。
5. 设计 `ToolAftermathService`，先用于 Mining 和 ClearObstacle，避免每个节点重复处理菜单、掉落物和 Ladder 查询。
6. 扩展 `CollectLootNode`：在当前最小版附近拾取基础上，继续支持批量拾取、价值过滤和战术 profile 控制。
7. 将稳定后的工具后处理能力逐步回流 Route 清障、Farm 清障、砍树和未来战斗掉落。

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
