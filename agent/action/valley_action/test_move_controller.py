import unittest
from types import SimpleNamespace

from agent.action.valley_action.AStar import RouteTile
from agent.action.valley_action.action_type import StardewAction
from agent.action.valley_action.move_controller import MoveController
from server.type import Position, Tile


class MoveControllerTest(unittest.TestCase):
    def test_smooth_long_path_prefers_horizontal_axis_for_straight_segment(self):
        state = SimpleNamespace(
            tile_size=64,
            player_size=(48, 32),
            position=Position(40 * 64 + 32, 16 * 64 + 32),
            player_tile=Tile(40, 16),
        )
        tile_path = [
            RouteTile(40, 16, "walk"),
            RouteTile(41, 16, "walk"),
            RouteTile(42, 16, "walk"),
            RouteTile(43, 16, "walk"),
            RouteTile(44, 16, "walk"),
            RouteTile(45, 16, "walk"),
            RouteTile(46, 16, "walk"),
        ]

        command, next_path_index, is_done = MoveController().get_next_move_command(
            state,
            tile_path,
            0,
            smooth_long_path=True,
            smooth_min_remaining_tiles=3,
            smooth_lookahead_tiles=6,
        )

        self.assertEqual(command.action, StardewAction.MOVE_RIGHT)
        self.assertEqual(next_path_index, 1)
        self.assertFalse(is_done)

    def test_smooth_long_path_does_not_flatten_detour_around_obstacle(self):
        state = SimpleNamespace(
            tile_size=64,
            player_size=(48, 32),
            position=Position(64 * 64 + 32, 17 * 64 + 32),
            player_tile=Tile(64, 17),
        )
        tile_path = [
            RouteTile(65, 17, "walk"),
            RouteTile(64, 17, "walk"),
            RouteTile(63, 18, "walk"),
            RouteTile(62, 18, "walk"),
            RouteTile(61, 18, "walk"),
            RouteTile(60, 18, "walk"),
            RouteTile(59, 17, "walk"),
        ]

        command, next_path_index, is_done = MoveController().get_next_move_command(
            state,
            tile_path,
            1,
            smooth_long_path=True,
            smooth_min_remaining_tiles=3,
            smooth_lookahead_tiles=5,
        )

        self.assertEqual(command.action, StardewAction.MOVE_DOWN_LEFT)
        self.assertEqual(next_path_index, 2)
        self.assertFalse(is_done)


if __name__ == "__main__":
    unittest.main()
