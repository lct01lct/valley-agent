import time

from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.switch_tool_debug_logger import SwitchToolDebugLogger
from agent.behavior_tree.tool_selection import (
    find_tool_item,
    get_toolbar_index,
    get_toolbar_key,
    is_current_tool,
)


MAX_TOOLBAR_SWITCH_ATTEMPTS = 6
MAX_SLOT_SWITCH_ATTEMPTS = 8
STATE_SETTLE_TICKS = 8
SWITCH_TOOL_TIMEOUT_SECONDS = 5.0


class SwitchToolNode(BTNode):
    def __init__(self, owner: str = "Route") -> None:
        self.owner = owner
        self._required_tool: str | None = None
        self._started_at: float | None = None
        self._tab_attempt_count = 0
        self._slot_attempt_count = 0
        self._wait_ticks = 0
        self._last_debug_heartbeat_at = 0.0
        self.switch_tool_debug_logger = SwitchToolDebugLogger()

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        if not blackboard.require_switch_tool:
            self._reset()
            return "SUCCESS"

        if blackboard.required_tool_owner is not None and blackboard.required_tool_owner != self.owner:
            self._reset()
            return "SUCCESS"

        game_state = context.state
        required_tool = blackboard.required_tool
        if game_state is None or required_tool is None:
            return "RUNNING"

        if self._required_tool != required_tool:
            self._start(required_tool)

        self._log_debug_heartbeat(blackboard, game_state, required_tool)

        if is_current_tool(game_state, required_tool):
            print(f"\n🟢 [SwitchToolNode:{self.owner}] 已切换到目标工具: {required_tool}")
            self._log(
                f"切换完成: required_tool={required_tool}, current_tool={self._get_current_tool_snapshot(game_state)}"
            )
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
                f"\n🧰 [SwitchToolNode:{self.owner}] 目标工具 {required_tool} 在工具栏第 {target_toolbar_index + 1} 页，"
                f"当前第 {current_toolbar_index + 1} 页，按 Tab 切换。"
            )
            response = context.executor_client.send_command(StardewCommand(action=StardewAction.SWITCH_TOOL, key=["tab"]))
            self._log(
                f"发送 Tab 切换工具栏: required_tool={required_tool}, response={response}, "
                f"tab_attempt={self._tab_attempt_count}/{MAX_TOOLBAR_SWITCH_ATTEMPTS}, "
                f"current_toolbar={current_toolbar_index}, target_toolbar={target_toolbar_index}, "
                f"current_tool={self._get_current_tool_snapshot(game_state)}"
            )
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
        print(f"\n🧰 [SwitchToolNode:{self.owner}] 按下槽位键 {slot_key}，切换到工具: {required_tool}")
        response = context.executor_client.send_command(StardewCommand(action=StardewAction.SWITCH_TOOL, key=[slot_key]))
        self._log(
            f"发送槽位切换: required_tool={required_tool}, key={slot_key}, response={response}, "
            f"slot_attempt={self._slot_attempt_count}/{MAX_SLOT_SWITCH_ATTEMPTS}, "
            f"tool_item={self._format_inventory_item(tool_item)}, "
            f"current_tool={self._get_current_tool_snapshot(game_state)}"
        )
        return "RUNNING"

    def _start(self, required_tool: str) -> None:
        self._required_tool = required_tool
        self._started_at = time.time()
        self._tab_attempt_count = 0
        self._slot_attempt_count = 0
        self._wait_ticks = 0
        self._last_debug_heartbeat_at = 0.0
        print(f"\n🟡 [SwitchToolNode:{self.owner}] 准备切换工具: {required_tool}")
        self._log(f"开始切换工具: owner={self.owner}, required_tool={required_tool}")

    def _finish(self, blackboard: AgentBlackboard) -> None:
        blackboard.require_switch_tool = False
        blackboard.is_switching_tool = False
        blackboard.required_tool_owner = None
        blackboard.required_tool = None
        if blackboard.prompt.startswith("切换工具失败"):
            blackboard.prompt = ""
        self._reset()

    def _fail(self, blackboard: AgentBlackboard, reason: str) -> None:
        print(f"\n🔴 [SwitchToolNode:{self.owner}] {reason}")
        self._log(
            f"切换工具失败: owner={self.owner}, required_tool={self._required_tool}, reason={reason}, "
            f"tab_attempt={self._tab_attempt_count}, slot_attempt={self._slot_attempt_count}, "
            f"wait_ticks={self._wait_ticks}, clear_obstacle_tile={blackboard.clear_obstacle_tile}, "
            f"clear_obstacle_type={blackboard.clear_obstacle_type}"
        )
        if blackboard.clear_obstacle_tile is not None:
            blackboard.failed_clear_obstacles.add(
                (blackboard.clear_obstacle_tile.x, blackboard.clear_obstacle_tile.y)
            )
        blackboard.prompt = f"切换工具失败，需要重新规划或人工检查背包：{reason}"
        blackboard.require_switch_tool = False
        blackboard.is_switching_tool = False
        blackboard.required_tool_owner = None
        blackboard.require_clear_obstacle = False
        blackboard.clear_obstacle_owner = None
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
        self._last_debug_heartbeat_at = 0.0

    def _log(self, message: str) -> None:
        self.switch_tool_debug_logger.log(f"[SwitchToolNode:{self.owner}] {message}")

    def _log_debug_heartbeat(self, blackboard: AgentBlackboard, game_state, required_tool: str) -> None:
        now = time.time()
        if now - self._last_debug_heartbeat_at < 0.25:
            return

        self._last_debug_heartbeat_at = now
        tool_item = find_tool_item(game_state, required_tool)
        target_toolbar_index = None if tool_item is None else get_toolbar_index(tool_item.index)
        self._log(
            f"心跳: owner={self.owner}, required_tool={required_tool}, "
            f"current_tool={self._get_current_tool_snapshot(game_state)}, "
            f"CurrentToolIndex={game_state.inventory.current_tool_index}, "
            f"CurrentToolbarIndex={game_state.inventory.current_toolbar_index}, "
            f"target_toolbar={target_toolbar_index}, tool_item={self._format_inventory_item(tool_item)}, "
            f"tab_attempt={self._tab_attempt_count}/{MAX_TOOLBAR_SWITCH_ATTEMPTS}, "
            f"slot_attempt={self._slot_attempt_count}/{MAX_SLOT_SWITCH_ATTEMPTS}, wait_ticks={self._wait_ticks}, "
            f"blackboard=require_switch_tool={blackboard.require_switch_tool}, "
            f"is_switching_tool={blackboard.is_switching_tool}, required_tool_owner={blackboard.required_tool_owner}, "
            f"clear_obstacle_owner={blackboard.clear_obstacle_owner}, clear_obstacle_tile={blackboard.clear_obstacle_tile}, "
            f"clear_obstacle_type={blackboard.clear_obstacle_type}, "
            f"inventory_preview={self._format_inventory_preview(game_state)}"
        )

    def _get_current_tool_snapshot(self, game_state) -> str:
        current_index = game_state.inventory.current_tool_index
        for item in game_state.inventory.items:
            if item.index == current_index:
                return self._format_inventory_item(item)
        return f"index={current_index}, item=<unknown>"

    def _format_inventory_item(self, item) -> str:
        if item is None:
            return "None"
        return (
            f"index={item.index}, name={item.name}, display_name={item.display_name}, "
            f"qualified_item_id={item.qualified_item_id}"
        )

    def _format_inventory_preview(self, game_state) -> str:
        items = sorted(game_state.inventory.items, key=lambda item: item.index)
        preview_items = [self._format_inventory_item(item) for item in items[:12]]
        return "[" + "; ".join(preview_items) + "]"
