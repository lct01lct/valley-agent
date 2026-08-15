import json
import time

from agent.action.chest.chest_knowledge_service import ChestKnowledgeService
from agent.action.inventory.task_inventory_policy import InventoryTaskContext, InventoryTransferCandidate, TaskInventoryPolicy
from agent.action.location.location import LOCATIONS
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.base_task import BaseTask
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard, UnreceivableLootRecord
from agent.behavior_tree.chest_node import ChestItemRequest, ChestNode, ChestTask
from agent.behavior_tree.inventory_recovery_debug_logger import InventoryRecoveryDebugLogger
from agent.behavior_tree.player_context import PlayerContext
from server.valley_server import StardewState
from server.type import Tile


INVENTORY_RECOVERY_TIMEOUT_SECONDS = 14.0
MAX_STORE_ITEM_TYPES_PER_RECOVERY = 6


class InventoryRecoveryNode(BTNode):
    """
    背包满后的任务感知型恢复节点。

    第一版边界：
    - 只响应 CollectLoot 暴露的“背包满且仍有掉落物无法接收”。
    - 优先在当前场景找最近箱子，存入与当前任务无关的物品。
    - 若没有箱子，再丢弃与当前任务无关的物品，并短期忽略 Agent 自己丢出的 Debris。
    """

    def __init__(self) -> None:
        self.task_inventory_policy = TaskInventoryPolicy()
        self.chest_node = ChestNode()
        self.chest_knowledge_service = ChestKnowledgeService(self._log)
        self.debug_logger = InventoryRecoveryDebugLogger()
        self._started_at: float | None = None
        self._chest_task: ChestTask | None = None
        self._discard_candidate: InventoryTransferCandidate | None = None

    def initialize(self) -> None:
        self.debug_logger.clear()

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        if not self._has_collect_loot_recovery_request(blackboard):
            self._reset()
            return "FAILURE"

        game_state = context.state
        if game_state is None:
            return "RUNNING"

        if self._started_at is None:
            self._start(blackboard, game_state)

        self._record_departure_path_breadcrumb(blackboard, game_state)

        if self._started_at is not None and time.time() - self._started_at > INVENTORY_RECOVERY_TIMEOUT_SECONDS:
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
            self._mark_recovery_failed(
                blackboard,
                f"背包恢复超时: elapsed={time.time() - self._started_at:.2f}s",
            )
            self._reset()
            return "FAILURE"

        current_task = self._get_current_recovery_task(blackboard)
        if self._chest_task is not None:
            return await self._run_store_to_chest(
                context,
                blackboard,
                game_state,
                current_task,
                self._chest_task.chest_tile,
                [],
            )

        transfer_candidates = self.task_inventory_policy.find_task_irrelevant_transfer_candidates(
            game_state,
            current_task,
        )
        if not transfer_candidates:
            self._mark_recovery_failed(blackboard, "没有可存箱或可丢弃的任务无关物品")
            self._reset()
            return "FAILURE"

        chest_tile = self._resolve_nearest_chest_tile(context, game_state)
        if chest_tile is not None:
            return await self._run_store_to_chest(context, blackboard, game_state, current_task, chest_tile, transfer_candidates)

        return self._run_discard_fallback(context, blackboard, game_state, transfer_candidates[0])

    def _has_collect_loot_recovery_request(self, blackboard: AgentBlackboard) -> bool:
        return (
            blackboard.inventory_check_failed
            and blackboard.inventory_failure_reason == "INVENTORY_FULL_WHILE_COLLECTING"
            and not bool(blackboard.inventory_recovery_context.get("recovery_failed"))
        )

    def _start(self, blackboard: AgentBlackboard, game_state: StardewState) -> None:
        self._started_at = time.time()
        if not blackboard.inventory_recovery_departure_path:
            blackboard.inventory_recovery_departure_path = [game_state.player_tile]
        residual_record = blackboard.collect_loot_residual_record
        if residual_record is not None and not residual_record.inventory_recovery_departure_path:
            residual_record.inventory_recovery_departure_path = list(blackboard.inventory_recovery_departure_path)
        self._log(
            f"开始背包恢复: location={game_state.location_name}, player={game_state.player_tile}, "
            f"context={blackboard.inventory_recovery_context}, "
            f"departure_path={self._format_tile_list(blackboard.inventory_recovery_departure_path)}"
        )
        print("\n🎒 [InventoryRecoveryNode] 背包已满，准备整理任务无关物品。")

    def _record_departure_path_breadcrumb(self, blackboard: AgentBlackboard, game_state: StardewState) -> None:
        current_tile = game_state.player_tile
        if not blackboard.inventory_recovery_departure_path:
            blackboard.inventory_recovery_departure_path = [current_tile]
        elif blackboard.inventory_recovery_departure_path[-1] != current_tile:
            blackboard.inventory_recovery_departure_path.append(current_tile)

        residual_record = blackboard.collect_loot_residual_record
        if residual_record is not None:
            residual_record.inventory_recovery_departure_path = list(blackboard.inventory_recovery_departure_path)

    async def _run_store_to_chest(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: BaseTask | None,
        chest_tile: Tile,
        transfer_candidates: list[InventoryTransferCandidate],
    ) -> NodeStatus:
        if self._chest_task is None:
            item_requests = self._build_store_item_requests(transfer_candidates)
            if not item_requests:
                self._mark_recovery_failed(blackboard, "任务无关物品候选无法转换为 ChestItemRequest")
                self._reset()
                return "FAILURE"

            self._chest_task = ChestTask(
                task_type="CHEST",
                desc="背包恢复：把任务无关物品存入最近箱子",
                chest_action="PUT",
                target_loc=game_state.location_name,
                chest_tile=chest_tile,
                items=item_requests,
            )
            self._log(
                f"选择最近箱子执行存物恢复: chest={chest_tile}, "
                f"items={self._format_item_requests(item_requests)}, "
                f"task_context={self._format_task_context(current_task)}"
            )

        status = await self._run_chest_node_with_temporary_plan(blackboard, context, self._chest_task)
        if status == "RUNNING":
            return "RUNNING"

        if self._chest_task is None:
            self._mark_recovery_failed(blackboard, "ChestTask 状态异常，无法确认存箱恢复结果")
            self._reset()
            return "FAILURE"

        if self._last_temporary_chest_task_completed:
            print("\n🟢 [InventoryRecoveryNode] 已把任务无关物品存入箱子，交回 CollectLoot 继续拾取。")
            self._complete_recovery(blackboard)
            self._reset()
            return "SUCCESS"

        self._mark_recovery_failed(blackboard, "最近箱子存物失败")
        self._reset()
        return "FAILURE"

    _last_temporary_chest_task_completed = False

    async def _run_chest_node_with_temporary_plan(
        self,
        blackboard: AgentBlackboard,
        context: PlayerContext,
        chest_task: ChestTask,
    ) -> NodeStatus:
        original_plan = blackboard.macro_plan
        original_step_index = blackboard.current_step_index
        original_prompt = blackboard.prompt
        self._last_temporary_chest_task_completed = False
        try:
            blackboard.macro_plan = [chest_task]
            blackboard.current_step_index = 0
            status = await self.chest_node.run(blackboard, context)
            self._last_temporary_chest_task_completed = blackboard.current_step_index >= 1
            if self._last_temporary_chest_task_completed:
                return "SUCCESS"
            if status == "RUNNING":
                return "RUNNING"
            return status
        finally:
            temporary_prompt = blackboard.prompt
            blackboard.macro_plan = original_plan
            blackboard.current_step_index = original_step_index
            if temporary_prompt != original_prompt and not self._last_temporary_chest_task_completed:
                blackboard.prompt = temporary_prompt
            else:
                blackboard.prompt = original_prompt

    def _run_discard_fallback(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        transfer_candidate: InventoryTransferCandidate,
    ) -> NodeStatus:
        if self._discard_candidate is None:
            self._discard_candidate = transfer_candidate
            self._log(
                f"当前场景没有可用箱子，进入丢弃兜底: item={transfer_candidate.item_name}, "
                f"qualified_item_id={transfer_candidate.qualified_item_id}, count={transfer_candidate.count}, "
                f"index={transfer_candidate.index}, reason={transfer_candidate.reason}"
            )

        response = context.executor_client.send_command(
            StardewCommand(
                action=StardewAction.DISCARD_INVENTORY_ITEM,
                item_name=self._discard_candidate.item_name,
                qualified_item_id=self._discard_candidate.qualified_item_id,
                count=self._discard_candidate.count,
            )
        )
        result = self._parse_discard_response(response)
        if result.get("status") != "SUCCESS":
            self._mark_recovery_failed(blackboard, f"丢弃任务无关物品失败: response={response}")
            self._reset()
            return "FAILURE"

        self._register_agent_dropped_item_skip(blackboard, game_state, self._discard_candidate)
        print(
            f"\n🟢 [InventoryRecoveryNode] 已丢弃任务无关物品: "
            f"{self._discard_candidate.item_name} x{result.get('discarded_count', self._discard_candidate.count)}"
        )
        self._complete_recovery(blackboard)
        self._reset()
        return "SUCCESS"

    def _resolve_nearest_chest_tile(self, context: PlayerContext, game_state: StardewState) -> Tile | None:
        location_name = str(game_state.location_name)
        if not self._is_supported_location(location_name):
            self._log(f"当前场景不在 Location 协议内，跳过自动查箱: location={location_name}")
            return None

        cached_chests = context.map_knowledge_cache.get_chest_locations(game_state.location_name)
        if cached_chests:
            nearest_cached_chest = self._sort_chest_tiles_by_distance(
                [chest.tile for chest in cached_chests],
                game_state.player_tile,
            )[0]
            self._log(f"从 MapKnowledgeCache 选择最近箱子: chest={nearest_cached_chest}, cached_count={len(cached_chests)}")
            return nearest_cached_chest

        queried_chests = self.chest_knowledge_service.query_chests(context, game_state.location_name)
        if not queried_chests:
            self._log(f"当前场景没有查询到箱子: location={game_state.location_name}")
            return None

        nearest_queried_chest = self._sort_chest_tiles_by_distance(
            [chest.tile for chest in queried_chests],
            game_state.player_tile,
        )[0]
        self._log(f"低频 QUERY_CHESTS 后选择最近箱子: chest={nearest_queried_chest}, count={len(queried_chests)}")
        return nearest_queried_chest

    def _is_supported_location(self, location_name: str) -> bool:
        return location_name in set(LOCATIONS)

    def _build_store_item_requests(self, transfer_candidates: list[InventoryTransferCandidate]) -> list[ChestItemRequest]:
        return [
            ChestItemRequest(
                item_name=candidate.item_name,
                qualified_item_id=candidate.qualified_item_id,
                count=candidate.count,
            )
            for candidate in transfer_candidates[:MAX_STORE_ITEM_TYPES_PER_RECOVERY]
            if candidate.item_name and candidate.count > 0
        ]

    def _register_agent_dropped_item_skip(
        self,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        transfer_candidate: InventoryTransferCandidate,
    ) -> None:
        item_key = (
            f"qid:{transfer_candidate.qualified_item_id}"
            if transfer_candidate.qualified_item_id
            else f"name:{transfer_candidate.item_name}"
        )
        blackboard.unreceivable_loot_records.append(
            UnreceivableLootRecord(
                owner=None,
                location_name=str(game_state.location_name),
                source_tile=None,
                source_type=None,
                item_key=item_key,
                inventory_signature=(),
                expires_at=time.time() + 8.0,
                reason="Agent 主动丢弃的任务无关物品，短期忽略避免重新捡回",
            )
        )
        self._log(f"登记 Agent 主动丢弃物短期忽略: item_key={item_key}, location={game_state.location_name}")

    def _complete_recovery(self, blackboard: AgentBlackboard) -> None:
        residual_record = blackboard.collect_loot_residual_record
        if residual_record is not None:
            residual_record.inventory_recovery_departure_path = list(blackboard.inventory_recovery_departure_path)
        self._log(
            f"背包恢复完成，交回 CollectLoot 继续处理原掉落物请求: "
            f"departure_path={self._format_tile_list(blackboard.inventory_recovery_departure_path)}"
        )
        blackboard.inventory_check_failed = False
        blackboard.inventory_risk_level = None
        blackboard.inventory_failure_reason = None
        blackboard.inventory_recovery_hint = None
        blackboard.inventory_recovery_strategy = None
        blackboard.inventory_recovery_context = {}
        blackboard.inventory_recovery_task = None
        blackboard.inventory_discard_candidates = []
        blackboard.collect_loot_resume_after_inventory_recovery = True

    def _mark_recovery_failed(self, blackboard: AgentBlackboard, reason: str) -> None:
        blackboard.inventory_recovery_context = {
            **blackboard.inventory_recovery_context,
            "recovery_failed": True,
            "recovery_failed_reason": reason,
        }
        self._log(f"背包恢复失败: {reason}, context={blackboard.inventory_recovery_context}")
        print(f"\n🟡 [InventoryRecoveryNode] 背包恢复失败，交回 CollectLoot 跳过当前不可接收掉落物: {reason}")

    def _get_current_recovery_task(self, blackboard: AgentBlackboard) -> BaseTask | None:
        if blackboard.inventory_recovery_task is not None:
            return blackboard.inventory_recovery_task
        if blackboard.macro_plan and blackboard.current_step_index < len(blackboard.macro_plan):
            return blackboard.macro_plan[blackboard.current_step_index]
        return None

    def _parse_discard_response(self, response: str | None) -> dict:
        if response is None:
            return {"status": "FAILURE", "reason": "NO_RESPONSE"}
        try:
            response_data = json.loads(response)
        except json.JSONDecodeError:
            return {"status": "FAILURE", "reason": response}
        if not isinstance(response_data, dict):
            return {"status": "FAILURE", "reason": "INVALID_RESPONSE"}
        return response_data

    def _sort_chest_tiles_by_distance(self, tiles: list[Tile], player_tile: Tile) -> list[Tile]:
        return sorted(tiles, key=lambda tile: (abs(tile.x - player_tile.x) + abs(tile.y - player_tile.y), tile.x, tile.y))

    def _format_item_requests(self, item_requests: list[ChestItemRequest]) -> str:
        return ", ".join(f"{item.item_name}({item.qualified_item_id or 'name'}):{item.count}" for item in item_requests)

    def _format_task_context(self, current_task: BaseTask | None) -> str:
        task_context: InventoryTaskContext = self.task_inventory_policy.build_context(current_task)
        return (
            f"task_type={getattr(current_task, 'task_type', None)}, "
            f"required={sorted(task_context.required_item_names)}, "
            f"protected_qids={sorted(task_context.protected_qualified_item_ids)}, "
            f"expected_drops={sorted(task_context.expected_stackable_drop_qualified_item_ids)}"
        )

    def _format_tile_list(self, tiles: list[Tile]) -> str:
        return "[" + ", ".join(f"({tile.x}, {tile.y})" for tile in tiles) + "]"

    def _build_inventory_signature(self, game_state: StardewState) -> tuple[tuple[str, int], ...]:
        counts: dict[str, int] = {}
        for item in game_state.inventory.items:
            qualified_item_id = str(getattr(item, "qualified_item_id", "") or "").strip()
            item_name = str(getattr(item, "name", "") or "").strip()
            stack = int(getattr(item, "stack", 0) or 0)
            key = f"qid:{qualified_item_id}" if qualified_item_id else f"name:{item_name}"
            counts[key] = counts.get(key, 0) + max(stack, 1)
        return tuple(sorted(counts.items()))

    def _log(self, message: str) -> None:
        self.debug_logger.log(f"[InventoryRecoveryNode] {message}")

    def _reset(self) -> None:
        self._started_at = None
        self._chest_task = None
        self._discard_candidate = None
        self._last_temporary_chest_task_completed = False
        self.chest_node._reset()
