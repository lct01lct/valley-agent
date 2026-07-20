import heapq
from typing import Callable, List, Literal, Tuple, Set, Dict, Optional
from agent.action.location.location import Location
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from server.valley_server import StardewState
from server.type import Tile

destructible_obstacles = ["weeds", "twig", "warp"]
type RouteActionType = Literal["walk", "blocked", "weeds", "twig", "stone", "warp", "door"]


class RouteTile(Tile):
    def __init__(self, x: int, y: int, type: RouteActionType):
        super().__init__(x, y)
        self.type: RouteActionType = type

    def __repr__(self):
        return f"({self.x}, {self.y}, {self.type})"


class AStarParser:
    def __init__(self):
        # 基础移动方向：(dx, dy, base_cost)
        self.directions = [
            (0, 1, 1.0),
            (0, -1, 1.0),
            (1, 0, 1.0),
            (-1, 0, 1.0),  # 正方向
            (1, 1, 1.414),
            (1, -1, 1.414),
            (-1, 1, 1.414),
            (-1, -1, 1.414),  # 斜方向
        ]

        self._stuck_counter = 0
        self._last_px = 0.0
        self._last_py = 0.0
        self._last_move_command: StardewCommand | None = None
        self._last_primary_axis: Literal["horizontal", "vertical"] | None = None

    def _get_blocked_tiles(self, state: StardewState) -> Set[Tile]:
        blocked: Set[Tile] = set()
        hard_layers = [
            "Wall",
            "Bush",
            "Stone",
            "Twig",
            "Weeds",
            "TreeStump",
            "Tree0",
            "Tree5",
            "Tree4",
            "Tree3",
            "Tree2",
            "Tree1",
            "FruitTree0",
            "FruitTree5",
            "FruitTree4",
            "FruitTree3",
            "FruitTree2",
            "FruitTree1",
        ]

        for layer in hard_layers:
            blocked.update(state.layers.get(layer, set()))

        bed_tiles = state.layers.get("Bed", set())
        for tile in bed_tiles:
            if self._is_bed_center_tile(state, tile):
                continue
            blocked.add(tile)
        return blocked

    def _is_bed_center_tile(self, state: StardewState, tile: Tile) -> bool:
        bed_tiles = state.layers.get("Bed", set())
        if not bed_tiles:
            return False

        x, y = tile.x, tile.y

        if tile not in bed_tiles:
            return False

        min_x = min(t.x for t in bed_tiles)
        max_x = max(t.x for t in bed_tiles)
        min_y = min(t.y for t in bed_tiles)
        max_y = max(t.y for t in bed_tiles)

        width = max_x - min_x + 1
        height = max_y - min_y + 1

        if width == 2 and height == 3:
            center_y = min_y + 1
            return y == center_y

        if width == 3 and height == 2:
            center_x = min_x + 1
            return x == center_x

        return False

    def get_goal_tiles(self, state: StardewState, target_location: Location) -> Set[RouteTile]:
        return {
            RouteTile(*warp.tile, type="walk" if warp.is_passable else "door")
            for warp in state.warps
            if warp.target_location == target_location
        }

    def is_locate_inside_tile(self, state: StardewState) -> bool:
        tile_left = state.player_tile.x * 64
        tile_right = tile_left + 64
        tile_top = state.player_tile.y * 64
        tile_bottom = tile_top + 64

        player_width, player_height = state.player_size
        person_left = state.position.x - player_width / 2
        person_right = state.position.x + player_width / 2
        person_top = state.position.y - player_height / 2
        person_bottom = state.position.y + player_height / 2

        is_x_inside = (person_left >= tile_left) and (person_right <= tile_right)
        is_y_inside = (person_top >= tile_top) and (person_bottom <= tile_bottom)

        return is_x_inside and is_y_inside

    def find_path_to_warp_zone(
        self,
        state: StardewState,
        start: RouteTile,
        goal_tiles: Set[RouteTile],
        cost_function: Callable[[Tile, Tile, StardewState, float], Tuple[bool, float, RouteActionType]] | None = None,
    ) -> Optional[List[RouteTile]]:

        if not goal_tiles:
            return None

        if start in goal_tiles:
            return [start]

        # 🎯 备用保底：如果外部没传 cost_function，默认生成一个普通的走格子账本
        if cost_function is None:
            blocked_tiles = self._get_blocked_tiles(state)

            def default_cost_func(curr, neigh, st, base_c):
                if neigh != start and neigh in blocked_tiles:
                    return False, float("inf"), "blocked"
                return True, base_c, "walk"

            cost_function = default_cost_func

        min_dist_to_goal = min(self._heuristic(start, goal) for goal in goal_tiles)

        # 视野合围拦截逻辑（保持你原版的安全切停，但将硬编码拦截升级为账本拦截）
        if min_dist_to_goal <= 30:
            has_passable_entrance = False
            for goal in goal_tiles:
                for dx, dy in [(0, 1), (0, -1), (1, 0), (-1, 0)]:
                    stand_tile = Tile(goal.x + dx, goal.y + dy)
                    if stand_tile.x >= 0 and stand_tile.y >= 0:
                        # 借用注入的账本判断四周有没有能落脚的通路
                        is_pass, _, _ = cost_function(goal, stand_tile, state, 1.0)
                        if is_pass:
                            has_passable_entrance = True
                            break
                if has_passable_entrance:
                    break

            if not has_passable_entrance:
                print(f"⚠️ [动态视野合围拦截] 目标大门 {goal_tiles} 周边在当前代价模型下已无立足点！切停。")
                return None

        MAX_MAP_LIMIT = 200

        _heapq_counter = 0
        open_set: List[tuple[float, float, int, RouteTile]] = []
        initial_h = min(self._heuristic(start, goal) for goal in goal_tiles)
        heapq.heappush(open_set, (initial_h, 0.0, _heapq_counter, start))

        # 🌟 修改点：came_from 不仅记录父坐标，还要记录从父坐标踩入这一格时的 action_type
        # 格式：{ 当前格: (父网格, 动作标记字符串) }
        came_from: Dict[RouteTile, RouteTile] = {}
        g_score: Dict[Tile, float] = {start: 0.0}

        while open_set:
            _, current_g, _, current = heapq.heappop(open_set)

            if current in goal_tiles:
                self._last_calculated_rich_path = self._extract_rich_path(came_from, current)
                return self._last_calculated_rich_path

            for dx, dy, base_cost in self.directions:
                neighbor = RouteTile(current.x + dx, current.y + dy, "walk")

                goal_tile = None
                for _goal_tile in goal_tiles:
                    if neighbor == _goal_tile:
                        goal_tile = _goal_tile
                        break

                if goal_tile:
                    if dx == 0 or dy == 0:
                        came_from[neighbor] = RouteTile(
                            current.x, current.y, goal_tile.type
                        )  # 进传送阵通常是普通的迈腿

                        self._last_calculated_rich_path = self._extract_rich_path(came_from, neighbor)

                        return self._last_calculated_rich_path
                    else:
                        continue

                if neighbor.x < -1 or neighbor.y < -1 or neighbor.x > MAX_MAP_LIMIT or neighbor.y > MAX_MAP_LIMIT:
                    continue
                if (neighbor.x < 0 or neighbor.y < 0) and neighbor not in goal_tiles:
                    continue

                # 🌟 核心解耦点：不再直接查 blocked_tiles，而是无条件交给外部传入的代价函数算账
                is_passable, step_cost, action_type = cost_function(current, neighbor, state, base_cost)

                if not is_passable:
                    continue

                # 斜向防切墙角逻辑（同样升级为由代价函数判定斜角两侧通断）
                if dx != 0 and dy != 0:
                    side_tile_1 = Tile(current.x + dx, current.y)
                    side_tile_2 = Tile(current.x, current.y + dy)
                    s1_pass, _, _ = cost_function(current, side_tile_1, state, 1.0)
                    s2_pass, _, _ = cost_function(current, side_tile_2, state, 1.0)
                    if not s1_pass or not s2_pass:
                        continue

                tentative_g = current_g + step_cost

                if neighbor not in g_score or tentative_g < g_score[neighbor]:
                    # 随路把外部给这步盖的“动作章 (action_type)”存进追踪表中
                    came_from[neighbor] = RouteTile(current.x, current.y, action_type)
                    g_score[neighbor] = tentative_g
                    min_h = min(self._heuristic(neighbor, goal) for goal in goal_tiles)
                    f_score = tentative_g + min_h
                    _heapq_counter += 1
                    heapq.heappush(open_set, (f_score, tentative_g, _heapq_counter, neighbor))

        if min_dist_to_goal < 15:
            print(f"❌ [绝路切停] A* 队列已排空！从起点 {start} 无法到达目标 {goal_tiles}。")
            return None
        else:
            return []

    def _extract_rich_path(self, came_from: Dict[RouteTile, RouteTile], current: RouteTile) -> List[RouteTile]:
        rich_path: List[RouteTile] = []

        while current in came_from:
            parent = came_from[current]

            rich_path.append(RouteTile(current.x, current.y, parent.type))
            current = parent

        rich_path.append(RouteTile(current.x, current.y, "walk"))
        rich_path.reverse()

        return rich_path

    def _heuristic(self, p1: Tile, p2: Tile) -> float:
        dx, dy = abs(p1.x - p2.x), abs(p1.y - p2.y)
        return 1.414 * dx + (dy - dx) if dx < dy else 1.414 * dy + (dx - dy)

    def _reconstruct_path(self, came_from: dict, current: Tile) -> List[Tile]:
        total_path = [current]
        while current in came_from:
            current = came_from[current]
            total_path.append(current)
        total_path.reverse()
        return total_path

    def get_next_move_command(
        self,
        state: StardewState,
        current_path: List[RouteTile],
    ) -> Tuple[StardewCommand, List[RouteTile], bool]:
        if len(current_path) < 2:
            self._last_move_command = None
            return StardewCommand(action=StardewAction.IDLE), current_path, False

        next_tile = current_path[1]

        if self._is_player_centered_on_tile(state, next_tile):
            next_path = current_path[1:]
            if len(next_path) < 2:
                self._last_move_command = None
                return StardewCommand(action=StardewAction.IDLE), next_path, True

            if self._last_move_command is not None:
                return self._last_move_command, next_path, True

            return self._build_move_command_to_tile(state, next_path[1], pixel_dead_zone=4.0), next_path, True

        command = self._build_move_command_to_tile(state, next_tile, pixel_dead_zone=4.0)
        if command.action != StardewAction.IDLE:
            self._last_move_command = command
        return command, current_path, False

    def _is_player_centered_on_tile(self, state: StardewState, tile: Tile) -> bool:
        tile_size = state.tile_size or 64
        tile_left = tile.x * tile_size
        tile_right = tile_left + tile_size
        tile_top = tile.y * tile_size
        tile_bottom = tile_top + tile_size

        player_width, player_height = state.player_size
        person_left = state.position.x - player_width / 2
        person_right = state.position.x + player_width / 2
        person_top = state.position.y - player_height / 2
        person_bottom = state.position.y + player_height / 2

        return (
            person_left >= tile_left
            and person_right <= tile_right
            and person_top >= tile_top
            and person_bottom <= tile_bottom
        )

    def _build_face_command(self, player_tile: Tile, target_tile: Tile) -> StardewCommand:
        if target_tile.x > player_tile.x:
            return StardewCommand(action=StardewAction.MOVE_RIGHT, key=["d"])
        if target_tile.x < player_tile.x:
            return StardewCommand(action=StardewAction.MOVE_LEFT, key=["a"])
        if target_tile.y > player_tile.y:
            return StardewCommand(action=StardewAction.MOVE_DOWN, key=["s"])
        if target_tile.y < player_tile.y:
            return StardewCommand(action=StardewAction.MOVE_UP, key=["w"])
        return StardewCommand(action=StardewAction.IDLE)

    def _build_move_command_to_tile(
        self, state: StardewState, target_tile: Tile, pixel_dead_zone: float = 4.0
    ) -> StardewCommand:
        tile_size = state.tile_size or 64
        player_width, player_height = state.player_size
        person_half_width = player_width / 2
        person_half_height = player_height / 2

        target_left = target_tile.x * tile_size + person_half_width
        target_right = (target_tile.x + 1) * tile_size - person_half_width
        target_top = target_tile.y * tile_size + person_half_height
        target_bottom = (target_tile.y + 1) * tile_size - person_half_height

        pressed_keys = set()
        edge_dead_zone = 0.5

        if state.position.x < target_left - edge_dead_zone:
            pressed_keys.add("d")
        elif state.position.x > target_right + edge_dead_zone:
            pressed_keys.add("a")

        if state.position.y < target_top - edge_dead_zone:
            pressed_keys.add("s")
        elif state.position.y > target_bottom + edge_dead_zone:
            pressed_keys.add("w")

        if "w" in pressed_keys and "d" in pressed_keys:
            command = self._build_diagonal_move_command(StardewAction.MOVE_UP_RIGHT, "w", "d")
        elif "w" in pressed_keys and "a" in pressed_keys:
            command = self._build_diagonal_move_command(StardewAction.MOVE_UP_LEFT, "w", "a")
        elif "s" in pressed_keys and "d" in pressed_keys:
            command = self._build_diagonal_move_command(StardewAction.MOVE_DOWN_RIGHT, "s", "d")
        elif "s" in pressed_keys and "a" in pressed_keys:
            command = self._build_diagonal_move_command(StardewAction.MOVE_DOWN_LEFT, "s", "a")
        elif "w" in pressed_keys:
            self._last_primary_axis = "vertical"
            command = StardewCommand(action=StardewAction.MOVE_UP, key=["w"])
        elif "s" in pressed_keys:
            self._last_primary_axis = "vertical"
            command = StardewCommand(action=StardewAction.MOVE_DOWN, key=["s"])
        elif "a" in pressed_keys:
            self._last_primary_axis = "horizontal"
            command = StardewCommand(action=StardewAction.MOVE_LEFT, key=["a"])
        elif "d" in pressed_keys:
            self._last_primary_axis = "horizontal"
            command = StardewCommand(action=StardewAction.MOVE_RIGHT, key=["d"])
        else:
            return StardewCommand(action=StardewAction.IDLE)

        return command

    def _build_diagonal_move_command(
        self,
        action: StardewAction,
        vertical_key: Literal["w", "s"],
        horizontal_key: Literal["a", "d"],
    ) -> StardewCommand:
        if self._last_primary_axis == "horizontal":
            key = [horizontal_key, vertical_key]
        else:
            key = [vertical_key, horizontal_key]

        self._last_primary_axis = self._last_primary_axis or "vertical"
        return StardewCommand(action=action, key=key)  # type: ignore


astar_solver = AStarParser()
