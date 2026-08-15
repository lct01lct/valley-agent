from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.action.valley_action.AStar import RouteTile
from server.valley_server import StardewState
from server.type import Tile


class MoveController:
    """
    高频局部移动控制器。

    A* 只负责产出 tile_path；这里负责每 tick 根据玩家身体盒和下一个 tile
    输出连续移动命令，并在到达 tile 后立即推进到下一个 tile，不额外 IDLE 一帧。
    """

    def __init__(self):
        self._last_primary_axis: str | None = None
        self._last_target_edge_debug: str = ""
        self._last_path_follow_debug: str = ""

    def get_next_move_command(
        self,
        state: StardewState,
        tile_path: list[RouteTile],
        path_index: int,
        smooth_long_path: bool = False,
        smooth_min_remaining_tiles: int = 6,
        smooth_lookahead_tiles: int = 5,
    ) -> tuple[StardewCommand, int, bool]:
        if not tile_path or path_index >= len(tile_path):
            self._last_path_follow_debug = "mode=empty_path"
            return StardewCommand(action=StardewAction.IDLE), path_index, True

        next_index = path_index
        while next_index < len(tile_path) and self._should_advance_tile(state, tile_path, next_index):
            next_index += 1

        if next_index >= len(tile_path):
            self._last_path_follow_debug = f"mode=path_done, next_index={next_index}"
            return StardewCommand(action=StardewAction.IDLE), next_index, True

        command = None
        remaining_tiles = len(tile_path) - next_index
        if smooth_long_path and remaining_tiles >= smooth_min_remaining_tiles:
            command = self._build_smooth_long_path_command(
                state,
                tile_path,
                next_index,
                smooth_lookahead_tiles,
            )

        if command is None:
            command = self.build_move_command_to_tile(state, tile_path[next_index])
            self._last_path_follow_debug = (
                f"mode=tile, target={tile_path[next_index]}, next_index={next_index}, "
                f"remaining={remaining_tiles}"
            )

        return command, next_index, False

    def _should_advance_tile(self, state: StardewState, tile_path: list[RouteTile], path_index: int) -> bool:
        target_tile = tile_path[path_index]
        is_final_tile = path_index >= len(tile_path) - 1

        if self.is_player_inside_tile(state, target_tile):
            return True

        if is_final_tile:
            return False

        return state.player_tile == target_tile

    def is_player_inside_tile(self, state: StardewState, tile: Tile, margin: float = 0.0) -> bool:
        tile_size = state.tile_size or 64
        player_width, player_height = state.player_size
        half_width = player_width / 2
        half_height = player_height / 2

        tile_left = tile.x * tile_size
        tile_right = tile_left + tile_size
        tile_top = tile.y * tile_size
        tile_bottom = tile_top + tile_size

        player_left = state.position.x - half_width
        player_right = state.position.x + half_width
        player_top = state.position.y - half_height
        player_bottom = state.position.y + half_height

        return (
            player_left >= tile_left + margin
            and player_right <= tile_right - margin
            and player_top >= tile_top + margin
            and player_bottom <= tile_bottom - margin
        )

    def build_move_command_to_tile(
        self,
        state: StardewState,
        target_tile: Tile,
        edge_dead_zone: float = 0.5,
    ) -> StardewCommand:
        tile_size = state.tile_size or 64
        player_width, player_height = state.player_size
        half_width = player_width / 2
        half_height = player_height / 2

        target_left = target_tile.x * tile_size + half_width
        target_right = (target_tile.x + 1) * tile_size - half_width
        target_top = target_tile.y * tile_size + half_height
        target_bottom = (target_tile.y + 1) * tile_size - half_height

        pressed_keys: set[str] = set()

        if state.position.x < target_left - edge_dead_zone:
            pressed_keys.add("d")
        elif state.position.x > target_right + edge_dead_zone:
            pressed_keys.add("a")

        if state.position.y < target_top - edge_dead_zone:
            pressed_keys.add("s")
        elif state.position.y > target_bottom + edge_dead_zone:
            pressed_keys.add("w")

        if "w" in pressed_keys and "d" in pressed_keys:
            return self._build_diagonal_move_command(StardewAction.MOVE_UP_RIGHT, "w", "d")
        if "w" in pressed_keys and "a" in pressed_keys:
            return self._build_diagonal_move_command(StardewAction.MOVE_UP_LEFT, "w", "a")
        if "s" in pressed_keys and "d" in pressed_keys:
            return self._build_diagonal_move_command(StardewAction.MOVE_DOWN_RIGHT, "s", "d")
        if "s" in pressed_keys and "a" in pressed_keys:
            return self._build_diagonal_move_command(StardewAction.MOVE_DOWN_LEFT, "s", "a")
        if "w" in pressed_keys:
            self._last_primary_axis = "vertical"
            return StardewCommand(action=StardewAction.MOVE_UP, key=["w"])
        if "s" in pressed_keys:
            self._last_primary_axis = "vertical"
            return StardewCommand(action=StardewAction.MOVE_DOWN, key=["s"])
        if "a" in pressed_keys:
            self._last_primary_axis = "horizontal"
            return StardewCommand(action=StardewAction.MOVE_LEFT, key=["a"])
        if "d" in pressed_keys:
            self._last_primary_axis = "horizontal"
            return StardewCommand(action=StardewAction.MOVE_RIGHT, key=["d"])

        return StardewCommand(action=StardewAction.IDLE)

    def _build_smooth_long_path_command(
        self,
        state: StardewState,
        tile_path: list[RouteTile],
        path_index: int,
        lookahead_tiles: int,
    ) -> StardewCommand | None:
        path_segment = tile_path[path_index : path_index + max(2, lookahead_tiles)]
        if len(path_segment) < 2:
            return None

        first_tile = path_segment[0]
        last_tile = path_segment[-1]
        span_x = last_tile.x - first_tile.x
        span_y = last_tile.y - first_tile.y

        horizontal_sign = self._get_monotonic_sign([tile.x for tile in path_segment])
        vertical_sign = self._get_monotonic_sign([tile.y for tile in path_segment])
        horizontal_extent = max(tile.x for tile in path_segment) - min(tile.x for tile in path_segment)
        vertical_extent = max(tile.y for tile in path_segment) - min(tile.y for tile in path_segment)

        if (
            horizontal_sign != 0
            and horizontal_extent >= 2
            and vertical_extent == 0
            and abs(span_x) >= max(2, abs(span_y) * 2)
        ):
            if not self._is_player_aligned_with_smooth_segment(state, first_tile, "horizontal"):
                self._last_path_follow_debug = (
                    f"mode=smooth_horizontal_wait_alignment, index={path_index}, "
                    f"segment={self._format_path_segment(path_segment)}, player={state.player_tile}"
                )
                return None

            self._last_primary_axis = "horizontal"
            command = self._build_axis_move_command("horizontal", horizontal_sign)
            self._last_path_follow_debug = (
                f"mode=smooth_horizontal, index={path_index}, segment={self._format_path_segment(path_segment)}, "
                f"extent=({horizontal_extent},{vertical_extent}), span=({span_x},{span_y}), command={command.action}"
            )
            return command

        if (
            vertical_sign != 0
            and vertical_extent >= 2
            and horizontal_extent == 0
            and abs(span_y) >= max(2, abs(span_x) * 2)
        ):
            if not self._is_player_aligned_with_smooth_segment(state, first_tile, "vertical"):
                self._last_path_follow_debug = (
                    f"mode=smooth_vertical_wait_alignment, index={path_index}, "
                    f"segment={self._format_path_segment(path_segment)}, player={state.player_tile}"
                )
                return None

            self._last_primary_axis = "vertical"
            command = self._build_axis_move_command("vertical", vertical_sign)
            self._last_path_follow_debug = (
                f"mode=smooth_vertical, index={path_index}, segment={self._format_path_segment(path_segment)}, "
                f"extent=({horizontal_extent},{vertical_extent}), span=({span_x},{span_y}), command={command.action}"
            )
            return command

        self._last_path_follow_debug = (
            f"mode=smooth_skipped, index={path_index}, segment={self._format_path_segment(path_segment)}, "
            f"extent=({horizontal_extent},{vertical_extent}), span=({span_x},{span_y})"
        )
        return None

    def _is_player_aligned_with_smooth_segment(
        self,
        state: StardewState,
        first_tile: Tile,
        axis: str,
    ) -> bool:
        if axis == "horizontal":
            return state.player_tile.y == first_tile.y
        if axis == "vertical":
            return state.player_tile.x == first_tile.x
        return False

    def is_player_close_to_target_edge(
        self,
        state: StardewState,
        stand_tile: Tile,
        target_tile: Tile,
        edge_margin: float = 2.0,
        edge_dead_zone: float = 4.0,
    ) -> bool:
        return self.build_move_command_to_target_edge(
            state,
            stand_tile,
            target_tile,
            edge_margin=edge_margin,
            edge_dead_zone=edge_dead_zone,
        ).action == StardewAction.IDLE

    def build_move_command_to_target_edge(
        self,
        state: StardewState,
        stand_tile: Tile,
        target_tile: Tile,
        edge_margin: float = 2.0,
        edge_dead_zone: float = 4.0,
    ) -> StardewCommand:
        tile_size = state.tile_size or 64
        player_width, player_height = state.player_size
        half_width = player_width / 2
        half_height = player_height / 2

        stand_left = stand_tile.x * tile_size
        stand_right = stand_left + tile_size
        stand_top = stand_tile.y * tile_size
        stand_bottom = stand_top + tile_size

        min_x = stand_left + half_width + edge_margin
        max_x = stand_right - half_width - edge_margin
        min_y = stand_top + half_height + edge_margin
        max_y = stand_bottom - half_height - edge_margin

        pressed_keys: set[str] = set()
        delta_x = target_tile.x - stand_tile.x
        delta_y = target_tile.y - stand_tile.y

        if delta_x > 0:
            desired_x = max_x
            if state.position.x < desired_x - edge_dead_zone:
                pressed_keys.add("d")
        elif delta_x < 0:
            desired_x = min_x
            if state.position.x > desired_x + edge_dead_zone:
                pressed_keys.add("a")
        elif state.position.x < min_x - edge_dead_zone:
            pressed_keys.add("d")
        elif state.position.x > max_x + edge_dead_zone:
            pressed_keys.add("a")

        if delta_y > 0:
            desired_y = max_y
            if state.position.y < desired_y - edge_dead_zone:
                pressed_keys.add("s")
        elif delta_y < 0:
            desired_y = min_y
            if state.position.y > desired_y + edge_dead_zone:
                pressed_keys.add("w")
        elif state.position.y < min_y - edge_dead_zone:
            pressed_keys.add("s")
        elif state.position.y > max_y + edge_dead_zone:
            pressed_keys.add("w")

        self._last_target_edge_debug = (
            f"stand={stand_tile}, target={target_tile}, "
            f"player_pos=({state.position.x:.1f},{state.position.y:.1f}), "
            f"range_x=({min_x:.1f},{max_x:.1f}), range_y=({min_y:.1f},{max_y:.1f}), "
            f"delta=({delta_x},{delta_y}), edge_margin={edge_margin}, "
            f"edge_dead_zone={edge_dead_zone}, keys={sorted(pressed_keys)}"
        )

        if "w" in pressed_keys and "d" in pressed_keys:
            return self._build_diagonal_move_command(StardewAction.MOVE_UP_RIGHT, "w", "d")
        if "w" in pressed_keys and "a" in pressed_keys:
            return self._build_diagonal_move_command(StardewAction.MOVE_UP_LEFT, "w", "a")
        if "s" in pressed_keys and "d" in pressed_keys:
            return self._build_diagonal_move_command(StardewAction.MOVE_DOWN_RIGHT, "s", "d")
        if "s" in pressed_keys and "a" in pressed_keys:
            return self._build_diagonal_move_command(StardewAction.MOVE_DOWN_LEFT, "s", "a")
        if "w" in pressed_keys:
            self._last_primary_axis = "vertical"
            return StardewCommand(action=StardewAction.MOVE_UP, key=["w"])
        if "s" in pressed_keys:
            self._last_primary_axis = "vertical"
            return StardewCommand(action=StardewAction.MOVE_DOWN, key=["s"])
        if "a" in pressed_keys:
            self._last_primary_axis = "horizontal"
            return StardewCommand(action=StardewAction.MOVE_LEFT, key=["a"])
        if "d" in pressed_keys:
            self._last_primary_axis = "horizontal"
            return StardewCommand(action=StardewAction.MOVE_RIGHT, key=["d"])

        return StardewCommand(action=StardewAction.IDLE)

    def get_target_edge_debug_snapshot(self) -> str:
        return self._last_target_edge_debug

    def get_path_follow_debug_snapshot(self) -> str:
        return self._last_path_follow_debug

    def build_face_command(self, player_tile: Tile, target_tile: Tile) -> StardewCommand:
        if target_tile.x > player_tile.x:
            return StardewCommand(action=StardewAction.MOVE_RIGHT, key=["d"])
        if target_tile.x < player_tile.x:
            return StardewCommand(action=StardewAction.MOVE_LEFT, key=["a"])
        if target_tile.y > player_tile.y:
            return StardewCommand(action=StardewAction.MOVE_DOWN, key=["s"])
        if target_tile.y < player_tile.y:
            return StardewCommand(action=StardewAction.MOVE_UP, key=["w"])
        return StardewCommand(action=StardewAction.IDLE)

    def reset(self) -> None:
        self._last_primary_axis = None
        self._last_target_edge_debug = ""
        self._last_path_follow_debug = ""

    def _build_axis_move_command(self, axis: str, sign: int) -> StardewCommand:
        if axis == "horizontal":
            if sign > 0:
                return StardewCommand(action=StardewAction.MOVE_RIGHT, key=["d"])
            return StardewCommand(action=StardewAction.MOVE_LEFT, key=["a"])

        if sign > 0:
            return StardewCommand(action=StardewAction.MOVE_DOWN, key=["s"])
        return StardewCommand(action=StardewAction.MOVE_UP, key=["w"])

    def _get_monotonic_sign(self, values: list[int]) -> int:
        non_zero_deltas = [next_value - value for value, next_value in zip(values, values[1:]) if next_value != value]
        if not non_zero_deltas:
            return 0

        first_sign = 1 if non_zero_deltas[0] > 0 else -1
        if all((delta > 0) == (first_sign > 0) for delta in non_zero_deltas):
            return first_sign

        return 0

    def _format_path_segment(self, path_segment: list[RouteTile]) -> str:
        return "[" + ", ".join(f"({tile.x},{tile.y})" for tile in path_segment) + "]"

    def _build_diagonal_move_command(
        self,
        action: StardewAction,
        vertical_key: str,
        horizontal_key: str,
    ) -> StardewCommand:
        if self._last_primary_axis == "horizontal":
            key = [horizontal_key, vertical_key]
        else:
            key = [vertical_key, horizontal_key]

        self._last_primary_axis = self._last_primary_axis or "vertical"
        return StardewCommand(action=action, key=key)  # type: ignore
