import time

from agent.action.inventory.inventory_policy import InventoryPolicy, InventoryRecoveryHint
from agent.action.valley_action.AStar import RouteTile
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.action.valley_action.clearance_policy import get_obstacle_type_at_tile, normalize_obstacle_type
from agent.action.valley_action.move_controller import MoveController
from agent.action.valley_action.positioning_controller import PositioningController, PositioningGoal
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard, ResidualLootRecord, UnreceivableLootRecord
from agent.behavior_tree.collect_loot_debug_logger import CollectLootDebugLogger
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.tool_selection import has_tool_area_tree1_risk, select_required_tool_for_obstacle
from server.valley_server import IGNORED_DEBRIS_QUALIFIED_ITEM_IDS
from server.type import Tile

COLLECT_LOOT_TIMEOUT_SECONDS = 6.0
COLLECT_TREE_LOOT_TIMEOUT_SECONDS = 12.0
COLLECT_SINGLE_LOOT_TIMEOUT_SECONDS = 4.0
DEFAULT_MAGNETIC_RADIUS_RATIO = 0.5
MAGNETIC_RADIUS_TILE_BUFFER = 0.1
LOOT_CLUSTER_LINK_RADIUS_TILES = 2
LOOT_CLUSTER_MAX_STAND_CANDIDATES = 8
TREE_LOOT_CLUSTER_MAX_STAND_CANDIDATES = 32
TREE_LOOT_COLLECT_RADIUS_TILES = 6
WEEDS_LOOT_COLLECT_RADIUS_TILES = 3
STONE_LOOT_COLLECT_RADIUS_TILES = 4
LOOT_COLLECT_STAND_SEARCH_RADIUS_TILES = 3
LOOT_RELOCATE_SEARCH_RADIUS_TILES = 6
LOOT_SOURCE_RETURN_RADIUS_TILES = 3
LOOT_CLEARABLE_OBSTACLE_TYPES = {"grass", "weeds", "twig", "stone"}
TREE_LOOT_MAX_PATH_TILES = 12
LOOT_MAGNETIC_STALL_SECONDS = 0.25
LOOT_POSITION_PROGRESS_EPSILON = 1.0
UNRECEIVABLE_LOOT_SKIP_SECONDS = 5.0
INVENTORY_RECOVERY_RETURN_ARRIVAL_RADIUS_TILES = 1


class CollectLootNode(BTNode):
    """
    工具动作后的近距离自动拾取节点。

    本节点只处理已有掉落物请求，不主动制造新任务。
    掉落物路径被局部障碍阻塞时，可通过黑板转交 ClearObstacleNode 清障后继续拾取。
    """

    def __init__(self) -> None:
        self.positioning_controller = PositioningController()
        self.inventory_recovery_return_move_controller = MoveController()
        self.inventory_policy = InventoryPolicy()
        self.collect_loot_debug_logger = CollectLootDebugLogger()
        self._started_at: float | None = None
        self._target_tile: Tile | None = None
        self._target_started_at: float | None = None
        self._swept_loot_tiles: set[tuple[int, int]] = set()
        self._sweep_pass_count = 0
        self._source_signature: tuple[str | None, int | None, int | None, str | None] | None = None
        self._last_cluster_log_signature: tuple | None = None
        self._inventory_snapshot: dict[str, int] = {}
        self._observed_loot_item_keys: set[str] = set()
        self._last_magnetic_player_position: tuple[float, float] | None = None
        self._last_magnetic_debris_position: tuple[float, float] | None = None
        self._magnetic_stall_started_at: float | None = None
        self._returning_to_loot_source_after_inventory_recovery = False
        self._inventory_recovery_return_path: list[RouteTile] = []
        self._inventory_recovery_return_path_index = 0

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        if not blackboard.require_collect_loot:
            self._reset()
            return "FAILURE"

        game_state = context.state
        if game_state is None:
            return "RUNNING"

        self._prune_unreceivable_loot_records(blackboard, game_state)
        if self._should_yield_to_inventory_recovery(blackboard):
            self._pause_collect_timeout_while_waiting_inventory_recovery()
            self.positioning_controller.reset()
            self._log(
                f"背包恢复请求处理中，暂停拾取并让出行为树控制权: "
                f"context={blackboard.inventory_recovery_context}"
            )
            return "FAILURE"

        if self._started_at is None:
            self._start(blackboard, game_state)
        elif self._source_changed(blackboard):
            self._restart_for_source_change(blackboard, game_state)

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
            if self._observed_loot_item_keys and not self._has_observed_inventory_gain(game_state):
                inventory_gain_items = self._get_inventory_gain_items(game_state)
                if inventory_gain_items:
                    self._log(
                        f"可识别掉落物已消失，但背包增量身份与观察身份不一致，按真实背包增量完成拾取: "
                        f"owner={blackboard.collect_loot_owner}, source={blackboard.collect_loot_source_tile}, "
                        f"observed_item_keys={sorted(self._observed_loot_item_keys)}, "
                        f"inventory_gain_items={inventory_gain_items}"
                    )
                    context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
                    self._finish(blackboard)
                    return "SUCCESS"

                self._log(
                    f"可识别掉落物已离开可见范围，但背包尚未观察到增量，继续等待 state 刷新: "
                    f"owner={blackboard.collect_loot_owner}, source={blackboard.collect_loot_source_tile}, "
                    f"observed_item_keys={sorted(self._observed_loot_item_keys)}, "
                    f"inventory_snapshot={self._inventory_snapshot}, "
                    f"current_inventory={self._snapshot_inventory_items(game_state)}"
                )
                context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
                return "RUNNING"

            self._log(
                f"掉落物已全部消失或已拾取: owner={blackboard.collect_loot_owner}, "
                f"source={blackboard.collect_loot_source_tile}, skipped={blackboard.skipped_loot_tiles}"
            )
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
            self._finish(blackboard)
            return "SUCCESS"

        if self._return_to_residual_loot_after_inventory_recovery(blackboard, context, game_state):
            return "RUNNING"

        target_tile = self._select_target_tile(blackboard, game_state)
        if target_tile is None:
            self._log(
                f"当前 pending 掉落物已不在 state 中，结束本轮拾取: "
                f"owner={blackboard.collect_loot_owner}, source={blackboard.collect_loot_source_tile}, "
                f"pending={self._format_tiles(blackboard.pending_loot_tiles)}, swept={sorted(self._swept_loot_tiles)}"
            )
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
            self._finish(blackboard)
            return "SUCCESS"

        if self._target_tile != target_tile:
            self._start_target(target_tile)

        debris = self._get_debris_for_tile(blackboard, game_state, target_tile)
        if debris is not None:
            inventory_decision = self.inventory_policy.can_accept_debris(game_state, debris)
            if not inventory_decision.can_accept:
                recovery_hint = self.inventory_policy.build_recovery_hint(game_state, inventory_decision)
                if blackboard.inventory_recovery_context.get("recovery_failed"):
                    self._register_unreceivable_loot_skip(
                        blackboard,
                        game_state,
                        debris,
                        inventory_decision.reason,
                    )
                    self._clear_inventory_recovery_request(blackboard)
                    context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
                    self._discard_observed_loot_item_key(debris, f"背包恢复失败后跳过: {inventory_decision.reason}")
                    self._skip_target(
                        blackboard,
                        target_tile,
                        f"背包恢复失败，跳过当前不可接收掉落物: {inventory_decision.reason}",
                    )
                    return "RUNNING"

                receivable_target_tile = self._select_receivable_alternative_target(
                    blackboard,
                    game_state,
                    target_tile,
                )
                if receivable_target_tile is not None:
                    self._target_tile = None
                    self._target_started_at = None
                    self.positioning_controller.reset()
                    self._log(
                        f"当前掉落物暂不可接收，先拾取同来源中仍可接收的掉落物: "
                        f"blocked_target={target_tile}, next_target={receivable_target_tile}, "
                        f"reason={inventory_decision.reason}"
                    )
                    return "RUNNING"

                self._register_unreceivable_loot_skip(
                    blackboard,
                    game_state,
                    debris,
                    inventory_decision.reason,
                )
                self._register_inventory_recovery(
                    blackboard,
                    game_state,
                    target_tile,
                    inventory_decision.reason,
                    recovery_hint,
                )
                context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
                self.positioning_controller.reset()
                self._log(
                    f"背包无法接收掉落物，暂停拾取并交给 InventoryRecovery: target={target_tile}, "
                    f"reason={inventory_decision.reason}"
                )
                return "FAILURE"

        if self._is_loot_collected_for_source(blackboard, game_state, target_tile):
            self._complete_target(blackboard, target_tile, "目标掉落物已消失")
            return "RUNNING"

        if (
            self._target_started_at is not None
            and time.time() - self._target_started_at > COLLECT_SINGLE_LOOT_TIMEOUT_SECONDS
        ):
            self._skip_target(blackboard, target_tile, "单个掉落物拾取超时")
            return "RUNNING"

        if self._is_target_in_magnetic_range(blackboard, game_state, target_tile):
            if debris is not None and self._is_magnetic_pickup_stalled(game_state, debris):
                context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
                self.positioning_controller.reset()
                if self._request_clear_obstacle_for_loot_path(
                    blackboard,
                    context,
                    game_state,
                    target_tile,
                    allow_when_in_magnetic_range=True,
                ):
                    self._log(
                        f"掉落物虽在磁吸范围内但已停滞，尝试转交清障后再拾取: target={target_tile}, "
                        f"source={blackboard.collect_loot_source_tile}, source_type={blackboard.collect_loot_source_type}"
                    )
                    return "FAILURE"

                self._skip_target(
                    blackboard,
                    target_tile,
                    "已在磁吸范围内但人物/掉落物没有有效变化，且没有可清障碍可处理",
                )
                return "RUNNING"

            command = self._build_move_command_to_debris_center(blackboard, game_state, target_tile)
            response = context.executor_client.send_command(command)
            self._mark_target_covered(
                target_tile,
                game_state,
                f"无需重新规划站位，已在磁吸范围内继续贴近等待 state 消失: "
                f"command={command.action}, response={response}",
            )
            return "RUNNING"

        positioning_result = self.positioning_controller.tick(
            game_state,
            PositioningGoal(
                candidate_stand_tiles=self._build_collect_candidate_tiles(blackboard, game_state, target_tile),
                tool_target_tile=None,
                extra_blocked_tiles=self._build_collect_extra_blocked_tiles(game_state),
            ),
        )
        if positioning_result.status == "FAILED":
            if self._move_to_loose_tree_loot_stand(blackboard, context, game_state, target_tile):
                return "RUNNING"
            if self._request_clear_obstacle_for_loot_path(blackboard, context, game_state, target_tile):
                return "FAILURE"
            self._skip_target(blackboard, target_tile, f"无法规划到掉落物附近: reason={positioning_result.reason}")
            return "RUNNING"

        if positioning_result.command is not None:
            current_path_length = self.positioning_controller.get_current_path_length()
            if (
                self._is_tree_collect_mode(blackboard)
                and current_path_length > TREE_LOOT_MAX_PATH_TILES
                and not blackboard.collect_loot_resume_after_inventory_recovery
            ):
                context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
                self._skip_target(
                    blackboard,
                    target_tile,
                    f"树木掉落物拾取路径过长，放弃绕路拾取: path_len={current_path_length}, "
                    f"max_path={TREE_LOOT_MAX_PATH_TILES}",
                )
                return "RUNNING"

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
            elif blackboard.collect_loot_resume_after_inventory_recovery and current_path_length > TREE_LOOT_MAX_PATH_TILES:
                self._log(
                    f"背包恢复后返回掉落物现场，临时放宽树木拾取路径长度限制: "
                    f"target={target_tile}, path_len={current_path_length}, max_path={TREE_LOOT_MAX_PATH_TILES}"
                )
            return "RUNNING"

        if positioning_result.status == "READY":
            if self._is_target_in_magnetic_range(blackboard, game_state, target_tile):
                if debris is not None and self._is_magnetic_pickup_stalled(game_state, debris):
                    context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
                    self._skip_target(
                        blackboard,
                        target_tile,
                        "已进入磁吸范围但人物/掉落物没有有效变化，判定当前掉落物无法继续吸附",
                    )
                    return "RUNNING"

                command = self._build_move_command_to_debris_center(blackboard, game_state, target_tile)
                response = context.executor_client.send_command(command)
                self._mark_target_covered(
                    target_tile,
                    game_state,
                    f"已进入磁吸范围但掉落物仍存在，继续贴近等待 state 消失: "
                    f"stand_tile={positioning_result.stand_tile}, command={command.action}, response={response}",
                )
                return "RUNNING"

            command = self._build_move_command_to_magnetic_range(blackboard, game_state, target_tile)
            if command.action == StardewAction.IDLE:
                self._log(
                    f"站位 READY 且贴近命令已停止，但掉落物仍存在，继续等待 state 刷新或单目标超时: target={target_tile}, "
                    f"stand_tile={positioning_result.stand_tile}, player={game_state.player_tile}, "
                    f"player_position={game_state.position}"
                )
                self._mark_target_covered(
                    target_tile,
                    game_state,
                    f"贴近命令已停止: stand_tile={positioning_result.stand_tile}",
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

    def _start(self, blackboard: AgentBlackboard, game_state) -> None:
        self._started_at = time.time()
        self._sweep_pass_count = 1
        self._source_signature = self._build_source_signature(blackboard)
        blackboard.collect_loot_resume_after_inventory_recovery = False
        blackboard.collect_loot_residual_record = None
        self._returning_to_loot_source_after_inventory_recovery = False
        self._inventory_snapshot = self._snapshot_inventory_items(game_state)
        self._observed_loot_item_keys = set()
        self._observe_visible_loot_item_keys(blackboard, game_state)
        self._log(
            f"开始自动拾取: owner={blackboard.collect_loot_owner}, "
            f"source={blackboard.collect_loot_source_tile}, source_type={blackboard.collect_loot_source_type}, "
            f"pending={self._format_tiles(blackboard.pending_loot_tiles)}, "
            f"observed_item_keys={sorted(self._observed_loot_item_keys)}"
        )

    def _restart_for_source_change(self, blackboard: AgentBlackboard, game_state) -> None:
        old_signature = self._source_signature
        self._started_at = time.time()
        self._target_tile = None
        self._target_started_at = None
        self._swept_loot_tiles = set()
        self._sweep_pass_count = 1
        self._source_signature = self._build_source_signature(blackboard)
        self._last_cluster_log_signature = None
        blackboard.collect_loot_resume_after_inventory_recovery = False
        blackboard.collect_loot_residual_record = None
        self._returning_to_loot_source_after_inventory_recovery = False
        self._inventory_snapshot = self._snapshot_inventory_items(game_state)
        self._observed_loot_item_keys = set()
        self._observe_visible_loot_item_keys(blackboard, game_state)
        self.positioning_controller.reset()
        self._log(
            f"拾取来源发生变化，重置拾取计时和扫掠状态: old={old_signature}, new={self._source_signature}, "
            f"pending={self._format_tiles(blackboard.pending_loot_tiles)}, "
            f"observed_item_keys={sorted(self._observed_loot_item_keys)}"
        )

    def _pause_collect_timeout_while_waiting_clear(self) -> None:
        now = time.time()
        self._started_at = now
        self._target_started_at = None

    def _pause_collect_timeout_while_waiting_inventory_recovery(self) -> None:
        now = time.time()
        self._started_at = now
        self._target_started_at = None

    def _source_changed(self, blackboard: AgentBlackboard) -> bool:
        return self._source_signature != self._build_source_signature(blackboard)

    def _build_source_signature(
        self, blackboard: AgentBlackboard
    ) -> tuple[str | None, int | None, int | None, str | None]:
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
        self._last_magnetic_player_position = None
        self._last_magnetic_debris_position = None
        self._magnetic_stall_started_at = None
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
        self._last_magnetic_player_position = None
        self._last_magnetic_debris_position = None
        self._magnetic_stall_started_at = None
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
        self._last_magnetic_player_position = None
        self._last_magnetic_debris_position = None
        self._magnetic_stall_started_at = None
        self.positioning_controller.reset()

    def _register_inventory_recovery(
        self,
        blackboard: AgentBlackboard,
        game_state,
        target_tile: Tile,
        reason: str,
        recovery_hint: InventoryRecoveryHint,
    ) -> None:
        summary = self.inventory_policy.build_summary(game_state)
        blackboard.inventory_check_failed = True
        blackboard.inventory_risk_level = "FULL_BLOCKED"
        blackboard.inventory_failure_reason = "INVENTORY_FULL_WHILE_COLLECTING"
        blackboard.inventory_recovery_hint = recovery_hint.reason
        blackboard.inventory_recovery_strategy = recovery_hint.strategy
        blackboard.inventory_recovery_task = (
            blackboard.macro_plan[blackboard.current_step_index]
            if blackboard.macro_plan and blackboard.current_step_index < len(blackboard.macro_plan)
            else None
        )
        blackboard.inventory_discard_candidates = [
            {
                "item_name": candidate.item_name,
                "qualified_item_id": candidate.qualified_item_id,
                "count": candidate.count,
                "index": candidate.index,
                "reason": candidate.reason,
            }
            for candidate in recovery_hint.discard_candidates
        ]
        blackboard.inventory_recovery_departure_path = [game_state.player_tile]
        self._register_residual_loot_record(blackboard, game_state, target_tile)
        blackboard.inventory_recovery_context = {
            "owner": blackboard.collect_loot_owner,
            "source_tile": self._format_tile(blackboard.collect_loot_source_tile),
            "source_type": blackboard.collect_loot_source_type,
            "target_tile": self._format_tile(target_tile),
            "risk_level": summary.risk_level,
            "free_slots": summary.free_slots,
            "occupied_slots": summary.occupied_slots,
            "max_items": summary.max_items,
            "protected_items": summary.protected_items,
            "reason": reason,
        }
        self._log(
            f"背包无法接收掉落物，跳过当前掉落物并暴露恢复意图: target={target_tile}, "
            f"reason={reason}, strategy={recovery_hint.strategy}, hint={recovery_hint.reason}, "
            f"discard_candidates={blackboard.inventory_discard_candidates}, "
            f"context={blackboard.inventory_recovery_context}"
        )

    def _register_residual_loot_record(
        self,
        blackboard: AgentBlackboard,
        game_state,
        recovery_target_tile: Tile,
    ) -> None:
        source_tile = blackboard.collect_loot_source_tile
        observed_item_keys = set(self._observed_loot_item_keys)
        remaining_tiles: list[Tile] = []
        seen_tiles: set[Tile] = set()
        for debris in self._get_observed_debris_for_current_source(blackboard, game_state):
            item_key = self._build_debris_item_key(debris)
            if item_key is not None:
                observed_item_keys.add(item_key)
            if debris.tile in seen_tiles:
                continue
            seen_tiles.add(debris.tile)
            remaining_tiles.append(debris.tile)

        for tile in blackboard.pending_loot_tiles:
            if tile in seen_tiles:
                continue
            seen_tiles.add(tile)
            remaining_tiles.append(tile)

        blackboard.collect_loot_residual_record = ResidualLootRecord(
            owner=blackboard.collect_loot_owner,
            location_name=str(getattr(game_state, "location_name", "") or ""),
            source_tile=source_tile,
            source_type=blackboard.collect_loot_source_type,
            observed_item_keys=observed_item_keys,
            remaining_tiles=remaining_tiles,
            recovery_target_tile=recovery_target_tile,
            created_at=time.time(),
        )
        self._log(
            f"记录背包恢复后的残留掉落物上下文: owner={blackboard.collect_loot_owner}, "
            f"source={source_tile}, source_type={blackboard.collect_loot_source_type}, "
            f"recovery_target={recovery_target_tile}, observed_item_keys={sorted(observed_item_keys)}, "
            f"remaining={self._format_tiles(remaining_tiles)}"
        )

    def _register_unreceivable_loot_skip(
        self,
        blackboard: AgentBlackboard,
        game_state,
        debris,
        reason: str,
    ) -> None:
        item_key = self._build_debris_item_key(debris)
        if item_key is None:
            return

        record = UnreceivableLootRecord(
            owner=blackboard.collect_loot_owner,
            location_name=str(getattr(game_state, "location_name", "") or ""),
            source_tile=blackboard.collect_loot_source_tile,
            source_type=self._normalize_source_type(blackboard.collect_loot_source_type),
            item_key=item_key,
            inventory_signature=self._build_inventory_signature(game_state),
            expires_at=time.time() + UNRECEIVABLE_LOOT_SKIP_SECONDS,
            reason=reason,
        )
        existing_records = {
            existing_record.key: existing_record for existing_record in blackboard.unreceivable_loot_records
        }
        existing_records[record.key] = record
        blackboard.unreceivable_loot_records = list(existing_records.values())
        self._log(
            f"登记不可接收掉落物短期跳过: owner={record.owner}, location={record.location_name}, "
            f"source={self._format_tile(record.source_tile)}, source_type={record.source_type}, "
            f"item_key={item_key}, ttl={UNRECEIVABLE_LOOT_SKIP_SECONDS:.1f}s, reason={reason}"
        )

    def _prune_unreceivable_loot_records(self, blackboard: AgentBlackboard, game_state) -> None:
        now = time.time()
        inventory_signature = self._build_inventory_signature(game_state)
        kept_records: list[UnreceivableLootRecord] = []
        removed_records: list[UnreceivableLootRecord] = []
        for record in blackboard.unreceivable_loot_records:
            if record.expires_at <= now or (
                record.inventory_signature and record.inventory_signature != inventory_signature
            ):
                removed_records.append(record)
                continue
            kept_records.append(record)

        if not removed_records:
            return

        blackboard.unreceivable_loot_records = kept_records
        self._log(
            f"清理不可接收掉落物短期跳过记录: removed={len(removed_records)}, "
            f"remaining={len(kept_records)}, inventory_changed={any(record.inventory_signature != inventory_signature for record in removed_records)}"
        )

    def _remove_unreceivable_pending_loot_tiles(self, blackboard: AgentBlackboard, game_state) -> None:
        if not blackboard.pending_loot_tiles or not blackboard.unreceivable_loot_records:
            return

        kept_tiles: list[Tile] = []
        removed_tiles: list[Tile] = []
        for tile in blackboard.pending_loot_tiles:
            debris = self._get_debris_for_tile_without_skip(blackboard, game_state, tile)
            if debris is not None and self._is_unreceivable_loot_skipped(blackboard, game_state, debris):
                blackboard.skipped_loot_tiles.add((tile.x, tile.y))
                removed_tiles.append(tile)
                continue
            kept_tiles.append(tile)

        if not removed_tiles:
            return

        blackboard.pending_loot_tiles = kept_tiles
        self._log(
            f"移除当前背包状态下不可接收的 pending 掉落物: "
            f"removed={self._format_tiles(removed_tiles)}, remaining={self._format_tiles(kept_tiles)}"
        )

    def _is_unreceivable_loot_skipped(self, blackboard: AgentBlackboard, game_state, debris) -> bool:
        item_key = self._build_debris_item_key(debris)
        if item_key is None:
            return False

        location_name = str(getattr(game_state, "location_name", "") or "")
        source_tile = blackboard.collect_loot_source_tile
        source_type = self._normalize_source_type(blackboard.collect_loot_source_type)
        inventory_signature = self._build_inventory_signature(game_state)
        for record in blackboard.unreceivable_loot_records:
            if record.item_key != item_key:
                continue
            if record.owner is not None and record.owner != blackboard.collect_loot_owner:
                continue
            if record.location_name != location_name:
                continue
            if record.source_type is not None and record.source_type != source_type:
                continue
            if record.source_tile is not None and self._format_tile(record.source_tile) != self._format_tile(source_tile):
                continue
            if record.inventory_signature and record.inventory_signature != inventory_signature:
                continue
            return True
        return False

    def _should_yield_to_inventory_recovery(self, blackboard: AgentBlackboard) -> bool:
        return (
            blackboard.inventory_check_failed
            and blackboard.inventory_failure_reason == "INVENTORY_FULL_WHILE_COLLECTING"
            and not bool(blackboard.inventory_recovery_context.get("recovery_failed"))
        )

    def _clear_inventory_recovery_request(self, blackboard: AgentBlackboard) -> None:
        blackboard.inventory_check_failed = False
        blackboard.inventory_risk_level = None
        blackboard.inventory_failure_reason = None
        blackboard.inventory_recovery_hint = None
        blackboard.inventory_recovery_strategy = None
        blackboard.inventory_recovery_context = {}
        blackboard.inventory_recovery_task = None
        blackboard.inventory_discard_candidates = []

    def _finish(self, blackboard: AgentBlackboard) -> None:
        blackboard.require_collect_loot = False
        blackboard.collect_loot_owner = None
        blackboard.collect_loot_source_tile = None
        blackboard.collect_loot_source_type = None
        blackboard.pending_loot_tiles = []
        blackboard.skipped_loot_tiles = set()
        blackboard.collect_loot_resume_after_inventory_recovery = False
        blackboard.collect_loot_residual_record = None
        blackboard.inventory_recovery_departure_path = []
        self._reset()

    def _reset(self) -> None:
        self._started_at = None
        self._target_tile = None
        self._target_started_at = None
        self._swept_loot_tiles = set()
        self._sweep_pass_count = 0
        self._source_signature = None
        self._last_cluster_log_signature = None
        self._inventory_snapshot = {}
        self._observed_loot_item_keys = set()
        self._last_magnetic_player_position = None
        self._last_magnetic_debris_position = None
        self._magnetic_stall_started_at = None
        self._returning_to_loot_source_after_inventory_recovery = False
        self._inventory_recovery_return_path = []
        self._inventory_recovery_return_path_index = 0
        self.positioning_controller.reset()
        self.inventory_recovery_return_move_controller.reset()

    def _refresh_pending_loot_tiles(self, blackboard: AgentBlackboard, game_state) -> None:
        self._remove_unreceivable_pending_loot_tiles(blackboard, game_state)
        self._observe_visible_loot_item_keys(blackboard, game_state)
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

    def _return_to_residual_loot_after_inventory_recovery(
        self,
        blackboard: AgentBlackboard,
        context: PlayerContext,
        game_state,
    ) -> bool:
        source_tile = blackboard.collect_loot_source_tile
        if source_tile is None:
            self._returning_to_loot_source_after_inventory_recovery = False
            return False
        if not blackboard.collect_loot_resume_after_inventory_recovery:
            self._returning_to_loot_source_after_inventory_recovery = False
            return False
        if not self._is_tree_collect_mode(blackboard):
            self._returning_to_loot_source_after_inventory_recovery = False
            return False

        if self._return_along_inventory_recovery_departure_path(blackboard, context, game_state):
            return True

        candidate_tiles, return_target_desc = self._build_inventory_recovery_return_candidate_tiles(
            blackboard,
            game_state,
            source_tile,
        )
        if not candidate_tiles:
            self._returning_to_loot_source_after_inventory_recovery = False
            return False

        should_start_return = not self._is_player_near_any_tile(
            game_state.player_tile,
            candidate_tiles,
            LOOT_RELOCATE_SEARCH_RADIUS_TILES,
        )
        if not self._returning_to_loot_source_after_inventory_recovery and not should_start_return:
            return False

        if not self._returning_to_loot_source_after_inventory_recovery:
            self._returning_to_loot_source_after_inventory_recovery = True
            self.positioning_controller.reset()
            self._log(
                f"背包恢复后开始返回残留掉落物区域: "
                f"source={source_tile}, target={return_target_desc}, player={game_state.player_tile}, "
                f"nearest_distance={self._nearest_tile_distance(game_state.player_tile, candidate_tiles)}"
            )

        positioning_result = self.positioning_controller.tick(
            game_state,
            PositioningGoal(
                candidate_stand_tiles=candidate_tiles,
                tool_target_tile=None,
                extra_blocked_tiles=self._build_collect_extra_blocked_tiles(game_state),
            ),
        )
        if positioning_result.status == "FAILED":
            self._log(
                f"背包恢复后返回残留掉落物区域失败，继续尝试具体掉落物站位: "
                f"source={source_tile}, target={return_target_desc}, player={game_state.player_tile}, "
                f"reason={positioning_result.reason}"
            )
            self._returning_to_loot_source_after_inventory_recovery = False
            self.positioning_controller.reset()
            return False

        if positioning_result.command is None:
            self._restore_residual_loot_record_after_recovery(blackboard, game_state)
            self._log(
                f"背包恢复后已回到残留掉落物区域，继续刷新并拾取残留: "
                f"source={source_tile}, target={return_target_desc}, player={game_state.player_tile}, "
                f"status={positioning_result.status}"
            )
            self._returning_to_loot_source_after_inventory_recovery = False
            self.positioning_controller.reset()
            return False

        response = context.executor_client.send_command(positioning_result.command)
        self._log(
            f"背包恢复后先返回残留掉落物区域: source={source_tile}, target={return_target_desc}, "
            f"command={positioning_result.command.action}, response={response}, "
            f"stand_tile={positioning_result.stand_tile}, player={game_state.player_tile}, "
            f"positioning={self.positioning_controller.get_debug_snapshot()}"
        )
        if response == "BUSY":
            self.positioning_controller.reset()
        return True

    def _return_along_inventory_recovery_departure_path(
        self,
        blackboard: AgentBlackboard,
        context: PlayerContext,
        game_state,
    ) -> bool:
        residual_record = blackboard.collect_loot_residual_record
        raw_path = (
            residual_record.inventory_recovery_departure_path
            if residual_record is not None and residual_record.inventory_recovery_departure_path
            else blackboard.inventory_recovery_departure_path
        )
        if len(raw_path) < 2:
            return False

        departure_path = self._dedupe_consecutive_tiles(raw_path)
        if not departure_path:
            return False

        return_anchor = departure_path[0]
        if self._tile_distance(game_state.player_tile, return_anchor) <= INVENTORY_RECOVERY_RETURN_ARRIVAL_RADIUS_TILES:
            self._restore_residual_loot_record_after_recovery(blackboard, game_state)
            self._log(
                f"背包恢复后已沿原路回到离场区域，继续拾取残留: "
                f"anchor={return_anchor}, player={game_state.player_tile}, "
                f"recorded_path_len={len(departure_path)}"
            )
            self._clear_inventory_recovery_return_path()
            blackboard.inventory_recovery_departure_path = []
            if residual_record is not None:
                residual_record.inventory_recovery_departure_path = []
            self._returning_to_loot_source_after_inventory_recovery = False
            return False

        if not self._inventory_recovery_return_path:
            reverse_path = list(reversed(departure_path))
            self._inventory_recovery_return_path = [
                RouteTile(tile.x, tile.y, "walk")
                for tile in reverse_path
            ]
            self._inventory_recovery_return_path_index = 0
            self.inventory_recovery_return_move_controller.reset()
            self.positioning_controller.reset()
            self._returning_to_loot_source_after_inventory_recovery = True
            self._log(
                f"背包恢复后沿存箱路径原路返回: "
                f"anchor={return_anchor}, player={game_state.player_tile}, "
                f"path_len={len(self._inventory_recovery_return_path)}, "
                f"path={self._format_tiles(departure_path)}"
            )

        command, next_path_index, is_done = self.inventory_recovery_return_move_controller.get_next_move_command(
            game_state,
            self._inventory_recovery_return_path,
            self._inventory_recovery_return_path_index,
            smooth_long_path=True,
        )
        self._inventory_recovery_return_path_index = next_path_index

        if is_done or command.action == StardewAction.IDLE:
            self._restore_residual_loot_record_after_recovery(blackboard, game_state)
            self._log(
                f"背包恢复后原路返回路径完成，继续刷新并拾取残留: "
                f"anchor={return_anchor}, player={game_state.player_tile}, "
                f"path_index={self._inventory_recovery_return_path_index}, "
                f"path_len={len(self._inventory_recovery_return_path)}"
            )
            self._clear_inventory_recovery_return_path()
            blackboard.inventory_recovery_departure_path = []
            if residual_record is not None:
                residual_record.inventory_recovery_departure_path = []
            self._returning_to_loot_source_after_inventory_recovery = False
            return False

        response = context.executor_client.send_command(command)
        self._log(
            f"背包恢复后沿存箱路径返回: anchor={return_anchor}, command={command.action}, "
            f"response={response}, player={game_state.player_tile}, "
            f"path_index={self._inventory_recovery_return_path_index}, "
            f"path_len={len(self._inventory_recovery_return_path)}, "
            f"follow={self.inventory_recovery_return_move_controller.get_path_follow_debug_snapshot()}"
        )
        if response == "BUSY":
            self._clear_inventory_recovery_return_path()
        return True

    def _clear_inventory_recovery_return_path(self) -> None:
        self._inventory_recovery_return_path = []
        self._inventory_recovery_return_path_index = 0
        self.inventory_recovery_return_move_controller.reset()

    def _dedupe_consecutive_tiles(self, tiles: list[Tile]) -> list[Tile]:
        deduped_tiles: list[Tile] = []
        for tile in tiles:
            if deduped_tiles and deduped_tiles[-1] == tile:
                continue
            deduped_tiles.append(tile)
        return deduped_tiles

    def _build_inventory_recovery_return_candidate_tiles(
        self,
        blackboard: AgentBlackboard,
        game_state,
        source_tile: Tile,
    ) -> tuple[set[Tile], str]:
        """
        背包恢复会把角色带离掉落物现场。恢复后的第一目标应先回到 source
        附近，再用最新 state 和物品身份重新定位残留掉落物；不要在箱子旁
        直接对旧掉落物 tile 做远距离精确站位 A*。
        """
        residual_record = blackboard.collect_loot_residual_record
        if residual_record is not None and self._residual_record_matches_current_source(blackboard, game_state):
            candidate_tiles = self._build_loot_source_return_candidate_tiles(source_tile)
            return (
                candidate_tiles,
                f"residual_source={source_tile}, recorded={self._format_tiles(residual_record.remaining_tiles)}, "
                f"item_keys={sorted(residual_record.observed_item_keys)}",
            )

        recovery_target_tile = self._get_inventory_recovery_context_target_tile(blackboard)
        if recovery_target_tile is not None:
            return (
                self._build_loot_source_return_candidate_tiles(recovery_target_tile),
                f"recovery_target={recovery_target_tile}",
            )

        return (
            self._build_loot_source_return_candidate_tiles(source_tile),
            f"fallback_source={source_tile}",
        )

    def _restore_residual_loot_record_after_recovery(self, blackboard: AgentBlackboard, game_state) -> None:
        residual_record = blackboard.collect_loot_residual_record
        if residual_record is None:
            return
        if not self._residual_record_matches_current_source(blackboard, game_state):
            return

        self._observed_loot_item_keys.update(residual_record.observed_item_keys)
        latest_tiles = self._get_residual_loot_tiles_from_latest_state(blackboard, game_state, residual_record)
        if latest_tiles:
            blackboard.pending_loot_tiles = latest_tiles
            self._target_tile = None
            self._target_started_at = None
            self._last_cluster_log_signature = None
            self._log(
                f"背包恢复后按残留记录重定位掉落物: source={residual_record.source_tile}, "
                f"recorded={self._format_tiles(residual_record.remaining_tiles)}, "
                f"latest={self._format_tiles(latest_tiles)}, item_keys={sorted(residual_record.observed_item_keys)}"
            )
        else:
            self._log(
                f"背包恢复后回到 source 附近，但未再看到记录中的残留掉落物: "
                f"source={residual_record.source_tile}, recorded={self._format_tiles(residual_record.remaining_tiles)}, "
                f"item_keys={sorted(residual_record.observed_item_keys)}"
            )
        blackboard.collect_loot_residual_record = None

    def _get_residual_loot_tiles_from_latest_state(
        self,
        blackboard: AgentBlackboard,
        game_state,
        residual_record: ResidualLootRecord,
    ) -> list[Tile]:
        source_tile = residual_record.source_tile
        if source_tile is None:
            return []

        radius = self._get_dynamic_collect_radius(blackboard)
        loot_tiles: list[Tile] = []
        seen_tiles: set[Tile] = set()
        for debris in getattr(game_state, "debris", []):
            if not self._is_collectible_debris_for_source(blackboard, debris):
                continue
            item_key = self._build_debris_item_key(debris)
            if residual_record.observed_item_keys and item_key not in residual_record.observed_item_keys:
                continue
            if self._is_unreceivable_loot_skipped(blackboard, game_state, debris):
                continue
            if not self._is_tile_in_dynamic_loot_radius(source_tile, debris.tile, radius):
                continue
            if debris.tile in seen_tiles:
                continue
            seen_tiles.add(debris.tile)
            loot_tiles.append(debris.tile)

        return sorted(loot_tiles, key=lambda tile: self._tile_distance(game_state.player_tile, tile))

    def _residual_record_matches_current_source(self, blackboard: AgentBlackboard, game_state) -> bool:
        residual_record = blackboard.collect_loot_residual_record
        if residual_record is None:
            return False
        if residual_record.owner != blackboard.collect_loot_owner:
            return False
        if residual_record.source_type != blackboard.collect_loot_source_type:
            return False
        if residual_record.source_tile != blackboard.collect_loot_source_tile:
            return False
        location_name = str(getattr(game_state, "location_name", "") or "")
        return residual_record.location_name == location_name

    def _get_inventory_recovery_context_target_tile(self, blackboard: AgentBlackboard) -> Tile | None:
        raw_target_tile = blackboard.inventory_recovery_context.get("target_tile")
        if (
            isinstance(raw_target_tile, tuple)
            and len(raw_target_tile) == 2
            and isinstance(raw_target_tile[0], int)
            and isinstance(raw_target_tile[1], int)
        ):
            return Tile(raw_target_tile[0], raw_target_tile[1])
        return None

    def _build_loot_source_return_candidate_tiles(self, source_tile: Tile) -> set[Tile]:
        candidate_tiles: set[Tile] = set()
        for dx in range(-LOOT_SOURCE_RETURN_RADIUS_TILES, LOOT_SOURCE_RETURN_RADIUS_TILES + 1):
            for dy in range(-LOOT_SOURCE_RETURN_RADIUS_TILES, LOOT_SOURCE_RETURN_RADIUS_TILES + 1):
                candidate_tiles.add(Tile(source_tile.x + dx, source_tile.y + dy))
        return candidate_tiles

    def _remove_absent_loot_tiles(self, blackboard: AgentBlackboard, game_state) -> None:
        existing_tiles = set(self._get_existing_requested_loot_tiles(blackboard, game_state))
        if len(existing_tiles) == len(blackboard.pending_loot_tiles):
            return

        relocated_tiles = self._find_relocated_loot_tiles(blackboard, game_state, existing_tiles)
        removed_tiles = [
            tile
            for tile in blackboard.pending_loot_tiles
            if tile not in existing_tiles and tile not in relocated_tiles
        ]
        blackboard.pending_loot_tiles = [
            *[tile for tile in blackboard.pending_loot_tiles if tile in existing_tiles],
            *relocated_tiles,
        ]
        if relocated_tiles:
            self._log(
                f"按物品身份重定位弹跳/磁吸中的掉落物: "
                f"observed_item_keys={sorted(self._observed_loot_item_keys)}, relocated={self._format_tiles(relocated_tiles)}, "
                f"source={blackboard.collect_loot_source_tile}, player={game_state.player_tile}"
            )
        if removed_tiles:
            self._log(f"移除已消失掉落物: removed={self._format_tiles(removed_tiles)}")

    def _find_relocated_loot_tiles(
        self,
        blackboard: AgentBlackboard,
        game_state,
        existing_tiles: set[Tile],
    ) -> list[Tile]:
        if not self._observed_loot_item_keys:
            return []

        pending_tile_count = len(blackboard.pending_loot_tiles)
        if len(existing_tiles) >= pending_tile_count:
            return []

        missing_count = pending_tile_count - len(existing_tiles)
        skipped_tiles = blackboard.skipped_loot_tiles
        source_tile = blackboard.collect_loot_source_tile
        candidate_debris = []
        seen_tiles: set[Tile] = set()
        for debris in getattr(game_state, "debris", []):
            if not self._is_collectible_debris_for_source(blackboard, debris):
                continue
            if self._is_unreceivable_loot_skipped(blackboard, game_state, debris):
                continue
            if debris.tile in existing_tiles or debris.tile in seen_tiles:
                continue
            if (debris.tile.x, debris.tile.y) in skipped_tiles:
                continue
            item_key = self._build_debris_item_key(debris)
            if item_key not in self._observed_loot_item_keys:
                continue
            if not self._is_relocated_loot_candidate_in_range(source_tile, game_state.player_tile, debris.tile):
                continue

            seen_tiles.add(debris.tile)
            source_distance = (
                self._tile_chebyshev_distance(source_tile, debris.tile) if source_tile is not None else 0
            )
            player_distance = self._tile_distance(game_state.player_tile, debris.tile)
            candidate_debris.append((source_distance, player_distance, debris.tile, item_key))

        candidate_debris.sort(key=lambda item: (item[0], item[1], item[2].x, item[2].y))
        relocated_tiles = [tile for _, _, tile, _ in candidate_debris[:missing_count]]
        return relocated_tiles

    def _is_relocated_loot_candidate_in_range(
        self,
        source_tile: Tile | None,
        player_tile: Tile,
        debris_tile: Tile,
    ) -> bool:
        if source_tile is not None and self._tile_chebyshev_distance(source_tile, debris_tile) <= LOOT_RELOCATE_SEARCH_RADIUS_TILES:
            return True
        return self._tile_chebyshev_distance(player_tile, debris_tile) <= LOOT_RELOCATE_SEARCH_RADIUS_TILES

    def _get_existing_requested_loot_tiles(self, blackboard: AgentBlackboard, game_state) -> list[Tile]:
        if self._is_dynamic_local_collect_mode(blackboard):
            return self._get_dynamic_loot_tiles_near_source(blackboard, game_state)

        requested_tiles = set(blackboard.pending_loot_tiles)
        existing_tiles: list[Tile] = []
        seen_tiles: set[Tile] = set()
        for debris in getattr(game_state, "debris", []):
            if not self._is_collectible_debris_for_source(blackboard, debris):
                continue
            if self._is_unreceivable_loot_skipped(blackboard, game_state, debris):
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
            if not self._is_collectible_debris_for_source(blackboard, debris):
                continue
            if self._is_unreceivable_loot_skipped(blackboard, game_state, debris):
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
        acceptable_tiles = [
            tile for tile in existing_tiles if self._can_accept_loot_tile(blackboard, game_state, tile)
        ]
        if self._target_tile in acceptable_tiles:
            return self._target_tile

        if not existing_tiles:
            return None

        if acceptable_tiles:
            selected_tile = min(acceptable_tiles, key=lambda tile: self._tile_distance(game_state.player_tile, tile))
            nearest_tile = min(existing_tiles, key=lambda tile: self._tile_distance(game_state.player_tile, tile))
            if selected_tile != nearest_tile and self._target_tile != selected_tile:
                self._log(
                    f"优先选择当前背包可接收的掉落物，延后不可接收目标: "
                    f"selected={selected_tile}, nearest={nearest_tile}, "
                    f"pending={self._format_tiles(existing_tiles)}"
                )
            return selected_tile

        if self._target_tile in existing_tiles:
            return self._target_tile

        return min(existing_tiles, key=lambda tile: self._tile_distance(game_state.player_tile, tile))

    def _can_accept_loot_tile(self, blackboard: AgentBlackboard, game_state, target_tile: Tile) -> bool:
        if self.inventory_policy is None:
            return True

        debris = self._get_debris_for_tile(blackboard, game_state, target_tile)
        if debris is None:
            return True

        return self.inventory_policy.can_accept_debris(game_state, debris).can_accept

    def _select_receivable_alternative_target(
        self,
        blackboard: AgentBlackboard,
        game_state,
        blocked_target_tile: Tile,
    ) -> Tile | None:
        receivable_tiles = [
            tile
            for tile in self._get_existing_requested_loot_tiles(blackboard, game_state)
            if tile != blocked_target_tile and self._can_accept_loot_tile(blackboard, game_state, tile)
        ]
        if not receivable_tiles:
            return None

        return min(receivable_tiles, key=lambda tile: self._tile_distance(game_state.player_tile, tile))

    def _build_collect_candidate_tiles(self, blackboard: AgentBlackboard, game_state, target_tile: Tile) -> set[Tile]:
        target_debris = self._get_debris_for_tile(blackboard, game_state, target_tile)
        if target_debris is None:
            return {target_tile}

        cluster_tiles = self._build_loot_cluster(blackboard, game_state, target_tile)
        cluster_debris = [
            cluster_item
            for tile in cluster_tiles
            if (cluster_item := self._get_debris_for_tile(blackboard, game_state, tile))
        ]
        stand_radius = LOOT_COLLECT_STAND_SEARCH_RADIUS_TILES
        min_x = min(tile.x for tile in cluster_tiles) - stand_radius
        max_x = max(tile.x for tile in cluster_tiles) + stand_radius
        min_y = min(tile.y for tile in cluster_tiles) - stand_radius
        max_y = max(tile.y for tile in cluster_tiles) + stand_radius
        target_collect_radius = self._get_target_collect_radius(game_state, target_tile)
        effective_magnetic_radius = self._get_effective_magnetic_radius(game_state)
        candidate_scores: list[tuple[int, int, int, int, int, Tile]] = []

        for x in range(min_x, max_x + 1):
            for y in range(min_y, max_y + 1):
                candidate_tile = Tile(x, y)
                if not self._can_tile_cover_debris_with_magnetic_range(
                    game_state,
                    candidate_tile,
                    target_debris.position,
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
                cluster_distance = sum(
                    self._tile_distance(candidate_tile, cluster_item.tile) for cluster_item in cluster_debris
                )
                player_distance = self._tile_distance(game_state.player_tile, candidate_tile)
                candidate_scores.append(
                    (-cover_count, player_distance, cluster_distance, candidate_tile.x, candidate_tile.y, candidate_tile)
                )

        if not candidate_scores:
            return {target_tile}

        max_stand_candidates = self._get_max_stand_candidates_for_collect_mode(blackboard)
        candidate_scores.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4]))
        selected_scores = candidate_scores[:max_stand_candidates]
        best_cover_count = -min(score[0] for score in selected_scores)
        best_candidates = [candidate_tile for _, _, _, _, _, candidate_tile in selected_scores]
        candidate_tiles = set(best_candidates)
        self._log_cluster_candidate_once(target_tile, cluster_tiles, candidate_tiles, best_cover_count, game_state)
        return candidate_tiles or {target_tile}

    def _move_to_loose_tree_loot_stand(
        self,
        blackboard: AgentBlackboard,
        context: PlayerContext,
        game_state,
        target_tile: Tile,
    ) -> bool:
        if not self._is_tree_collect_mode(blackboard):
            return False

        candidate_tiles = self._build_loose_tree_loot_candidate_tiles(blackboard, game_state, target_tile)
        if not candidate_tiles:
            return False

        positioning_result = self.positioning_controller.tick(
            game_state,
            PositioningGoal(
                candidate_stand_tiles=candidate_tiles,
                tool_target_tile=None,
                extra_blocked_tiles=self._build_collect_extra_blocked_tiles(game_state),
            ),
        )
        if positioning_result.status == "FAILED":
            self._log(
                f"树木掉落物宽松靠近站位也不可达，准备按原逻辑跳过: "
                f"target={target_tile}, player={game_state.player_tile}, reason={positioning_result.reason}, "
                f"candidates={self._format_tiles(candidate_tiles)}"
            )
            self.positioning_controller.reset()
            return False

        if positioning_result.command is None:
            self._log(
                f"已到达树木掉落物宽松靠近站位，但仍未进入磁吸范围，交回精确拾取失败流程: "
                f"target={target_tile}, stand_tile={positioning_result.stand_tile}, player={game_state.player_tile}"
            )
            self.positioning_controller.reset()
            return False

        response = context.executor_client.send_command(positioning_result.command)
        self._log(
            f"精确磁吸站位不可达，先靠近树木掉落物聚类: "
            f"target={target_tile}, command={positioning_result.command.action}, response={response}, "
            f"stand_tile={positioning_result.stand_tile}, player={game_state.player_tile}, "
            f"positioning={self.positioning_controller.get_debug_snapshot()}"
        )
        if response == "BUSY":
            self.positioning_controller.reset()
        return True

    def _build_loose_tree_loot_candidate_tiles(
        self,
        blackboard: AgentBlackboard,
        game_state,
        target_tile: Tile,
        include_source_tile: bool = True,
    ) -> set[Tile]:
        cluster_tiles = self._build_loot_cluster(blackboard, game_state, target_tile)
        source_tile = blackboard.collect_loot_source_tile
        if include_source_tile and source_tile is not None:
            cluster_tiles = [*cluster_tiles, source_tile]

        min_x = min(tile.x for tile in cluster_tiles) - LOOT_SOURCE_RETURN_RADIUS_TILES
        max_x = max(tile.x for tile in cluster_tiles) + LOOT_SOURCE_RETURN_RADIUS_TILES
        min_y = min(tile.y for tile in cluster_tiles) - LOOT_SOURCE_RETURN_RADIUS_TILES
        max_y = max(tile.y for tile in cluster_tiles) + LOOT_SOURCE_RETURN_RADIUS_TILES
        cluster_center_x = sum(tile.x for tile in cluster_tiles) / len(cluster_tiles)
        cluster_center_y = sum(tile.y for tile in cluster_tiles) / len(cluster_tiles)
        scored_tiles: list[tuple[float, float, int, int, int, Tile]] = []

        for x in range(min_x, max_x + 1):
            for y in range(min_y, max_y + 1):
                candidate_tile = Tile(x, y)
                distance_to_cluster = min(self._tile_distance(candidate_tile, tile) for tile in cluster_tiles)
                if distance_to_cluster > LOOT_SOURCE_RETURN_RADIUS_TILES:
                    continue

                center_distance = abs(candidate_tile.x - cluster_center_x) + abs(candidate_tile.y - cluster_center_y)
                player_distance = self._tile_distance(game_state.player_tile, candidate_tile)
                scored_tiles.append(
                    (
                        distance_to_cluster,
                        center_distance,
                        player_distance,
                        candidate_tile.x,
                        candidate_tile.y,
                        candidate_tile,
                    )
                )

        scored_tiles.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4]))
        return {tile for *_scores, tile in scored_tiles[:TREE_LOOT_CLUSTER_MAX_STAND_CANDIDATES]}

    def _build_collect_extra_blocked_tiles(self, game_state) -> set[Tile]:
        """
        拾取路径只负责“靠近并磁吸”，不应该把玩家推到可清障碍物上。

        Route/Farm 可以在业务阶段显式请求清障；CollectLoot 这里只把当前
        state 已知的轻量清障碍物作为额外阻塞交给 PositioningController，
        让站位路径优先绕开它们。若真的无路可走，再走现有的失败/轻量清障流程。
        """
        extra_blocked_tiles: set[Tile] = set()
        for layer_name, layer_tiles in game_state.layers.items():
            normalized_layer_name = normalize_obstacle_type(layer_name)
            if normalized_layer_name not in LOOT_CLEARABLE_OBSTACLE_TYPES:
                continue
            extra_blocked_tiles.update(layer_tiles)

        extra_blocked_tiles.discard(game_state.player_tile)
        return extra_blocked_tiles

    def _get_max_stand_candidates_for_collect_mode(self, blackboard: AgentBlackboard) -> int:
        if self._is_tree_collect_mode(blackboard):
            return TREE_LOOT_CLUSTER_MAX_STAND_CANDIDATES
        return LOOT_CLUSTER_MAX_STAND_CANDIDATES

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

    def _is_loot_collected_for_source(
        self,
        blackboard: AgentBlackboard,
        game_state,
        target_tile: Tile,
    ) -> bool:
        for debris in getattr(game_state, "debris", []):
            if not self._is_collectible_debris_for_source(blackboard, debris):
                continue
            if debris.tile == target_tile:
                return False
        return True

    def _is_target_in_magnetic_range(
        self,
        blackboard: AgentBlackboard,
        game_state,
        target_tile: Tile,
        should_log: bool = True,
    ) -> bool:
        debris = self._get_debris_for_tile(blackboard, game_state, target_tile)
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

    def _get_debris_for_tile(self, blackboard: AgentBlackboard, game_state, target_tile: Tile):
        for debris in getattr(game_state, "debris", []):
            if not self._is_collectible_debris_for_source(blackboard, debris):
                continue
            if self._is_unreceivable_loot_skipped(blackboard, game_state, debris):
                continue
            if debris.tile == target_tile:
                return debris
        return None

    def _get_debris_for_tile_without_skip(self, blackboard: AgentBlackboard, game_state, target_tile: Tile):
        for debris in getattr(game_state, "debris", []):
            if not self._is_collectible_debris_for_source(blackboard, debris):
                continue
            if debris.tile == target_tile:
                return debris
        return None

    def _is_magnetic_pickup_stalled(self, game_state, debris) -> bool:
        player_position = (float(game_state.position.x), float(game_state.position.y))
        debris_position = (float(debris.position.x), float(debris.position.y))

        if self._last_magnetic_player_position is None or self._last_magnetic_debris_position is None:
            self._last_magnetic_player_position = player_position
            self._last_magnetic_debris_position = debris_position
            self._magnetic_stall_started_at = None
            return False

        player_delta = self._position_delta(player_position, self._last_magnetic_player_position)
        debris_delta = self._position_delta(debris_position, self._last_magnetic_debris_position)
        self._last_magnetic_player_position = player_position
        self._last_magnetic_debris_position = debris_position

        if player_delta > LOOT_POSITION_PROGRESS_EPSILON or debris_delta > LOOT_POSITION_PROGRESS_EPSILON:
            self._magnetic_stall_started_at = None
            return False

        now = time.time()
        if self._magnetic_stall_started_at is None:
            self._magnetic_stall_started_at = now
            return False

        return now - self._magnetic_stall_started_at >= LOOT_MAGNETIC_STALL_SECONDS

    def _position_delta(self, current_position: tuple[float, float], previous_position: tuple[float, float]) -> float:
        return max(
            abs(current_position[0] - previous_position[0]),
            abs(current_position[1] - previous_position[1]),
        )

    def _is_collectible_debris(self, debris) -> bool:
        qualified_item_id = str(getattr(debris, "qualified_item_id", "") or "").strip()
        return bool(
            qualified_item_id
            and getattr(debris, "name", "")
            and getattr(debris, "display_name", "")
            and qualified_item_id not in IGNORED_DEBRIS_QUALIFIED_ITEM_IDS
        )

    def _is_collectible_debris_for_source(self, blackboard: AgentBlackboard, debris) -> bool:
        return self._is_collectible_debris(debris)

    def _mark_target_covered(self, target_tile: Tile, game_state, reason: str) -> None:
        self._swept_loot_tiles.add((target_tile.x, target_tile.y))
        self._log(
            f"拾取路径已覆盖掉落物但尚未确认消失: target={target_tile}, reason={reason}, "
            f"player_tile={game_state.player_tile}, player_position={game_state.position}, "
            f"swept={sorted(self._swept_loot_tiles)}"
        )

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
        magnetic_radius = (
            self._get_effective_magnetic_radius(game_state) if magnetic_radius is None else magnetic_radius
        )
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

    def _build_move_command_to_magnetic_range(
        self,
        blackboard: AgentBlackboard,
        game_state,
        target_tile: Tile,
    ) -> StardewCommand:
        debris = self._get_debris_for_tile(blackboard, game_state, target_tile)
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

    def _build_move_command_to_debris_center(
        self,
        blackboard: AgentBlackboard,
        game_state,
        target_tile: Tile,
    ) -> StardewCommand:
        debris = self._get_debris_for_tile(blackboard, game_state, target_tile)
        if debris is None:
            return StardewCommand(action=StardewAction.IDLE)

        tile_size = game_state.tile_size or 64
        close_radius = max(8.0, tile_size * 0.15)
        pressed_keys: set[str] = set()
        distance_x = game_state.position.x - debris.position.x
        distance_y = game_state.position.y - debris.position.y

        if distance_x < -close_radius:
            pressed_keys.add("d")
        elif distance_x > close_radius:
            pressed_keys.add("a")

        if distance_y < -close_radius:
            pressed_keys.add("s")
        elif distance_y > close_radius:
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
        allow_when_in_magnetic_range: bool = False,
    ) -> bool:
        owner = blackboard.collect_loot_owner
        if owner not in ("Route", "Farm"):
            return False
        if (
            not allow_when_in_magnetic_range
            and self._is_target_in_magnetic_range(blackboard, game_state, target_tile, should_log=False)
        ):
            self._mark_target_covered(target_tile, game_state, "已在磁吸范围内，不为拾取触发清障")
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

    def _is_stone_collect_mode(self, blackboard: AgentBlackboard) -> bool:
        normalized_source_type = normalize_obstacle_type(blackboard.collect_loot_source_type or "")
        return normalized_source_type in {"stone", "miningnode", "mining_node", "breakable_container"}

    def _is_dynamic_local_collect_mode(self, blackboard: AgentBlackboard) -> bool:
        return (
            self._is_tree_collect_mode(blackboard)
            or self._is_area_clear_collect_mode(blackboard)
            or self._is_stone_collect_mode(blackboard)
        )

    def _get_dynamic_collect_radius(self, blackboard: AgentBlackboard) -> int:
        if self._is_tree_collect_mode(blackboard):
            return TREE_LOOT_COLLECT_RADIUS_TILES
        if self._is_stone_collect_mode(blackboard):
            return STONE_LOOT_COLLECT_RADIUS_TILES
        return WEEDS_LOOT_COLLECT_RADIUS_TILES

    def _observe_visible_loot_item_keys(self, blackboard: AgentBlackboard, game_state) -> None:
        for debris in self._get_observed_debris_for_current_source(blackboard, game_state):
            item_key = self._build_debris_item_key(debris)
            if item_key is None:
                continue
            self._observed_loot_item_keys.add(item_key)

    def _get_observed_debris_for_current_source(self, blackboard: AgentBlackboard, game_state) -> list:
        skipped_tiles = blackboard.skipped_loot_tiles
        if self._is_dynamic_local_collect_mode(blackboard):
            source_tile = blackboard.collect_loot_source_tile
            if source_tile is None:
                return []
            radius = self._get_dynamic_collect_radius(blackboard)
            return [
                debris
                for debris in getattr(game_state, "debris", [])
                if self._is_collectible_debris_for_source(blackboard, debris)
                and not self._is_unreceivable_loot_skipped(blackboard, game_state, debris)
                and (debris.tile.x, debris.tile.y) not in skipped_tiles
                and self._is_tile_in_dynamic_loot_radius(source_tile, debris.tile, radius)
            ]

        requested_tiles = set(blackboard.pending_loot_tiles)
        return [
            debris
            for debris in getattr(game_state, "debris", [])
            if self._is_collectible_debris_for_source(blackboard, debris)
            and not self._is_unreceivable_loot_skipped(blackboard, game_state, debris)
            and debris.tile in requested_tiles
            and (debris.tile.x, debris.tile.y) not in skipped_tiles
        ]

    def _build_debris_item_key(self, debris) -> str | None:
        qualified_item_id = str(getattr(debris, "qualified_item_id", "") or "").strip()
        if qualified_item_id:
            return f"qid:{qualified_item_id}"
        return None

    def _discard_observed_loot_item_key(self, debris, reason: str) -> None:
        item_key = self._build_debris_item_key(debris)
        if item_key is None or item_key not in self._observed_loot_item_keys:
            return

        self._observed_loot_item_keys.discard(item_key)
        self._log(
            f"撤销掉落物背包增量等待: item_key={item_key}, reason={reason}, "
            f"remaining_observed_item_keys={sorted(self._observed_loot_item_keys)}"
        )

    def _snapshot_inventory_items(self, game_state) -> dict[str, int]:
        counts: dict[str, int] = {}
        inventory = getattr(game_state, "inventory", None)
        for item in getattr(inventory, "items", []):
            stack = int(getattr(item, "stack", 0) or 0)
            qualified_item_id = str(getattr(item, "qualified_item_id", "") or "").strip()
            if qualified_item_id:
                key = f"qid:{qualified_item_id}"
                counts[key] = counts.get(key, 0) + stack

            name = str(getattr(item, "name", "") or "").strip()
            if name:
                key = f"name:{name}"
                counts[key] = counts.get(key, 0) + stack
        return counts

    def _build_inventory_signature(self, game_state) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(self._snapshot_inventory_items(game_state).items()))

    def _has_observed_inventory_gain(self, game_state) -> bool:
        current_inventory = self._snapshot_inventory_items(game_state)
        for item_key in self._observed_loot_item_keys:
            if current_inventory.get(item_key, 0) > self._inventory_snapshot.get(item_key, 0):
                return True
        return False

    def _get_inventory_gain_items(self, game_state) -> list[str]:
        current_inventory = self._snapshot_inventory_items(game_state)
        gain_items: list[str] = []
        for item_key, current_count in sorted(current_inventory.items()):
            old_count = self._inventory_snapshot.get(item_key, 0)
            if current_count > old_count:
                gain_items.append(f"{item_key}:{old_count}->{current_count}")
        return gain_items

    def _is_player_busy(self, game_state) -> bool:
        return bool(getattr(game_state, "using_tool", False)) or not bool(getattr(game_state, "can_move", True))

    def _tile_distance(self, start_tile: Tile, end_tile: Tile) -> int:
        return abs(start_tile.x - end_tile.x) + abs(start_tile.y - end_tile.y)

    def _tile_chebyshev_distance(self, start_tile: Tile, end_tile: Tile) -> int:
        return max(abs(start_tile.x - end_tile.x), abs(start_tile.y - end_tile.y))

    def _is_player_near_any_tile(self, player_tile: Tile, candidate_tiles: set[Tile], max_distance: int) -> bool:
        return any(self._tile_chebyshev_distance(player_tile, candidate_tile) <= max_distance for candidate_tile in candidate_tiles)

    def _nearest_tile_distance(self, player_tile: Tile, candidate_tiles: set[Tile]) -> int | None:
        if not candidate_tiles:
            return None
        return min(self._tile_chebyshev_distance(player_tile, candidate_tile) for candidate_tile in candidate_tiles)

    def _format_tiles(self, tiles: list[Tile]) -> str:
        return str([(tile.x, tile.y) for tile in tiles])

    def _format_tile(self, tile: Tile | None) -> tuple[int, int] | None:
        if tile is None:
            return None
        return (tile.x, tile.y)

    def _normalize_source_type(self, source_type: str | None) -> str | None:
        if source_type is None:
            return None
        return normalize_obstacle_type(source_type) or source_type

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
