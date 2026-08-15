import unittest
from types import SimpleNamespace

from agent.behavior_tree.chest_node import ChestItemRequest, ChestNode, ChestTask
from agent.memory.map_knowledge_cache import ChestContentItem, ChestContentKnowledge, MapKnowledgeCache
from server.type import Tile


class ChestNodeTest(unittest.TestCase):
    def test_take_search_ignores_items_already_in_inventory(self) -> None:
        node = ChestNode()
        game_state = self._build_state(
            [
                self._build_inventory_item("Pickaxe", "(T)Pickaxe"),
            ]
        )
        context = SimpleNamespace(map_knowledge_cache=MapKnowledgeCache())
        chest_tile = Tile(66, 17)
        context.map_knowledge_cache.remember_chest_content(
            ChestContentKnowledge.create(
                location_name="Farm",
                tile=chest_tile,
                items=[
                    self._build_chest_item("Axe", "(T)Axe", is_tool=True),
                    self._build_chest_item("Hoe", "(T)Hoe", is_tool=True),
                    self._build_chest_item("Scythe", "(W)47", is_tool=True),
                    self._build_chest_item("Watering Can", "(T)WateringCan", is_tool=True),
                ],
                source="QUERY_CHEST_CONTENT",
            )
        )
        chest_task = ChestTask(
            task_type="CHEST",
            desc="确保基础工具",
            chest_action="TAKE",
            target_loc="Farm",
            chest_tile=None,
            items=[
                ChestItemRequest(item_name="Axe", count=1),
                ChestItemRequest(item_name="Hoe", count=1),
                ChestItemRequest(item_name="Pickaxe", count=1),
                ChestItemRequest(item_name="Scythe", count=1),
                ChestItemRequest(item_name="Watering Can", count=1),
            ],
        )

        search_item_requests = node._get_take_search_item_requests(game_state, chest_task)
        cached_chest_tile = node._get_cached_chest_tile_for_items(context, game_state, chest_task)

        self.assertEqual([item.item_name for item in search_item_requests], ["Axe", "Hoe", "Scythe", "Watering Can"])
        self.assertEqual(cached_chest_tile, chest_tile)

    def _build_state(self, items: list):
        return SimpleNamespace(
            player_tile=Tile(65, 18),
            inventory=SimpleNamespace(items=items),
        )

    def _build_inventory_item(self, name: str, qualified_item_id: str):
        return SimpleNamespace(
            name=name,
            display_name=name,
            qualified_item_id=qualified_item_id,
            stack=1,
        )

    def _build_chest_item(
        self,
        name: str,
        qualified_item_id: str,
        *,
        is_tool: bool = False,
    ) -> ChestContentItem:
        return ChestContentItem(
            Name=name,
            DisplayName=name,
            QualifiedItemId=qualified_item_id,
            Stack=1,
            Category=0,
            IsTool=is_tool,
        )


if __name__ == "__main__":
    unittest.main()
