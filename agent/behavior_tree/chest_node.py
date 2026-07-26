import json
import time
from dataclasses import dataclass
from typing import Literal

from agent.action.chest.chest_knowledge_service import ChestKnowledgeService
from agent.action.location.location import Location
from agent.action.valley_action.action_type import ChestItemPayload, KeyType, StardewAction, StardewCommand
from agent.action.valley_action.positioning_controller import PositioningController, PositioningGoal, PositioningResult
from agent.action.valley_action.tool_targeting import build_tool_target_face_command, is_tool_targeting
from agent.base_task import BaseTask, TaskType
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard, BorrowedChestItem
from agent.behavior_tree.chest_debug_logger import ChestDebugLogger
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.tool_selection import count_inventory_items
from agent.memory.map_knowledge_cache import ChestContentKnowledge
from server.valley_server import StardewState
from server.type import Tile

type ChestAction = Literal[
    "TAKE",  # 从指定箱子取出指定物品；Chest P0。
    "PUT",  # 向指定箱子存入指定物品；Chest P1。
    "QUERY",  # 走到指定箱子旁，打开查看并写入运行期箱子知识缓存；Chest P2。
    "SCAN",  # 逐个走到当前场景箱子旁，打开查看并写入运行期箱子知识缓存；Chest P2/P3 测试入口。
]


CHEST_ACTION_TIMEOUT_SECONDS = 8.0
CHEST_VERIFY_TIMEOUT_SECONDS = 2.0
CHEST_MENU_WAIT_SECONDS = 0.5
CHEST_STAND_TILE_MARGIN_PX = 1.0
CHEST_INTERACTION_EDGE_MARGIN_PX = 1.0
CHEST_INTERACTION_POSITION_TOLERANCE_PX = 2.0
BORROWABLE_TOOL_NAMES: tuple[str, ...] = ("Axe", "Hoe", "Pickaxe", "Scythe", "Watering Can")


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
        chest_tile: Tile | None,
        item_name: str | None = None,
        count: int = 0,
        qualified_item_id: str | None = None,
        items: list[ChestItemRequest] | None = None,
    ):
        super().__init__(task_type=task_type, desc=desc)
        self.chest_action = chest_action
        self.target_loc: Location = target_loc
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
    Chest P0/P1/P2/P3：指定箱子取放物品，或交互式打开箱子建立内容缓存。

    当前不模拟鼠标 UI。节点会复用 PositioningController 走到箱子上下左右相邻格并面向箱子，
    先打开箱子界面，再发送 SMAPI 结构化动作 TAKE_ITEMS_FROM_CHEST / PUT_ITEMS_TO_CHEST，
    或在打开后读取箱子内容写入 MapKnowledgeCache。
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
        self._scan_chest_tiles: list[Tile] = []
        self._scan_index = 0
        self._scanned_chest_count = 0
        self._last_debug_heartbeat_at = 0.0
        self.chest_debug_logger = ChestDebugLogger()
        self.chest_knowledge_service = ChestKnowledgeService(self._log)

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

        if current_task.chest_action not in ("TAKE", "PUT", "QUERY", "SCAN"):
            self._fail(context, blackboard, current_task, f"Chest 暂不支持动作: {current_task.chest_action}")
            return "SUCCESS"

        invalid_items = [item for item in current_task.items if item.count <= 0 or not item.item_name]
        if current_task.chest_action in ("TAKE", "PUT") and (not current_task.items or invalid_items):
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

        if self._started_at is not None and time.time() - self._started_at > CHEST_ACTION_TIMEOUT_SECONDS:
            self._fail(context, blackboard, current_task, f"Chest {current_task.chest_action} 动作超时")
            return "SUCCESS"

        if self._is_waiting_for_inventory_verification(game_state, context, blackboard, current_task):
            return "RUNNING"

        if current_task.chest_action == "SCAN":
            return self._scan_location_chests(context, blackboard, game_state, current_task)

        if current_task.chest_action == "QUERY":
            return self._query_chest_content(context, blackboard, game_state, current_task)

        if current_task.chest_action == "TAKE" and self._has_enough_items_in_inventory(
            game_state, blackboard, current_task
        ):
            return "SUCCESS"

        if current_task.chest_action == "TAKE" and current_task.chest_tile is None:
            cached_chest_tile = self._get_cached_chest_tile_for_items(context, game_state, current_task)
            if cached_chest_tile is not None:
                if self._resolved_chest_tile is None:
                    self._resolved_chest_tile = cached_chest_tile
                    print(
                        f"\n📦 [ChestNode] 从缓存自动选择箱子: chest={cached_chest_tile}, "
                        f"items={self._format_item_requests(current_task.items)}"
                    )
                    self._log(
                        f"从缓存自动选择箱子: chest={cached_chest_tile}, "
                        f"items={self._format_item_requests(current_task.items)}"
                    )
            else:
                return self._search_chests_and_take(context, blackboard, game_state, current_task)

        if current_task.chest_action == "PUT" and not self._has_any_put_item_in_inventory(game_state, current_task):
            self._fail(context, blackboard, current_task, "背包中没有任何可存入箱子的目标物品")
            return "SUCCESS"

        resolved_chest_tile = self._resolve_chest_tile(context, blackboard, game_state, current_task)
        if resolved_chest_tile is None:
            return "SUCCESS" if not blackboard.macro_plan else "RUNNING"

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
        self._scan_chest_tiles = []
        self._scan_index = 0
        self._scanned_chest_count = 0
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
            self._fail(
                context, blackboard, current_task, f"没有可执行的箱子转移动作: action={current_task.chest_action}"
            )
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
        context.map_knowledge_cache.mark_chest_content_stale(
            current_task.target_loc,
            resolved_chest_tile,
            reason=f"{transfer_action.value} accepted",
        )
        if current_task.chest_action == "TAKE":
            self._record_borrowed_tool_items(blackboard, current_task, resolved_chest_tile, result.results)
        elif current_task.chest_action == "PUT":
            self._mark_borrowed_tool_items_returned(blackboard, current_task, resolved_chest_tile, result.results)
        self._log(
            f"标记箱子内容缓存过期: location={current_task.target_loc}, "
            f"chest={resolved_chest_tile}, reason={transfer_action.value} accepted"
        )
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
            print(f"\n🟢 [ChestNode] {action_desc}完成: " f"items={self._format_item_requests(current_task.items)}")
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
        game_state: StardewState,
        current_task: ChestTask,
    ) -> Tile | None:
        if self._resolved_chest_tile is not None:
            return self._resolved_chest_tile

        if current_task.chest_tile is None:
            if current_task.chest_action == "PUT":
                borrowed_chest_tile = self._resolve_borrowed_return_chest_tile(blackboard, current_task)
                if borrowed_chest_tile is not None:
                    self._resolved_chest_tile = borrowed_chest_tile
                    self._log(
                        f"根据借用记录解析归还箱子: chest={borrowed_chest_tile}, "
                        f"items={self._format_item_requests(current_task.items)}"
                    )
                    return self._resolved_chest_tile

                semantic_chest_tile = self._resolve_semantic_tool_chest_tile(context, game_state, current_task)
                if semantic_chest_tile is not None:
                    self._resolved_chest_tile = semantic_chest_tile
                    self._log(
                        f"根据箱子语义记忆解析归还箱子: chest={semantic_chest_tile}, "
                        f"items={self._format_item_requests(current_task.items)}"
                    )
                    return self._resolved_chest_tile

            self._fail(context, blackboard, current_task, f"{current_task.chest_action} 当前必须指定 chest_tile")
            return None

        chests = self.chest_knowledge_service.query_chests(context, current_task.target_loc)
        if chests is None:
            self._fail(context, blackboard, current_task, "查询箱子坐标失败")
            return None

        chest_tiles = [chest.tile for chest in chests]
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

    def _scan_location_chests(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: ChestTask,
    ) -> NodeStatus:
        if not self._ensure_scan_chest_tiles(context, blackboard, current_task):
            return "SUCCESS"

        if self._scan_index >= len(self._scan_chest_tiles):
            print(
                f"\n📦 [ChestNode] 场景箱子交互式扫描完成: "
                f"location={current_task.target_loc}, count={self._scanned_chest_count}"
            )
            self._log(
                f"Chest SCAN 完成: location={current_task.target_loc}, "
                f"scanned={self._scanned_chest_count}/{len(self._scan_chest_tiles)}"
            )
            blackboard.current_step_index += 1
            self._reset()
            return "SUCCESS"

        chest_tile = self._scan_chest_tiles[self._scan_index]
        if self._resolved_chest_tile != chest_tile:
            self._begin_scan_chest(chest_tile)

        content_result = self._open_and_cache_current_scan_chest(
            context, blackboard, game_state, current_task, chest_tile
        )
        if content_result != "READY":
            return content_result

        self._close_chest_menu(context)
        self._scanned_chest_count += 1
        self._scan_index += 1
        self._reset_current_chest_interaction()
        self._log(
            f"完成单个箱子查看并关闭: location={current_task.target_loc}, "
            f"chest={chest_tile}, progress={self._scan_index}/{len(self._scan_chest_tiles)}"
        )
        return "RUNNING"

    def _query_chest_content(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: ChestTask,
    ) -> NodeStatus:
        if current_task.chest_tile is None:
            self._fail(context, blackboard, current_task, "QUERY 必须指定 chest_tile；扫描全场景请使用 SCAN")
            return "SUCCESS"

        if not self._scan_chest_tiles:
            self._scan_chest_tiles = [current_task.chest_tile]
            self._scan_index = 0
            self._scanned_chest_count = 0
            self._begin_scan_chest(current_task.chest_tile)

        chest_tile = self._scan_chest_tiles[self._scan_index]
        content_result = self._open_and_cache_current_scan_chest(
            context, blackboard, game_state, current_task, chest_tile
        )
        if content_result != "READY":
            return content_result

        self._close_chest_menu(context)
        print(f"\n📦 [ChestNode] 打开查看指定箱子完成: chest={current_task.chest_tile}")
        self._log(f"Chest QUERY 完成: location={current_task.target_loc}, chest={current_task.chest_tile}")
        blackboard.current_step_index += 1
        self._reset()
        return "SUCCESS"

    def _search_chests_and_take(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: ChestTask,
    ) -> NodeStatus:
        if not self._ensure_take_search_chest_tiles(context, blackboard, game_state, current_task):
            return "SUCCESS"

        if self._scan_index >= len(self._scan_chest_tiles):
            self._fail(
                context,
                blackboard,
                current_task,
                f"逐箱打开查看后仍未找到满足取物需求的箱子: items={self._format_item_requests(current_task.items)}",
            )
            return "SUCCESS"

        chest_tile = self._scan_chest_tiles[self._scan_index]
        if self._resolved_chest_tile != chest_tile:
            self._begin_scan_chest(chest_tile)

        content_result = self._open_and_cache_current_scan_chest(
            context, blackboard, game_state, current_task, chest_tile
        )
        if content_result != "READY":
            return content_result

        if self._does_opened_chest_content_satisfy_requests(context, current_task, chest_tile):
            print(f"\n📦 [ChestNode] 打开查看后找到目标箱子: chest={chest_tile}")
            self._log(
                f"打开查看后找到目标箱子，准备取物: chest={chest_tile}, "
                f"items={self._format_item_requests(current_task.items)}"
            )
            self._transfer_chest_items(context, blackboard, game_state, current_task, chest_tile)
            return "RUNNING"

        self._close_chest_menu(context)
        self._scan_index += 1
        self._reset_current_chest_interaction()
        self._log(
            f"当前箱子不满足取物需求，关闭后继续查找: chest={chest_tile}, "
            f"progress={self._scan_index}/{len(self._scan_chest_tiles)}, "
            f"items={self._format_item_requests(current_task.items)}"
        )
        return "RUNNING"

    def _ensure_scan_chest_tiles(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        current_task: ChestTask,
    ) -> bool:
        if self._scan_chest_tiles:
            return True

        chests = self.chest_knowledge_service.query_chests(context, current_task.target_loc)
        if chests is None:
            self._fail(context, blackboard, current_task, f"查询箱子坐标失败: location={current_task.target_loc}")
            return False

        self._scan_chest_tiles = [chest.tile for chest in chests]
        self._scan_index = 0
        self._scanned_chest_count = 0
        self._log(
            f"准备交互式遍历箱子: location={current_task.target_loc}, "
            f"chests={self._format_tile_list(self._scan_chest_tiles)}"
        )
        return True

    def _ensure_take_search_chest_tiles(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: ChestTask,
    ) -> bool:
        if self._scan_chest_tiles:
            return True

        chests = self.chest_knowledge_service.query_chests(context, current_task.target_loc)
        if chests is None:
            self._fail(context, blackboard, current_task, f"查询箱子坐标失败: location={current_task.target_loc}")
            return False

        unknown_tiles: list[Tile] = []
        stale_matching_tiles: list[Tile] = []
        stale_fallback_tiles: list[Tile] = []
        skipped_fresh_tiles: list[Tile] = []

        for chest in chests:
            chest_tile = chest.tile
            cached_content = context.map_knowledge_cache.get_chest_content(
                current_task.target_loc,
                chest_tile,
                include_stale=True,
            )
            if cached_content is None:
                unknown_tiles.append(chest_tile)
                continue

            if not cached_content.is_stale:
                if self._does_cached_chest_content_satisfy_requests(cached_content, current_task.items):
                    self._scan_chest_tiles = [chest_tile]
                    self._scan_index = 0
                    self._scanned_chest_count = 0
                    self._log(
                        f"TAKE 搜索队列命中已知新鲜缓存: chest={chest_tile}, "
                        f"items={self._format_item_requests(current_task.items)}"
                    )
                    return True

                skipped_fresh_tiles.append(chest_tile)
                continue

            if self._does_cached_chest_content_satisfy_requests(cached_content, current_task.items):
                stale_matching_tiles.append(chest_tile)
            else:
                stale_fallback_tiles.append(chest_tile)

        self._scan_chest_tiles = (
            self._sort_tiles_by_distance(
                stale_matching_tiles,
                game_state.player_tile,
            )
            + self._sort_tiles_by_distance(
                unknown_tiles,
                game_state.player_tile,
            )
            + self._sort_tiles_by_distance(
                stale_fallback_tiles,
                game_state.player_tile,
            )
        )
        self._scan_index = 0
        self._scanned_chest_count = 0

        self._log(
            f"准备按需搜索箱子: location={current_task.target_loc}, "
            f"items={self._format_item_requests(current_task.items)}, "
            f"queue={self._format_tile_list(self._scan_chest_tiles)}, "
            f"unknown={self._format_tile_list(unknown_tiles)}, "
            f"stale_matching={self._format_tile_list(stale_matching_tiles)}, "
            f"stale_fallback={self._format_tile_list(stale_fallback_tiles)}, "
            f"skipped_fresh_non_matching={self._format_tile_list(skipped_fresh_tiles)}"
        )

        if self._scan_chest_tiles:
            if skipped_fresh_tiles:
                print(f"\n📦 [ChestNode] 跳过已知不匹配箱子: " f"chests={self._format_tile_list(skipped_fresh_tiles)}")
            return True

        self._fail(
            context,
            blackboard,
            current_task,
            f"已知箱子缓存均不包含目标物品，且没有未知箱子可查看: "
            f"items={self._format_item_requests(current_task.items)}",
        )
        return False

    def _begin_scan_chest(self, chest_tile: Tile) -> None:
        self._resolved_chest_tile = chest_tile
        self._reset_current_chest_interaction()
        print(f"\n📦 [ChestNode] 准备走到箱子旁打开查看: chest={chest_tile}")
        self._log(f"开始打开查看箱子: chest={chest_tile}")

    def _open_and_cache_current_scan_chest(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: ChestTask,
        chest_tile: Tile,
    ) -> NodeStatus | Literal["READY"]:
        if self._locked_stand_tile is None:
            positioning_result = self._tick_chest_positioning(game_state, context, chest_tile)
            if positioning_result.status == "FAILED":
                self._fail(
                    context,
                    blackboard,
                    current_task,
                    f"无法移动并面向待查看箱子: chest_tile={chest_tile}, reason={positioning_result.reason}",
                )
                return "SUCCESS"
            if positioning_result.status == "MOVING":
                return "RUNNING"
            if positioning_result.stand_tile is not None:
                self._locked_stand_tile = positioning_result.stand_tile
            if positioning_result.status == "FACING":
                return "RUNNING"

        stand_tile = self._locked_stand_tile or game_state.player_tile
        if not self._is_player_at_chest_interaction_position(game_state, stand_tile, chest_tile):
            command = self._build_move_to_chest_interaction_position_command(game_state, stand_tile, chest_tile)
            response = context.executor_client.send_command(command)
            target_position = self._get_chest_interaction_position(game_state, stand_tile, chest_tile)
            self._log(
                f"查看箱子前靠近交互边缘: chest={chest_tile}, stand_tile={stand_tile}, "
                f"target_position=({target_position[0]:.1f}, {target_position[1]:.1f}), "
                f"player_position={game_state.position}, command={command.action}, response={response}"
            )
            return "RUNNING"

        if not is_tool_targeting(game_state, chest_tile):
            command = build_tool_target_face_command(game_state.player_tile, chest_tile)
            response = context.executor_client.send_command(command)
            self._log(
                f"查看箱子前最终面向校验: chest={chest_tile}, stand_tile={stand_tile}, "
                f"player_tile={game_state.player_tile}, tool_target={game_state.tool_target.tile}, "
                f"command={command.action}, response={response}"
            )
            return "RUNNING"

        if not self._has_opened_chest:
            self._open_chest(context, game_state, current_task, chest_tile, stand_tile)
            return "RUNNING"

        if self._opened_chest_at is not None and time.time() - self._opened_chest_at < CHEST_MENU_WAIT_SECONDS:
            self._log(
                f"等待箱子界面稳定后查看内容: elapsed={time.time() - self._opened_chest_at:.2f}s, "
                f"required={CHEST_MENU_WAIT_SECONDS:.2f}s, chest={chest_tile}"
            )
            return "RUNNING"

        chest_content = self.chest_knowledge_service.query_chest_content(context, current_task.target_loc, chest_tile)
        if chest_content is None:
            self._fail(context, blackboard, current_task, f"打开箱子后查询内容失败: chest={chest_tile}")
            return "SUCCESS"

        self._log(
            f"打开查看箱子内容完成: chest={chest_tile}, "
            f"items={self.chest_knowledge_service.format_chest_content_items(chest_content.items)}"
        )
        return "READY"

    def _get_cached_chest_tile_for_items(
        self,
        context: PlayerContext,
        game_state: StardewState,
        current_task: ChestTask,
    ) -> Tile | None:
        cached_matches = context.map_knowledge_cache.find_chests_containing_items(
            current_task.target_loc,
            current_task.items,
            player_tile=game_state.player_tile,
        )
        if not cached_matches:
            return None
        return cached_matches[0].tile

    def _does_opened_chest_content_satisfy_requests(
        self,
        context: PlayerContext,
        current_task: ChestTask,
        chest_tile: Tile,
    ) -> bool:
        chest_content = context.map_knowledge_cache.get_chest_content(current_task.target_loc, chest_tile)
        if chest_content is None:
            return False
        matched_chests = context.map_knowledge_cache.find_chests_containing_items(
            current_task.target_loc,
            current_task.items,
            player_tile=chest_tile,
        )
        return any(matched_chest.tile == chest_tile for matched_chest in matched_chests)

    def _does_cached_chest_content_satisfy_requests(
        self,
        chest_content: ChestContentKnowledge,
        item_requests: list[ChestItemRequest],
    ) -> bool:
        for item_request in item_requests:
            if self._count_items_in_cached_chest_content(chest_content, item_request) < item_request.count:
                return False
        return True

    def _count_items_in_cached_chest_content(
        self,
        chest_content: ChestContentKnowledge,
        item_request: ChestItemRequest,
    ) -> int:
        total_count = 0
        for item in chest_content.items:
            if item_request.qualified_item_id is not None:
                if item.QualifiedItemId != item_request.qualified_item_id:
                    continue
            elif item.Name != item_request.item_name and item.DisplayName != item_request.item_name:
                continue
            total_count += max(item.Stack, 1)
        return total_count

    def _sort_tiles_by_distance(self, tiles: list[Tile], player_tile: Tile | None) -> list[Tile]:
        return sorted(
            tiles,
            key=lambda tile: (
                self._get_tile_distance(player_tile, tile),
                tile.x,
                tile.y,
            ),
        )

    def _get_tile_distance(self, start_tile: Tile | None, end_tile: Tile) -> int:
        if start_tile is None:
            return 0
        return abs(start_tile.x - end_tile.x) + abs(start_tile.y - end_tile.y)

    def _record_borrowed_tool_items(
        self,
        blackboard: AgentBlackboard,
        current_task: ChestTask,
        chest_tile: Tile,
        item_results: list[ChestItemActionResult],
    ) -> None:
        if current_task.chest_action != "TAKE":
            return

        recorded_items: list[str] = []
        for item_result in item_results:
            if item_result.transferred_count <= 0:
                continue
            if not self._is_borrowable_tool_name(item_result.item_name):
                continue

            self._add_borrowed_chest_item(
                blackboard,
                BorrowedChestItem(
                    location_name=current_task.target_loc,
                    chest_tile=chest_tile,
                    item_name=item_result.item_name,
                    qualified_item_id=item_result.qualified_item_id,
                    count=item_result.transferred_count,
                ),
            )
            recorded_items.append(f"{item_result.item_name}:{item_result.transferred_count}")

        if recorded_items:
            self._log(
                f"记录工具借用来源: location={current_task.target_loc}, chest={chest_tile}, "
                f"items={recorded_items}, borrowed={self._format_borrowed_chest_items(blackboard.borrowed_chest_items)}"
            )

    def _mark_borrowed_tool_items_returned(
        self,
        blackboard: AgentBlackboard,
        current_task: ChestTask,
        chest_tile: Tile,
        item_results: list[ChestItemActionResult],
    ) -> None:
        if current_task.chest_action != "PUT":
            return

        returned_items: list[str] = []
        for item_result in item_results:
            if item_result.transferred_count <= 0:
                continue
            if not self._is_borrowable_tool_name(item_result.item_name):
                continue

            remaining_count = item_result.transferred_count
            next_borrowed_items: list[BorrowedChestItem] = []
            for borrowed_item in blackboard.borrowed_chest_items:
                if remaining_count <= 0:
                    next_borrowed_items.append(borrowed_item)
                    continue

                if not self._matches_borrowed_item(
                    borrowed_item,
                    current_task.target_loc,
                    chest_tile,
                    item_result.item_name,
                    item_result.qualified_item_id,
                ):
                    next_borrowed_items.append(borrowed_item)
                    continue

                consumed_count = min(borrowed_item.count, remaining_count)
                borrowed_item.count -= consumed_count
                remaining_count -= consumed_count
                if borrowed_item.count > 0:
                    next_borrowed_items.append(borrowed_item)

            blackboard.borrowed_chest_items = next_borrowed_items
            returned_items.append(f"{item_result.item_name}:{item_result.transferred_count - remaining_count}")

        if returned_items:
            self._log(
                f"扣减已归还工具借用记录: location={current_task.target_loc}, chest={chest_tile}, "
                f"items={returned_items}, borrowed={self._format_borrowed_chest_items(blackboard.borrowed_chest_items)}"
            )

    def _add_borrowed_chest_item(
        self,
        blackboard: AgentBlackboard,
        borrowed_item: BorrowedChestItem,
    ) -> None:
        for existing_item in blackboard.borrowed_chest_items:
            if existing_item.key == borrowed_item.key:
                existing_item.count += borrowed_item.count
                return
        blackboard.borrowed_chest_items.append(borrowed_item)

    def _resolve_borrowed_return_chest_tile(
        self,
        blackboard: AgentBlackboard,
        current_task: ChestTask,
    ) -> Tile | None:
        borrowed_by_tile: dict[tuple[int, int], list[BorrowedChestItem]] = {}
        for borrowed_item in blackboard.borrowed_chest_items:
            if borrowed_item.location_name != current_task.target_loc:
                continue
            borrowed_by_tile.setdefault((borrowed_item.chest_tile.x, borrowed_item.chest_tile.y), []).append(
                borrowed_item
            )

        for borrowed_items in borrowed_by_tile.values():
            if self._borrowed_items_satisfy_requests(borrowed_items, current_task.items):
                return borrowed_items[0].chest_tile
        return None

    def _resolve_semantic_tool_chest_tile(
        self,
        context: PlayerContext,
        game_state: StardewState,
        current_task: ChestTask,
    ) -> Tile | None:
        if not current_task.items or not all(
            self._is_borrowable_tool_name(item.item_name) for item in current_task.items
        ):
            return None

        semantic_chests = context.map_knowledge_cache.find_chest_semantics(
            current_task.target_loc,
            required_label="tool_chest",
            intended_item_names={item.item_name for item in current_task.items},
            player_tile=game_state.player_tile,
        )
        if not semantic_chests:
            return None
        return semantic_chests[0].tile

    def _borrowed_items_satisfy_requests(
        self,
        borrowed_items: list[BorrowedChestItem],
        item_requests: list[ChestItemRequest],
    ) -> bool:
        for item_request in item_requests:
            matched_count = 0
            for borrowed_item in borrowed_items:
                if self._matches_item_identity(
                    borrowed_item.item_name,
                    borrowed_item.qualified_item_id,
                    item_request.item_name,
                    item_request.qualified_item_id,
                ):
                    matched_count += borrowed_item.count
            if matched_count < item_request.count:
                return False
        return True

    def _matches_borrowed_item(
        self,
        borrowed_item: BorrowedChestItem,
        location_name: Location,
        chest_tile: Tile,
        item_name: str,
        qualified_item_id: str | None,
    ) -> bool:
        return (
            borrowed_item.location_name == location_name
            and borrowed_item.chest_tile == chest_tile
            and self._matches_item_identity(
                borrowed_item.item_name,
                borrowed_item.qualified_item_id,
                item_name,
                qualified_item_id,
            )
        )

    def _matches_item_identity(
        self,
        source_item_name: str,
        source_qualified_item_id: str | None,
        target_item_name: str,
        target_qualified_item_id: str | None,
    ) -> bool:
        if target_qualified_item_id is not None or source_qualified_item_id is not None:
            return source_qualified_item_id == target_qualified_item_id
        return self._normalize_tool_text(source_item_name) == self._normalize_tool_text(target_item_name)

    def _is_borrowable_tool_name(self, item_name: str) -> bool:
        normalized_item_name = self._normalize_tool_text(item_name)
        for tool_name in BORROWABLE_TOOL_NAMES:
            normalized_tool_name = self._normalize_tool_text(tool_name)
            if normalized_item_name == normalized_tool_name or normalized_item_name.endswith(
                f" {normalized_tool_name}"
            ):
                return True
        return False

    def _normalize_tool_text(self, value: str) -> str:
        return " ".join(value.strip().lower().split())

    def _format_borrowed_chest_items(self, borrowed_items: list[BorrowedChestItem]) -> str:
        return str(
            [
                {
                    "location": item.location_name,
                    "chest": item.chest_tile,
                    "item": item.item_name,
                    "qualified_item_id": item.qualified_item_id,
                    "count": item.count,
                }
                for item in borrowed_items
            ]
        )

    def _reset_current_chest_interaction(self) -> None:
        self._opened_chest_at = None
        self._has_opened_chest = False
        self._has_closed_chest = False
        self._has_sent_transfer_command = False
        self._locked_stand_tile = None
        self.positioning_controller.reset()

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
        key: list[KeyType] = []

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
        return any(self._count_inventory_item(game_state, item_request) > 0 for item_request in current_task.items)

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
        return ", ".join(f"{item.item_name}({item.qualified_item_id or 'name'}):{item.count}" for item in item_requests)

    def _format_item_results(self, item_results: list[ChestItemActionResult]) -> str:
        return ", ".join(
            f"{item.item_name}:{item.status}/{item.reason or 'NO_REASON'} "
            f"transferred={item.transferred_count}/{item.requested_count}"
            for item in item_results
        )

    def _format_tile_list(self, tiles: list[Tile]) -> str:
        return str(sorted(tiles, key=lambda tile: (tile.x, tile.y)))

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
        self._scan_chest_tiles = []
        self._scan_index = 0
        self._scanned_chest_count = 0
        self._last_debug_heartbeat_at = 0.0
        self.positioning_controller.reset()
