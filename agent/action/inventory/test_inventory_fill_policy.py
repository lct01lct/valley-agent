import unittest
from types import SimpleNamespace

from agent.action.inventory.inventory_fill_policy import InventoryFillPolicy, InventoryGoal
from agent.behavior_tree.farm_node import FarmTask
from agent.memory.map_knowledge_cache import ChestContentItem, ChestContentKnowledge, ChestLocationKnowledge
from server.type import Tile


class InventoryFillPolicyTest(unittest.TestCase):
    def test_fill_inventory_selects_task_irrelevant_new_slot_items(self) -> None:
        policy = InventoryFillPolicy()
        state = self._build_state(
            free_slots=2,
            player_tile=Tile(10, 10),
            items=[
                self._build_inventory_item("Hoe", "(T)Hoe", is_tool=True),
                self._build_inventory_item("Parsnip Seeds", "(O)472"),
            ],
        )
        next_task = FarmTask(
            task_type="FARM",
            desc="测试 Farm 后续任务",
            farm_action="PLANT_AND_WATER",
            target_loc="Farm",
            seed_name="Parsnip Seeds",
            count=1,
        )
        chest_content = self._build_chest_content(
            Tile(11, 10),
            [
                self._build_chest_item("Hoe", "(T)Hoe", is_tool=True),
                self._build_chest_item("Parsnip Seeds", "(O)472"),
                self._build_chest_item("Wood", "(O)388"),
                self._build_chest_item("Clay", "(O)330"),
                self._build_chest_item("Fiber", "(O)771"),
            ],
        )

        take_plan = policy.build_fill_inventory_take_plan(state, [chest_content], next_task)

        self.assertIsNotNone(take_plan)
        self.assertEqual(
            [(item.item_name, item.qualified_item_id, item.count) for item in take_plan.item_requests],
            [("Wood", "(O)388", 1), ("Clay", "(O)330", 1)],
        )

    def test_fill_inventory_goal_can_keep_target_free_slots(self) -> None:
        policy = InventoryFillPolicy()
        state = self._build_state(
            free_slots=3,
            player_tile=Tile(10, 10),
            items=[],
        )
        chest_content = self._build_chest_content(
            Tile(11, 10),
            [
                self._build_chest_item("Wood", "(O)388"),
                self._build_chest_item("Clay", "(O)330"),
                self._build_chest_item("Fiber", "(O)771"),
            ],
        )

        take_plan = policy.build_fill_inventory_take_plan(
            state,
            [chest_content],
            next_task=None,
            goal=InventoryGoal(target_free_slots=1),
        )

        self.assertIsNotNone(take_plan)
        self.assertEqual(
            [(item.item_name, item.qualified_item_id, item.count) for item in take_plan.item_requests],
            [("Wood", "(O)388", 1), ("Clay", "(O)330", 1)],
        )

    def test_empty_chest_respects_new_slot_capacity_but_keeps_stackable_items(self) -> None:
        policy = InventoryFillPolicy()
        state = self._build_state(
            free_slots=1,
            player_tile=Tile(10, 10),
            items=[
                self._build_inventory_item("Stone", "(O)390"),
            ],
        )
        chest_content = self._build_chest_content(
            Tile(11, 10),
            [
                self._build_chest_item("Stone", "(O)390", stack=20),
                self._build_chest_item("Clay", "(O)330", stack=3),
                self._build_chest_item("Coal", "(O)382", stack=5),
            ],
        )

        take_plan = policy.build_empty_chest_take_plan(state, chest_content)

        self.assertIsNotNone(take_plan)
        self.assertEqual(
            [(item.item_name, item.qualified_item_id, item.count) for item in take_plan.item_requests],
            [("Stone", "(O)390", 20), ("Clay", "(O)330", 3)],
        )

    def test_observation_plan_prefers_unknown_chest_when_known_contents_are_not_enough(self) -> None:
        policy = InventoryFillPolicy()
        state = self._build_state(free_slots=1, player_tile=Tile(10, 10), items=[])
        known_chest_content = self._build_chest_content(
            Tile(12, 10),
            [
                self._build_chest_item("Hoe", "(T)Hoe", is_tool=True),
            ],
        )

        observe_plan = policy.build_next_chest_observation_plan(
            state,
            [
                ChestLocationKnowledge.create("Farm", Tile(12, 10), source="QUERY_CHESTS"),
                ChestLocationKnowledge.create("Farm", Tile(11, 10), source="QUERY_CHESTS"),
            ],
            [known_chest_content],
            InventoryGoal(target_free_slots=0),
        )

        self.assertIsNotNone(observe_plan)
        self.assertEqual(observe_plan.chest_tile, Tile(11, 10))

    def _build_state(self, free_slots: int, player_tile: Tile, items: list):
        return SimpleNamespace(
            player_tile=player_tile,
            inventory=SimpleNamespace(
                free_slots=free_slots,
                items=items,
            ),
        )

    def _build_inventory_item(
        self,
        name: str,
        qualified_item_id: str,
        *,
        is_tool: bool = False,
    ):
        return SimpleNamespace(
            name=name,
            display_name=name,
            qualified_item_id=qualified_item_id,
            is_tool=is_tool,
        )

    def _build_chest_content(self, tile: Tile, items: list[ChestContentItem]) -> ChestContentKnowledge:
        return ChestContentKnowledge.create(
            location_name="Farm",
            tile=tile,
            items=items,
            source="QUERY_CHEST_CONTENT",
        )

    def _build_chest_item(
        self,
        name: str,
        qualified_item_id: str,
        *,
        stack: int = 1,
        is_tool: bool = False,
    ) -> ChestContentItem:
        return ChestContentItem(
            Name=name,
            DisplayName=name,
            QualifiedItemId=qualified_item_id,
            Stack=stack,
            Category=0,
            IsTool=is_tool,
        )


if __name__ == "__main__":
    unittest.main()
