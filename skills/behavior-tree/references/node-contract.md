# 行为树节点契约

## 必需结构

每个行为树节点都必须遵守当前项目契约：

```python
class SomeNode(BTNode):
    def initialize(self) -> None:
        ...

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        ...
```

可能不需要初始化

当前 `NodeStatus` 定义为：

```python
type NodeStatus = Literal["RUNNING", "SUCCESS", "FAILURE"]
```

## 状态语义

在这些情况下返回 `"RUNNING"`：

- 节点正在负责当前帧。
- 动作已经开始，但还没有完成。
- 节点正在等待游戏状态出现或稳定。
- 节点拥有的后台过程还在运行。

在这些情况下返回 `"SUCCESS"`：

- 节点已经完整完成自己的职责。
- 父级 `Sequence` 应该继续执行下一个子节点。
- 父级 `Selector` 可以认为这个分支已经处理完成。

在这些情况下返回 `"FAILURE"`：

- 节点不适用于当前黑板、任务或游戏状态。
- 节点无法安全继续，应该让兜底分支获得机会。
- 节点需要规划器恢复，并且已经把恢复上下文写入黑板。

不要用异常表示正常游戏失败，例如“不是当前任务”“商店打烊”“状态还没准备好”。异常只用于状态损坏、契约不可能成立、或需要人工干预的情况。

## 初始化

`initialize()` 用于启动日志，以及初始化子节点或技能。

不要把单次任务状态放进 `initialize()`。同一个节点实例可能会处理多个任务，单次任务状态应该在成功、失败或超时后通过私有 `_reset()` 方法重置。

## 黑板使用

使用 `blackboard.macro_plan` 和 `blackboard.current_step_index` 消费宏观任务。

消费计划的节点应遵循这个模式：

1. 如果没有计划，或索引越界，重置本地状态并返回 `"FAILURE"`。
2. 读取 `current_task = blackboard.macro_plan[blackboard.current_step_index]`。
3. 如果 `current_task` 不是期望的任务类型，重置本地状态并返回 `"FAILURE"`。
4. 持续执行，直到能从游戏状态验证成功条件。
5. 只执行一次 `blackboard.current_step_index += 1`。
6. 重置本地状态并返回 `"SUCCESS"`。

`blackboard.prompt` 只放给规划器的短恢复提示，不要塞长日志。

跨节点标志使用专门字段，风格参考当前的 `require_open_door`。

## 上下文使用

使用 `context.state` 作为游戏状态事实来源。

如果 `context.state is None`，通常返回 `"RUNNING"`，因为 Observer 可能还没有产出新帧。

使用 `context.executor_client.send_command(...)` 执行动作。命令应保持确定性，并和当前状态绑定。

不要只因为“命令已经发送”就判定节点成功。只要可行，成功必须从后续游戏状态验证。

## 异步规则

`ValleyAgent` 会高频调用 `run(...)`。

在 `async def run(...)` 内：

- 优先使用非阻塞检查。
- 使用 `await asyncio.sleep(...)`，不要使用 `time.sleep(...)`。
- 尽量避免长时间阻塞的 socket 调用。
- 不要在动作节点里调用慢速 LLM API。

## 超时处理

长时间运行节点应记录 `start_time`。

超时时：

- 如果继续移动不安全，发送 `IDLE`。
- 打印清晰失败日志。
- 重置本地状态。
- 返回 `"FAILURE"`，或通过黑板触发规划器恢复。

## 接入行为树

把节点接入 `ValleyAgent.behavior_tree` 时，保持优先级语义：

- 防御/安全节点应位于顶层 `Selector` 的更靠前位置。
- 确定性任务节点应位于 `LLM_Node` 之前。
- `LLM_Node` 应保持兜底规划器角色，不要变成每帧控制器。

不要因为节点文件存在就把它接入树里。只有当前任务确实需要时才接入。
