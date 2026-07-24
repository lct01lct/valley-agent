from dataclasses import dataclass
from typing import Literal

from agent.action.valley_action.AStar import RouteTile, astar_solver
from agent.action.valley_action.action_type import StardewCommand
from agent.action.valley_action.move_controller import MoveController
from agent.action.valley_action.tool_targeting import build_tool_target_face_command, is_tool_targeting
from server.valley_server import StardewState
from server.type import Tile


type PositioningStatus = Literal[
    "MOVING",  # 正在移动到候选站位，还不能执行交互动作
    "FACING",  # 已到候选站位，正在原地调整工具目标方向
    "READY",  # 已到候选站位，且工具目标已经对准，可以执行交互动作
    "FAILED",  # 无法规划到任何候选站位，调用方需要失败或重新规划
]


@dataclass(frozen=True)
class PositioningGoal:
    candidate_stand_tiles: set[Tile]
    tool_target_tile: Tile | None = None
    extra_blocked_tiles: set[Tile] | None = None
    allowed_blocked_tiles: set[Tile] | None = None
    require_close_to_target: bool = False


@dataclass(frozen=True)
class PositioningResult:
    status: PositioningStatus
    command: StardewCommand | None = None
    stand_tile: Tile | None = None
    reason: str | None = None


class PositioningController:
    """
    通用交互站位控制器。

    调用方只描述“允许站在哪些格子”和“工具目标应该指向哪一格”。
    本控制器负责选择可达站位、缓存路径、驱动 MoveController，并在站好后原地转向。
    """

    def __init__(self) -> None:
        self.move_controller = MoveController()
        self._tile_path: list[RouteTile] = []
        self._path_index = 0
        self._goal_key: tuple[
            tuple[tuple[int, int], ...],
            tuple[int, int] | None,
            tuple[tuple[int, int], ...],
            bool,
        ] | None = None

    def reset(self) -> None:
        self._tile_path = []
        self._path_index = 0
        self._goal_key = None
        self.move_controller.reset()

    def get_debug_snapshot(self) -> str:
        return (
            f"goal_key={self._goal_key}, "
            f"path_len={len(self._tile_path)}, "
            f"path_index={self._path_index}, "
            f"path_end={self._get_path_end_tile()}"
        )

    def tick(self, state: StardewState, goal: PositioningGoal) -> PositioningResult:
        extra_blocked_tiles = goal.extra_blocked_tiles or set()
        allowed_blocked_tiles = goal.allowed_blocked_tiles or set()
        candidate_stand_tiles = self._filter_candidate_stand_tiles(
            state,
            goal.candidate_stand_tiles,
            extra_blocked_tiles,
            allowed_blocked_tiles,
        )
        if not candidate_stand_tiles:
            return PositioningResult(status="FAILED", reason="没有可用候选站位")

        if state.player_tile in candidate_stand_tiles:
            self._tile_path = []
            self._path_index = 0
            return self._build_arrived_result(
                state,
                state.player_tile,
                goal.tool_target_tile,
                goal.require_close_to_target,
            )

        goal_key = self._build_goal_key(
            candidate_stand_tiles,
            goal.tool_target_tile,
            allowed_blocked_tiles,
            goal.require_close_to_target,
        )
        if self._goal_key != goal_key:
            self._goal_key = goal_key
            self._tile_path = []
            self._path_index = 0
            self.move_controller.reset()

        if not self._tile_path or self._path_index >= len(self._tile_path):
            self._tile_path = self._build_path_to_stand_tiles(
                state,
                candidate_stand_tiles,
                extra_blocked_tiles,
                allowed_blocked_tiles,
            )
            self._path_index = 0

            if not self._tile_path:
                return PositioningResult(status="FAILED", reason="无法规划到任何候选站位")

        command, next_path_index, is_done = self.move_controller.get_next_move_command(
            state,
            self._tile_path,
            self._path_index,
        )
        self._path_index = next_path_index

        if is_done:
            stand_tile = self._get_path_end_tile()
            self._tile_path = []
            return PositioningResult(status="MOVING", stand_tile=stand_tile, reason="已走到路径末端，等待 state 确认")

        return PositioningResult(status="MOVING", command=command, stand_tile=self._get_path_end_tile())

    def _build_arrived_result(
        self,
        state: StardewState,
        stand_tile: Tile,
        tool_target_tile: Tile | None,
        require_close_to_target: bool,
    ) -> PositioningResult:
        if tool_target_tile is None:
            return PositioningResult(status="READY", stand_tile=stand_tile)

        if require_close_to_target and not self.move_controller.is_player_close_to_target_edge(
            state,
            stand_tile,
            tool_target_tile,
        ):
            command = self.move_controller.build_move_command_to_target_edge(
                state,
                stand_tile,
                tool_target_tile,
            )
            return PositioningResult(
                status="MOVING",
                command=command,
                stand_tile=stand_tile,
                reason="已到候选站位，继续贴近交互目标边缘",
            )

        if is_tool_targeting(state, tool_target_tile):
            return PositioningResult(status="READY", stand_tile=stand_tile)

        command = build_tool_target_face_command(state.player_tile, tool_target_tile)
        return PositioningResult(status="FACING", command=command, stand_tile=stand_tile)

    def _build_path_to_stand_tiles(
        self,
        state: StardewState,
        candidate_stand_tiles: set[Tile],
        extra_blocked_tiles: set[Tile],
        allowed_blocked_tiles: set[Tile],
    ) -> list[RouteTile]:
        goal_tiles = {RouteTile(tile.x, tile.y, type="walk") for tile in candidate_stand_tiles}
        blocked_tiles = astar_solver._get_blocked_tiles(state) | extra_blocked_tiles
        start = RouteTile(*state.player_tile, type="walk")

        def positioning_cost_func(curr, neigh, st, base_c):
            if neigh != start and neigh in blocked_tiles and neigh not in allowed_blocked_tiles:
                return False, float("inf"), "blocked"
            return True, base_c, "walk"

        path = astar_solver.find_path_to_warp_zone(
            state,
            start,
            goal_tiles,
            cost_function=positioning_cost_func,
        )
        return path or []

    def _filter_candidate_stand_tiles(
        self,
        state: StardewState,
        candidate_stand_tiles: set[Tile],
        extra_blocked_tiles: set[Tile],
        allowed_blocked_tiles: set[Tile],
    ) -> set[Tile]:
        map_width, map_height = state.map_size
        blocked_tiles = astar_solver._get_blocked_tiles(state) | extra_blocked_tiles
        result: set[Tile] = set()

        for tile in candidate_stand_tiles:
            if tile.x < 0 or tile.y < 0 or tile.x >= map_width or tile.y >= map_height:
                continue
            if tile in blocked_tiles and tile not in allowed_blocked_tiles:
                continue
            result.add(tile)

        return result

    def _get_path_end_tile(self) -> Tile | None:
        if not self._tile_path:
            return None
        return Tile(self._tile_path[-1].x, self._tile_path[-1].y)

    def _build_goal_key(
        self,
        candidate_stand_tiles: set[Tile],
        tool_target_tile: Tile | None,
        allowed_blocked_tiles: set[Tile],
        require_close_to_target: bool,
    ) -> tuple[tuple[tuple[int, int], ...], tuple[int, int] | None, tuple[tuple[int, int], ...], bool]:
        stand_key = tuple(sorted((tile.x, tile.y) for tile in candidate_stand_tiles))
        target_key = None if tool_target_tile is None else (tool_target_tile.x, tool_target_tile.y)
        allowed_key = tuple(sorted((tile.x, tile.y) for tile in allowed_blocked_tiles))
        return stand_key, target_key, allowed_key, require_close_to_target
