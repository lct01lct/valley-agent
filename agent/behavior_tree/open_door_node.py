from agent.action.ui.ui_event_classifier import UiEventClassifier
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import ActionFeedbackEvent, AgentBlackboard, InteractionSession
from agent.behavior_tree.player_context import PlayerContext


class OpenDoorNode(BTNode):
    def __init__(self) -> None:
        self.classifier = UiEventClassifier()

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        if blackboard.require_open_door:
            blackboard.require_open_door = False

            blackboard.pending_interaction = InteractionSession(
                owner="Route",
                intent="ENTER_LOCATION",
                expected_menu_types={"DialogueBox"},
            )
            res = context.executor_client.send_command(StardewCommand(action=StardewAction.OPEN_DOOR, key=["x"]))
            blackboard.is_opening_door = False
            if res and res not in ("SUCCESS", "TIMEOUT"):
                classification = self.classifier.classify_dialog_text(res)
                if classification.category == "BUSINESS_FAILURE":
                    blackboard.action_feedback_event = ActionFeedbackEvent(
                        event_type=classification.event_type,
                        source_owner="Route",
                        text=res,
                        should_replan=classification.should_replan,
                    )
                    blackboard.prompt = res
                    blackboard.macro_plan = []
                    blackboard.should_reset_route = True
                    blackboard.pending_interaction = None

                    close_dialog_res = context.executor_client.send_command(
                        StardewCommand(action=StardewAction.CLOSE_DIALOG, key=["x"])
                    )
                    print(
                        f"🟡 [OpenDoorNode] 入口交互失败: type={classification.event_type}, "
                        f"close_dialog={close_dialog_res}, text={res}"
                    )
                    return "FAILURE"

            blackboard.pending_interaction = None
            return "SUCCESS"

        return "SUCCESS"
