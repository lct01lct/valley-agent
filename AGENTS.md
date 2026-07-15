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
- A*、交互节点和后续技能负责确定性执行；不要让 LLM 直接参与每帧按键控制。

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
│   └── RouteNode
└── LLM_Node
```

`ValleyAgent` 在主循环中先刷新 `PlayerContext.state`，再运行行为树。`Selector` 每个 tick 从左到右轮询子节点，遇到 `RUNNING` 或 `SUCCESS` 就停止本轮扫描。

因此，`Guard`、`Route` 分支和 `LLM_Node` 是顶层 Selector 下的同级候选分支。`LLM_Node` 是最后的兜底分支：只有前面的确定性节点没有可执行工作时，它才负责生成或补充宏观计划。

### AgentBlackboard 是调度状态中心

`AgentBlackboard` 是跨节点通讯和系统调度状态中心，用于保存宏观计划、当前步骤、LLM 异步状态、节点间信号和恢复上下文。

精确边界：

- `ValleyAgent` 和 `Selector` 负责驱动 tick 与轮询节点。
- `AgentBlackboard` 负责保存调度状态和跨节点信号。
- 各行为树节点通过 blackboard 协作，但不应绕过黑板私自耦合彼此内部状态。

## Codex 必须显式读取的项目 Skills

项目内 Skill 位于 `skills/`。项目级 Skill 不一定会被 Codex 自动发现，因此不要依赖自动触发；符合以下条件时，必须显式打开并遵循对应 `SKILL.md`：

- 新增、修改、重构或审查代码：`skills/code-style/SKILL.md`
- 新增、修改或审查行为树节点：`skills/behavior-tree/SKILL.md`

如果任务同时涉及编码和行为树，应先读取 `code-style`，再读取 `behavior-tree`。`SKILL.md` 引用的 `references/`、`assets/` 或模板应按其中的路由说明继续读取。用户明确要求的规则优先于 Skill；行为树专属契约以 `behavior-tree` Skill 为准。

## 当前主要模块

- `main.py`：当前入口，加载环境、清理日志、初始化 `ValleyAgent` 并提交任务。
- `agent/valley_agent.py`：创建行为树、`PlayerContext` 和 `AgentBlackboard`，运行高频 tick 循环。
- `agent/behavior_tree/`：行为树节点、黑板、玩家上下文、规划兜底和寻路控制。
- `agent/action/valley_action/AStar.py`：本地 A* 寻路、路线动作标注和移动命令生成。
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
- 清障必须验证工具可用、体力允许、玩家朝向正确，并在动作后从新状态确认障碍已经消失。
- 不重复实现寻路或动作逻辑；共享行为下沉到 A*、动作层或复用节点。
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
- A*、代价函数和跨地图规划：使用小型确定性地图与固定障碍布局测试。
- 行为树节点：用可控黑板、状态快照和假 Executor 验证状态转换。
- SMAPI 协议：同时验证 C# 序列化/响应和 Python 解析/超时。
- 游戏内行为：检查 `logs/`，确认移动、清障、开门和最终地点状态真实发生。

汇报完成时，必须说明运行了哪些检查、哪些结果已通过，以及哪些仍需要进入游戏实测。

## Git 与工作区安全

- 编辑前执行 `git status --short`。
- 不回滚或覆盖与当前任务无关的用户改动。
- 不删除生成资源、日志或实验文件，除非用户明确要求。
- 不提交 `.env`、日志、截图或本机专属部署路径，除非用户明确要求追踪。
