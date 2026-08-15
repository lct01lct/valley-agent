import unittest
from types import MethodType, SimpleNamespace

from agent.behavior_tree.chest_node import ChestTask
from agent.behavior_tree.inventory_node import InventoryNode, InventoryTask
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

    def _build_state(self, free_slots: int):
        return SimpleNamespace(
            location_name="Farm",
            inventory=SimpleNamespace(
                free_slots=free_slots,
                occupied_slots=12 - free_slots,
                max_items=12,
            ),
        )


if __name__ == "__main__":
    unittest.main()
