# Valley Agent

Valley Agent 是一个让 AI 自主游玩《星露谷物语》的实验项目。项目当前采用“行为树 + AI 决策 + SMAPI 结构化状态”的架构：AI 负责决定宏观目标；行为树负责实时调度，确定性算法和游戏技能负责执行与验证；SMAPI 用于底层的游戏行为实现。

当前入口文件是 `main.py`。

## 项目目标

自主规划并完成《星露谷物语》中的长期任务，包括导航、交互、种植、采集、交易、战斗和日程管理。

项目不会让 LLM 直接控制每一帧按键。实时动作由行为树、A*、确定性节点和 SMAPI 状态验证完成；LLM 只负责低频的高层计划、失败反思和记忆。

## 当前阶段

当前第一阶段目标是完成智能寻路 MVP，并使用 `LLM_Node` 返回的模拟 `RouteTask` 验证完整执行闭环。这里的“智能寻路”包括避障、动态重规划、破坏障碍物、开门，以及从最新游戏状态验证到达结果。

阶段进度、已知缺口、验收标准和开发顺序见：

- `docs/current-stage.md`

## 系统架构

```mermaid
flowchart LR
    subgraph StateLoop["独立高频 State 通道（不依赖行为树）"]
        Game["Stardew Valley"] --> Observer["SMAPI Observer"]
        Observer --> ObserverClient["Python Observer Client"]
        ObserverClient --> Context["PlayerContext.state"]
    end

    Main["main.py"] --> Agent["ValleyAgent<br/>tick driver"]
    Agent --> Selector["Selector<br/>从左到右轮询"]

    Selector --> Guard["Guard 分支<br/>Defend_Node"]
    Selector --> RouteBranch["Route 分支<br/>OpenDoorNode + RouteNode"]
    Selector --> LLM["LLM_Node<br/>最后兜底"]

    Blackboard["AgentBlackboard<br/>跨节点通讯与调度状态中心"]
    Guard <--> Blackboard
    RouteBranch <--> Blackboard
    LLM <--> Blackboard

    Context -.->|最新状态| Guard
    Context -.->|最新状态| RouteBranch
    Context -.->|规划上下文| LLM

    RouteBranch --> AStar["A* / 确定性动作"]
    AStar --> ExecutorClient["Python Executor Client"]
    ExecutorClient --> Executor["SMAPI Executor"]
    Executor --> Game
```

这张图里有两个需要分清的通道：

- `State` 是独立高频更新的数据通道，由 SMAPI Observer 持续写入 `PlayerContext.state`，不依赖行为树运行。
- 行为树是运行时控制通道，`ValleyAgent` 每个 tick 刷新 state 后轮询节点，并通过 `AgentBlackboard` 协调计划和跨节点信号。

## 当前行为树

```text
Selector
├── Sequence("Guard")
│   └── Defend_Node
├── Sequence("Route")
│   ├── OpenDoorNode
│   └── RouteNode
└── LLM_Node
```

行为树每个 tick 从高优先级分支开始扫描：

1. `Defend_Node` 预留给紧急安全行为。
2. `OpenDoorNode` 和 `RouteNode` 消费当前路线计划并执行确定性动作。
3. `LLM_Node` 是最后兜底：当前面节点没有可执行计划时，才在后台生成模拟计划并写入黑板。
4. 新计划到达后，Selector 重新从高优先级节点扫描。

因此，`LLM_Node` 和 `Route` 分支在顶层 Selector 视角是同级概念，只是优先级更低、职责更偏规划兜底。

## AgentBlackboard 的角色

`AgentBlackboard` 是跨节点通讯和调度状态中心。它保存宏观计划、当前步骤、LLM 异步状态、节点间信号和恢复上下文。

它不是具体执行节点，也不是替代行为树的 tick driver。当前 tick 仍由 `ValleyAgent` 和 `Selector` 驱动；blackboard 负责让多个节点围绕同一份计划和状态信号协作。

## 目录结构

```text
valley-agent/
├── main.py                         # 当前入口
├── agent/
│   ├── valley_agent.py             # 主循环和行为树装配
│   ├── base_task.py                # 基础任务类型
│   ├── behavior_tree/              # 节点、黑板和玩家上下文
│   ├── action/valley_action/       # 动作模型和 A*
│   └── prompt/                     # Planner 提示词
├── server/
│   └── valley_server.py            # Python TCP 客户端与状态解析
├── StardewMemoryExporter/          # SMAPI Observer/Executor Mod
├── skills/                         # 项目内 Codex Skills
├── docs/
│   └── current-stage.md            # 当前阶段目标、进度和验收
├── scripts/                        # 环境和日志脚本
├── AGENTS.md                       # Agent 开发约束
└── README.md
```

## 环境与运行

项目当前使用：

- macOS
- Stardew Valley 1.6 + SMAPI
- Conda 环境名：`valley`
- Python：`3.13.5`
- .NET / C# SMAPI Mod

安装并运行 Python Agent：

```bash
./setup.sh
```

环境已经准备好时可以直接运行：

```bash
conda activate valley
python main.py
```

构建并部署 SMAPI Mod：

```bash
./StardewMemoryExporter/setup.sh
```

首次运行会由 `scripts/init_env.py` 创建 `.env`。当前主要配置包括：

```dotenv
SMAPI_SEVER_HOST=127.0.0.1
SMAPI_OBSERVER_SERVER_PORT=9999
SMAPI_EXECUTOR_SEVER_PORT=8888
GOOGLE_API_KEY=<Your_Google_API_Key>
```

变量名中的 `SEVER` 是当前代码已经使用的历史拼写，修改前需要同步迁移消费端。不要提交真实 API Key。

## 项目 Skills

项目内 Codex Skill 不保证被自动发现。使用 Codex 开发时，应根据任务显式读取：

- `skills/code-style/SKILL.md`：编码、协议和验证规范。
- `skills/behavior-tree/SKILL.md`：行为树节点契约、模板和示例。

完整的强制加载规则见 `AGENTS.md`。

## 后续路线

完成智能寻路 MVP 后，再逐步推进：

1. 将 `main.py` 的自然语言任务稳定注入 Planner。
2. 用类型化 Plan Schema 替换模拟计划。
3. 接入真实 LLM 进行宏观任务生成和失败恢复。
4. 扩展时间、金钱、体力、背包、菜单、作物、天气和 NPC 状态。
5. 实现购买、种植、浇水、收获、采集、战斗和日程管理技能。
6. 建立离线回放、半实时场景和游戏内端到端 Benchmark。
