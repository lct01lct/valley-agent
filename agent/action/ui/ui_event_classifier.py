from typing import Literal

from agent.behavior_tree.blackboard import FeedbackEventType


type UiEventCategory = Literal[
    "BUSINESS_FAILURE",  # 业务失败反馈，例如打烊、上锁，需要 Planner/LLM 重新规划或业务节点停止重试
    "TOOL_NOTICE",  # 工具动作产生的奖励/提示，例如晶球提示，关闭后通常继续当前任务
    "BLOCKING_NOTICE",  # 普通阻塞文本提示，关闭后通常继续当前任务
]


class UiEventClassification:
    def __init__(
        self,
        category: UiEventCategory,
        event_type: FeedbackEventType,
        should_replan: bool = False,
    ) -> None:
        self.category = category
        self.event_type = event_type
        self.should_replan = should_replan


class UiEventClassifier:
    def classify_dialog_text(self, text: str) -> UiEventClassification:
        normalized_text = text.strip()
        if self._is_location_closed_text(normalized_text):
            return UiEventClassification(
                category="BUSINESS_FAILURE",
                event_type="LOCATION_CLOSED",
                should_replan=True,
            )

        if self._is_locked_door_text(normalized_text):
            return UiEventClassification(
                category="BUSINESS_FAILURE",
                event_type="LOCKED_DOOR",
                should_replan=True,
            )

        if self._is_tool_reward_notice(normalized_text):
            return UiEventClassification(
                category="TOOL_NOTICE",
                event_type="TOOL_REWARD_NOTICE",
                should_replan=False,
            )

        return UiEventClassification(
            category="BLOCKING_NOTICE",
            event_type="BLOCKING_DIALOG",
            should_replan=False,
        )

    def _is_location_closed_text(self, text: str) -> bool:
        return any(keyword in text for keyword in ("打烊", "关门", "营业时间", "closed", "business hours"))

    def _is_locked_door_text(self, text: str) -> bool:
        return any(keyword in text for keyword in ("上锁", "锁住", "locked"))

    def _is_tool_reward_notice(self, text: str) -> bool:
        return any(keyword in text for keyword in ("晶球", "geode", "Geode"))
