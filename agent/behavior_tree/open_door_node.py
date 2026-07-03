from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.player_context import PlayerContext


class OpenDoorNode(BTNode):
    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:

        if blackboard.require_open_door:
            blackboard.require_open_door = False

            # context.executor_client.send_command(StardewCommand(action=StardewAction.OPEN_DOOR, key=["x"]))
            return "SUCCESS"
        else:

            return "SUCCESS"
