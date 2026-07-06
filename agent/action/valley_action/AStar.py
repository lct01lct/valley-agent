import heapq
import math
from typing import Any, Callable, List, Tuple, Set, Dict, Optional
from agent.action.location.location import Location
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from server.valley_server import StardewState


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

    @staticmethod
    def get_path_coords(path_point) -> Tuple[int, int]:
        if isinstance(path_point, dict):
            return int(path_point["x"]), int(path_point["y"])
        return int(path_point[0]), int(path_point[1])

    @classmethod
    def annotate_path_points(cls, path, target_warp_passable: bool = True, target_warp_tile=None):
        if not path:
            return []

        annotated_path = []
        for tile in path:
            tile_coords = cls.get_path_coords(tile)
            point_type = "walk"
            if not target_warp_passable and target_warp_tile is not None:
                if cls.get_path_coords(target_warp_tile) == tile_coords:
                    point_type = "open_door"
            annotated_path.append({"x": tile_coords[0], "y": tile_coords[1], "type": point_type})

        return annotated_path

    def _get_blocked_tiles(self, state: StardewState) -> Set[Tuple[int, int]]:
        blocked: Set[Tuple[int, int]] = set()
        hard_layers = [
            "Wall",
            "Object",
            "Stone",
            "Weeds",
            # "Bed",
            "Twig",
            "Bush",
            "TreeStump",
            "Tree5",
            "Tree4",
            "Tree3",
            "Tree2",
            "Tree1",
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

    def _is_bed_center_tile(self, state: StardewState, tile: Tuple[int, int]) -> bool:
        bed_tiles = state.layers.get("Bed", set())
        if not bed_tiles:
            return False

        x, y = tile

        if tile not in bed_tiles:
            return False

        min_x = min(t[0] for t in bed_tiles)
        max_x = max(t[0] for t in bed_tiles)
        min_y = min(t[1] for t in bed_tiles)
        max_y = max(t[1] for t in bed_tiles)

        width = max_x - min_x + 1
        height = max_y - min_y + 1

        if width == 2 and height == 3:
            center_y = min_y + 1
            return y == center_y

        if width == 3 and height == 2:
            center_x = min_x + 1
            return x == center_x

        return False

    def get_goal_tiles(self, state: StardewState, target_location: Location) -> Set[Tuple[int, int]]:
        return {(warp.tile_x, warp.tile_y) for warp in state.warps if warp.target_location == target_location}

    def is_locate_inside_tile(self, state: StardewState) -> bool:
        tile_left = state.player_tile_x * 64
        tile_right = tile_left + 64
        tile_top = state.player_tile_y * 64
        tile_bottom = tile_top + 64

        person_left = state.position[0] - 24
        person_right = state.position[0] + 24
        person_top = state.position[1] - 16
        person_bottom = state.position[1] + 16

        is_x_inside = (person_left >= tile_left) and (person_right <= tile_right)
        is_y_inside = (person_top >= tile_top) and (person_bottom <= tile_bottom)

        return is_x_inside and is_y_inside

    def find_path_to_warp_zone(
        self,
        state: StardewState,
        start: Tuple[int, int],
        goal_tiles: Set[Tuple[int, int]],
        cost_function: (
            Callable[[Tuple[int, int], Tuple[int, int], StardewState, float], Tuple[bool, float, str]] | None
        ) = None,
    ) -> Optional[List[Tuple[int, int]]]:
        """
        保持原函数名不变。
        支持外部传入 cost_function 进行动态 G 值代价精算。
        """
        start = (int(start[0]), int(start[1]))

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
                    stand_tile = (goal[0] + dx, goal[1] + dy)
                    if stand_tile[0] >= 0 and stand_tile[1] >= 0:
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

        open_set = []
        initial_h = min(self._heuristic(start, goal) for goal in goal_tiles)
        heapq.heappush(open_set, (initial_h, 0.0, start))

        # 🌟 修改点：came_from 不仅记录父坐标，还要记录从父坐标踩入这一格时的 action_type
        # 格式：{ 当前格: (父网格, 动作标记字符串) }
        came_from: Dict[Tuple[int, int], Tuple[Tuple[int, int], str]] = {}
        g_score: Dict[Tuple[int, int], float] = {start: 0.0}

        while open_set:
            _, current_g, current = heapq.heappop(open_set)

            if current in goal_tiles:
                # 🌟 核心改动：在返回前，将富路径动作链条悄悄提取出来
                self._last_calculated_rich_path = self._extract_rich_path(came_from, current)
                # 依然返回你原有的纯坐标 List，不破坏外部依赖
                return [(pt["x"], pt["y"]) for pt in self._last_calculated_rich_path]

            for dx, dy, base_cost in self.directions:
                neighbor = (current[0] + dx, current[1] + dy)

                if neighbor in goal_tiles:
                    if dx == 0 or dy == 0:
                        came_from[neighbor] = (current, "walk")  # 进传送阵通常是普通的迈腿
                        self._last_calculated_rich_path = self._extract_rich_path(came_from, neighbor)
                        return [(pt["x"], pt["y"]) for pt in self._last_calculated_rich_path]
                    else:
                        continue

                if neighbor[0] < -1 or neighbor[1] < -1 or neighbor[0] > MAX_MAP_LIMIT or neighbor[1] > MAX_MAP_LIMIT:
                    continue
                if (neighbor[0] < 0 or neighbor[1] < 0) and neighbor not in goal_tiles:
                    continue

                # 🌟 核心解耦点：不再直接查 blocked_tiles，而是无条件交给外部传入的代价函数算账
                is_passable, step_cost, action_type = cost_function(current, neighbor, state, base_cost)

                if not is_passable:
                    continue

                # 斜向防切墙角逻辑（同样升级为由代价函数判定斜角两侧通断）
                if dx != 0 and dy != 0:
                    side_tile_1 = (current[0] + dx, current[1])
                    side_tile_2 = (current[0], current[1] + dy)
                    s1_pass, _, _ = cost_function(current, side_tile_1, state, 1.0)
                    s2_pass, _, _ = cost_function(current, side_tile_2, state, 1.0)
                    if not s1_pass or not s2_pass:
                        continue

                tentative_g = current_g + step_cost

                if neighbor not in g_score or tentative_g < g_score[neighbor]:
                    # 随路把外部给这步盖的“动作章 (action_type)”存进追踪表中
                    came_from[neighbor] = (current, action_type)
                    g_score[neighbor] = tentative_g
                    min_h = min(self._heuristic(neighbor, goal) for goal in goal_tiles)
                    f_score = tentative_g + min_h
                    heapq.heappush(open_set, (f_score, tentative_g, neighbor))

        if min_dist_to_goal < 15:
            print(f"❌ [绝路切停] A* 队列已排空！从起点 {start} 无法到达目标 {goal_tiles}。")
            return None
        else:
            return []

    def _extract_rich_path(self, came_from: dict, current: Tuple[int, int]) -> List[Dict[str, Any]]:
        rich_path = []
        while current in came_from:
            parent, action_type = came_from[current]
            rich_path.append({"x": current[0], "y": current[1], "type": action_type})
            current = parent

        rich_path.append({"x": current[0], "y": current[1], "type": "walk"})
        rich_path.reverse()
        return rich_path

    def _heuristic(self, p1: Tuple[int, int], p2: Tuple[int, int]) -> float:
        dx, dy = abs(p1[0] - p2[0]), abs(p1[1] - p2[1])
        return 1.414 * dx + (dy - dx) if dx < dy else 1.414 * dy + (dx - dy)

    def _reconstruct_path(self, came_from: dict, current: Tuple[int, int]) -> List[Tuple[int, int]]:
        total_path = [current]
        while current in came_from:
            current = came_from[current]
            total_path.append(current)
        total_path.reverse()
        return total_path

    def get_next_move_command(
        self, state: StardewState, current_path: List[Tuple[int, int]], target_warp_passable: bool = True
    ) -> Tuple[StardewCommand, List[Tuple[int, int]], bool]:
        if not current_path:
            return StardewCommand(action=StardewAction.IDLE), [], False

        tile_size = 64
        px, py = state.position

        if not hasattr(self, "_current_tile_frame_count"):
            self._current_tile_frame_count = 0
            self._last_tracked_tile = None
            self._last_px = px
            self._last_py = py

        target_tile = self.get_path_coords(current_path[0])

        is_approaching_blocked_warp = (not target_warp_passable) and (len(current_path) == 2)

        current_tile = self.get_path_coords(target_tile)
        last_tracked_tile = (
            self.get_path_coords(self._last_tracked_tile) if self._last_tracked_tile is not None else None
        )
        if current_tile == last_tracked_tile:
            self._current_tile_frame_count += 1
        else:
            self._current_tile_frame_count = 0
            self._last_tracked_tile = current_tile

        delta_x = abs(px - self._last_px)
        delta_y = abs(py - self._last_py)
        self._last_px = px
        self._last_py = py

        block_x = self._current_tile_frame_count > 6 and delta_x < 0.2
        block_y = self._current_tile_frame_count > 6 and delta_y < 0.2

        # 强推超时机制
        if self._current_tile_frame_count >= 15:
            if len(current_path) > 1:
                current_path = current_path[1:]
            else:
                current_path = []
            self._current_tile_frame_count = 0
            if not current_path:
                return StardewCommand(action=StardewAction.IDLE), [], False
            target_tile = self.get_path_coords(current_path[0])

        target_x = target_tile[0] * tile_size + (tile_size / 2)
        target_y = target_tile[1] * tile_size + (tile_size / 2)

        dist_to_center = ((px - target_x) ** 2 + (py - target_y) ** 2) ** 0.5

        player_radius = math.sqrt(((64 - 48) / 2) ** 2 + ((64 - 32) / 2) ** 2)

        # 如果下一步是不可通行的门，绝对不能消费掉当前的倒数第二格！
        if dist_to_center <= player_radius and len(current_path) > 1 and not is_approaching_blocked_warp:
            current_path = current_path[1:]
            self._current_tile_frame_count = 0
            if not current_path:
                return StardewCommand(action=StardewAction.IDLE, key=[]), [], False
            target_tile = self.get_path_coords(current_path[0])
            target_x = target_tile[0] * tile_size + (tile_size / 2)
            target_y = target_tile[1] * tile_size + (tile_size / 2)

        if is_approaching_blocked_warp:
            door_tile = self.get_path_coords(current_path[1])  # 这才是不可通行大门的真实坐标

            door_center_x = door_tile[0] * tile_size + (tile_size / 2)
            door_center_y = door_tile[1] * tile_size + (tile_size / 2)

            dist_to_door = ((px - door_center_x) ** 2 + (py - door_center_y) ** 2) ** 0.5

            if dist_to_door <= 65.5:
                px_tile, py_tile = state.player_tile_x, state.player_tile_y

                turn_command = StardewCommand(action=StardewAction.IDLE)
                if door_tile[0] > px_tile:
                    turn_command = StardewCommand(action=StardewAction.MOVE_RIGHT, key=["d"])
                elif door_tile[0] < px_tile:
                    turn_command = StardewCommand(action=StardewAction.MOVE_LEFT, key=["a"])
                elif door_tile[1] > py_tile:
                    turn_command = StardewCommand(action=StardewAction.MOVE_DOWN, key=["s"])
                elif door_tile[1] < py_tile:
                    turn_command = StardewCommand(action=StardewAction.MOVE_UP, key=["w"])

                print(
                    f"\n🏁 [极致贴近] 已彻底贴死在大门边缘 (到门中心距离: {dist_to_door:.2f}px)。执行单帧面向并清空路径。"
                )
                return turn_command, [], True

        diff_x = target_x - px
        diff_y = target_y - py

        pressed_keys = set()
        pixel_dead_zone = 1.0 if is_approaching_blocked_warp else 4.0

        if diff_x > pixel_dead_zone and not block_x:
            pressed_keys.add("d")
        elif diff_x < -pixel_dead_zone and not block_x:
            pressed_keys.add("a")

        if diff_y > pixel_dead_zone and not block_y:
            pressed_keys.add("s")
        elif diff_y < -pixel_dead_zone and not block_y:
            pressed_keys.add("w")

        if "w" in pressed_keys and "d" in pressed_keys:
            command = StardewCommand(action=StardewAction.MOVE_UP_RIGHT, key=["w", "d"])
        elif "w" in pressed_keys and "a" in pressed_keys:
            command = StardewCommand(action=StardewAction.MOVE_UP_LEFT, key=["w", "a"])
        elif "s" in pressed_keys and "d" in pressed_keys:
            command = StardewCommand(action=StardewAction.MOVE_DOWN_RIGHT, key=["s", "d"])
        elif "s" in pressed_keys and "a" in pressed_keys:
            command = StardewCommand(action=StardewAction.MOVE_DOWN_LEFT, key=["s", "a"])
        elif "w" in pressed_keys:
            command = StardewCommand(action=StardewAction.MOVE_UP, key=["w"])
        elif "s" in pressed_keys:
            command = StardewCommand(action=StardewAction.MOVE_DOWN, key=["s"])
        elif "a" in pressed_keys:
            command = StardewCommand(action=StardewAction.MOVE_LEFT, key=["a"])
        elif "d" in pressed_keys:
            command = StardewCommand(action=StardewAction.MOVE_RIGHT, key=["d"])
        else:
            command = StardewCommand(action=StardewAction.IDLE)

        return command, current_path, False


astar_solver = AStarParser()
