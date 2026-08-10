import asyncio
import time
import unittest
from types import SimpleNamespace

from agent.action.inventory.inventory_policy import InventoryPolicy
from agent.action.valley_action.action_type import StardewAction
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.collect_loot_node import CollectLootNode
from server.type import Tile


class FakeDebugLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def log(self, message: str) -> None:
        self.messages.append(message)


class FakePositioningController:
    def reset(self) -> None:
        pass


class FakeExecutorClient:
    def __init__(self) -> None:
        self.commands: list = []

    def send_command(self, command) -> str:
        self.commands.append(command)
        return "SUCCESS"


class CollectLootNodeTest(unittest.TestCase):
    def test_finishes_when_debris_identity_mismatches_but_inventory_gain_exists(self) -> None:
        node = self._build_node()
        blackboard = AgentBlackboard()
        blackboard.require_collect_loot = True
        blackboard.collect_loot_owner = "Mining"
        blackboard.collect_loot_source_tile = Tile(14, 12)
        blackboard.collect_loot_source_type = "Stone"
        blackboard.pending_loot_tiles = []

        node._started_at = time.time()
        node._source_signature = node._build_source_signature(blackboard)
        node._inventory_snapshot = {
            "qid:(O)388": 0,
            "name:Wood": 0,
            "qid:(O)390": 2,
            "name:Stone": 2,
        }
        node._observed_loot_item_keys = {"qid:(O)388"}

        game_state = self._build_game_state(
            inventory_items=[
                self._build_inventory_item("(O)390", "Stone", 3),
            ],
            debris=[],
        )
        context = SimpleNamespace(state=game_state, executor_client=FakeExecutorClient())

        status = asyncio.run(node.run(blackboard, context))

        self.assertEqual(status, "SUCCESS")
        self.assertFalse(blackboard.require_collect_loot)
        self.assertEqual(context.executor_client.commands[-1].action, StardewAction.IDLE)
        self.assertTrue(any("背包增量身份与观察身份不一致" in message for message in node.collect_loot_debug_logger.messages))

    def test_debris_without_qualified_item_id_is_never_collectible(self) -> None:
        node = self._build_node()
        debris = SimpleNamespace(name="RESOURCE", source="RESOURCE", qualified_item_id="", is_collectible=True)

        stone_blackboard = AgentBlackboard()
        stone_blackboard.collect_loot_source_type = "Stone"
        self.assertFalse(node._is_collectible_debris_for_source(stone_blackboard, debris))

        identified_debris = SimpleNamespace(
            name="Wood",
            display_name="木材",
            source="OBJECT",
            qualified_item_id="(O)388",
            is_collectible=True,
        )
        self.assertTrue(node._is_collectible_debris_for_source(stone_blackboard, identified_debris))

        weeds_debris = SimpleNamespace(
            name="Weeds",
            display_name="杂草",
            source="RESOURCE",
            qualified_item_id="(O)0",
            is_collectible=True,
        )
        self.assertFalse(node._is_collectible_debris_for_source(stone_blackboard, weeds_debris))

    def test_relocates_visible_loot_by_item_key_when_original_tile_is_empty(self) -> None:
        node = self._build_node()
        blackboard = AgentBlackboard()
        blackboard.require_collect_loot = True
        blackboard.collect_loot_owner = "Farm"
        blackboard.collect_loot_source_tile = Tile(43, 17)
        blackboard.collect_loot_source_type = "twig"
        blackboard.pending_loot_tiles = [Tile(42, 15)]

        node._observed_loot_item_keys = {"qid:(O)388"}
        game_state = self._build_game_state(
            inventory_items=[],
            debris=[
                self._build_debris(Tile(39, 15), "(O)388", "Wood"),
            ],
        )
        game_state.player_tile = Tile(44, 16)

        node._remove_absent_loot_tiles(blackboard, game_state)

        self.assertEqual(blackboard.pending_loot_tiles, [Tile(39, 15)])
        self.assertTrue(any("按物品身份重定位" in message for message in node.collect_loot_debug_logger.messages))

    def test_tree_loot_candidates_keep_near_magnetic_stand_tiles(self) -> None:
        node = self._build_node()
        blackboard = AgentBlackboard()
        blackboard.collect_loot_owner = "Farm"
        blackboard.collect_loot_source_tile = Tile(43, 15)
        blackboard.collect_loot_source_type = "tree"
        blackboard.pending_loot_tiles = [Tile(38, 15), Tile(37, 15)]

        wood_debris = self._build_debris(Tile(38, 15), "(O)388", "Wood")
        wood_debris.position = SimpleNamespace(x=38 * 64 + 32.0, y=15 * 64 + 32.0)
        far_wood_debris = self._build_debris(Tile(37, 15), "(O)388", "Wood")
        far_wood_debris.position = SimpleNamespace(x=37 * 64 + 32.0, y=15 * 64 + 32.0)
        game_state = self._build_game_state(
            inventory_items=[],
            debris=[wood_debris, far_wood_debris],
        )
        game_state.player_tile = Tile(41, 16)
        game_state.position = SimpleNamespace(x=41 * 64 + 32.0, y=16 * 64 + 32.0)
        game_state.applied_magnetic_radius = 128.0

        candidate_tiles = node._build_collect_candidate_tiles(blackboard, game_state, Tile(38, 15))

        self.assertIn(Tile(40, 16), candidate_tiles)

    def test_tree_loot_candidates_keep_more_stand_tiles_before_reachability_check(self) -> None:
        node = self._build_node()
        blackboard = AgentBlackboard()
        blackboard.collect_loot_owner = "Farm"
        blackboard.collect_loot_source_tile = Tile(43, 15)
        blackboard.collect_loot_source_type = "tree"
        blackboard.pending_loot_tiles = [Tile(37, 15), Tile(39, 14), Tile(39, 16)]

        game_state = self._build_game_state(
            inventory_items=[],
            debris=[
                self._build_debris(Tile(37, 15), "(O)388", "Wood"),
                self._build_debris(Tile(39, 14), "(O)388", "Wood"),
                self._build_debris(Tile(39, 16), "(O)92", "Sap"),
            ],
        )
        game_state.player_tile = Tile(41, 15)
        game_state.position = SimpleNamespace(x=41 * 64 + 32.0, y=15 * 64 + 32.0)
        game_state.applied_magnetic_radius = 128.0

        candidate_tiles = node._build_collect_candidate_tiles(blackboard, game_state, Tile(37, 15))

        self.assertGreater(len(candidate_tiles), 8)

    def test_unreceivable_loot_is_skipped_until_inventory_changes(self) -> None:
        node = self._build_node()
        node.inventory_policy = InventoryPolicy()
        blackboard = AgentBlackboard()
        blackboard.require_collect_loot = True
        blackboard.collect_loot_owner = "Mining"
        blackboard.collect_loot_source_tile = Tile(22, 25)
        blackboard.collect_loot_source_type = "BREAKABLE_CONTAINER"
        blackboard.pending_loot_tiles = [Tile(21, 23)]

        game_state = self._build_game_state(
            inventory_items=[
                self._build_inventory_item("(O)390", "Stone", 999),
            ],
            debris=[
                self._build_debris(Tile(21, 23), "(O)709", "Hardwood"),
            ],
        )
        game_state.location_name = "UndergroundMine2"
        game_state.inventory.free_slots = 0
        game_state.inventory.max_items = 1
        game_state.inventory.occupied_slots = 1
        context = SimpleNamespace(state=game_state, executor_client=FakeExecutorClient())

        first_status = asyncio.run(node.run(blackboard, context))

        self.assertEqual(first_status, "RUNNING")
        self.assertEqual(len(blackboard.unreceivable_loot_records), 1)
        self.assertFalse(blackboard.pending_loot_tiles)
        self.assertTrue(any("登记不可接收掉落物短期跳过" in message for message in node.collect_loot_debug_logger.messages))

        blackboard.pending_loot_tiles = [Tile(21, 23)]
        second_status = asyncio.run(node.run(blackboard, context))

        self.assertEqual(second_status, "SUCCESS")
        self.assertFalse(blackboard.require_collect_loot)
        self.assertTrue(any("不可接收的 pending 掉落物" in message for message in node.collect_loot_debug_logger.messages))

        game_state.inventory.items.append(self._build_inventory_item("(O)709", "Hardwood", 1))
        node._prune_unreceivable_loot_records(blackboard, game_state)

        self.assertFalse(blackboard.unreceivable_loot_records)
        self.assertTrue(any("清理不可接收掉落物短期跳过记录" in message for message in node.collect_loot_debug_logger.messages))

    def _build_node(self) -> CollectLootNode:
        node = CollectLootNode.__new__(CollectLootNode)
        node.positioning_controller = FakePositioningController()
        node.inventory_policy = None
        node.collect_loot_debug_logger = FakeDebugLogger()
        node._started_at = None
        node._target_tile = None
        node._target_started_at = None
        node._swept_loot_tiles = set()
        node._sweep_pass_count = 0
        node._source_signature = None
        node._last_cluster_log_signature = None
        node._inventory_snapshot = {}
        node._observed_loot_item_keys = set()
        return node

    def _build_game_state(self, inventory_items: list, debris: list):
        return SimpleNamespace(
            using_tool=False,
            can_move=True,
            inventory=SimpleNamespace(items=inventory_items),
            debris=debris,
            player_tile=Tile(0, 0),
            position=SimpleNamespace(x=0.0, y=0.0),
            tile_size=64,
            player_size=(48, 32),
            applied_magnetic_radius=64,
        )

    def _build_inventory_item(self, qualified_item_id: str, name: str, stack: int):
        return SimpleNamespace(
            qualified_item_id=qualified_item_id,
            name=name,
            display_name=name,
            stack=stack,
            index=0,
            is_tool=False,
            is_weapon=False,
            maximum_stack_size=999,
        )

    def _build_debris(self, tile: Tile, qualified_item_id: str, name: str):
        return SimpleNamespace(
            tile=tile,
            qualified_item_id=qualified_item_id,
            name=name,
            display_name=name,
            source="OBJECT",
            is_collectible=True,
            position=SimpleNamespace(x=float(tile.x * 64), y=float(tile.y * 64)),
        )


if __name__ == "__main__":
    unittest.main()
