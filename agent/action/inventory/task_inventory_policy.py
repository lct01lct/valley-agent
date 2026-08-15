from dataclasses import dataclass

from agent.base_task import BaseTask
from server.valley_server import InventoryItem, StardewState


COMMON_TOOL_NAMES = {
    "Axe",
    "Hoe",
    "Pickaxe",
    "Scythe",
    "Watering Can",
}

MINING_EXPECTED_DROP_QUALIFIED_ITEM_IDS = {
    "(O)378",  # Copper Ore，铜矿石
    "(O)380",  # Iron Ore，铁矿石
    "(O)384",  # Gold Ore，金矿石
    "(O)386",  # Iridium Ore，铱矿石
    "(O)390",  # Stone，石头
    "(O)382",  # Coal，煤炭
    "(O)535",  # Geode，晶球
    "(O)536",  # Frozen Geode，冰封晶球
    "(O)537",  # Magma Geode，岩浆晶球
    "(O)60",  # Emerald，绿宝石
    "(O)62",  # Aquamarine，海蓝宝石
    "(O)64",  # Ruby，红宝石
    "(O)66",  # Amethyst，紫水晶
    "(O)68",  # Topaz，黄水晶
    "(O)70",  # Jade，翡翠
    "(O)72",  # Diamond，钻石
    "(O)80",  # Quartz，石英
    "(O)82",  # Fire Quartz，火水晶
    "(O)84",  # Frozen Tear，泪晶
    "(O)86",  # Earth Crystal，地晶
}

FARM_EXPECTED_DROP_QUALIFIED_ITEM_IDS = {
    "(O)92",  # Sap，树液
    "(O)388",  # Wood，木材
    "(O)390",  # Stone，石头
    "(O)771",  # Fiber，纤维
    "(O)770",  # Mixed Seeds，混合种子
    "(O)309",  # Acorn，橡子
    "(O)310",  # Maple Seed，枫树种子
    "(O)311",  # Pine Cone，松果
    "(O)292",  # Mahogany Seed，桃花心木种子
}

ROUTE_EXPECTED_DROP_QUALIFIED_ITEM_IDS = {
    "(O)92",  # Sap，树液
    "(O)388",  # Wood，木材
    "(O)390",  # Stone，石头
    "(O)771",  # Fiber，纤维
    "(O)770",  # Mixed Seeds，混合种子
}


@dataclass(frozen=True)
class InventoryTaskContext:
    required_item_names: set[str]
    protected_qualified_item_ids: set[str]
    expected_stackable_drop_qualified_item_ids: set[str]


@dataclass(frozen=True)
class InventoryTransferCandidate:
    item_name: str
    qualified_item_id: str | None
    count: int
    index: int
    reason: str


class TaskInventoryPolicy:
    """
    根据当前宏观任务判断哪些背包物品与任务相关。

    这层只做策略判断，不发送命令、不读写黑板。第一版采用集中规则表；
    未来可以由 Planner / AI 为任务注入 expected drops 或 protected items。
    """

    def build_context(self, current_task: BaseTask | None) -> InventoryTaskContext:
        task_type = getattr(current_task, "task_type", None)
        required_item_names = set(COMMON_TOOL_NAMES)
        protected_qualified_item_ids: set[str] = set()
        expected_stackable_drop_qualified_item_ids: set[str] = set()

        if task_type == "MINE":
            required_item_names.update({"Pickaxe", "Sword"})
            expected_stackable_drop_qualified_item_ids.update(MINING_EXPECTED_DROP_QUALIFIED_ITEM_IDS)
        elif task_type == "FARM":
            required_item_names.update({"Axe", "Hoe", "Pickaxe", "Scythe", "Watering Can"})
            seed_name = str(getattr(current_task, "seed_name", "") or "").strip()
            if seed_name:
                required_item_names.add(seed_name)
            expected_stackable_drop_qualified_item_ids.update(FARM_EXPECTED_DROP_QUALIFIED_ITEM_IDS)
        elif task_type == "ROUTE":
            required_item_names.update({"Axe", "Pickaxe", "Scythe", "Sword"})
            expected_stackable_drop_qualified_item_ids.update(ROUTE_EXPECTED_DROP_QUALIFIED_ITEM_IDS)
        elif task_type == "CHEST":
            for item_request in getattr(current_task, "items", []) or []:
                item_name = str(getattr(item_request, "item_name", "") or "").strip()
                if item_name:
                    required_item_names.add(item_name)
                qualified_item_id = str(getattr(item_request, "qualified_item_id", "") or "").strip()
                if qualified_item_id:
                    protected_qualified_item_ids.add(qualified_item_id)

        return InventoryTaskContext(
            required_item_names=required_item_names,
            protected_qualified_item_ids=protected_qualified_item_ids,
            expected_stackable_drop_qualified_item_ids=expected_stackable_drop_qualified_item_ids,
        )

    def find_task_irrelevant_transfer_candidates(
        self,
        state: StardewState,
        current_task: BaseTask | None,
    ) -> list[InventoryTransferCandidate]:
        task_context = self.build_context(current_task)
        candidates: list[InventoryTransferCandidate] = []
        for item in state.inventory.items:
            if self.should_keep_item(item, task_context):
                continue
            candidates.append(
                InventoryTransferCandidate(
                    item_name=item.name or item.display_name,
                    qualified_item_id=item.qualified_item_id or None,
                    count=max(item.stack, 1),
                    index=item.index,
                    reason="非工具、非武器、非当前任务必需物，也不是当前任务预期继续产生的可堆叠掉落物",
                )
            )
        return candidates

    def should_keep_item(self, item: InventoryItem, task_context: InventoryTaskContext) -> bool:
        if item.is_tool or item.is_weapon:
            return True
        if item.name in task_context.required_item_names or item.display_name in task_context.required_item_names:
            return True
        if item.qualified_item_id in task_context.protected_qualified_item_ids:
            return True
        if item.qualified_item_id in task_context.expected_stackable_drop_qualified_item_ids:
            return True
        return False
