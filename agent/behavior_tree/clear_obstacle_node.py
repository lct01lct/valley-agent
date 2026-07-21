import time

from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.tool_targeting import build_tool_target_face_command, format_tool_target, is_tool_targeting
from agent.behavior_tree.tool_selection import get_required_tool_for_obstacle, is_current_tool
from server.type import Tile


CLEARABLE_OBSTACLE_LAYERS: dict[str, tuple[str, ...]] = {
    "stone": ("Stone",),
    "twig": ("Twig",),
    "weeds": ("Weeds",),
}


class ClearObstacleNode(BTNode):
    def __init__(self) -> None:
        self._target_tile: Tile | None = None
        self._obstacle_type: str | None = None
        self._started_at: float | None = None
        self._attempt_count = 0
        self._wait_ticks = 0
        self._has_faced_target = False

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        if not blackboard.require_clear_obstacle:
            self._reset()
            return "SUCCESS"

        game_state = context.state
        target_tile = blackboard.clear_obstacle_tile
        obstacle_type = blackboard.clear_obstacle_type

        if game_state is None or target_tile is None or obstacle_type is None:
            return "RUNNING"

        if self._target_changed(target_tile, obstacle_type):
            self._start(target_tile, obstacle_type)

        if not self._obstacle_exists(game_state.layers, target_tile, obstacle_type):
            print(f"\n🟢 [ClearObstacleNode] 障碍物已清除: {obstacle_type} @ {target_tile}")
            self._finish(blackboard)
            return "SUCCESS"

        if not self._is_next_to_target(game_state.player_tile, target_tile):
            self._fail(blackboard, f"玩家不在障碍物上下左右相邻格，无法清理: player={game_state.player_tile}, target={target_tile}")
            return "SUCCESS"

        required_tool = blackboard.required_tool or get_required_tool_for_obstacle(obstacle_type)
        if required_tool is None:
            self._fail(blackboard, f"障碍物没有配置可用工具: obstacle_type={obstacle_type}")
            return "SUCCESS"

        if not is_current_tool(game_state, required_tool):
            blackboard.required_tool = required_tool
            blackboard.require_switch_tool = True
            blackboard.is_switching_tool = True
            print(f"\n🟡 [ClearObstacleNode] 当前工具不是 {required_tool}，等待切换工具后再清理。")
            return "RUNNING"

        if self._started_at is not None and time.time() - self._started_at > 8.0:
            self._fail(blackboard, f"清障超时: {obstacle_type} @ {target_tile}")
            return "SUCCESS"

        if not is_tool_targeting(game_state, target_tile):
            command = build_tool_target_face_command(game_state.player_tile, target_tile)
            context.executor_client.send_command(command)
            self._has_faced_target = True
            self._wait_ticks = 0
            print(
                f"\n🧭 [ClearObstacleNode] 面向清障目标: player={game_state.player_tile}, "
                f"target={target_tile}, tool_target={format_tool_target(game_state.tool_target)}"
            )
            return "RUNNING"

        self._wait_ticks += 1
        if self._wait_ticks < 8:
            return "RUNNING"

        self._wait_ticks = 0
        self._attempt_count += 1
        if self._attempt_count > 6:
            self._fail(blackboard, f"清障重试次数耗尽: {obstacle_type} @ {target_tile}")
            return "SUCCESS"

        print(f"\n🧹 [ClearObstacleNode] 使用当前工具清理障碍物: {obstacle_type} @ {target_tile}")
        context.executor_client.send_command(StardewCommand(action=StardewAction.USE_TOOL, key=["c"]))
        return "RUNNING"

    def _target_changed(self, target_tile: Tile, obstacle_type: str) -> bool:
        return self._target_tile != target_tile or self._obstacle_type != obstacle_type

    def _start(self, target_tile: Tile, obstacle_type: str) -> None:
        self._target_tile = target_tile
        self._obstacle_type = obstacle_type
        self._started_at = time.time()
        self._attempt_count = 0
        self._wait_ticks = 0
        self._has_faced_target = False
        print(f"\n🟡 [ClearObstacleNode] 准备清理必要障碍物: {obstacle_type} @ {target_tile}")

    def _finish(self, blackboard: AgentBlackboard) -> None:
        if self._target_tile is not None:
            blackboard.failed_clear_obstacles.discard((self._target_tile.x, self._target_tile.y))
        blackboard.require_clear_obstacle = False
        blackboard.clear_obstacle_tile = None
        blackboard.clear_obstacle_type = None
        blackboard.require_switch_tool = False
        blackboard.is_switching_tool = False
        blackboard.required_tool = None
        self._reset()

    def _fail(self, blackboard: AgentBlackboard, reason: str) -> None:
        print(f"\n🔴 [ClearObstacleNode] {reason}")
        if self._target_tile is not None:
            blackboard.failed_clear_obstacles.add((self._target_tile.x, self._target_tile.y))
        blackboard.prompt = f"清障失败，后续寻路应绕开该障碍：{reason}"
        blackboard.require_clear_obstacle = False
        blackboard.clear_obstacle_tile = None
        blackboard.clear_obstacle_type = None
        blackboard.require_switch_tool = False
        blackboard.is_switching_tool = False
        blackboard.required_tool = None
        self._reset()

    def _reset(self) -> None:
        self._target_tile = None
        self._obstacle_type = None
        self._started_at = None
        self._attempt_count = 0
        self._wait_ticks = 0
        self._has_faced_target = False

    def _obstacle_exists(self, layers: dict[str, set[Tile]], target_tile: Tile, obstacle_type: str) -> bool:
        for layer_name in CLEARABLE_OBSTACLE_LAYERS.get(obstacle_type, ()):
            if target_tile in layers.get(layer_name, set()):
                return True
        return False

    def _is_next_to_target(self, player_tile: Tile, target_tile: Tile) -> bool:
        distance_x = abs(player_tile.x - target_tile.x)
        distance_y = abs(player_tile.y - target_tile.y)
        return distance_x + distance_y == 1
