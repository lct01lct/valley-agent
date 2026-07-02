from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.base_task import BaseTask, TaskType
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.player_context import PlayerContext


class Defend_Node(BTNode):
    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        # print("defend")
        return "FAILURE"


class DefendTask(BaseTask):
    def __init__(self, task_type: TaskType, desc: str):
        super().__init__(task_type=task_type, desc=desc)
