import unittest

from agent.action.valley_action.action_type import StardewAction
from agent.action.valley_action.positioning_controller import PositioningController, PositioningGoal
from server.type import Tile
from server.valley_server import StardewState


def _build_state(player_tile: Tile) -> StardewState:
    return StardewState(
        {
            "location_name": "Farm",
            "position": [player_tile.x * 64 + 32, player_tile.y * 64 + 32],
            "tile_coordinate": [player_tile.x, player_tile.y],
            "tile_size": 64,
            "map_size": [10, 10],
            "obstacles": [],
        }
    )


class PositioningControllerTest(unittest.TestCase):
    def test_arriving_at_locked_stand_tile_returns_ready_without_empty_moving_frame(self):
        controller = PositioningController()
        goal = PositioningGoal(
            candidate_stand_tiles={Tile(2, 1)},
            tool_target_tile=None,
            smooth_long_path=False,
        )

        first_result = controller.tick(_build_state(Tile(1, 1)), goal)

        self.assertEqual(first_result.status, "MOVING")
        self.assertIsNotNone(first_result.command)
        self.assertEqual(first_result.command.action, StardewAction.MOVE_RIGHT)
        self.assertEqual(first_result.stand_tile, Tile(2, 1))

        second_result = controller.tick(_build_state(Tile(2, 1)), goal)

        self.assertEqual(second_result.status, "READY")
        self.assertIsNone(second_result.command)
        self.assertEqual(second_result.stand_tile, Tile(2, 1))


if __name__ == "__main__":
    unittest.main()
