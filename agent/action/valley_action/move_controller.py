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

    def get_next_move_command(
        self,
        state: StardewState,
        tile_path: list[RouteTile],
        path_index: int,
    ) -> tuple[StardewCommand, int, bool]:
        if not tile_path or path_index >= len(tile_path):
            return StardewCommand(action=StardewAction.IDLE), path_index, True

        next_index = path_index
        while next_index < len(tile_path) and self._should_advance_tile(state, tile_path, next_index):
            next_index += 1

        if next_index >= len(tile_path):
            return StardewCommand(action=StardewAction.IDLE), next_index, True

        command = self.build_move_command_to_tile(state, tile_path[next_index])
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
        return StardewCommand(action=action, key=key)
