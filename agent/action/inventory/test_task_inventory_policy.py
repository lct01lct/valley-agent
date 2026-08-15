import unittest
from types import SimpleNamespace

from agent.action.inventory.task_inventory_policy import TaskInventoryPolicy
from agent.behavior_tree.farm_node import FarmTask
from agent.behavior_tree.mining_node import MiningTask


class TaskInventoryPolicyTest(unittest.TestCase):
    def test_mining_keeps_tools_and_expected_drops_but_stores_task_irrelevant_items(self) -> None:
        policy = TaskInventoryPolicy()
        state = self._build_state(
            [
                self._build_item("Pickaxe", "(T)Pickaxe", 1, is_tool=True, index=0),
                self._build_item("Stone", "(O)390", 20, index=1),
                self._build_item("Copper Ore", "(O)378", 5, index=2),
                self._build_item("Parsnip Seeds", "(O)472", 15, index=3),
            ]
        )
        mining_task = MiningTask(
            task_type="MINE",
            desc="测试 Mining 背包整理",
            mine_action="FIND_NEXT_LEVEL",
            collect_opportunity_resources=True,
        )

        candidates = policy.find_task_irrelevant_transfer_candidates(state, mining_task)

        self.assertEqual([candidate.item_name for candidate in candidates], ["Parsnip Seeds"])

    def test_farm_keeps_seed_and_expected_tree_drops_but_stores_unrelated_ore(self) -> None:
        policy = TaskInventoryPolicy()
        state = self._build_state(
            [
                self._build_item("Hoe", "(T)Hoe", 1, is_tool=True, index=0),
                self._build_item("Parsnip Seeds", "(O)472", 10, index=1),
                self._build_item("Wood", "(O)388", 30, index=2),
                self._build_item("Copper Ore", "(O)378", 5, index=3),
            ]
        )
        farm_task = FarmTask(
            task_type="FARM",
            desc="测试 Farm 背包整理",
            farm_action="PLANT_AND_WATER",
            target_loc="Farm",
            seed_name="Parsnip Seeds",
            count=1,
        )

        candidates = policy.find_task_irrelevant_transfer_candidates(state, farm_task)

        self.assertEqual([candidate.item_name for candidate in candidates], ["Copper Ore"])

    def _build_state(self, items: list):
        return SimpleNamespace(inventory=SimpleNamespace(items=items))

    def _build_item(
        self,
        name: str,
        qualified_item_id: str,
        stack: int,
        *,
        is_tool: bool = False,
        is_weapon: bool = False,
        index: int = 0,
    ):
        return SimpleNamespace(
            index=index,
            name=name,
            display_name=name,
            qualified_item_id=qualified_item_id,
            stack=stack,
            is_tool=is_tool,
            is_weapon=is_weapon,
        )


if __name__ == "__main__":
    unittest.main()
