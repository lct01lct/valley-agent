import asyncio

from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.player_context import PlayerContext

closed_door_tip = ["打烊", "上锁"]


class OpenDoorNode(BTNode):
    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:

        if blackboard.require_open_door:
            blackboard.require_open_door = False

            res = context.executor_client.send_command(StardewCommand(action=StardewAction.OPEN_DOOR, key=["x"]))
            blackboard.is_opening_door = False
            close_door_flag = False
            if res:
                for tip in closed_door_tip:
                    if tip in res:
                        close_door_flag = True
                        print(f"🟡 [OpenDoorNode] 门被锁住了，无法打开！")

                        await asyncio.sleep(1.0)
                        close_dialog_res = context.executor_client.send_command(
                            StardewCommand(action=StardewAction.CLOSE_DIALOG, key=["x"])
                        )
                        if close_dialog_res == "SUCCESS":
                            blackboard.prompt = "打烊"
                            blackboard.macro_plan = []
                            blackboard.should_reset_route = True
                            return "FAILURE"
                        else:
                            raise ValueError("🔴 [OpenDoorNode] 关闭对话框失败，可能需要手动干预！")

                # 如果门没锁， 直接通行
                if not close_door_flag:
                    return "SUCCESS"

            return "SUCCESS"
        else:

            return "SUCCESS"
