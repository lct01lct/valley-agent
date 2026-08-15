from dataclasses import dataclass
from typing import Literal

from agent.action.inventory.task_inventory_policy import TaskInventoryPolicy
from agent.base_task import BaseTask
from agent.behavior_tree.chest_node import ChestItemRequest
from agent.memory.map_knowledge_cache import ChestContentItem, ChestContentKnowledge, ChestLocationKnowledge
from server.valley_server import StardewState
from server.type import Tile

type InventoryItemPolicy = Literal[
    "TASK_IRRELEVANT_ITEMS",  # 选择与当前/后续任务无关、非工具、非受保护物品，用于测试或准备状态
]

type InventorySourcePolicy = Literal[
    "KNOWN_CHESTS",  # 只使用运行期已观察或已缓存的箱子内容
    "OBSERVED_CHESTS",  # 缓存不足时允许通过交互式开箱观察当前场景箱子
]


@dataclass(frozen=True)
class InventoryGoal:
    """
    背包目标状态。

    这层表达“希望背包变成什么样”，不表达具体打开哪个箱子、拿什么物品。
    未来 Planner / LLM 应优先生成 InventoryGoal，再由策略层求解为 ChestTask。
    """

    target_free_slots: int | None = None
    preserve_required_items: bool = True
    item_policy: InventoryItemPolicy = "TASK_IRRELEVANT_ITEMS"
    allowed_sources: tuple[InventorySourcePolicy, ...] = ("KNOWN_CHESTS", "OBSERVED_CHESTS")
    allow_stale_chest_cache: bool = True

    @classmethod
    def fill_inventory(cls) -> "InventoryGoal":
        return cls(target_free_slots=0)


@dataclass(frozen=True)
class InventoryChestTakePlan:
    chest_content: ChestContentKnowledge
    item_requests: list[ChestItemRequest]
    reason: str


@dataclass(frozen=True)
class InventoryChestObservationPlan:
    chest_tile: Tile
    reason: str


class InventoryFillPolicy:
    """
    背包目标状态的候选物品选择策略。

    这层只根据已观察到的背包 state 和箱子内容缓存选择“可以拿什么”，不移动、不开箱、不发命令。
    """

    def __init__(self) -> None:
        self.task_inventory_policy = TaskInventoryPolicy()

    def build_fill_inventory_take_plan(
        self,
        state: StardewState,
        chest_contents: list[ChestContentKnowledge],
        next_task: BaseTask | None,
        goal: InventoryGoal | None = None,
    ) -> InventoryChestTakePlan | None:
        goal = goal or InventoryGoal.fill_inventory()
        free_slots = int(state.inventory.free_slots or 0)
        target_free_slots = max(0, int(goal.target_free_slots or 0))
        slots_to_fill = max(0, free_slots - target_free_slots)
        if slots_to_fill <= 0:
            return None
        if goal.item_policy != "TASK_IRRELEVANT_ITEMS":
            return None

        task_context = self.task_inventory_policy.build_context(next_task if goal.preserve_required_items else None)
        inventory_item_keys = self._build_inventory_item_keys(state)
        for chest_content in self._sort_chest_contents_by_distance(chest_contents, state):
            item_requests: list[ChestItemRequest] = []
            used_item_keys: set[tuple[str, str]] = set()
            for item in chest_content.items:
                if len(item_requests) >= slots_to_fill:
                    break
                if not self._is_fill_candidate(item, task_context.required_item_names, task_context.protected_qualified_item_ids):
                    continue
                item_key = self._build_chest_item_key(item)
                if item_key in inventory_item_keys or item_key in used_item_keys:
                    continue
                item_requests.append(self._build_item_request(item, count=1))
                used_item_keys.add(item_key)

            if item_requests:
                return InventoryChestTakePlan(
                    chest_content=chest_content,
                    item_requests=item_requests,
                    reason=(
                        f"选择 {len(item_requests)} 个任务无关新物品，"
                        f"将空格从 {free_slots} 降到目标 {target_free_slots}"
                    ),
                )

        return None

    def build_empty_chest_take_plan(
        self,
        state: StardewState,
        chest_content: ChestContentKnowledge,
    ) -> InventoryChestTakePlan | None:
        free_slots = int(state.inventory.free_slots or 0)
        if free_slots <= 0 or not chest_content.items:
            return None

        inventory_item_keys = self._build_inventory_item_keys(state)
        item_requests: list[ChestItemRequest] = []
        reserved_new_slots = 0
        for item in chest_content.items:
            if self._is_empty_item(item):
                continue
            item_key = self._build_chest_item_key(item)
            needs_new_slot = item_key not in inventory_item_keys
            if needs_new_slot:
                if reserved_new_slots >= free_slots:
                    continue
                reserved_new_slots += 1

            item_requests.append(self._build_item_request(item, count=max(item.Stack, 1)))

        if not item_requests:
            return None

        return InventoryChestTakePlan(
            chest_content=chest_content,
            item_requests=item_requests,
            reason=(
                f"按背包剩余容量从箱子取物: free_slots={free_slots}, "
                f"new_slots={reserved_new_slots}, item_types={len(item_requests)}"
            ),
        )

    def build_next_chest_observation_plan(
        self,
        state: StardewState,
        chest_locations: list[ChestLocationKnowledge],
        chest_contents: list[ChestContentKnowledge],
        goal: InventoryGoal,
        observed_chest_tiles: set[tuple[int, int]] | None = None,
    ) -> InventoryChestObservationPlan | None:
        if "OBSERVED_CHESTS" not in goal.allowed_sources:
            return None

        observed_chest_tiles = observed_chest_tiles or set()
        content_by_tile = {
            (chest_content.tile.x, chest_content.tile.y): chest_content for chest_content in chest_contents
        }
        unknown_tiles: list[Tile] = []
        stale_tiles: list[Tile] = []
        for chest_location in chest_locations:
            chest_tile = chest_location.tile
            chest_key = (chest_tile.x, chest_tile.y)
            if chest_key in observed_chest_tiles:
                continue

            chest_content = content_by_tile.get(chest_key)
            if chest_content is None:
                unknown_tiles.append(chest_tile)
                continue
            if chest_content.is_stale:
                stale_tiles.append(chest_tile)

        sorted_unknown_tiles = self._sort_tiles_by_distance(unknown_tiles, state.player_tile)
        if sorted_unknown_tiles:
            return InventoryChestObservationPlan(
                chest_tile=sorted_unknown_tiles[0],
                reason=f"已知箱子内容不足，优先观察未知箱子: unknown={len(sorted_unknown_tiles)}",
            )

        sorted_stale_tiles = self._sort_tiles_by_distance(stale_tiles, state.player_tile)
        if sorted_stale_tiles:
            return InventoryChestObservationPlan(
                chest_tile=sorted_stale_tiles[0],
                reason=f"已知箱子内容不足，刷新过期箱子内容: stale={len(sorted_stale_tiles)}",
            )

        return None

    def _is_fill_candidate(
        self,
        item: ChestContentItem,
        required_item_names: set[str],
        protected_qualified_item_ids: set[str],
    ) -> bool:
        if self._is_empty_item(item):
            return False
        if item.IsTool:
            return False
        if item.QualifiedItemId in protected_qualified_item_ids:
            return False
        if item.Name in required_item_names or item.DisplayName in required_item_names:
            return False
        return True

    def _is_empty_item(self, item: ChestContentItem) -> bool:
        return not item.Name or not item.DisplayName or not item.QualifiedItemId or item.Stack <= 0

    def _build_item_request(self, item: ChestContentItem, count: int) -> ChestItemRequest:
        return ChestItemRequest(
            item_name=item.Name or item.DisplayName,
            qualified_item_id=item.QualifiedItemId or None,
            count=count,
        )

    def _build_inventory_item_keys(self, state: StardewState) -> set[tuple[str, str]]:
        item_keys: set[tuple[str, str]] = set()
        for item in state.inventory.items:
            qualified_item_id = str(getattr(item, "qualified_item_id", "") or "").strip()
            item_name = str(getattr(item, "name", "") or getattr(item, "display_name", "") or "").strip()
            if not item_name and not qualified_item_id:
                continue
            item_keys.add((qualified_item_id, item_name))
        return item_keys

    def _build_chest_item_key(self, item: ChestContentItem) -> tuple[str, str]:
        return item.QualifiedItemId, item.Name or item.DisplayName

    def _sort_chest_contents_by_distance(
        self,
        chest_contents: list[ChestContentKnowledge],
        state: StardewState,
    ) -> list[ChestContentKnowledge]:
        return sorted(
            chest_contents,
            key=lambda chest_content: (
                abs(chest_content.tile.x - state.player_tile.x) + abs(chest_content.tile.y - state.player_tile.y),
                chest_content.tile.x,
                chest_content.tile.y,
            ),
        )

    def _sort_tiles_by_distance(self, tiles: list[Tile], player_tile: Tile | None) -> list[Tile]:
        return sorted(
            tiles,
            key=lambda tile: (
                abs(tile.x - player_tile.x) + abs(tile.y - player_tile.y) if player_tile is not None else 0,
                tile.x,
                tile.y,
            ),
        )
