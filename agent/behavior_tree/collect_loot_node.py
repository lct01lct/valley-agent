import time

from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.action.valley_action.clearance_policy import get_obstacle_type_at_tile, normalize_obstacle_type
from agent.action.valley_action.positioning_controller import PositioningController, PositioningGoal
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.collect_loot_debug_logger import CollectLootDebugLogger
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.tool_selection import has_tool_area_tree1_risk, select_required_tool_for_obstacle
from server.type import Tile

COLLECT_LOOT_TIMEOUT_SECONDS = 6.0
COLLECT_TREE_LOOT_TIMEOUT_SECONDS = 12.0
COLLECT_SINGLE_LOOT_TIMEOUT_SECONDS = 4.0
MAX_LOOT_SWEEP_PASSES = 3
DEFAULT_MAGNETIC_RADIUS_RATIO = 0.5
MAGNETIC_RADIUS_TILE_BUFFER = 0.3
LOOT_CLUSTER_LINK_RADIUS_TILES = 2
LOOT_CLUSTER_MAX_STAND_CANDIDATES = 8
TREE_LOOT_COLLECT_RADIUS_TILES = 6
WEEDS_LOOT_COLLECT_RADIUS_TILES = 3
LOOT_COLLECT_STAND_SEARCH_RADIUS_TILES = 3
LOOT_CLEARABLE_OBSTACLE_TYPES = {"grass", "weeds", "twig", "stone"}


class CollectLootNode(BTNode):
    """
    工具动作后的近距离自动拾取节点。

    本节点只处理已有掉落物请求，不主动制造新任务，也不为拾取触发清障。
    树的掉落物允许部分拾取，无法到达的掉落物会被跳过并记录日志。
    """

    def __init__(self) -> None:
        self.positioning_controller = PositioningController()
        self.collect_loot_debug_logger = CollectLootDebugLogger()
        self._started_at: float | None = None
        self._target_tile: Tile | None = None
        self._target_started_at: float | None = None
        self._swept_loot_tiles: set[tuple[int, int]] = set()
        self._sweep_pass_count = 0
        self._source_signature: tuple[str | None, int | None, int | None, str | None] | None = None
        self._last_cluster_log_signature: tuple | None = None

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        if not blackboard.require_collect_loot:
            self._reset()
            return "FAILURE"

        game_state = context.state
        if game_state is None:
            return "RUNNING"

        if self._started_at is None:
            self._start(blackboard)
        elif self._source_changed(blackboard):
            self._restart_for_source_change(blackboard)

        if blackboard.require_clear_obstacle and blackboard.clear_obstacle_owner == blackboard.collect_loot_owner:
            self._pause_collect_timeout_while_waiting_clear()
            self.positioning_controller.reset()
            self._log(
                f"拾取路径等待清障节点处理: owner={blackboard.collect_loot_owner}, "
                f"clear_tile={blackboard.clear_obstacle_tile}, obstacle={blackboard.clear_obstacle_type}, "
                f"source_type={blackboard.collect_loot_source_type}, pending={self._format_tiles(blackboard.pending_loot_tiles)}"
            )
            return "FAILURE"

        if self._started_at is not None and time.time() - self._started_at > self._get_collect_timeout_seconds(
            blackboard
        ):
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
            self._log(
                f"拾取总超时，结束本轮拾取: owner={blackboard.collect_loot_owner}, "
                f"pending={self._format_tiles(blackboard.pending_loot_tiles)}, skipped={blackboard.skipped_loot_tiles}"
            )
            self._finish(blackboard)
            return "SUCCESS"

        if self._is_player_busy(game_state):
            self.positioning_controller.reset()
            self._log(
                f"玩家仍在工具动作/锁移动状态，暂缓拾取移动: "
                f"UsingTool={getattr(game_state, 'using_tool', False)}, CanMove={getattr(game_state, 'can_move', True)}, "
                f"owner={blackboard.collect_loot_owner}, source={blackboard.collect_loot_source_tile}"
            )
            return "RUNNING"

        self._refresh_pending_loot_tiles(blackboard, game_state)
        if not blackboard.pending_loot_tiles:
            self._log(
                f"掉落物已全部消失或已拾取: owner={blackboard.collect_loot_owner}, "
                f"source={blackboard.collect_loot_source_tile}, skipped={blackboard.skipped_loot_tiles}"
            )
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
            self._finish(blackboard)
            return "SUCCESS"

        target_tile = self._select_target_tile(blackboard, game_state)
        if target_tile is None:
            if blackboard.pending_loot_tiles and self._should_continue_sweeping_pending_loot(blackboard):
                self._sweep_pass_count += 1
                self._swept_loot_tiles = set()
                self._target_tile = None
                self._target_started_at = None
                self.positioning_controller.reset()
                self._log(
                    f"发现仍有掉落物，开始第 {self._sweep_pass_count} 轮补扫: "
                    f"owner={blackboard.collect_loot_owner}, source={blackboard.collect_loot_source_tile}, "
                    f"pending={self._format_tiles(blackboard.pending_loot_tiles)}, "
                    f"allow_over_limit={self._is_tree_collect_mode(blackboard)}"
                )
                return "RUNNING"

            self._log(
                f"当前掉落物都已被拾取路径扫过，结束本轮拾取: "
                f"owner={blackboard.collect_loot_owner}, source={blackboard.collect_loot_source_tile}, "
                f"pending={self._format_tiles(blackboard.pending_loot_tiles)}, swept={sorted(self._swept_loot_tiles)}, "
                f"sweep_pass={self._sweep_pass_count}/{MAX_LOOT_SWEEP_PASSES}"
            )
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
            self._finish(blackboard)
            return "SUCCESS"

        if self._target_tile != target_tile:
            self._start_target(target_tile)

        if self._is_loot_collected(game_state, target_tile):
            self._complete_target(blackboard, target_tile, "目标掉落物已消失")
            return "RUNNING"

        if (
            self._target_started_at is not None
            and time.time() - self._target_started_at > COLLECT_SINGLE_LOOT_TIMEOUT_SECONDS
        ):
            self._skip_target(blackboard, target_tile, "单个掉落物拾取超时")
            return "RUNNING"

        positioning_result = self.positioning_controller.tick(
            game_state,
            PositioningGoal(
                candidate_stand_tiles=self._build_collect_candidate_tiles(blackboard, game_state, target_tile),
                tool_target_tile=None,
            ),
        )
        if positioning_result.status == "FAILED":
            if self._request_clear_obstacle_for_loot_path(blackboard, context, game_state, target_tile):
                return "FAILURE"
            self._skip_target(blackboard, target_tile, f"无法规划到掉落物附近: reason={positioning_result.reason}")
            return "RUNNING"

        if positioning_result.command is not None:
            response = context.executor_client.send_command(positioning_result.command)
            self._log(
                f"移动到掉落物附近: target={target_tile}, status={positioning_result.status}, "
                f"command={positioning_result.command.action}, response={response}, "
                f"stand_tile={positioning_result.stand_tile}, player={game_state.player_tile}, "
                f"positioning={self.positioning_controller.get_debug_snapshot()}"
            )
            if response == "BUSY":
                self._target_started_at = time.time()
                self.positioning_controller.reset()
                self._log(f"C# Executor 忙碌，重置拾取站位路径并等待下一帧: target={target_tile}")
            return "RUNNING"

        if positioning_result.status == "READY":
            if self._is_target_in_magnetic_range(game_state, target_tile):
                self._sweep_target(
                    target_tile,
                    game_state,
                    f"已到达聚类拾取覆盖站位: stand_tile={positioning_result.stand_tile}，不中断移动等待",
                )
                return "RUNNING"

            command = self._build_move_command_to_magnetic_range(game_state, target_tile)
            if command.action == StardewAction.IDLE:
                self._log(
                    f"站位 READY 且贴近命令已停止，标记当前掉落物已覆盖: target={target_tile}, "
                    f"stand_tile={positioning_result.stand_tile}, player={game_state.player_tile}, "
                    f"player_position={game_state.position}"
                )
                self._sweep_target(
                    target_tile,
                    game_state,
                    f"贴近命令已停止: stand_tile={positioning_result.stand_tile}，不中断移动等待",
                )
                return "RUNNING"

            response = context.executor_client.send_command(command)
            self._log(
                f"站位 READY 但尚未进入容错磁吸范围，继续像素级贴近: target={target_tile}, "
                f"stand_tile={positioning_result.stand_tile}, player={game_state.player_tile}, "
                f"player_position={game_state.position}, command={command.action}, response={response}"
            )
            if response == "BUSY":
                self._target_started_at = time.time()
            return "RUNNING"

        return "RUNNING"

    def _start(self, blackboard: AgentBlackboard) -> None:
        self._started_at = time.time()
        self._sweep_pass_count = 1
        self._source_signature = self._build_source_signature(blackboard)
        self._log(
            f"开始自动拾取: owner={blackboard.collect_loot_owner}, "
            f"source={blackboard.collect_loot_source_tile}, source_type={blackboard.collect_loot_source_type}, "
            f"pending={self._format_tiles(blackboard.pending_loot_tiles)}"
        )

    def _restart_for_source_change(self, blackboard: AgentBlackboard) -> None:
        old_signature = self._source_signature
        self._started_at = time.time()
        self._target_tile = None
        self._target_started_at = None
        self._swept_loot_tiles = set()
        self._sweep_pass_count = 1
        self._source_signature = self._build_source_signature(blackboard)
        self._last_cluster_log_signature = None
        self.positioning_controller.reset()
        self._log(
            f"拾取来源发生变化，重置拾取计时和扫掠状态: old={old_signature}, new={self._source_signature}, "
            f"pending={self._format_tiles(blackboard.pending_loot_tiles)}"
        )

    def _pause_collect_timeout_while_waiting_clear(self) -> None:
        now = time.time()
        self._started_at = now
        self._target_started_at = None

    def _source_changed(self, blackboard: AgentBlackboard) -> bool:
        return self._source_signature != self._build_source_signature(blackboard)

    def _should_continue_sweeping_pending_loot(self, blackboard: AgentBlackboard) -> bool:
        if self._sweep_pass_count < MAX_LOOT_SWEEP_PASSES:
            return True
        return self._is_tree_collect_mode(blackboard)

    def _build_source_signature(self, blackboard: AgentBlackboard) -> tuple[str | None, int | None, int | None, str | None]:
        source_tile = blackboard.collect_loot_source_tile
        return (
            blackboard.collect_loot_owner,
            None if source_tile is None else source_tile.x,
            None if source_tile is None else source_tile.y,
            blackboard.collect_loot_source_type,
        )

    def _start_target(self, target_tile: Tile) -> None:
        self._target_tile = target_tile
        self._target_started_at = time.time()
        self.positioning_controller.reset()
        self._last_cluster_log_signature = None
        self._log(f"选择掉落物目标: target={target_tile}")

    def _complete_target(self, blackboard: AgentBlackboard, target_tile: Tile, reason: str) -> None:
        blackboard.pending_loot_tiles = [tile for tile in blackboard.pending_loot_tiles if tile != target_tile]
        self._log(
            f"掉落物目标完成: target={target_tile}, reason={reason}, remaining={self._format_tiles(blackboard.pending_loot_tiles)}"
        )
        self._target_tile = None
        self._target_started_at = None
        self.positioning_controller.reset()

    def _skip_target(self, blackboard: AgentBlackboard, target_tile: Tile, reason: str) -> None:
        blackboard.skipped_loot_tiles.add((target_tile.x, target_tile.y))
        blackboard.pending_loot_tiles = [tile for tile in blackboard.pending_loot_tiles if tile != target_tile]
        allow_partial = self._is_partial_collect_allowed(blackboard.collect_loot_source_type)
        self._log(
            f"跳过掉落物: target={target_tile}, reason={reason}, allow_partial={allow_partial}, "
            f"source_type={blackboard.collect_loot_source_type}, remaining={self._format_tiles(blackboard.pending_loot_tiles)}"
        )
        self._target_tile = None
        self._target_started_at = None
        self.positioning_controller.reset()

    def _finish(self, blackboard: AgentBlackboard) -> None:
        blackboard.require_collect_loot = False
        blackboard.collect_loot_owner = None
        blackboard.collect_loot_source_tile = None
        blackboard.collect_loot_source_type = None
        blackboard.pending_loot_tiles = []
        blackboard.skipped_loot_tiles = set()
        self._reset()

    def _reset(self) -> None:
        self._started_at = None
        self._target_tile = None
        self._target_started_at = None
        self._swept_loot_tiles = set()
        self._sweep_pass_count = 0
        self._source_signature = None
        self._last_cluster_log_signature = None
        self.positioning_controller.reset()

    def _refresh_pending_loot_tiles(self, blackboard: AgentBlackboard, game_state) -> None:
        if self._is_dynamic_local_collect_mode(blackboard):
            self._refresh_dynamic_local_loot_tiles(blackboard, game_state)
            return

        self._remove_absent_loot_tiles(blackboard, game_state)

    def _refresh_dynamic_local_loot_tiles(self, blackboard: AgentBlackboard, game_state) -> None:
        current_tiles = self._get_dynamic_loot_tiles_near_source(blackboard, game_state)
        if current_tiles == blackboard.pending_loot_tiles:
            return

        old_tiles = blackboard.pending_loot_tiles
        blackboard.pending_loot_tiles = current_tiles
        self._log(
            f"刷新动态掉落物范围: source={blackboard.collect_loot_source_tile}, "
            f"source_type={blackboard.collect_loot_source_type}, radius={self._get_dynamic_collect_radius(blackboard)}, "
            f"old={self._format_tiles(old_tiles)}, "
            f"current={self._format_tiles(current_tiles)}"
        )

    def _remove_absent_loot_tiles(self, blackboard: AgentBlackboard, game_state) -> None:
        existing_tiles = set(self._get_existing_requested_loot_tiles(blackboard, game_state))
        if len(existing_tiles) == len(blackboard.pending_loot_tiles):
            return

        removed_tiles = [tile for tile in blackboard.pending_loot_tiles if tile not in existing_tiles]
        blackboard.pending_loot_tiles = [tile for tile in blackboard.pending_loot_tiles if tile in existing_tiles]
        if removed_tiles:
            self._log(f"移除已消失掉落物: removed={self._format_tiles(removed_tiles)}")

    def _get_existing_requested_loot_tiles(self, blackboard: AgentBlackboard, game_state) -> list[Tile]:
        if self._is_dynamic_local_collect_mode(blackboard):
            return self._get_dynamic_loot_tiles_near_source(blackboard, game_state)

        requested_tiles = set(blackboard.pending_loot_tiles)
        existing_tiles: list[Tile] = []
        seen_tiles: set[Tile] = set()
        for debris in getattr(game_state, "debris", []):
            if not self._is_collectible_debris(debris):
                continue
            if debris.tile not in requested_tiles:
                continue
            if debris.tile in seen_tiles:
                continue
            seen_tiles.add(debris.tile)
            existing_tiles.append(debris.tile)
        return existing_tiles

    def _get_dynamic_loot_tiles_near_source(self, blackboard: AgentBlackboard, game_state) -> list[Tile]:
        source_tile = blackboard.collect_loot_source_tile
        if source_tile is None:
            return []

        radius = self._get_dynamic_collect_radius(blackboard)
        skipped_tiles = blackboard.skipped_loot_tiles
        loot_tiles: list[Tile] = []
        seen_tiles: set[Tile] = set()
        for debris in getattr(game_state, "debris", []):
            if not self._is_collectible_debris(debris):
                continue
            if (debris.tile.x, debris.tile.y) in skipped_tiles:
                continue
            if debris.tile in seen_tiles:
                continue
            if not self._is_tile_in_dynamic_loot_radius(source_tile, debris.tile, radius):
                continue
            seen_tiles.add(debris.tile)
            loot_tiles.append(debris.tile)

        return sorted(loot_tiles, key=lambda tile: self._tile_distance(game_state.player_tile, tile))

    def _is_tile_in_dynamic_loot_radius(self, source_tile: Tile, debris_tile: Tile, radius: int) -> bool:
        distance_x = abs(source_tile.x - debris_tile.x)
        distance_y = abs(source_tile.y - debris_tile.y)
        return max(distance_x, distance_y) <= radius

    def _select_target_tile(self, blackboard: AgentBlackboard, game_state) -> Tile | None:
        existing_tiles = self._get_existing_requested_loot_tiles(blackboard, game_state)
        if (
            self._target_tile in existing_tiles
            and (self._target_tile.x, self._target_tile.y) not in self._swept_loot_tiles
        ):
            return self._target_tile

        unswept_tiles = [tile for tile in existing_tiles if (tile.x, tile.y) not in self._swept_loot_tiles]
        if not unswept_tiles:
            return None

        return min(unswept_tiles, key=lambda tile: self._tile_distance(game_state.player_tile, tile))

    def _build_collect_candidate_tiles(self, blackboard: AgentBlackboard, game_state, target_tile: Tile) -> set[Tile]:
        debris = self._get_debris_for_tile(game_state, target_tile)
        if debris is None:
            return {target_tile}

        cluster_tiles = self._build_loot_cluster(blackboard, game_state, target_tile)
        cluster_debris = [debris for tile in cluster_tiles if (debris := self._get_debris_for_tile(game_state, tile))]
        stand_radius = LOOT_COLLECT_STAND_SEARCH_RADIUS_TILES
        min_x = min(tile.x for tile in cluster_tiles) - stand_radius
        max_x = max(tile.x for tile in cluster_tiles) + stand_radius
        min_y = min(tile.y for tile in cluster_tiles) - stand_radius
        max_y = max(tile.y for tile in cluster_tiles) + stand_radius
        target_collect_radius = self._get_target_collect_radius(game_state, target_tile)
        effective_magnetic_radius = self._get_effective_magnetic_radius(game_state)
        candidate_scores: list[tuple[int, int, int, Tile]] = []

        for x in range(min_x, max_x + 1):
            for y in range(min_y, max_y + 1):
                candidate_tile = Tile(x, y)
                if not self._can_tile_cover_debris_with_magnetic_range(
                    game_state,
                    candidate_tile,
                    debris.position,
                    target_collect_radius,
                ):
                    continue

                cover_count = sum(
                    1
                    for cluster_item in cluster_debris
                    if self._can_tile_cover_debris_with_magnetic_range(
                        game_state,
                        candidate_tile,
                        cluster_item.position,
                        effective_magnetic_radius,
                    )
                )
                cluster_distance = sum(self._tile_distance(candidate_tile, cluster_item.tile) for cluster_item in cluster_debris)
                player_distance = self._tile_distance(game_state.player_tile, candidate_tile)
                candidate_scores.append((-cover_count, cluster_distance, player_distance, candidate_tile))

        if not candidate_scores:
            return {target_tile}

        candidate_scores.sort(key=lambda item: (item[0], item[1], item[2], item[3].x, item[3].y))
        best_cover_count = -candidate_scores[0][0]
        best_candidates = [
            candidate_tile
            for cover_count, _, _, candidate_tile in candidate_scores
        ][:LOOT_CLUSTER_MAX_STAND_CANDIDATES]
        candidate_tiles = set(best_candidates)
        self._log_cluster_candidate_once(target_tile, cluster_tiles, candidate_tiles, best_cover_count, game_state)
        return candidate_tiles or {target_tile}

    def _build_loot_cluster(self, blackboard: AgentBlackboard, game_state, target_tile: Tile) -> list[Tile]:
        existing_tiles = self._get_existing_requested_loot_tiles(blackboard, game_state)
        existing_tile_set = set(existing_tiles)
        if target_tile not in existing_tile_set:
            return [target_tile]

        cluster_tiles: set[Tile] = {target_tile}
        frontier = [target_tile]
        while frontier:
            current_tile = frontier.pop()
            for candidate_tile in existing_tiles:
                if candidate_tile in cluster_tiles:
                    continue
                if self._tile_chebyshev_distance(current_tile, candidate_tile) > LOOT_CLUSTER_LINK_RADIUS_TILES:
                    continue
                cluster_tiles.add(candidate_tile)
                frontier.append(candidate_tile)

        return sorted(cluster_tiles, key=lambda tile: self._tile_distance(game_state.player_tile, tile))

    def _is_loot_collected(self, game_state, target_tile: Tile) -> bool:
        for debris in getattr(game_state, "debris", []):
            if not self._is_collectible_debris(debris):
                continue
            if debris.tile == target_tile:
                return False
        return True

    def _is_target_in_magnetic_range(self, game_state, target_tile: Tile, should_log: bool = True) -> bool:
        debris = self._get_debris_for_tile(game_state, target_tile)
        if debris is None:
            return False

        raw_magnetic_radius = float(getattr(game_state, "applied_magnetic_radius", 0.0))
        base_magnetic_radius = self._get_effective_magnetic_radius(game_state)
        magnetic_radius = self._get_target_collect_radius(game_state, target_tile)

        if magnetic_radius <= 0:
            return False

        distance_x = abs(game_state.position.x - debris.position.x)
        distance_y = abs(game_state.position.y - debris.position.y)
        is_in_range = distance_x <= magnetic_radius and distance_y <= magnetic_radius
        if is_in_range and should_log:
            self._log(
                f"命中磁吸范围保守阈值: target={target_tile}, debris_position={debris.position}, "
                f"player_position={game_state.position}, raw_magnetic_radius={raw_magnetic_radius:.1f}, "
                f"effective_magnetic_radius={base_magnetic_radius:.1f}, collect_radius={magnetic_radius:.1f}, "
                f"tile_buffer={MAGNETIC_RADIUS_TILE_BUFFER:.1f}, "
                f"delta=({distance_x:.1f}, {distance_y:.1f})"
            )
        return is_in_range

    def _get_debris_for_tile(self, game_state, target_tile: Tile):
        for debris in getattr(game_state, "debris", []):
            if not self._is_collectible_debris(debris):
                continue
            if debris.tile == target_tile:
                return debris
        return None

    def _is_collectible_debris(self, debris) -> bool:
        return bool(getattr(debris, "is_collectible", False))

    def _sweep_target(self, target_tile: Tile, game_state, reason: str) -> None:
        self._swept_loot_tiles.add((target_tile.x, target_tile.y))
        self._log(
            f"拾取路径已覆盖掉落物，不停留等待: target={target_tile}, reason={reason}, "
            f"player_tile={game_state.player_tile}, player_position={game_state.position}, "
            f"swept={sorted(self._swept_loot_tiles)}"
        )
        self._target_tile = None
        self._target_started_at = None
        self.positioning_controller.reset()

    def _can_tile_cover_debris_with_magnetic_range(
        self,
        game_state,
        stand_tile: Tile,
        debris_position,
        magnetic_radius: float | None = None,
    ) -> bool:
        tile_size = game_state.tile_size or 64
        player_width, player_height = game_state.player_size
        half_width = player_width / 2
        half_height = player_height / 2
        magnetic_radius = self._get_effective_magnetic_radius(game_state) if magnetic_radius is None else magnetic_radius
        if magnetic_radius <= 0:
            return False

        min_x = stand_tile.x * tile_size + half_width
        max_x = (stand_tile.x + 1) * tile_size - half_width
        min_y = stand_tile.y * tile_size + half_height
        max_y = (stand_tile.y + 1) * tile_size - half_height

        nearest_x = min(max(debris_position.x, min_x), max_x)
        nearest_y = min(max(debris_position.y, min_y), max_y)
        return (
            abs(nearest_x - debris_position.x) <= magnetic_radius
            and abs(nearest_y - debris_position.y) <= magnetic_radius
        )

    def _get_effective_magnetic_radius(self, game_state) -> float:
        tile_size = game_state.tile_size or 64
        raw_magnetic_radius = float(getattr(game_state, "applied_magnetic_radius", 0.0))
        if raw_magnetic_radius > 0:
            return max(0.0, raw_magnetic_radius - tile_size * MAGNETIC_RADIUS_TILE_BUFFER)
        return tile_size * DEFAULT_MAGNETIC_RADIUS_RATIO

    def _get_target_collect_radius(self, game_state, target_tile: Tile) -> float:
        return self._get_effective_magnetic_radius(game_state)

    def _build_move_command_to_magnetic_range(self, game_state, target_tile: Tile) -> StardewCommand:
        debris = self._get_debris_for_tile(game_state, target_tile)
        if debris is None:
            return StardewCommand(action=StardewAction.IDLE)

        magnetic_radius = self._get_target_collect_radius(game_state, target_tile)
        if magnetic_radius <= 0:
            return StardewCommand(action=StardewAction.IDLE)

        pressed_keys: set[str] = set()
        distance_x = game_state.position.x - debris.position.x
        distance_y = game_state.position.y - debris.position.y

        if distance_x < -magnetic_radius:
            pressed_keys.add("d")
        elif distance_x > magnetic_radius:
            pressed_keys.add("a")

        if distance_y < -magnetic_radius:
            pressed_keys.add("s")
        elif distance_y > magnetic_radius:
            pressed_keys.add("w")

        if "w" in pressed_keys and "d" in pressed_keys:
            return StardewCommand(action=StardewAction.MOVE_UP_RIGHT, key=["w", "d"])
        if "w" in pressed_keys and "a" in pressed_keys:
            return StardewCommand(action=StardewAction.MOVE_UP_LEFT, key=["w", "a"])
        if "s" in pressed_keys and "d" in pressed_keys:
            return StardewCommand(action=StardewAction.MOVE_DOWN_RIGHT, key=["s", "d"])
        if "s" in pressed_keys and "a" in pressed_keys:
            return StardewCommand(action=StardewAction.MOVE_DOWN_LEFT, key=["s", "a"])
        if "w" in pressed_keys:
            return StardewCommand(action=StardewAction.MOVE_UP, key=["w"])
        if "s" in pressed_keys:
            return StardewCommand(action=StardewAction.MOVE_DOWN, key=["s"])
        if "a" in pressed_keys:
            return StardewCommand(action=StardewAction.MOVE_LEFT, key=["a"])
        if "d" in pressed_keys:
            return StardewCommand(action=StardewAction.MOVE_RIGHT, key=["d"])

        return StardewCommand(action=StardewAction.IDLE)

    def _request_clear_obstacle_for_loot_path(
        self,
        blackboard: AgentBlackboard,
        context: PlayerContext,
        game_state,
        target_tile: Tile,
    ) -> bool:
        owner = blackboard.collect_loot_owner
        if owner not in ("Route", "Farm"):
            return False
        if self._is_tree_collect_mode(blackboard):
            self._skip_target(blackboard, target_tile, "树木掉落物拾取不触发清障；磁吸候选站位不可达")
            return False
        if self._is_target_in_magnetic_range(game_state, target_tile, should_log=False):
            self._sweep_target(target_tile, game_state, "已在磁吸范围内，不为拾取触发清障")
            return False

        candidate_tiles = self._build_collect_candidate_tiles(blackboard, game_state, target_tile)
        search_tiles = sorted(
            candidate_tiles | {target_tile},
            key=lambda tile: self._tile_distance(game_state.player_tile, tile),
        )
        for tile in search_tiles:
            obstacle_type = get_obstacle_type_at_tile(game_state, tile)
            normalized_obstacle_type = normalize_obstacle_type(obstacle_type or "")
            if normalized_obstacle_type not in LOOT_CLEARABLE_OBSTACLE_TYPES:
                continue

            required_tool = select_required_tool_for_obstacle(game_state, obstacle_type, tile, owner)
            if required_tool is None:
                continue

            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
            blackboard.require_clear_obstacle = True
            blackboard.clear_obstacle_owner = owner
            blackboard.clear_obstacle_tile = tile
            blackboard.clear_obstacle_type = obstacle_type
            blackboard.require_switch_tool = True
            blackboard.required_tool_owner = owner
            blackboard.is_switching_tool = True
            blackboard.required_tool = required_tool
            self._log(
                f"拾取路径需要轻量清障，转交 ClearObstacleNode: owner={owner}, target={target_tile}, "
                f"clear_tile={tile}, obstacle={obstacle_type}, required_tool={required_tool}, "
                f"tool_area_tree1_risk={has_tool_area_tree1_risk(game_state, tile, required_tool)}"
            )
            self.positioning_controller.reset()
            return True

        return False

    def _get_collect_timeout_seconds(self, blackboard: AgentBlackboard) -> float:
        if self._is_tree_collect_mode(blackboard):
            return COLLECT_TREE_LOOT_TIMEOUT_SECONDS
        return COLLECT_LOOT_TIMEOUT_SECONDS

    def _is_partial_collect_allowed(self, source_type: str | None) -> bool:
        return normalize_obstacle_type(source_type or "") == "tree"

    def _is_tree_collect_mode(self, blackboard: AgentBlackboard) -> bool:
        return normalize_obstacle_type(blackboard.collect_loot_source_type or "") == "tree"

    def _is_area_clear_collect_mode(self, blackboard: AgentBlackboard) -> bool:
        normalized_source_type = normalize_obstacle_type(blackboard.collect_loot_source_type or "")
        return normalized_source_type == "weeds"

    def _is_dynamic_local_collect_mode(self, blackboard: AgentBlackboard) -> bool:
        return self._is_tree_collect_mode(blackboard) or self._is_area_clear_collect_mode(blackboard)

    def _get_dynamic_collect_radius(self, blackboard: AgentBlackboard) -> int:
        if self._is_tree_collect_mode(blackboard):
            return TREE_LOOT_COLLECT_RADIUS_TILES
        return WEEDS_LOOT_COLLECT_RADIUS_TILES

    def _is_player_busy(self, game_state) -> bool:
        return bool(getattr(game_state, "using_tool", False)) or not bool(getattr(game_state, "can_move", True))

    def _tile_distance(self, start_tile: Tile, end_tile: Tile) -> int:
        return abs(start_tile.x - end_tile.x) + abs(start_tile.y - end_tile.y)

    def _tile_chebyshev_distance(self, start_tile: Tile, end_tile: Tile) -> int:
        return max(abs(start_tile.x - end_tile.x), abs(start_tile.y - end_tile.y))

    def _format_tiles(self, tiles: list[Tile]) -> str:
        return str([(tile.x, tile.y) for tile in tiles])

    def _log_cluster_candidate_once(
        self,
        target_tile: Tile,
        cluster_tiles: list[Tile],
        candidate_tiles: set[Tile],
        cover_count: int,
        game_state,
    ) -> None:
        if len(cluster_tiles) <= 1:
            return

        signature = (
            target_tile.x,
            target_tile.y,
            tuple((tile.x, tile.y) for tile in cluster_tiles),
            tuple(sorted((tile.x, tile.y) for tile in candidate_tiles)),
        )
        if self._last_cluster_log_signature == signature:
            return

        self._last_cluster_log_signature = signature
        self._log(
            f"聚类拾取站位规划: target={target_tile}, cluster={self._format_tiles(cluster_tiles)}, "
            f"stand_candidates={self._format_tiles(list(candidate_tiles))}, cover_count={cover_count}, "
            f"player={game_state.player_tile}, effective_magnetic_radius={self._get_effective_magnetic_radius(game_state):.1f}"
        )

    def _log(self, message: str) -> None:
        self.collect_loot_debug_logger.log(f"[CollectLootNode] {message}")
