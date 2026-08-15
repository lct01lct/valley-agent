from dataclasses import dataclass

from agent.action.inventory.task_inventory_policy import TaskInventoryPolicy
from agent.base_task import BaseTask
from agent.behavior_tree.chest_node import ChestItemRequest
from agent.memory.map_knowledge_cache import ChestContentItem, ChestContentKnowledge
from server.valley_server import StardewState


@dataclass(frozen=True)
class InventoryChestTakePlan:
    chest_content: ChestContentKnowledge
    item_requests: list[ChestItemRequest]
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
    ) -> InventoryChestTakePlan | None:
        free_slots = int(state.inventory.free_slots or 0)
        if free_slots <= 0:
            return None

        task_context = self.task_inventory_policy.build_context(next_task)
        inventory_item_keys = self._build_inventory_item_keys(state)
        for chest_content in self._sort_chest_contents_by_distance(chest_contents, state):
            item_requests: list[ChestItemRequest] = []
            used_item_keys: set[tuple[str, str]] = set()
            for item in chest_content.items:
                if len(item_requests) >= free_slots:
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
                    reason=f"选择 {len(item_requests)} 个任务无关新物品填充 {free_slots} 个空格",
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
