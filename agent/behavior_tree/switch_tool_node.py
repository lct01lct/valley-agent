import time

from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.tool_selection import (
    find_tool_item,
    get_toolbar_index,
    get_toolbar_key,
    is_current_tool,
)


MAX_TOOLBAR_SWITCH_ATTEMPTS = 6
MAX_SLOT_SWITCH_ATTEMPTS = 4
STATE_SETTLE_TICKS = 2
SWITCH_TOOL_TIMEOUT_SECONDS = 5.0


class SwitchToolNode(BTNode):
    def __init__(self) -> None:
        self._required_tool: str | None = None
        self._started_at: float | None = None
        self._tab_attempt_count = 0
        self._slot_attempt_count = 0
        self._wait_ticks = 0

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        if not blackboard.require_switch_tool:
            self._reset()
            return "SUCCESS"

        game_state = context.state
        required_tool = blackboard.required_tool
        if game_state is None or required_tool is None:
            return "RUNNING"

        if self._required_tool != required_tool:
            self._start(required_tool)

        if is_current_tool(game_state, required_tool):
            print(f"\n🟢 [SwitchToolNode] 已切换到目标工具: {required_tool}")
            self._finish(blackboard)
            return "SUCCESS"

        if self._started_at is not None and time.time() - self._started_at > SWITCH_TOOL_TIMEOUT_SECONDS:
            self._fail(blackboard, f"切换工具超时: required_tool={required_tool}")
            return "FAILURE"

        if self._wait_ticks > 0:
            self._wait_ticks -= 1
            return "RUNNING"

        tool_item = find_tool_item(game_state, required_tool)
        if tool_item is None:
            self._fail(blackboard, f"背包中没有找到目标工具: required_tool={required_tool}")
            return "FAILURE"

        target_toolbar_index = get_toolbar_index(tool_item.index)
        current_toolbar_index = game_state.inventory.current_toolbar_index

        if current_toolbar_index != target_toolbar_index:
            if self._tab_attempt_count >= MAX_TOOLBAR_SWITCH_ATTEMPTS:
                self._fail(
                    blackboard,
                    f"无法切到目标工具栏: required_tool={required_tool}, "
                    f"current_toolbar={current_toolbar_index}, target_toolbar={target_toolbar_index}",
                )
                return "FAILURE"

            self._tab_attempt_count += 1
            self._wait_ticks = STATE_SETTLE_TICKS
            print(
                f"\n🧰 [SwitchToolNode] 目标工具 {required_tool} 在工具栏第 {target_toolbar_index + 1} 页，"
                f"当前第 {current_toolbar_index + 1} 页，按 Tab 切换。"
            )
            context.executor_client.send_command(StardewCommand(action=StardewAction.SWITCH_TOOL, key=["tab"]))
            return "RUNNING"

        slot_key = get_toolbar_key(tool_item.index)
        if slot_key is None:
            self._fail(blackboard, f"目标工具槽位无法映射到快捷键: required_tool={required_tool}, index={tool_item.index}")
            return "FAILURE"

        if self._slot_attempt_count >= MAX_SLOT_SWITCH_ATTEMPTS:
            self._fail(blackboard, f"切换到目标工具槽位失败: required_tool={required_tool}, key={slot_key}")
            return "FAILURE"

        self._slot_attempt_count += 1
        self._wait_ticks = STATE_SETTLE_TICKS
        print(f"\n🧰 [SwitchToolNode] 按下槽位键 {slot_key}，切换到工具: {required_tool}")
        context.executor_client.send_command(StardewCommand(action=StardewAction.SWITCH_TOOL, key=[slot_key]))
        return "RUNNING"

    def _start(self, required_tool: str) -> None:
        self._required_tool = required_tool
        self._started_at = time.time()
        self._tab_attempt_count = 0
        self._slot_attempt_count = 0
        self._wait_ticks = 0
        print(f"\n🟡 [SwitchToolNode] 准备切换工具: {required_tool}")

    def _finish(self, blackboard: AgentBlackboard) -> None:
        blackboard.require_switch_tool = False
        blackboard.is_switching_tool = False
        self._reset()

    def _fail(self, blackboard: AgentBlackboard, reason: str) -> None:
        print(f"\n🔴 [SwitchToolNode] {reason}")
        blackboard.prompt = f"切换工具失败，需要重新规划或人工检查背包：{reason}"
        blackboard.require_switch_tool = False
        blackboard.is_switching_tool = False
        blackboard.require_clear_obstacle = False
        blackboard.clear_obstacle_tile = None
        blackboard.clear_obstacle_type = None
        blackboard.required_tool = None
        self._reset()

    def _reset(self) -> None:
        self._required_tool = None
        self._started_at = None
        self._tab_attempt_count = 0
        self._slot_attempt_count = 0
        self._wait_ticks = 0
