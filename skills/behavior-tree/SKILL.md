---
name: behavior-tree
description: 当需要在这个星露谷 AI Agent 项目中创建、修改或审查行为树节点时使用，尤其适用于 agent/behavior_tree/ 下的代码。覆盖 BTNode 实现、节点模板、黑板使用、NodeStatus 返回值、任务推进、重试/恢复逻辑，以及接入 ValleyAgent 的 Selector/Sequence 运行时。
---

# 行为树技能

## 核心流程

新增或修改行为树节点时，先确认当前运行契约：

1. 阅读 `agent/behavior_tree/behavior_tree.py`。
2. 阅读 `agent/behavior_tree/blackboard.py`。
3. 阅读 `agent/behavior_tree/player_context.py`
4. 根据目标节点类型，阅读一个相近示例：
   - 长时间移动或导航：`agent/behavior_tree/route_node.py`
   - 短交互动作：`agent/behavior_tree/open_door_node.py`
   - 高优先级保护行为：`agent/behavior_tree/defend_node.py`
   - 后台规划：`agent/behavior_tree/llm_node.py`
5. 如果是新建节点，从 `assets/node_template.py` 开始改。
6. 如果不确定行为树语义，阅读 `references/node-contract.md`。
7. 如果不确定该采用哪种节点模式，阅读 `references/examples.md`。

## 项目规则

行为树节点要小而聚焦，只在必要时保存内部状态，并遵守当前 `NodeStatus` 契约：

- 节点正在处理当前帧时，返回 `"RUNNING"`。
- 节点职责已经完成时，返回 `"SUCCESS"`。
- 节点不负责当前情况，或无法安全完成时，返回 `"FAILURE"`。

使用 `blackboard` 保存跨节点状态和计划进度。

使用 `context.state` 读取游戏状态，使用 `context.executor_client.send_command(...)` 发送游戏命令。

不要在底层执行节点里调用 LLM。LLM 规划应该留在 `LLM_Node` 或未来的规划器层。

避免在 `async def run(...)` 里使用 `time.sleep()`。必须等待时，使用 `await asyncio.sleep(...)`。

不要静默吞掉失败。打印清晰的中文日志；如果需要重新规划，通过 `blackboard` 暴露恢复状态。

新增一个节点时，不要顺手做大范围重构。改动要围绕当前行为保持聚焦。

## 新节点检查清单

完成新节点前，确认：

- 类继承自 `BTNode`。
- `run(...)` 是异步函数，并返回 `NodeStatus`。
- 可能需要安全处理 `context.state is None`。
- 多帧调用不会反复初始化一次性状态。
- 成功、失败、超时后都会重置内部状态。
- 消费计划的节点只在任务真正完成后执行一次 `blackboard.current_step_index += 1`。
- 当前任务/状态不属于该节点时，能快速返回 `"FAILURE"`。
- 发送给游戏的命令复用现有 `StardewAction` / `StardewCommand` 模式。
- 只有用户要求接入运行链路时，才把节点挂到 `ValleyAgent.behavior_tree`。

## 任务模型建议

如果节点需要消费新的任务类型，在节点附近或约定的任务模块里定义匹配的任务类，保持当前轻量风格：

```python
class ExampleTask(BaseTask):
    def __init__(self, task_type: TaskType, desc: str):
        super().__init__(task_type=task_type, desc=desc)
```

如果新增 `TaskType`，要有意识地更新 `agent/base_task.py`，并保证 Python 规划器输出和消费该任务的节点保持一致。
