from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.mining_node import MiningTask
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.tool_selection import find_tool_item


PICKAXE_TOOL_NAME = "Pickaxe"


class MiningResourceCheckNode(BTNode):
    """
    Mining 任务的轻量资源前置检查。

    P0 只要求背包里有 Pickaxe；后续体力、血量、背包容量、炸弹等资源管理
    应在 Mining P3 扩展，不在当前节点里提前塞复杂恢复。
    """

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        if not blackboard.macro_plan or blackboard.current_step_index >= len(blackboard.macro_plan):
            return "FAILURE"

        current_task = blackboard.macro_plan[blackboard.current_step_index]
        if not isinstance(current_task, MiningTask):
            return "FAILURE"

        if current_task.task_type != "MINE":
            return "FAILURE"

        game_state = context.state
        if game_state is None:
            return "RUNNING"

        if find_tool_item(game_state, PICKAXE_TOOL_NAME) is None:
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
            blackboard.prompt = (
                "MiningResourceCheckNode 检查失败：背包中没有 Pickaxe。"
                "需要 Planner 先安排 ChestTask 取回镐子，或人工确认工具位置。"
            )
            print("\n🔴 [MiningResourceCheckNode] 背包中没有 Pickaxe，无法开始 Mining P0。")
            return "FAILURE"

        return "SUCCESS"
