import json
import time
from dataclasses import dataclass
from typing import Literal

from agent.action.location.location import Location
from agent.action.valley_action.action_type import ChestItemPayload, StardewAction, StardewCommand
from agent.action.valley_action.positioning_controller import PositioningController, PositioningGoal, PositioningResult
from agent.action.valley_action.tool_targeting import build_tool_target_face_command, is_tool_targeting
from agent.base_task import BaseTask, TaskType
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.chest_debug_logger import ChestDebugLogger
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.tool_selection import count_inventory_items
from server.valley_server import StardewState
from server.type import Tile


type ChestAction = Literal[
    "TAKE",  # 从指定箱子取出指定物品；Chest P0。
    "PUT",  # 向指定箱子存入指定物品；Chest P1。
]


CHEST_ACTION_TIMEOUT_SECONDS = 8.0
CHEST_VERIFY_TIMEOUT_SECONDS = 2.0
CHEST_MENU_WAIT_SECONDS = 0.5
CHEST_STAND_TILE_MARGIN_PX = 1.0
CHEST_INTERACTION_EDGE_MARGIN_PX = 1.0
CHEST_INTERACTION_POSITION_TOLERANCE_PX = 2.0


@dataclass(frozen=True)
class ChestItemRequest:
    item_name: str
    count: int
    qualified_item_id: str | None = None

    @property
    def key(self) -> tuple[str, str | None]:
        return self.item_name, self.qualified_item_id


@dataclass(frozen=True)
class ChestItemActionResult:
    status: str
    item_name: str
    qualified_item_id: str | None
    requested_count: int
    transferred_count: int
    inventory_count: int
    reason: str


@dataclass(frozen=True)
class ChestBatchActionResult:
    status: str
    reason: str
    results: list[ChestItemActionResult]


class ChestTask(BaseTask):
    def __init__(
        self,
        task_type: TaskType,
        desc: str,
        chest_action: ChestAction,
        target_loc: Location,
        chest_tile: Tile,
        item_name: str | None = None,
        count: int = 0,
        qualified_item_id: str | None = None,
        items: list[ChestItemRequest] | None = None,
    ):
        super().__init__(task_type=task_type, desc=desc)
        self.chest_action = chest_action
        self.target_loc = target_loc
        self.chest_tile = chest_tile
        self.items = items or self._build_single_item_request(item_name, count, qualified_item_id)
        self.item_name = self.items[0].item_name if self.items else item_name or ""
        self.count = self.items[0].count if len(self.items) == 1 else sum(item.count for item in self.items)
        self.qualified_item_id = self.items[0].qualified_item_id if len(self.items) == 1 else qualified_item_id

    def _build_single_item_request(
        self,
        item_name: str | None,
        count: int,
        qualified_item_id: str | None,
    ) -> list[ChestItemRequest]:
        if item_name is None:
            return []
        return [ChestItemRequest(item_name=item_name, count=count, qualified_item_id=qualified_item_id)]


class ChestNode(BTNode):
    """
    Chest P0/P1：指定箱子取物或存物。

    当前不模拟鼠标 UI。节点先复用 PositioningController 走到箱子上下左右相邻格并面向箱子，
    然后发送 SMAPI 结构化动作 TAKE_ITEMS_FROM_CHEST / PUT_ITEMS_TO_CHEST，
    并通过下一帧 inventory state 验证背包数量变化。
    """

    def __init__(self) -> None:
        self.positioning_controller = PositioningController()
        self._task_signature: tuple | None = None
        self._started_at: float | None = None
        self._before_counts: dict[tuple[str, str | None], int] = {}
        self._expected_after_counts: dict[tuple[str, str | None], int] = {}
        self._verify_started_at: float | None = None
        self._resolved_chest_tile: Tile | None = None
        self._opened_chest_at: float | None = None
        self._has_opened_chest = False
        self._has_closed_chest = False
        self._has_sent_transfer_command = False
        self._locked_stand_tile: Tile | None = None
        self._last_debug_heartbeat_at = 0.0
        self.chest_debug_logger = ChestDebugLogger()

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        if not blackboard.macro_plan or blackboard.current_step_index >= len(blackboard.macro_plan):
            self._reset()
            return "FAILURE"

        current_task = blackboard.macro_plan[blackboard.current_step_index]
        if not isinstance(current_task, ChestTask):
            self._reset()
            return "FAILURE"

        if current_task.task_type != "CHEST":
            self._reset()
            return "FAILURE"

        game_state = context.state
        if game_state is None:
            return "RUNNING"

        if self._is_new_task(blackboard, current_task):
            self._start(blackboard, game_state, current_task)

        self._log_debug_heartbeat(blackboard, game_state, current_task)

        if current_task.chest_action not in ("TAKE", "PUT"):
            self._fail(context, blackboard, current_task, f"Chest 暂不支持动作: {current_task.chest_action}")
            return "SUCCESS"

        invalid_items = [item for item in current_task.items if item.count <= 0 or not item.item_name]
        if not current_task.items or invalid_items:
            self._fail(context, blackboard, current_task, f"取物清单为空或存在非法数量: items={current_task.items}")
            return "SUCCESS"

        if game_state.location_name != current_task.target_loc:
            self._fail(
                context,
                blackboard,
                current_task,
                f"当前场景不是箱子任务目标场景: current={game_state.location_name}, target={current_task.target_loc}",
            )
            return "SUCCESS"

        resolved_chest_tile = self._resolve_chest_tile(context, blackboard, current_task)
        if resolved_chest_tile is None:
            return "SUCCESS" if not blackboard.macro_plan else "RUNNING"

        if self._started_at is not None and time.time() - self._started_at > CHEST_ACTION_TIMEOUT_SECONDS:
            self._fail(context, blackboard, current_task, f"Chest {current_task.chest_action} 动作超时")
            return "SUCCESS"

        if self._is_waiting_for_inventory_verification(game_state, context, blackboard, current_task):
            return "RUNNING"

        if current_task.chest_action == "TAKE" and self._has_enough_items_in_inventory(game_state, blackboard, current_task):
            return "SUCCESS"

        if current_task.chest_action == "PUT" and not self._has_any_put_item_in_inventory(game_state, current_task):
            self._fail(context, blackboard, current_task, "背包中没有任何可存入箱子的目标物品")
            return "SUCCESS"

        if self._locked_stand_tile is None:
            positioning_result = self._tick_chest_positioning(game_state, context, resolved_chest_tile)
            if positioning_result.status == "FAILED":
                self._fail(
                    context,
                    blackboard,
                    current_task,
                    f"无法移动并面向箱子: chest_tile={resolved_chest_tile}, reason={positioning_result.reason}",
                )
                return "SUCCESS"

            if positioning_result.status == "MOVING":
                return "RUNNING"

            if positioning_result.stand_tile is not None:
                self._locked_stand_tile = positioning_result.stand_tile

            if positioning_result.status == "FACING":
                return "RUNNING"

        stand_tile = self._locked_stand_tile or game_state.player_tile
        if not self._is_player_at_chest_interaction_position(game_state, stand_tile, resolved_chest_tile):
            command = self._build_move_to_chest_interaction_position_command(
                game_state,
                stand_tile,
                resolved_chest_tile,
            )
            response = context.executor_client.send_command(command)
            target_position = self._get_chest_interaction_position(game_state, stand_tile, resolved_chest_tile)
            self._log(
                f"靠近箱子交互边缘: chest={resolved_chest_tile}, stand_tile={stand_tile}, "
                f"target_position=({target_position[0]:.1f}, {target_position[1]:.1f}), "
                f"player_position={game_state.position}, command={command.action}, response={response}"
            )
            return "RUNNING"

        if not is_tool_targeting(game_state, resolved_chest_tile):
            command = build_tool_target_face_command(game_state.player_tile, resolved_chest_tile)
            response = context.executor_client.send_command(command)
            self._log(
                f"打开箱子前最终面向校验: chest={resolved_chest_tile}, stand_tile={stand_tile}, "
                f"player_tile={game_state.player_tile}, tool_target={game_state.tool_target.tile}, "
                f"command={command.action}, response={response}"
            )
            return "RUNNING"

        if not self._has_opened_chest:
            self._open_chest(context, game_state, current_task, resolved_chest_tile, stand_tile)
            return "RUNNING"

        if self._opened_chest_at is not None and time.time() - self._opened_chest_at < CHEST_MENU_WAIT_SECONDS:
            self._log(
                f"等待箱子界面稳定: elapsed={time.time() - self._opened_chest_at:.2f}s, "
                f"required={CHEST_MENU_WAIT_SECONDS:.2f}s"
            )
            return "RUNNING"

        self._transfer_chest_items(context, blackboard, game_state, current_task, resolved_chest_tile)
        return "RUNNING"

    def _start(self, blackboard: AgentBlackboard, game_state: StardewState, current_task: ChestTask) -> None:
        self._task_signature = self._build_task_signature(blackboard, current_task)
        self._started_at = time.time()
        self._before_counts = self._count_requested_items(game_state, current_task)
        self._expected_after_counts = {}
        self._verify_started_at = None
        self._resolved_chest_tile = None
        self._opened_chest_at = None
        self._has_opened_chest = False
        self._has_closed_chest = False
        self._has_sent_transfer_command = False
        self._locked_stand_tile = None
        self._last_debug_heartbeat_at = 0.0
        self.positioning_controller.reset()
        print(
            f"\n📦 [ChestNode] 收到箱子任务: action={current_task.chest_action}, "
            f"items={self._format_item_requests(current_task.items)}, chest={current_task.chest_tile}"
        )
        self._log(
            f"开始 ChestTask: action={current_task.chest_action}, items={self._format_item_requests(current_task.items)}, "
            f"target_loc={current_task.target_loc}, chest_tile={current_task.chest_tile}, "
            f"before_counts={self._before_counts}, player_tile={game_state.player_tile}"
        )

    def _has_enough_items_in_inventory(
        self,
        game_state: StardewState,
        blackboard: AgentBlackboard,
        current_task: ChestTask,
    ) -> bool:
        if self._has_opened_chest and not self._has_closed_chest:
            return False

        missing_items: list[str] = []
        for item_request in current_task.items:
            current_count = self._count_inventory_item(game_state, item_request)
            if current_count < item_request.count:
                missing_items.append(f"{item_request.item_name}:{current_count}/{item_request.count}")

        if missing_items:
            return False

        print(
            f"\n🟢 [ChestNode] 背包已有足够物品，跳过开箱取物: "
            f"items={self._format_item_requests(current_task.items)}"
        )
        self._log(
            f"背包已有足够物品，ChestTask 直接完成: "
            f"items={self._format_item_requests(current_task.items)}, chest={current_task.chest_tile}"
        )
        blackboard.current_step_index += 1
        self._reset()
        return True

    def _tick_chest_positioning(
        self,
        game_state: StardewState,
        context: PlayerContext,
        chest_tile: Tile,
    ) -> PositioningResult:
        result = self.positioning_controller.tick(
            game_state,
            PositioningGoal(
                candidate_stand_tiles=self._get_cardinal_neighbor_tiles(chest_tile),
                tool_target_tile=chest_tile,
                extra_blocked_tiles={chest_tile},
            ),
        )
        if result.command is not None:
            response = context.executor_client.send_command(result.command)
            self._log(
                f"发送箱子站位命令: chest={chest_tile}, status={result.status}, "
                f"command={result.command.action}, response={response}, stand_tile={result.stand_tile}, "
                f"positioning={self.positioning_controller.get_debug_snapshot()}"
            )

        if result.status == "MOVING":
            print(f"\n🚶 [ChestNode] 移动到箱子旁: chest={chest_tile}, stand_tile={result.stand_tile}")
        if result.status == "FACING":
            print(f"\n🧭 [ChestNode] 面向箱子: player={game_state.player_tile}, chest={chest_tile}")
        return result

    def _open_chest(
        self,
        context: PlayerContext,
        game_state: StardewState,
        current_task: ChestTask,
        resolved_chest_tile: Tile,
        stand_tile: Tile,
    ) -> None:
        print(f"\n📦 [ChestNode] 打开箱子界面: chest={resolved_chest_tile}")
        target_position = self._get_chest_interaction_position(game_state, stand_tile, resolved_chest_tile)
        response = context.executor_client.send_command(
            StardewCommand(
                action=StardewAction.OPEN_CHEST,
                location_name=current_task.target_loc,
                tile=(resolved_chest_tile.x, resolved_chest_tile.y),
            )
        )
        self._opened_chest_at = time.time()
        self._has_opened_chest = response == "SUCCESS"
        self._log(
            f"发送 OPEN_CHEST: chest={resolved_chest_tile}, stand_tile={stand_tile}, "
            f"target_position=({target_position[0]:.1f}, {target_position[1]:.1f}), "
            f"player_position={game_state.position}, player_tile={game_state.player_tile}, "
            f"tool_target={game_state.tool_target.tile}, response={response}"
        )

    def _transfer_chest_items(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: ChestTask,
        resolved_chest_tile: Tile,
    ) -> None:
        if self._has_sent_transfer_command:
            return

        transfer_item_requests = self._get_transfer_item_requests(game_state, current_task)
        if not transfer_item_requests:
            self._fail(context, blackboard, current_task, f"没有可执行的箱子转移动作: action={current_task.chest_action}")
            return

        transfer_action = (
            StardewAction.TAKE_ITEMS_FROM_CHEST
            if current_task.chest_action == "TAKE"
            else StardewAction.PUT_ITEMS_TO_CHEST
        )
        action_desc = "从箱子批量取物" if current_task.chest_action == "TAKE" else "向箱子批量存物"
        print(
            f"\n📦 [ChestNode] {action_desc}: "
            f"items={self._format_item_requests(transfer_item_requests)}, chest={resolved_chest_tile}"
        )
        response = context.executor_client.send_command(
            StardewCommand(
                action=transfer_action,
                location_name=current_task.target_loc,
                tile=(resolved_chest_tile.x, resolved_chest_tile.y),
                chest_items=[
                    ChestItemPayload(
                        item_name=item.item_name,
                        qualified_item_id=item.qualified_item_id,
                        count=item.count,
                    )
                    for item in transfer_item_requests
                ],
            )
        )
        self._has_sent_transfer_command = True
        result = self._parse_chest_batch_action_result(response)
        self._log(
            f"发送 {transfer_action.value}: response={response}, parsed={result}, "
            f"transfer_items={self._format_item_requests(transfer_item_requests)}, "
            f"before_counts={self._before_counts}, state_counts={self._count_requested_items(game_state, current_task)}"
        )

        if result.status not in ("SUCCESS", "PARTIAL_SUCCESS"):
            self._expected_after_counts = {}
            self._verify_started_at = None
            failure_reason = result.reason or result.status or "UNKNOWN_FAILURE"
            self._log(f"{transfer_action.value} 返回失败: reason={failure_reason}")
            if self._has_opened_chest and not self._has_closed_chest:
                self._close_chest_menu(context)
            self._fail(context, blackboard, current_task, f"箱子批量转移失败: reason={failure_reason}")
            return

        if not result.results:
            self._expected_after_counts = {}
            self._verify_started_at = None
            if self._has_opened_chest and not self._has_closed_chest:
                self._close_chest_menu(context)
            self._fail(context, blackboard, current_task, "箱子批量转移失败: C# 未返回物品结果")
            return

        failed_results = [
            item_result
            for item_result in result.results
            if not self._is_acceptable_transfer_result(current_task, item_result)
        ]
        if failed_results:
            self._expected_after_counts = {}
            self._verify_started_at = None
            failure_reason = self._format_item_results(failed_results)
            self._log(f"{transfer_action.value} 部分失败，准备关箱子后恢复: {failure_reason}")
            if self._has_opened_chest and not self._has_closed_chest:
                self._close_chest_menu(context)
            self._fail(context, blackboard, current_task, f"箱子批量转移部分失败: {failure_reason}")
            return

        self._expected_after_counts = {}
        for item_result in result.results:
            item_key = (item_result.item_name, item_result.qualified_item_id)
            before_count = self._before_counts.get(item_key, 0)
            if current_task.chest_action == "TAKE":
                self._expected_after_counts[item_key] = before_count + item_result.transferred_count
            else:
                self._expected_after_counts[item_key] = max(0, before_count - item_result.transferred_count)
        self._verify_started_at = time.time()

    def _is_waiting_for_inventory_verification(
        self,
        game_state: StardewState,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        current_task: ChestTask,
    ) -> bool:
        if not self._expected_after_counts:
            return False

        if not self._has_closed_chest:
            self._close_chest_menu(context)
            return True

        current_counts = self._count_requested_items(game_state, current_task)
        unverified_items: list[str] = []
        for item_request in current_task.items:
            item_key = item_request.key
            expected_count = self._expected_after_counts.get(item_key, item_request.count)
            current_count = current_counts.get(item_key, 0)
            if current_task.chest_action == "TAKE" and current_count < expected_count:
                unverified_items.append(f"{item_request.item_name}:{current_count}/{expected_count}")
            if current_task.chest_action == "PUT" and current_count > expected_count:
                unverified_items.append(f"{item_request.item_name}:{current_count}/{expected_count}")

        if not unverified_items:
            action_desc = "批量取物" if current_task.chest_action == "TAKE" else "批量存物"
            print(
                f"\n🟢 [ChestNode] {action_desc}完成: "
                f"items={self._format_item_requests(current_task.items)}"
            )
            self._log(
                f"背包验证通过: before_counts={self._before_counts}, "
                f"expected_counts={self._expected_after_counts}, current_counts={current_counts}"
            )
            blackboard.current_step_index += 1
            self._reset()
            return True

        if self._verify_started_at is not None and time.time() - self._verify_started_at > CHEST_VERIFY_TIMEOUT_SECONDS:
            self._fail(
                context,
                blackboard,
                current_task,
                f"箱子批量转移后背包验证超时: unverified={unverified_items}, expected={self._expected_after_counts}, current={current_counts}",
            )
            return True

        self._log(
            f"等待背包 state 验证箱子批量转移结果: unverified={unverified_items}, "
            f"expected={self._expected_after_counts}, current={current_counts}"
        )
        return True

    def _close_chest_menu(self, context: PlayerContext) -> None:
        response = context.executor_client.send_command(StardewCommand(action=StardewAction.CLOSE_MENU))
        self._has_closed_chest = True
        self._log(f"发送 CLOSE_MENU: response={response}")

    def _parse_chest_batch_action_result(self, response: str | None) -> ChestBatchActionResult:
        if response is None:
            return ChestBatchActionResult(status="FAILURE", reason="NO_RESPONSE", results=[])

        try:
            response_data = json.loads(response)
        except json.JSONDecodeError:
            return ChestBatchActionResult(status="FAILURE", reason=response, results=[])

        status = str(response_data.get("status", "FAILURE"))
        reason = str(response_data.get("reason", ""))
        results: list[ChestItemActionResult] = []
        for raw_result in response_data.get("results", []):
            if not isinstance(raw_result, dict):
                continue
            qualified_item_id = raw_result.get("qualified_item_id")
            results.append(
                ChestItemActionResult(
                    status=str(raw_result.get("status", "FAILURE")),
                    item_name=str(raw_result.get("item_name", "")),
                    qualified_item_id=str(qualified_item_id) if qualified_item_id else None,
                    requested_count=int(raw_result.get("requested_count", 0)),
                    transferred_count=int(raw_result.get("transferred_count", 0)),
                    inventory_count=int(raw_result.get("inventory_count", 0)),
                    reason=str(raw_result.get("reason", "")),
                )
            )

        return ChestBatchActionResult(status=status, reason=reason, results=results)

    def _resolve_chest_tile(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        current_task: ChestTask,
    ) -> Tile | None:
        if self._resolved_chest_tile is not None:
            return self._resolved_chest_tile

        response = context.executor_client.send_command(
            StardewCommand(
                action=StardewAction.QUERY_CHESTS,
                location_name=current_task.target_loc,
            )
        )
        chest_tiles = self._parse_query_chests_response(response)
        if chest_tiles is None:
            self._fail(context, blackboard, current_task, f"查询箱子坐标失败: response={response}")
            return None

        if current_task.chest_tile in chest_tiles:
            self._resolved_chest_tile = current_task.chest_tile
            self._log(f"指定箱子坐标校验通过: chest_tile={current_task.chest_tile}, all_chests={chest_tiles}")
            return self._resolved_chest_tile

        if len(chest_tiles) == 1:
            self._resolved_chest_tile = chest_tiles[0]
            print(
                f"\n📦 [ChestNode] 指定箱子坐标 {current_task.chest_tile} 不存在，"
                f"当前场景只有一个箱子，自动改用 {self._resolved_chest_tile}。"
            )
            self._log(
                f"指定箱子坐标不存在，使用唯一箱子坐标: requested={current_task.chest_tile}, "
                f"resolved={self._resolved_chest_tile}"
            )
            return self._resolved_chest_tile

        self._fail(
            context,
            blackboard,
            current_task,
            f"指定箱子不存在且无法唯一恢复: requested={current_task.chest_tile}, chests={chest_tiles}",
        )
        return None

    def _parse_query_chests_response(self, response: str | None) -> list[Tile] | None:
        if response is None:
            return None

        try:
            response_data = json.loads(response)
        except json.JSONDecodeError:
            return None

        if response_data.get("status") != "SUCCESS":
            return None

        chest_tiles: list[Tile] = []
        for raw_chest in response_data.get("chests", []):
            raw_tile = raw_chest.get("Tile") if isinstance(raw_chest, dict) else None
            if not isinstance(raw_tile, list) or len(raw_tile) < 2:
                continue
            chest_tiles.append(Tile(int(raw_tile[0]), int(raw_tile[1])))
        return chest_tiles

    def _fail(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        current_task: ChestTask,
        reason: str,
    ) -> None:
        context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
        blackboard.prompt = f"Chest {current_task.chest_action} 失败，需要恢复计划：{reason}"
        blackboard.macro_plan = []
        blackboard.current_step_index = 0
        print(f"\n🔴 [ChestNode] {reason}")
        self._log(
            f"ChestTask 失败: reason={reason}, task={current_task.desc}, "
            f"items={self._format_item_requests(current_task.items)}, chest={current_task.chest_tile}"
        )
        self._reset()

    def _is_new_task(self, blackboard: AgentBlackboard, current_task: ChestTask) -> bool:
        return self._task_signature != self._build_task_signature(blackboard, current_task)

    def _build_task_signature(self, blackboard: AgentBlackboard, current_task: ChestTask) -> tuple:
        return (
            blackboard.current_step_index,
            current_task.task_type,
            current_task.chest_action,
            current_task.target_loc,
            current_task.chest_tile,
            tuple((item.item_name, item.qualified_item_id, item.count) for item in current_task.items),
        )

    def _get_cardinal_neighbor_tiles(self, target_tile: Tile) -> set[Tile]:
        return {
            Tile(target_tile.x, target_tile.y - 1),
            Tile(target_tile.x + 1, target_tile.y),
            Tile(target_tile.x, target_tile.y + 1),
            Tile(target_tile.x - 1, target_tile.y),
        }

    def _get_chest_interaction_position(
        self,
        game_state: StardewState,
        stand_tile: Tile,
        chest_tile: Tile,
    ) -> tuple[float, float]:
        tile_size = game_state.tile_size
        player_width, player_height = game_state.player_size
        tile_left = stand_tile.x * tile_size
        tile_right = tile_left + tile_size
        tile_top = stand_tile.y * tile_size
        tile_bottom = tile_top + tile_size

        half_width = player_width / 2
        min_x = tile_left + half_width + CHEST_STAND_TILE_MARGIN_PX
        max_x = tile_right - half_width - CHEST_STAND_TILE_MARGIN_PX
        min_y = tile_top + player_height + CHEST_STAND_TILE_MARGIN_PX
        max_y = tile_bottom - CHEST_STAND_TILE_MARGIN_PX

        if stand_tile.x > chest_tile.x:
            target_x = tile_left + half_width + CHEST_INTERACTION_EDGE_MARGIN_PX
        elif stand_tile.x < chest_tile.x:
            target_x = tile_right - half_width - CHEST_INTERACTION_EDGE_MARGIN_PX
        else:
            target_x = self._clamp(game_state.position.x, min_x, max_x)

        if stand_tile.y > chest_tile.y:
            target_y = tile_top + player_height + CHEST_INTERACTION_EDGE_MARGIN_PX
        elif stand_tile.y < chest_tile.y:
            target_y = tile_bottom - CHEST_INTERACTION_EDGE_MARGIN_PX
        else:
            target_y = self._clamp(game_state.position.y, min_y, max_y)

        return self._clamp(target_x, min_x, max_x), self._clamp(target_y, min_y, max_y)

    def _is_player_at_chest_interaction_position(
        self,
        game_state: StardewState,
        stand_tile: Tile,
        chest_tile: Tile,
    ) -> bool:
        target_x, target_y = self._get_chest_interaction_position(game_state, stand_tile, chest_tile)
        return (
            abs(game_state.position.x - target_x) <= CHEST_INTERACTION_POSITION_TOLERANCE_PX
            and abs(game_state.position.y - target_y) <= CHEST_INTERACTION_POSITION_TOLERANCE_PX
        )

    def _build_move_to_chest_interaction_position_command(
        self,
        game_state: StardewState,
        stand_tile: Tile,
        chest_tile: Tile,
    ) -> StardewCommand:
        target_x, target_y = self._get_chest_interaction_position(game_state, stand_tile, chest_tile)
        key: list[str] = []

        if game_state.position.x < target_x - CHEST_INTERACTION_POSITION_TOLERANCE_PX:
            key.append("d")
        elif game_state.position.x > target_x + CHEST_INTERACTION_POSITION_TOLERANCE_PX:
            key.append("a")

        if game_state.position.y < target_y - CHEST_INTERACTION_POSITION_TOLERANCE_PX:
            key.append("s")
        elif game_state.position.y > target_y + CHEST_INTERACTION_POSITION_TOLERANCE_PX:
            key.append("w")

        if key == ["w"]:
            return StardewCommand(action=StardewAction.MOVE_UP, key=key)
        if key == ["s"]:
            return StardewCommand(action=StardewAction.MOVE_DOWN, key=key)
        if key == ["a"]:
            return StardewCommand(action=StardewAction.MOVE_LEFT, key=key)
        if key == ["d"]:
            return StardewCommand(action=StardewAction.MOVE_RIGHT, key=key)
        if set(key) == {"w", "d"}:
            return StardewCommand(action=StardewAction.MOVE_UP_RIGHT, key=["w", "d"])
        if set(key) == {"w", "a"}:
            return StardewCommand(action=StardewAction.MOVE_UP_LEFT, key=["w", "a"])
        if set(key) == {"s", "d"}:
            return StardewCommand(action=StardewAction.MOVE_DOWN_RIGHT, key=["s", "d"])
        if set(key) == {"s", "a"}:
            return StardewCommand(action=StardewAction.MOVE_DOWN_LEFT, key=["s", "a"])
        return StardewCommand(action=StardewAction.IDLE)

    def _clamp(self, value: float, min_value: float, max_value: float) -> float:
        return max(min_value, min(value, max_value))

    def _count_requested_items(
        self,
        game_state: StardewState,
        current_task: ChestTask,
    ) -> dict[tuple[str, str | None], int]:
        counts: dict[tuple[str, str | None], int] = {}
        for item_request in current_task.items:
            counts[item_request.key] = self._count_inventory_item(game_state, item_request)
        return counts

    def _get_transfer_item_requests(
        self,
        game_state: StardewState,
        current_task: ChestTask,
    ) -> list[ChestItemRequest]:
        transfer_item_requests: list[ChestItemRequest] = []
        for item_request in current_task.items:
            current_count = self._count_inventory_item(game_state, item_request)
            if current_task.chest_action == "TAKE":
                transfer_count = item_request.count - current_count
            else:
                transfer_count = min(item_request.count, current_count)

            if transfer_count <= 0:
                continue
            transfer_item_requests.append(
                ChestItemRequest(
                    item_name=item_request.item_name,
                    qualified_item_id=item_request.qualified_item_id,
                    count=transfer_count,
                )
            )
        return transfer_item_requests

    def _has_any_put_item_in_inventory(self, game_state: StardewState, current_task: ChestTask) -> bool:
        return any(
            self._count_inventory_item(game_state, item_request) > 0
            for item_request in current_task.items
        )

    def _is_acceptable_transfer_result(
        self,
        current_task: ChestTask,
        item_result: ChestItemActionResult,
    ) -> bool:
        if item_result.status == "SUCCESS":
            return True

        return (
            current_task.chest_action == "PUT"
            and item_result.transferred_count > 0
            and item_result.reason == "INVENTORY_NOT_ENOUGH"
        )

    def _count_inventory_item(self, game_state: StardewState, item_request: ChestItemRequest) -> int:
        if item_request.qualified_item_id is None:
            return count_inventory_items(game_state, item_request.item_name)

        total_count = 0
        for item in game_state.inventory.items:
            if item.qualified_item_id == item_request.qualified_item_id:
                total_count += max(item.stack, 1)
        return total_count

    def _format_item_requests(self, item_requests: list[ChestItemRequest]) -> str:
        return ", ".join(
            f"{item.item_name}({item.qualified_item_id or 'name'}):{item.count}"
            for item in item_requests
        )

    def _format_item_results(self, item_results: list[ChestItemActionResult]) -> str:
        return ", ".join(
            f"{item.item_name}:{item.status}/{item.reason or 'NO_REASON'} "
            f"transferred={item.transferred_count}/{item.requested_count}"
            for item in item_results
        )

    def _log_debug_heartbeat(
        self,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: ChestTask,
    ) -> None:
        now = time.time()
        if now - self._last_debug_heartbeat_at < 0.5:
            return

        self._last_debug_heartbeat_at = now
        self._log(
            f"心跳: step={blackboard.current_step_index}, action={current_task.chest_action}, "
            f"items={self._format_item_requests(current_task.items)}, before={self._before_counts}, "
            f"expected={self._expected_after_counts}, current={self._count_requested_items(game_state, current_task)}, "
            f"location={game_state.location_name}, player_tile={game_state.player_tile}, "
            f"chest_tile={current_task.chest_tile}, resolved_chest_tile={self._resolved_chest_tile}, "
            f"opened={self._has_opened_chest}, closed={self._has_closed_chest}, "
            f"locked_stand_tile={self._locked_stand_tile}, "
            f"positioning={self.positioning_controller.get_debug_snapshot()}"
        )

    def _log(self, message: str) -> None:
        self.chest_debug_logger.log(f"[ChestNode] {message}")

    def _reset(self) -> None:
        self._task_signature = None
        self._started_at = None
        self._before_counts = {}
        self._expected_after_counts = {}
        self._verify_started_at = None
        self._resolved_chest_tile = None
        self._opened_chest_at = None
        self._has_opened_chest = False
        self._has_closed_chest = False
        self._has_sent_transfer_command = False
        self._locked_stand_tile = None
        self._last_debug_heartbeat_at = 0.0
        self.positioning_controller.reset()
