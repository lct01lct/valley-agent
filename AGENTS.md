# AGENTS.md

## 项目目标

本项目的目标是开发一个可以玩《星露谷物语》的 AI Agent。当前推荐架构是“行为树优先”：

- 行为树负责实时控制循环、安全检查、重试逻辑和执行顺序。
- AI 模型负责高层规划、结构化任务生成和异常恢复建议。
- 具体游戏操作应该由确定性的技能和游戏状态验证来完成；不要把 LLM 调用放进每帧按键循环里。

当前项目入口文件是 `main.py`。

## 当前架构

- `main.py` 启动本地 Agent，清理日志，并发起一个硬编码任务。
- `agent/valley_agent.py` 负责主循环和行为树编排。
- `agent/behavior_tree/` 包含行为树节点、黑板、寻路、开门处理和玩家上下文。
- `agent/action/valley_action/AStar.py` 包含本地 A* 寻路逻辑。
- `server/valley_server.py` 包含 Python 侧的 SMAPI Observer/Executor TCP 客户端。
- `StardewMemoryExporter/` 是 SMAPI Mod，负责导出游戏状态并执行游戏命令。

`README.md` 目前更像历史路线图，可能包含较旧的 VLM-heavy 方案。当前方向应以“SMAPI 结构化状态 + 行为树 + AI Planner”为准。

## 开发原则

- 优先使用 SMAPI 导出的结构化游戏状态，而不是截图/VLM 感知。
- AI 决策应保持粗粒度：意图解析、宏观规划、任务修复、失败恢复。
- 底层游戏动作必须尽量确定、可验证：移动、开门、商店交互、背包检查、使用工具、种地循环。
- 每个任务都应该有明确的前置条件、执行逻辑、成功判定、超时处理和恢复策略。
- 行为树是主控层。需要新能力时，优先增加技能或行为树节点，而不是把 `LLM_Node` 变成通用执行器。
- 避免在多个地方重复实现寻路或动作逻辑；共享行为应该沉淀到 Agent/Skill 层。
- 不要假设 `README.md` 是最新的。修改前先检查当前代码。

## 当前已知缺口

- `ValleyAgent.invoke(task)` 虽然保存了用户任务，但还没有稳定地把任务注入到黑板或 Planner Prompt 中。
- `Agent_Model` 当前默认使用 mock 数据，并返回硬编码的 `RouteTask`。
- SMAPI Observer 当前导出的游戏快照还比较薄；完整游玩需要时间、金钱、体力、背包、菜单/对话状态、作物状态、天气、NPC 和动作结果等信息。
- Python 侧动作枚举比 C# Executor 实际支持的动作更多，两边能力尚未完全对齐。
- `server/valley_server.py` 中仍有部分旧 demo/遗留逻辑；除非是在有意识地重构，否则不要继续扩展这些旧逻辑。

## 常用命令

Python 环境初始化并运行：

```bash
./setup.sh
```

环境准备完成后，直接运行当前入口：

```bash
python main.py
```

部署 SMAPI Mod：

```bash
./StardewMemoryExporter/setup.sh
```

常用检查命令：

```bash
git status --short
rg --files
rg "class |def |async def" agent server StardewMemoryExporter
```

## 环境说明

- `setup.sh` 使用的 Python 环境名：`valley`
- `setup.sh` 请求的 Python 版本：`3.13.5`
- API Key 从 `.env` 加载；不要提交密钥。
- 当前模型代码在 `agent/behavior_tree/llm_node.py` 中通过 LangChain 使用 Google Gemini。
- SMAPI 连接配置由 `PlayerContext` 读取相关环境变量。

## 测试与验证

当前还没有成熟的自动化测试体系。每次修改后，应根据改动范围选择最强的可用验证方式：

- 纯 Python 逻辑：尽量添加或运行聚焦的小型单元检查。
- 寻路或任务规划：使用小型、确定性的场景验证。
- SMAPI 协议改动：同时验证 Python 解析和 C# 序列化/响应行为。
- 实际运行行为：检查 `logs/` 下的日志，并确认游戏内动作真的发生。

汇报完成时，需要说明已经验证了什么，以及哪些部分仍然需要进游戏实测。

## 代码风格

- 改动要小而聚焦，围绕当前请求展开。
- 新增 Python 任务/计划数据结构时使用类型标注。
- Planner 输出和 Action Result 优先使用结构化数据模型，避免自由文本协议。
- 避免在异步行为树路径里使用 `time.sleep()`；应使用 `await asyncio.sleep()`。
- 不要静默吞掉动作失败。失败原因应该通过黑板或任务结果暴露出来。
- 除非协议契约确实要求，否则不要在同一次改动里大范围重写 C# Mod 和 Python Agent。

## Git 与工作区安全

- 编辑前先检查 `git status --short`。
- 不要回滚与当前任务无关的用户改动。
- 不要删除生成资源、日志或本地实验文件，除非用户明确要求。
- 不要提交 `.env`、日志、截图或本机专属部署路径，除非用户明确要求追踪。

## 推荐的下一个里程碑

下一个架构里程碑应是一个真正结构化的 MVP：

1. 将 `main.py` 传入的任务写入黑板或 Planner 输入。
2. 用类型化 Plan Schema 替代 mock planner 输出。
3. 实现最小可用的 `BuyItem` 流程：
   - 导航到 `SeedShop`
   - 打开/交互商店
   - 购买指定物品和数量
   - 验证背包和金钱变化
4. 只有在状态验证成功后才返回任务成功，而不是到达某个地点就算完成。

