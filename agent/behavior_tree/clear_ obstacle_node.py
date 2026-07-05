from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.player_context import PlayerContext


class ClearObstacleNode(BTNode):
    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:

        return "SUCCESS"
