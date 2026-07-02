from typing import Literal, List

from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.player_context import Player_Mode, PlayerContext

type NodeStatus = Literal["RUNNING", "SUCCESS", "FAILURE"]


class BTNode:
    def initialize(self) -> None:
        pass

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        raise NotImplementedError


class Selector(BTNode):
    """选择节点：从左到右，只要有一个成功就立刻返回 True（处理优先级）"""

    def __init__(self, children: List[BTNode]):
        self.children = children

    def initialize(self):
        print(f"🛠️ [System]：正在初始化【选择节点 Selector】，包含 {len(self.children)} 个子分支...")
        for child in self.children:
            child.initialize()

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        for child in self.children:
            status = await child.run(blackboard, context)

            if status in ["RUNNING", "SUCCESS"]:
                return status

        return "FAILURE"


class Sequence(BTNode):
    """顺序节点：从左到右，必须全部成功才返回 True（处理连贯动作）"""

    def __init__(self, node_name: Player_Mode, children: List[BTNode]):
        self.node_name = node_name
        self.children = children

    def initialize(
        self,
    ):
        print(f"🛠️ [System]：正在初始化【{self.node_name} Sequence】，包含 {len(self.children)} 个子节点...")
        for child in self.children:
            child.initialize()

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        for child in self.children:
            status = await child.run(blackboard, context)

            if status in ["RUNNING", "FAILURE"]:
                return status

        return "SUCCESS"
