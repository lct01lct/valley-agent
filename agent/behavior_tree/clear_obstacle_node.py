import time

from agent.action.tool.tool_aftermath_service import ToolAftermathRequest, ToolAftermathService
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.action.valley_action.clearance_policy import ORDINARY_TREE_LAYERS, normalize_obstacle_type
from agent.action.valley_action.positioning_controller import PositioningController, PositioningGoal, PositioningResult
from agent.action.valley_action.tool_targeting import build_tool_target_face_command, format_tool_target, is_tool_targeting
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.clear_obstacle_debug_logger import ClearObstacleDebugLogger
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.tool_action_tracker import ToolActionTracker
from agent.behavior_tree.tool_selection import has_tool_area_tree1_risk, is_current_tool, select_required_tool_for_obstacle
from server.type import Tile


CLEARABLE_OBSTACLE_LAYERS: dict[str, tuple[str, ...]] = {
    "stone": ("Stone",),
    "Stone": ("Stone",),
    "twig": ("Twig",),
    "Twig": ("Twig",),
    "weeds": ("Weeds",),
    "Weeds": ("Weeds",),
    "grass": ("Grass",),
    "Grass": ("Grass",),
    "tree": (*ORDINARY_TREE_LAYERS, "TreeStump"),
    "Tree": (*ORDINARY_TREE_LAYERS, "TreeStump"),
}
CLEAR_OBSTACLE_TIMEOUT_SECONDS = 8.0
TREE_CLEAR_OBSTACLE_TIMEOUT_SECONDS = 18.0
STATE_SETTLE_TICKS = 8
MAX_CLEAR_ATTEMPTS = 6
MAX_TREE_CLEAR_ATTEMPTS = 24
POSITIONING_STUCK_TIMEOUT_SECONDS = 0.45
CLEAR_TOOL_START_GRACE_SECONDS = 0.35
CLEAR_TOOL_FINISH_TIMEOUT_SECONDS = 2.5


class ClearObstacleNode(BTNode):
    def __init__(self, owner: str = "Route") -> None:
        self.owner = owner
        self.positioning_controller = PositioningController()
        self._target_tile: Tile | None = None
        self._obstacle_type: str | None = None
        self._started_at: float | None = None
        self._attempt_count = 0
        self._wait_ticks = 0
        self._has_faced_target = False
        self._last_debug_heartbeat_at = 0.0
        self._last_positioning_position: tuple[float, float] | None = None
        self._positioning_stuck_started_at: float | None = None
        self._blocked_stand_tiles: set[Tile] = set()
        self._deferred_loot_tiles: list[Tile] = []
        self.tool_aftermath_service = ToolAftermathService()
        self.tool_action_tracker = ToolActionTracker(
            start_grace_seconds=CLEAR_TOOL_START_GRACE_SECONDS,
            finish_timeout_seconds=CLEAR_TOOL_FINISH_TIMEOUT_SECONDS,
        )
        self.clear_obstacle_debug_logger = ClearObstacleDebugLogger()

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        if not blackboard.require_clear_obstacle:
            self._reset()
            return "SUCCESS"

        if blackboard.clear_obstacle_owner is not None and blackboard.clear_obstacle_owner != self.owner:
            self._reset()
            return "SUCCESS"

        game_state = context.state
        target_tile = blackboard.clear_obstacle_tile
        obstacle_type = blackboard.clear_obstacle_type

        if game_state is None or target_tile is None or obstacle_type is None:
            return "RUNNING"

        if self._target_changed(target_tile, obstacle_type):
            self._start(target_tile, obstacle_type)

        self._log_debug_heartbeat(blackboard, game_state, target_tile, obstacle_type)

        if not self._obstacle_exists(game_state.layers, target_tile, obstacle_type):
            return self._handle_cleared_obstacle(context, blackboard, game_state, target_tile, obstacle_type)

        if self._started_at is not None and time.time() - self._started_at > self._get_clear_timeout_seconds(obstacle_type):
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
            self._fail(blackboard, f"清障超时: {obstacle_type} @ {target_tile}")
            return "SUCCESS"

        if self._is_waiting_for_external_player_action(game_state, target_tile, obstacle_type):
            return "RUNNING"

        if not self._is_next_to_target(game_state.player_tile, target_tile):
            positioning_result = self._tick_clear_positioning(game_state, context, target_tile)
            if positioning_result.status == "FAILED":
                context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
                self._fail(
                    blackboard,
                    f"无法移动到清障站位: player={game_state.player_tile}, target={target_tile}, "
                    f"reason={positioning_result.reason}",
                )
                return "SUCCESS"
            if positioning_result.status == "MOVING" and self._is_positioning_stuck(game_state, positioning_result):
                if self._try_block_stuck_stand_tile(positioning_result):
                    context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
                    return "RUNNING"

                context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
                self._fail(
                    blackboard,
                    f"移动到清障站位时卡住且没有备用站位: player={game_state.player_tile}, target={target_tile}, "
                    f"stand_tile={positioning_result.stand_tile}",
                )
                return "SUCCESS"
            return "RUNNING"

        self._reset_positioning_stuck_detection()

        clear_owner = "Farm" if self.owner == "Farm" else "Route"
        required_tool = blackboard.required_tool or select_required_tool_for_obstacle(
            game_state,
            obstacle_type,
            target_tile,
            clear_owner,
        )
        if required_tool is None:
            self._fail(blackboard, f"障碍物没有配置可用工具: obstacle_type={obstacle_type}")
            return "SUCCESS"

        if not is_current_tool(game_state, required_tool):
            blackboard.required_tool = required_tool
            blackboard.required_tool_owner = self.owner
            blackboard.require_switch_tool = True
            blackboard.is_switching_tool = True
            print(f"\n🟡 [ClearObstacleNode:{self.owner}] 当前工具不是 {required_tool}，等待切换工具后再清理。")
            self._log(
                f"等待切换工具: required_tool={required_tool}, current_tool={self._get_current_tool_name(game_state)}, "
                f"target={target_tile}, obstacle={obstacle_type}, "
                f"tool_area_tree1_risk={has_tool_area_tree1_risk(game_state, target_tile, required_tool)}"
            )
            return "RUNNING"

        if not is_tool_targeting(game_state, target_tile):
            command = build_tool_target_face_command(game_state.player_tile, target_tile)
            response = context.executor_client.send_command(command)
            self._has_faced_target = True
            self._wait_ticks = 0
            self.tool_action_tracker.reset()
            print(
                f"\n🧭 [ClearObstacleNode:{self.owner}] 面向清障目标: player={game_state.player_tile}, "
                f"target={target_tile}, tool_target={format_tool_target(game_state.tool_target)}"
            )
            self._log(
                f"发送转向命令: target={target_tile}, obstacle={obstacle_type}, command={command.action}, "
                f"response={response}, tool_target={format_tool_target(game_state.tool_target)}"
            )
            return "RUNNING"

        self._wait_ticks += 1
        if self._wait_ticks < STATE_SETTLE_TICKS:
            return "RUNNING"

        tool_action_status = self.tool_action_tracker.tick(game_state)
        if tool_action_status in ("WAITING_STARTED", "WAITING_FINISHED"):
            self._log(
                f"等待清障工具动作收招: target={target_tile}, obstacle={obstacle_type}, "
                f"UsingTool={game_state.using_tool}, CanMove={game_state.can_move}, "
                f"tracker={self.tool_action_tracker.get_debug_snapshot()}"
            )
            return "RUNNING"
        if tool_action_status == "FINISHED":
            aftermath_result = self.tool_aftermath_service.inspect_after_tool_action(
                context,
                game_state,
                ToolAftermathRequest(
                    owner="Farm" if self.owner == "Farm" else "Route",
                    action_name="CLEAR_OBSTACLE",
                    target_tile=target_tile,
                    check_ladder_at_target_tile=False,
                    target_tile_changed=not self._obstacle_exists(game_state.layers, target_tile, obstacle_type),
                ),
            )
            if aftermath_result.has_blocking_menu:
                self._log(
                    f"清障工具动作收招后发现阻塞 UI，等待 Guard 处理: target={target_tile}, "
                    f"obstacle={obstacle_type}, menu={aftermath_result.blocking_menu_type}, "
                    f"text={aftermath_result.blocking_menu_text}"
                )
                self.tool_action_tracker.reset()
                return "RUNNING"

            if self._is_tree_obstacle(obstacle_type):
                self._defer_tree_loot(target_tile, aftermath_result.nearby_loot_tiles)
            else:
                self._request_collect_loot(blackboard, target_tile, obstacle_type, aftermath_result.nearby_loot_tiles)
            self._log(
                f"清障工具动作已收招，等待下一帧验证结果: target={target_tile}, obstacle={obstacle_type}, "
                f"UsingTool={game_state.using_tool}, CanMove={game_state.can_move}, "
                f"target_state={self._format_farm_tile_state(game_state, target_tile)}, "
                f"aftermath={aftermath_result.reason}"
            )
            self.tool_action_tracker.reset()
            return "RUNNING"
        if tool_action_status == "TIMEOUT":
            self._log(
                f"清障工具动作等待超时，准备进入重试/失败判断: target={target_tile}, obstacle={obstacle_type}, "
                f"UsingTool={game_state.using_tool}, CanMove={game_state.can_move}, "
                f"tracker={self.tool_action_tracker.get_debug_snapshot()}"
            )
            self.tool_action_tracker.reset()

        if tool_action_status == "IDLE":
            pass
        else:
            if tool_action_status != "TIMEOUT":
                return "RUNNING"

        self._wait_ticks = 0
        self._attempt_count += 1
        if self._attempt_count > self._get_max_clear_attempts(obstacle_type):
            self._fail(blackboard, f"清障重试次数耗尽: {obstacle_type} @ {target_tile}")
            return "SUCCESS"

        clear_command = self._build_clear_command(required_tool)
        print(f"\n🧹 [ClearObstacleNode:{self.owner}] 使用当前工具清理障碍物: {obstacle_type} @ {target_tile}")
        response = context.executor_client.send_command(clear_command)
        if response == "BUSY":
            self._attempt_count -= 1
            self._log(
                f"C# Executor 忙碌，清障命令未执行，等待下一帧: target={target_tile}, "
                f"obstacle={obstacle_type}, command={clear_command.action}, "
                f"UsingTool={game_state.using_tool}, CanMove={game_state.can_move}"
            )
            return "RUNNING"

        self.tool_action_tracker.start()
        self._log(
            f"发送清障命令: target={target_tile}, obstacle={obstacle_type}, attempt={self._attempt_count}, "
            f"command={clear_command.action}, response={response}, tool_target={format_tool_target(game_state.tool_target)}, "
            f"UsingTool={game_state.using_tool}, CanMove={game_state.can_move}, "
            f"target_state={self._format_farm_tile_state(game_state, target_tile)}, "
            f"tracker={self.tool_action_tracker.get_debug_snapshot()}"
        )
        return "RUNNING"

    def _tick_clear_positioning(
        self,
        game_state,
        context: PlayerContext,
        target_tile: Tile,
    ) -> PositioningResult:
        result = self.positioning_controller.tick(
            game_state,
            PositioningGoal(
                candidate_stand_tiles=self._get_cardinal_neighbor_tiles(target_tile) - self._blocked_stand_tiles,
                tool_target_tile=target_tile,
                extra_blocked_tiles={target_tile},
            ),
        )
        if result.command is not None:
            response = context.executor_client.send_command(result.command)
            self._log(
                f"发送站位命令: target={target_tile}, status={result.status}, command={result.command.action}, "
                f"response={response}, stand_tile={result.stand_tile}, reason={result.reason}, "
                f"positioning={self.positioning_controller.get_debug_snapshot()}"
            )
        if result.status == "MOVING":
            print(
                f"\n🚶 [ClearObstacleNode:{self.owner}] 移动到清障站位: "
                f"target={target_tile}, stand_tile={result.stand_tile}"
            )
        elif result.status == "FACING":
            print(
                f"\n🧭 [ClearObstacleNode:{self.owner}] 面向清障目标: "
                f"player={game_state.player_tile}, target={target_tile}"
            )
        return result

    def _target_changed(self, target_tile: Tile, obstacle_type: str) -> bool:
        return self._target_tile != target_tile or self._obstacle_type != obstacle_type

    def _start(self, target_tile: Tile, obstacle_type: str) -> None:
        self._target_tile = target_tile
        self._obstacle_type = obstacle_type
        self._started_at = time.time()
        self._attempt_count = 0
        self._wait_ticks = 0
        self._has_faced_target = False
        self.tool_action_tracker.reset()
        self._last_debug_heartbeat_at = 0.0
        self._blocked_stand_tiles = set()
        self._deferred_loot_tiles = []
        self._reset_positioning_stuck_detection()
        self.positioning_controller.reset()
        print(f"\n🟡 [ClearObstacleNode:{self.owner}] 准备清理必要障碍物: {obstacle_type} @ {target_tile}")
        self._log(f"开始清障: owner={self.owner}, obstacle={obstacle_type}, target={target_tile}")

    def _finish(self, blackboard: AgentBlackboard) -> None:
        if self._target_tile is not None:
            blackboard.failed_clear_obstacles.discard((self._target_tile.x, self._target_tile.y))
        self._log(
            f"清障完成并清理黑板: target={self._target_tile}, obstacle={self._obstacle_type}, owner={self.owner}"
        )
        blackboard.require_clear_obstacle = False
        blackboard.clear_obstacle_owner = None
        blackboard.clear_obstacle_tile = None
        blackboard.clear_obstacle_type = None
        blackboard.require_switch_tool = False
        blackboard.is_switching_tool = False
        blackboard.required_tool_owner = None
        blackboard.required_tool = None
        self._reset()

    def _request_collect_loot(
        self,
        blackboard: AgentBlackboard,
        source_tile: Tile,
        source_type: str,
        loot_tiles: list[Tile],
    ) -> None:
        if self._should_skip_loot_check(source_type):
            self._log(f"该障碍类型不会产生掉落物，跳过自动拾取: source={source_tile}, source_type={source_type}")
            return

        if not loot_tiles and not self._should_probe_dynamic_loot(source_type):
            return

        should_replace_pending_loot = self._should_replace_pending_loot(source_type)
        if should_replace_pending_loot:
            blackboard.pending_loot_tiles = []
            blackboard.skipped_loot_tiles = set()

        known_tiles = {(tile.x, tile.y) for tile in blackboard.pending_loot_tiles}
        for loot_tile in loot_tiles:
            if (loot_tile.x, loot_tile.y) in known_tiles:
                continue
            blackboard.pending_loot_tiles.append(loot_tile)
            known_tiles.add((loot_tile.x, loot_tile.y))

        blackboard.require_collect_loot = True
        blackboard.collect_loot_owner = self.owner
        self._merge_collect_loot_source(blackboard, source_tile, source_type)
        self._log(
            f"发现工具掉落物，触发自动拾取: source={source_tile}, source_type={source_type}, "
            f"active_source={blackboard.collect_loot_source_tile}, active_source_type={blackboard.collect_loot_source_type}, "
            f"replace_pending={should_replace_pending_loot}, loot_tiles={self._format_tile_list(loot_tiles)}, "
            f"pending={self._format_tile_list(blackboard.pending_loot_tiles)}"
        )

    def _merge_collect_loot_source(
        self,
        blackboard: AgentBlackboard,
        source_tile: Tile,
        source_type: str,
    ) -> None:
        normalized_new_source_type = normalize_obstacle_type(source_type) or source_type
        normalized_current_source_type = normalize_obstacle_type(blackboard.collect_loot_source_type or "")
        if normalized_new_source_type == "tree" or normalized_current_source_type != "tree":
            blackboard.collect_loot_source_tile = source_tile
            blackboard.collect_loot_source_type = source_type

    def _should_probe_dynamic_loot(self, source_type: str) -> bool:
        normalized_source_type = normalize_obstacle_type(source_type) or source_type
        return normalized_source_type in {"weeds", "tree"}

    def _should_skip_loot_check(self, source_type: str) -> bool:
        normalized_source_type = normalize_obstacle_type(source_type) or source_type
        return normalized_source_type == "grass"

    def _should_replace_pending_loot(self, source_type: str) -> bool:
        normalized_source_type = normalize_obstacle_type(source_type) or source_type
        return normalized_source_type == "tree"

    def _handle_cleared_obstacle(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state,
        target_tile: Tile,
        obstacle_type: str,
    ) -> NodeStatus:
        if self.tool_action_tracker.is_idle():
            print(f"\n🟢 [ClearObstacleNode:{self.owner}] 障碍物已清除: {obstacle_type} @ {target_tile}")
            self._log(f"障碍物已清除: obstacle={obstacle_type}, target={target_tile}, player={game_state.player_tile}")
            self._request_deferred_tree_loot(blackboard, target_tile, obstacle_type)
            should_collect_loot = self._has_pending_collect_loot(blackboard)
            self._finish(blackboard)
            if should_collect_loot:
                self._log(
                    f"清障后存在待拾取掉落物，本轮返回 RUNNING 阻止业务节点同 tick 继续发布下一项清障: "
                    f"target={target_tile}, obstacle={obstacle_type}, pending={self._format_tile_list(blackboard.pending_loot_tiles)}"
                )
                return "RUNNING"
            return "SUCCESS"

        tool_action_status = self.tool_action_tracker.tick(game_state)
        if tool_action_status in ("WAITING_STARTED", "WAITING_FINISHED"):
            self._log(
                f"障碍已消失，等待清障工具动作收招后再观察掉落物: target={target_tile}, "
                f"obstacle={obstacle_type}, UsingTool={game_state.using_tool}, CanMove={game_state.can_move}, "
                f"tracker={self.tool_action_tracker.get_debug_snapshot()}"
            )
            return "RUNNING"

        if tool_action_status in ("FINISHED", "TIMEOUT"):
            aftermath_result = self.tool_aftermath_service.inspect_after_tool_action(
                context,
                game_state,
                ToolAftermathRequest(
                    owner="Farm" if self.owner == "Farm" else "Route",
                    action_name="CLEAR_OBSTACLE",
                    target_tile=target_tile,
                    check_ladder_at_target_tile=False,
                    target_tile_changed=True,
                ),
            )
            if self._is_tree_obstacle(obstacle_type):
                self._defer_tree_loot(target_tile, aftermath_result.nearby_loot_tiles)
                self._request_deferred_tree_loot(blackboard, target_tile, obstacle_type)
            else:
                self._request_collect_loot(blackboard, target_tile, obstacle_type, aftermath_result.nearby_loot_tiles)

            self._log(
                f"障碍已消失且工具动作已结束，完成清障后处理: target={target_tile}, obstacle={obstacle_type}, "
                f"tool_action_status={tool_action_status}, aftermath={aftermath_result.reason}"
            )
            self.tool_action_tracker.reset()
            print(f"\n🟢 [ClearObstacleNode:{self.owner}] 障碍物已清除: {obstacle_type} @ {target_tile}")
            should_collect_loot = self._has_pending_collect_loot(blackboard)
            self._finish(blackboard)
            if should_collect_loot:
                self._log(
                    f"清障后存在待拾取掉落物，本轮返回 RUNNING 阻止业务节点同 tick 继续发布下一项清障: "
                    f"target={target_tile}, obstacle={obstacle_type}, pending={self._format_tile_list(blackboard.pending_loot_tiles)}"
                )
                return "RUNNING"
            return "SUCCESS"

        return "RUNNING"

    def _fail(self, blackboard: AgentBlackboard, reason: str) -> None:
        print(f"\n🔴 [ClearObstacleNode:{self.owner}] {reason}")
        self._log(
            f"清障失败: owner={self.owner}, target={self._target_tile}, obstacle={self._obstacle_type}, "
            f"reason={reason}, attempts={self._attempt_count}, wait_ticks={self._wait_ticks}, "
            f"positioning={self.positioning_controller.get_debug_snapshot()}"
        )
        if self._target_tile is not None:
            blackboard.failed_clear_obstacles.add((self._target_tile.x, self._target_tile.y))
        blackboard.prompt = f"清障失败，后续寻路应绕开该障碍：{reason}"
        blackboard.require_clear_obstacle = False
        blackboard.clear_obstacle_owner = None
        blackboard.clear_obstacle_tile = None
        blackboard.clear_obstacle_type = None
        blackboard.require_switch_tool = False
        blackboard.is_switching_tool = False
        blackboard.required_tool_owner = None
        blackboard.required_tool = None
        self._reset()

    def _reset(self) -> None:
        self._target_tile = None
        self._obstacle_type = None
        self._started_at = None
        self._attempt_count = 0
        self._wait_ticks = 0
        self._has_faced_target = False
        self.tool_action_tracker.reset()
        self._last_debug_heartbeat_at = 0.0
        self._blocked_stand_tiles = set()
        self._deferred_loot_tiles = []
        self._reset_positioning_stuck_detection()
        self.positioning_controller.reset()

    def _obstacle_exists(self, layers: dict[str, set[Tile]], target_tile: Tile, obstacle_type: str) -> bool:
        normalized_obstacle_type = normalize_obstacle_type(obstacle_type) or obstacle_type
        for layer_name in CLEARABLE_OBSTACLE_LAYERS.get(normalized_obstacle_type, CLEARABLE_OBSTACLE_LAYERS.get(obstacle_type, ())):
            if target_tile in layers.get(layer_name, set()):
                return True
        return False

    def _is_tree_obstacle(self, obstacle_type: str | None) -> bool:
        return normalize_obstacle_type(obstacle_type) == "tree"

    def _has_pending_collect_loot(self, blackboard: AgentBlackboard) -> bool:
        return blackboard.require_collect_loot and blackboard.collect_loot_owner == self.owner

    def _defer_tree_loot(self, source_tile: Tile, loot_tiles: list[Tile]) -> None:
        if not loot_tiles:
            return

        known_tiles = {(tile.x, tile.y) for tile in self._deferred_loot_tiles}
        added_tiles: list[Tile] = []
        for loot_tile in loot_tiles:
            if (loot_tile.x, loot_tile.y) in known_tiles:
                continue
            self._deferred_loot_tiles.append(loot_tile)
            added_tiles.append(loot_tile)
            known_tiles.add((loot_tile.x, loot_tile.y))

        if added_tiles:
            self._log(
                f"树木清理过程中暂存掉落物，等待树和树桩完全清理后再拾取: "
                f"source={source_tile}, added={self._format_tile_list(added_tiles)}, "
                f"deferred={self._format_tile_list(self._deferred_loot_tiles)}"
            )

    def _request_deferred_tree_loot(self, blackboard: AgentBlackboard, source_tile: Tile, source_type: str) -> None:
        if not self._is_tree_obstacle(source_type) or not self._deferred_loot_tiles:
            return

        self._request_collect_loot(blackboard, source_tile, source_type, self._deferred_loot_tiles)
        self._log(
            f"树和树桩已完全清理，触发暂存掉落物拾取: source={source_tile}, "
            f"loot_tiles={self._format_tile_list(self._deferred_loot_tiles)}"
        )
        self._deferred_loot_tiles = []

    def _get_max_clear_attempts(self, obstacle_type: str) -> int:
        if normalize_obstacle_type(obstacle_type) == "tree":
            return MAX_TREE_CLEAR_ATTEMPTS
        return MAX_CLEAR_ATTEMPTS

    def _get_clear_timeout_seconds(self, obstacle_type: str) -> float:
        if normalize_obstacle_type(obstacle_type) == "tree":
            return TREE_CLEAR_OBSTACLE_TIMEOUT_SECONDS
        return CLEAR_OBSTACLE_TIMEOUT_SECONDS

    def _is_next_to_target(self, player_tile: Tile, target_tile: Tile) -> bool:
        distance_x = abs(player_tile.x - target_tile.x)
        distance_y = abs(player_tile.y - target_tile.y)
        return distance_x + distance_y == 1

    def _is_waiting_for_external_player_action(self, game_state, target_tile: Tile, obstacle_type: str) -> bool:
        if not self.tool_action_tracker.is_idle():
            return False
        if not game_state.using_tool and game_state.can_move:
            return False

        self._log(
            f"等待上一轮动作释放控制权，暂不发送清障命令: target={target_tile}, obstacle={obstacle_type}, "
            f"UsingTool={game_state.using_tool}, CanMove={game_state.can_move}, "
            f"player_tile={game_state.player_tile}, tool_target={format_tool_target(game_state.tool_target)}"
        )
        return True

    def _get_cardinal_neighbor_tiles(self, target_tile: Tile) -> set[Tile]:
        return {
            Tile(target_tile.x, target_tile.y - 1),
            Tile(target_tile.x + 1, target_tile.y),
            Tile(target_tile.x, target_tile.y + 1),
            Tile(target_tile.x - 1, target_tile.y),
        }

    def _is_positioning_stuck(self, game_state, positioning_result: PositioningResult) -> bool:
        command = positioning_result.command
        if command is None or not command.action.value.startswith("MOVE"):
            self._reset_positioning_stuck_detection()
            return False

        current_position = (game_state.position.x, game_state.position.y)
        now = time.time()
        if self._last_positioning_position is None:
            self._last_positioning_position = current_position
            self._positioning_stuck_started_at = now
            return False

        last_x, last_y = self._last_positioning_position
        is_position_changed = abs(current_position[0] - last_x) > 0.1 or abs(current_position[1] - last_y) > 0.1
        if is_position_changed:
            self._last_positioning_position = current_position
            self._positioning_stuck_started_at = now
            return False

        if self._positioning_stuck_started_at is None:
            self._positioning_stuck_started_at = now
            return False

        stuck_duration = now - self._positioning_stuck_started_at
        self._log(
            f"清障站位移动无位移: duration={stuck_duration:.2f}s, command={command.action}, "
            f"position={current_position}, target={self._target_tile}, stand_tile={positioning_result.stand_tile}, "
            f"positioning={self.positioning_controller.get_debug_snapshot()}"
        )
        return stuck_duration >= POSITIONING_STUCK_TIMEOUT_SECONDS

    def _reset_positioning_stuck_detection(self) -> None:
        self._last_positioning_position = None
        self._positioning_stuck_started_at = None

    def _try_block_stuck_stand_tile(self, positioning_result: PositioningResult) -> bool:
        stuck_stand_tile = positioning_result.stand_tile
        if stuck_stand_tile is None or stuck_stand_tile in self._blocked_stand_tiles:
            return False

        self._blocked_stand_tiles.add(stuck_stand_tile)
        self.positioning_controller.reset()
        self._reset_positioning_stuck_detection()
        self._log(
            f"清障站位疑似不可达，临时换备用站位: stuck_stand_tile={stuck_stand_tile}, "
            f"blocked_stand_tiles={self._format_tile_set(self._blocked_stand_tiles)}"
        )
        return True

    def _format_tile_set(self, tiles: set[Tile]) -> str:
        return str(sorted(tiles, key=lambda tile: (tile.x, tile.y)))

    def _format_tile_list(self, tiles: list[Tile]) -> str:
        return str([(tile.x, tile.y) for tile in tiles])

    def _log(self, message: str) -> None:
        self.clear_obstacle_debug_logger.log(f"[ClearObstacleNode:{self.owner}] {message}")

    def _log_debug_heartbeat(
        self,
        blackboard: AgentBlackboard,
        game_state,
        target_tile: Tile,
        obstacle_type: str,
    ) -> None:
        now = time.time()
        if now - self._last_debug_heartbeat_at < 0.25:
            return

        self._last_debug_heartbeat_at = now
        self._log(
            f"心跳: owner={self.owner}, target={target_tile}, obstacle={obstacle_type}, "
            f"player_tile={game_state.player_tile}, player_position={game_state.position}, "
            f"is_next_to_target={self._is_next_to_target(game_state.player_tile, target_tile)}, "
            f"can_clear_from_current={self._is_next_to_target(game_state.player_tile, target_tile)}, "
            f"current_tool={self._get_current_tool_name(game_state)}, "
            f"tool_target={format_tool_target(game_state.tool_target)}, "
            f"attempt={self._attempt_count}/{self._get_max_clear_attempts(obstacle_type)}, wait_ticks={self._wait_ticks}, "
            f"UsingTool={game_state.using_tool}, CanMove={game_state.can_move}, "
            f"tool_action={self.tool_action_tracker.get_debug_snapshot()}, "
            f"target_state={self._format_farm_tile_state(game_state, target_tile)}, "
            f"elapsed={0.0 if self._started_at is None else now - self._started_at:.2f}, "
            f"blackboard=require_clear_obstacle={blackboard.require_clear_obstacle}, "
            f"clear_obstacle_owner={blackboard.clear_obstacle_owner}, required_tool={blackboard.required_tool}, "
            f"required_tool_owner={blackboard.required_tool_owner}, require_switch_tool={blackboard.require_switch_tool}, "
            f"positioning={self.positioning_controller.get_debug_snapshot()}"
        )

    def _build_clear_command(self, required_tool: str) -> StardewCommand:
        if self._is_weapon_tool(required_tool):
            return StardewCommand(action=StardewAction.ATTACK_WEAPON, key=["c"])
        return StardewCommand(action=StardewAction.USE_TOOL, key=["c"])

    def _is_weapon_tool(self, tool_name: str) -> bool:
        normalized_tool_name = tool_name.strip().lower()
        return any(
            keyword in normalized_tool_name
            for keyword in ("sword", "blade", "saber", "cutlass", "katana", "claymore", "rapier", "slasher")
        )

    def _get_current_tool_name(self, game_state) -> str:
        current_index = game_state.inventory.current_tool_index
        for item in game_state.inventory.items:
            if item.index == current_index:
                return item.name
        return "<unknown>"

    def _format_farm_tile_state(self, game_state, target_tile: Tile) -> str:
        for farm_tile in getattr(game_state, "farm_tiles", []):
            if farm_tile.tile != target_tile:
                continue
            return (
                f"tile={farm_tile.tile}, TerrainFeatureType={farm_tile.terrain_feature_type}, "
                f"ObstacleType={farm_tile.obstacle_type}, CanHoe={farm_tile.can_hoe}, "
                f"CanPlant={farm_tile.can_plant}, HasHoeDirt={farm_tile.has_hoe_dirt}, "
                f"IsDiggable={farm_tile.is_diggable}, IsPassable={farm_tile.is_passable}, "
                f"HasNoSpawn={farm_tile.has_no_spawn}"
            )
        return "None"
