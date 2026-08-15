import unittest
from types import MethodType, SimpleNamespace

from agent.action.inventory.inventory_fill_policy import InventoryGoal
from agent.behavior_tree.chest_node import ChestTask
from agent.behavior_tree.inventory_node import InventoryNode, InventoryTask
from agent.memory.map_knowledge_cache import ChestContentItem, ChestContentKnowledge, ChestLocationKnowledge, MapKnowledgeCache
from server.type import Tile


class InventoryNodeTest(unittest.IsolatedAsyncioTestCase):
    async def test_fill_inventory_finishes_active_chest_task_before_free_slot_completion(self) -> None:
        node = InventoryNode()
        node._active_chest_task = ChestTask(
            task_type="CHEST",
            desc="临时取物任务",
            chest_action="TAKE",
            target_loc="Farm",
            chest_tile=Tile(61, 17),
            items=[],
        )
        run_active_calls: list[str] = []

        async def fake_run_active_chest_task(self, context, blackboard, current_task):
            run_active_calls.append(current_task.inventory_action)
            return "RUNNING"

        node._run_active_chest_task = MethodType(fake_run_active_chest_task, node)

        status = await node._run_fill_inventory(
            SimpleNamespace(),
            SimpleNamespace(current_step_index=3),
            self._build_state(free_slots=0),
            InventoryTask(
                task_type="INVENTORY",
                desc="填满背包",
                inventory_action="FILL_INVENTORY",
                target_loc="Farm",
            ),
        )

        self.assertEqual(status, "RUNNING")
        self.assertEqual(run_active_calls, ["FILL_INVENTORY"])

    async def test_reach_inventory_state_reuses_active_chest_task(self) -> None:
        node = InventoryNode()
        node._active_chest_task = ChestTask(
            task_type="CHEST",
            desc="临时取物任务",
            chest_action="TAKE",
            target_loc="Farm",
            chest_tile=Tile(61, 17),
            items=[],
        )
        run_active_calls: list[str] = []

        async def fake_run_active_chest_task(self, context, blackboard, current_task):
            run_active_calls.append(current_task.inventory_action)
            return "RUNNING"

        node._run_active_chest_task = MethodType(fake_run_active_chest_task, node)

        status = await node._run_reach_inventory_state(
            SimpleNamespace(),
            SimpleNamespace(current_step_index=3),
            self._build_state(free_slots=0),
            InventoryTask(
                task_type="INVENTORY",
                desc="达成背包目标状态",
                inventory_action="REACH_INVENTORY_STATE",
                target_loc="Farm",
                goal=InventoryGoal(target_free_slots=0),
            ),
        )

        self.assertEqual(status, "RUNNING")
        self.assertEqual(run_active_calls, ["REACH_INVENTORY_STATE"])

    async def test_reach_inventory_state_observes_unknown_chest_when_known_contents_are_not_enough(self) -> None:
        node = InventoryNode()
        map_knowledge_cache = MapKnowledgeCache()
        map_knowledge_cache.remember_chest_locations(
            "Farm",
            [
                ChestLocationKnowledge.create("Farm", Tile(12, 10), source="QUERY_CHESTS"),
                ChestLocationKnowledge.create("Farm", Tile(11, 10), source="QUERY_CHESTS"),
            ],
        )
        map_knowledge_cache.remember_chest_content(
            ChestContentKnowledge.create(
                "Farm",
                Tile(12, 10),
                [
                    ChestContentItem(
                        Name="Hoe",
                        DisplayName="Hoe",
                        QualifiedItemId="(T)Hoe",
                        Stack=1,
                        Category=0,
                        IsTool=True,
                    )
                ],
                source="QUERY_CHEST_CONTENT",
            )
        )
        run_active_tasks: list[ChestTask] = []

        async def fake_run_active_chest_task(self, context, blackboard, current_task):
            run_active_tasks.append(self._active_chest_task)
            return "RUNNING"

        node._run_active_chest_task = MethodType(fake_run_active_chest_task, node)

        status = await node._run_reach_inventory_state(
            SimpleNamespace(map_knowledge_cache=map_knowledge_cache),
            SimpleNamespace(current_step_index=3, macro_plan=[]),
            self._build_state(free_slots=1),
            InventoryTask(
                task_type="INVENTORY",
                desc="达成背包目标状态",
                inventory_action="REACH_INVENTORY_STATE",
                target_loc="Farm",
                goal=InventoryGoal(target_free_slots=0),
            ),
        )

        self.assertEqual(status, "RUNNING")
        self.assertEqual(len(run_active_tasks), 1)
        self.assertEqual(run_active_tasks[0].chest_action, "QUERY")
        self.assertEqual(run_active_tasks[0].chest_tile, Tile(11, 10))

    def _build_state(self, free_slots: int):
        return SimpleNamespace(
            location_name="Farm",
            inventory=SimpleNamespace(
                free_slots=free_slots,
                occupied_slots=12 - free_slots,
                max_items=12,
                items=[],
            ),
            player_tile=Tile(10, 10),
        )


if __name__ == "__main__":
    unittest.main()
