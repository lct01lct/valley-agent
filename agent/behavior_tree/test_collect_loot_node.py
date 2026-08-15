import asyncio
import time
import unittest
from types import SimpleNamespace

from agent.action.inventory.inventory_policy import InventoryPolicy
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.behavior_tree.blackboard import AgentBlackboard, UnreceivableLootRecord
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


class FakeLongPathPositioningController:
    def __init__(self) -> None:
        self.reset_count = 0

    def reset(self) -> None:
        self.reset_count += 1

    def tick(self, _game_state, _goal):
        return SimpleNamespace(
            status="MOVING",
            command=StardewCommand(action=StardewAction.MOVE_RIGHT, key=["d"]),
            stand_tile=Tile(20, 0),
            reason=None,
        )

    def get_current_path_length(self) -> int:
        return 125

    def get_debug_snapshot(self) -> str:
        return "fake_long_path"


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

    def test_collect_loot_does_not_treat_grass_as_extra_blocked_tile(self) -> None:
        node = self._build_node()
        game_state = self._build_game_state(inventory_items=[], debris=[])
        game_state.player_tile = Tile(10, 10)
        grass_tile = Tile(11, 10)
        weeds_tile = Tile(12, 10)
        game_state.layers = {
            "Grass": {grass_tile},
            "Weeds": {weeds_tile},
        }

        extra_blocked_tiles = node._build_collect_extra_blocked_tiles(game_state)

        self.assertNotIn(grass_tile, extra_blocked_tiles)
        self.assertIn(weeds_tile, extra_blocked_tiles)

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

        self.assertEqual(first_status, "FAILURE")
        self.assertTrue(blackboard.inventory_check_failed)
        self.assertEqual(blackboard.inventory_failure_reason, "INVENTORY_FULL_WHILE_COLLECTING")
        self.assertEqual(len(blackboard.unreceivable_loot_records), 1)
        self.assertEqual(blackboard.pending_loot_tiles, [Tile(21, 23)])
        self.assertTrue(any("登记不可接收掉落物短期跳过" in message for message in node.collect_loot_debug_logger.messages))

        second_status = asyncio.run(node.run(blackboard, context))

        self.assertEqual(second_status, "FAILURE")
        self.assertTrue(blackboard.inventory_check_failed)
        self.assertTrue(any("背包恢复请求处理中" in message for message in node.collect_loot_debug_logger.messages))

        game_state.inventory.items.append(self._build_inventory_item("(O)709", "Hardwood", 1))
        node._prune_unreceivable_loot_records(blackboard, game_state)

        self.assertFalse(blackboard.unreceivable_loot_records)
        self.assertTrue(any("清理不可接收掉落物短期跳过记录" in message for message in node.collect_loot_debug_logger.messages))

    def test_global_agent_dropped_loot_skip_survives_inventory_signature_change_until_expired(self) -> None:
        node = self._build_node()
        blackboard = AgentBlackboard()
        blackboard.collect_loot_owner = "Mining"
        blackboard.collect_loot_source_tile = Tile(3, 3)
        blackboard.collect_loot_source_type = "Stone"
        game_state = self._build_game_state(
            inventory_items=[self._build_inventory_item("(O)390", "Stone", 1)],
            debris=[],
        )
        game_state.location_name = "Farm"
        blackboard.unreceivable_loot_records.append(
            UnreceivableLootRecord(
                owner=None,
                location_name="Farm",
                source_tile=None,
                source_type=None,
                item_key="qid:(O)92",
                inventory_signature=(),
                expires_at=time.time() + 5.0,
                reason="Agent 主动丢弃",
            )
        )

        game_state.inventory.items.append(self._build_inventory_item("(O)388", "Wood", 1))
        node._prune_unreceivable_loot_records(blackboard, game_state)

        self.assertEqual(len(blackboard.unreceivable_loot_records), 1)
        self.assertTrue(
            node._is_unreceivable_loot_skipped(
                blackboard,
                game_state,
                self._build_debris(Tile(4, 4), "(O)92", "Sap"),
            )
        )

    def test_selects_stackable_loot_before_unreceivable_loot_when_inventory_is_full(self) -> None:
        node = self._build_node()
        node.inventory_policy = InventoryPolicy()
        blackboard = AgentBlackboard()
        blackboard.collect_loot_owner = "Farm"
        blackboard.collect_loot_source_tile = Tile(0, 0)
        blackboard.collect_loot_source_type = "tree"
        blackboard.pending_loot_tiles = [Tile(1, 0), Tile(2, 0)]

        game_state = self._build_game_state(
            inventory_items=[self._build_inventory_item("(O)388", "Wood", 1)],
            debris=[
                self._build_debris(Tile(1, 0), "(O)92", "Sap"),
                self._build_debris(Tile(2, 0), "(O)388", "Wood"),
            ],
        )
        game_state.inventory.free_slots = 0
        game_state.inventory.max_items = 1
        game_state.inventory.occupied_slots = 1

        selected_tile = node._select_target_tile(blackboard, game_state)

        self.assertEqual(selected_tile, Tile(2, 0))
        self.assertTrue(any("优先选择当前背包可接收的掉落物" in message for message in node.collect_loot_debug_logger.messages))

    def test_tree_loot_does_not_skip_long_path_when_target_can_stack(self) -> None:
        node = self._build_node()
        node.inventory_policy = InventoryPolicy()
        node.positioning_controller = FakeLongPathPositioningController()
        blackboard = AgentBlackboard()
        blackboard.require_collect_loot = True
        blackboard.collect_loot_owner = "Farm"
        blackboard.collect_loot_source_tile = Tile(0, 0)
        blackboard.collect_loot_source_type = "tree"
        blackboard.pending_loot_tiles = [Tile(2, 0)]

        wood_debris = self._build_debris(Tile(2, 0), "(O)388", "Wood")
        wood_debris.position = SimpleNamespace(x=2 * 64.0, y=0.0)
        game_state = self._build_game_state(
            inventory_items=[self._build_inventory_item("(O)388", "Wood", 1)],
            debris=[wood_debris],
        )
        game_state.inventory.free_slots = 0
        game_state.inventory.max_items = 1
        game_state.inventory.occupied_slots = 1
        game_state.position = SimpleNamespace(x=0.0, y=0.0)
        context = SimpleNamespace(state=game_state, executor_client=FakeExecutorClient())

        status = asyncio.run(node.run(blackboard, context))

        self.assertEqual(status, "RUNNING")
        self.assertEqual(blackboard.pending_loot_tiles, [Tile(2, 0)])
        self.assertFalse(blackboard.skipped_loot_tiles)
        self.assertEqual(context.executor_client.commands[-1].action, StardewAction.MOVE_RIGHT)
        self.assertTrue(any("当前背包可接收" in message for message in node.collect_loot_debug_logger.messages))

    def test_restores_receivable_skipped_loot_after_inventory_recovery(self) -> None:
        node = self._build_node()
        node.inventory_policy = InventoryPolicy()
        blackboard = AgentBlackboard()
        blackboard.collect_loot_owner = "Farm"
        blackboard.collect_loot_source_tile = Tile(43, 15)
        blackboard.collect_loot_source_type = "tree"
        blackboard.pending_loot_tiles = []
        blackboard.skipped_loot_tiles = {(37, 15), (38, 16)}

        game_state = self._build_game_state(
            inventory_items=[self._build_inventory_item("(O)388", "Wood", 10)],
            debris=[
                self._build_debris(Tile(37, 15), "(O)388", "Wood"),
                self._build_debris(Tile(38, 16), "(O)92", "Sap"),
            ],
        )
        game_state.inventory.free_slots = 0
        game_state.inventory.max_items = 1
        game_state.inventory.occupied_slots = 1

        node._restore_receivable_skipped_loot_tiles(blackboard, game_state)

        self.assertEqual(blackboard.pending_loot_tiles, [Tile(37, 15)])
        self.assertNotIn((37, 15), blackboard.skipped_loot_tiles)
        self.assertIn((38, 16), blackboard.skipped_loot_tiles)
        self.assertTrue(any("重新纳入 skipped" in message for message in node.collect_loot_debug_logger.messages))

    def _build_node(self) -> CollectLootNode:
        node = CollectLootNode.__new__(CollectLootNode)
        node.positioning_controller = FakePositioningController()
        node.inventory_recovery_return_move_controller = FakePositioningController()
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
            layers={},
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
