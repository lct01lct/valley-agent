import heapq
import math
from typing import List, Tuple, Set, Dict, Optional
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

    def _get_blocked_tiles(self, state: StardewState) -> Set[Tuple[int, int]]:
        blocked: Set[Tuple[int, int]] = set()
        hard_layers = [
            "WALL",
            "OBJECT",
            "STONE",
            "BUSH",
            "TREE_STUMP",
            "T5",
            "T4",
            "T3",
            "T2",
            "T1",
            "F5",
            "F4",
            "F3",
            "F2",
            "F1",
        ]

        for layer in hard_layers:
            blocked.update(state.layers.get(layer, set()))
        return blocked

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
        self, state: StardewState, start: Tuple[int, int], goal_tiles: Set[Tuple[int, int]]
    ) -> Optional[List[Tuple[int, int]]]:
        """
        视野内合围秒停版 A*：一旦目标进入视野且无立足点，不耗费 CPU 立即切停
        """
        start = (int(start[0]), int(start[1]))

        if not goal_tiles:
            return None

        if start in goal_tiles:
            return [start]

        blocked_tiles = self._get_blocked_tiles(state)

        # 🌟【核心新增：视野内合围秒杀闸门】
        # 计算玩家离大门的宏观距离
        min_dist_to_goal = min(self._heuristic(start, goal) for goal in goal_tiles)

        # 如果目标大门已经进入了玩家的可见视野（22格以内，约等于一个屏幕的宽度）
        if min_dist_to_goal <= 30:
            has_passable_entrance = False
            # 遍历视野内大门的所有直角交互方向（上下左右）
            for goal in goal_tiles:
                for dx, dy in [(0, 1), (0, -1), (1, 0), (-1, 0)]:
                    stand_tile = (goal[0] + dx, goal[1] + dy)

                    # 只要大门周围有一个合法的、没被石头/木头/墙壁阻挡的格子，就认为还有路可走
                    if stand_tile not in blocked_tiles and stand_tile[0] >= 0 and stand_tile[1] >= 0:
                        has_passable_entrance = True
                        break
                if has_passable_entrance:
                    break

            # 🚨 触网弹窗：如果大门就在视野内，且四周能踩的格子全被 blocked 堵死或属于非法虚空
            # 说明已经被突发障碍物死死合围！直接不走 A*，在起点秒级触发【None】切停！
            if not has_passable_entrance:
                print(
                    f"⚠️ [动态视野合围拦截] 目标大门 {goal_tiles} 已进入视野，但其四周交互点已全部被障碍物堵死！执行紧急切停。"
                )
                return None

        # MAX LIMIT 设为 200，保证小镇远端大门不被误杀
        MAX_MAP_LIMIT = 200

        # 2. 正常的 A* 核心寻路 logic
        open_set = []
        initial_h = min(self._heuristic(start, goal) for goal in goal_tiles)
        heapq.heappush(open_set, (initial_h, 0.0, start))

        came_from: Dict[Tuple[int, int], Tuple[int, int]] = {}
        g_score: Dict[Tuple[int, int], float] = {start: 0.0}

        while open_set:
            _, current_g, current = heapq.heappop(open_set)

            if current in goal_tiles:
                return self._reconstruct_path(came_from, current)

            for dx, dy, base_cost in self.directions:
                neighbor = (current[0] + dx, current[1] + dy)

                # 触网秒杀
                if neighbor in goal_tiles:
                    if dx == 0 or dy == 0:
                        came_from[neighbor] = current
                        return self._reconstruct_path(came_from, neighbor)
                    else:
                        continue

                # 限制负数虚空
                if neighbor[0] < -1 or neighbor[1] < -1:
                    continue
                if (neighbor[0] < 0 or neighbor[1] < 0) and neighbor not in goal_tiles:
                    continue

                # 限制右边和下边的庞大正数虚空
                if neighbor[0] > MAX_MAP_LIMIT or neighbor[1] > MAX_MAP_LIMIT:
                    continue

                # 判定非目标格子的普通阻挡
                if neighbor != start and neighbor in blocked_tiles:
                    continue

                # 斜向夹角障碍物检查
                if dx != 0 and dy != 0:
                    side_tile_1 = (current[0] + dx, current[1])
                    side_tile_2 = (current[0], current[1] + dy)

                    if side_tile_1 in blocked_tiles or side_tile_2 in blocked_tiles:
                        continue

                tentative_g = current_g + base_cost

                if neighbor not in g_score or tentative_g < g_score[neighbor]:
                    came_from[neighbor] = current
                    g_score[neighbor] = tentative_g
                    min_h = min(self._heuristic(neighbor, goal) for goal in goal_tiles)
                    f_score = tentative_g + min_h
                    heapq.heappush(open_set, (f_score, tentative_g, neighbor))

        # 如果在远端寻路时队列意外排空
        if min_dist_to_goal < 15:
            print(f"❌ [绝路切停] A* 队列已排空！从起点 {start} 无法到达目标 {goal_tiles}。")
            return None
        else:
            return []

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

    # ========================================================================
    # 🎯 单格驱动控制器（集成不可通行传送门的贴近与转身逻辑）
    # ========================================================================

    def get_next_move_command(
        self, state: StardewState, current_path: List[Tuple[int, int]], target_warp_passable: bool = True
    ) -> Tuple[StardewCommand, List[Tuple[int, int]]]:
        """
        引入终点自适应死区与绝对转向捕获机制
        """
        # 若路径走完，直接彻底静止（下一帧的绝对兜底，彻底无键静止）
        if not current_path:
            return StardewCommand(action=StardewAction.IDLE), []

        tile_size = 64
        px, py = state.position  # 精准中心

        # 初始化持久化状态
        if not hasattr(self, "_current_tile_frame_count"):
            self._current_tile_frame_count = 0
            self._last_tracked_tile = None
            self._last_px = px
            self._last_py = py

        target_tile = current_path[0]

        # 判定是否接近不可通行的大门
        is_approaching_blocked_warp = (not target_warp_passable) and (len(current_path) == 2)

        # 帧时效与卡挡状态计数
        if target_tile == self._last_tracked_tile:
            self._current_tile_frame_count += 1
        else:
            self._current_tile_frame_count = 0
            self._last_tracked_tile = target_tile

        # 计算这一帧的实际物理位移
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
                return StardewCommand(action=StardewAction.IDLE), []
            target_tile = current_path[0]

        # 1. 计算当前目标网格（当前踩着的站立格）的正中心物理坐标
        target_x = target_tile[0] * tile_size + (tile_size / 2)
        target_y = target_tile[1] * tile_size + (tile_size / 2)

        # 2. 计算当前目标格的中心欧氏距离
        dist_to_center = ((px - target_x) ** 2 + (py - target_y) ** 2) ** 0.5

        # 3. 消费网格判定（中途格子保持正常切换）
        player_radius = math.sqrt(((64 - 48) / 2) ** 2 + ((64 - 32) / 2) ** 2)

        # 如果下一步是不可通行的门，我们绝对不能消费掉当前的倒数第二格！
        if dist_to_center <= player_radius and len(current_path) > 1 and not is_approaching_blocked_warp:
            current_path = current_path[1:]
            self._current_tile_frame_count = 0
            if not current_path:
                return StardewCommand(action=StardewAction.IDLE, key=[]), []
            target_tile = current_path[0]
            target_x = target_tile[0] * tile_size + (tile_size / 2)
            target_y = target_tile[1] * tile_size + (tile_size / 2)

        # 🌟【门前精准截断与单帧转身机制 - 漏洞修复版】
        if is_approaching_blocked_warp:
            door_tile = current_path[1]  # 这才是不可通行大门的真实坐标

            # 计算大门本身的物理中心点
            door_center_x = door_tile[0] * tile_size + (tile_size / 2)
            door_center_y = door_tile[1] * tile_size + (tile_size / 2)

            # 🌟 精准计算玩家到【大门中心】的欧氏距离
            dist_to_door = ((px - door_center_x) ** 2 + (py - door_center_y) ** 2) ** 0.5

            if dist_to_door <= 65.5:
                px_tile, py_tile = state.player_tile_x, state.player_tile_y

                # 根据相对位置封装单帧转身动作
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
                    f"🏁 [极致贴近] 已彻底贴死在大门边缘 (到门中心距离: {dist_to_door:.2f}px)。执行单帧面向并清空路径。"
                )
                return turn_command, []

        # 4. 严密的符号偏差与防震死区计算
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

        # =================================================================
        # 将按键强类型映射为 StardewAction
        # =================================================================
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

        return command, current_path


astar_solver = AStarParser()
