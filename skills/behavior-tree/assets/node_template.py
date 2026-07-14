import asyncio
import time

from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.player_context import PlayerContext
from agent.base_task import BaseTask, TaskType


class ExampleNode(BTNode):
    """行为树节点模板：复制后把 ExampleNode 替换为具体节点名。"""

    def __init__(self):
        self.start_time: float | None = None
        self.is_doing = False
        self.timeout_seconds = 10.0

    def initialize(self) -> None:
        print("🛠️ [系统]：正在初始化【ExampleNode】...")

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        if not blackboard.macro_plan or blackboard.current_step_index >= len(blackboard.macro_plan):
            self._reset()
            return "FAILURE"

        current_task = blackboard.macro_plan[blackboard.current_step_index]
        if not isinstance(current_task, ExampleTask):
            self._reset()
            return "FAILURE"

        game_state = context.state
        if game_state is None:
            return "RUNNING"

        if not self.is_doing:
            self.is_doing = True
            self.start_time = time.time()
            print(f"🟢 [ExampleNode] 开始执行任务：{current_task.desc}")

        if self._is_timeout():
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE, key=[]))
            print(f"🔴 [ExampleNode] 执行超时：{current_task.desc}")
            self._reset()
            return "FAILURE"

        # TODO: 在这里根据 game_state 判断下一步动作。
        # command = StardewCommand(action=StardewAction.IDLE, key=[])
        # context.executor_client.send_command(command)

        if self._is_success(blackboard, context, current_task):
            blackboard.current_step_index += 1
            print(f"🏆 [ExampleNode] 任务完成：{current_task.desc}")
            self._reset()
            return "SUCCESS"

        await asyncio.sleep(0)
        return "RUNNING"

    def _is_success(self, blackboard: AgentBlackboard, context: PlayerContext, task: "ExampleTask") -> bool:
        # TODO: 用 context.state 验证成功条件，不要只因为命令已发送就返回 True。
        return False

    def _is_timeout(self) -> bool:
        return self.start_time is not None and time.time() - self.start_time > self.timeout_seconds

    def _reset(self) -> None:
        self.start_time = None
        self.is_doing = False


class ExampleTask(BaseTask):
    """任务模板：如需新增任务类型，同步更新 agent/base_task.py 的 TaskType。"""

    def __init__(self, task_type: TaskType, desc: str):
        super().__init__(task_type=task_type, desc=desc)
