import time

from agent.action.ui.ui_event_classifier import UiEventClassifier
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import ActionFeedbackEvent, AgentBlackboard
from agent.behavior_tree.player_context import PlayerContext


UI_GUARD_CLOSE_INTERVAL_SECONDS = 0.25
UI_GUARD_SAME_TEXT_SUPPRESS_SECONDS = 1.0


class UiGuardNode(BTNode):
    """
    Guard 层 UI 保护节点。

    只处理非预期阻塞 DialogueBox，并把打烊、上锁、晶球等文本转成结构化反馈。
    箱子、商店、NPC 等非 DialogueBox 菜单暂不自动关闭，避免误伤未来业务 UI。
    """

    def __init__(self) -> None:
        self.classifier = UiEventClassifier()
        self._last_close_at = 0.0
        self._last_handled_text = ""

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        game_state = context.state
        if game_state is None:
            return "FAILURE"

        menu_state = game_state.menu_state
        if not menu_state.is_menu_open:
            self._last_handled_text = ""
            return "FAILURE"

        if menu_state.menu_type != "DialogueBox":
            if blackboard.pending_interaction is not None and blackboard.pending_interaction.matches_menu_type(
                menu_state.menu_type
            ):
                return "FAILURE"
            return "FAILURE"

        text = menu_state.text.strip()
        if not text:
            return "FAILURE"

        classification = self.classifier.classify_dialog_text(text)
        source_owner = blackboard.pending_interaction.owner if blackboard.pending_interaction is not None else None
        target_name = blackboard.pending_interaction.target_name if blackboard.pending_interaction is not None else None
        target_tile = blackboard.pending_interaction.target_tile if blackboard.pending_interaction is not None else None

        if blackboard.pending_interaction is not None and blackboard.pending_interaction.matches_menu_type(
            menu_state.menu_type
        ):
            if classification.category != "BUSINESS_FAILURE":
                return "FAILURE"

        now = time.time()
        if now - self._last_close_at < UI_GUARD_CLOSE_INTERVAL_SECONDS:
            return "RUNNING"
        if text == self._last_handled_text and now - self._last_close_at < UI_GUARD_SAME_TEXT_SUPPRESS_SECONDS:
            return "RUNNING"

        response = context.executor_client.send_command(StardewCommand(action=StardewAction.CLOSE_DIALOG, key=["x"]))
        self._last_close_at = now
        self._last_handled_text = text

        blackboard.action_feedback_event = ActionFeedbackEvent(
            event_type=classification.event_type,
            source_owner=source_owner,
            text=text,
            target_name=target_name,
            target_tile=target_tile,
            should_replan=classification.should_replan,
        )

        if classification.should_replan:
            blackboard.prompt = text
            blackboard.macro_plan = []
            blackboard.should_reset_route = True
            blackboard.pending_interaction = None

        print(
            f"\n🛡️ [UiGuardNode] 关闭阻塞对话: type={classification.event_type}, "
            f"owner={source_owner}, target={target_name}, response={response}, text={text}"
        )
        return "RUNNING"
