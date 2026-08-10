import asyncio
import unittest
from types import SimpleNamespace

from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.mining_node import MiningTask
from agent.behavior_tree.mining_resource_check_node import MiningResourceCheckNode


class FakeExecutorClient:
    def __init__(self) -> None:
        self.commands: list = []

    def send_command(self, command) -> str:
        self.commands.append(command)
        return "SUCCESS"


class MiningResourceCheckNodeTest(unittest.TestCase):
    def test_full_inventory_does_not_stop_opportunity_mining_before_loot_exists(self) -> None:
        node = MiningResourceCheckNode()
        blackboard = AgentBlackboard()
        blackboard.macro_plan = [
            MiningTask(
                task_type="MINE",
                desc="测试满包时继续冲层",
                mine_action="FIND_NEXT_LEVEL",
                collect_opportunity_resources=True,
            )
        ]
        game_state = SimpleNamespace(
            location_name="UndergroundMine1",
            mine_level=1,
            inventory=SimpleNamespace(
                max_items=2,
                occupied_slots=2,
                free_slots=0,
                items=[
                    self._build_item("Pickaxe", "(T)Pickaxe", 1, is_tool=True, index=0),
                    self._build_item("Stone", "(O)390", 20, maximum_stack_size=999, index=1),
                ],
            ),
        )
        context = SimpleNamespace(state=game_state, executor_client=FakeExecutorClient())

        status = asyncio.run(node.run(blackboard, context))

        self.assertEqual(status, "SUCCESS")
        self.assertIsNone(blackboard.inventory_risk_level)
        self.assertFalse(blackboard.inventory_check_failed)
        self.assertEqual(context.executor_client.commands, [])

    def _build_item(
        self,
        name: str,
        qualified_item_id: str,
        stack: int,
        *,
        is_tool: bool = False,
        index: int = 0,
        maximum_stack_size: int = 1,
    ):
        return SimpleNamespace(
            index=index,
            name=name,
            display_name=name,
            qualified_item_id=qualified_item_id,
            stack=stack,
            maximum_stack_size=maximum_stack_size,
            is_tool=is_tool,
            is_weapon=False,
        )


if __name__ == "__main__":
    unittest.main()
