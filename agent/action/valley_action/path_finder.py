import heapq
from typing import List, Tuple, Optional


class Pathfinder:
    def __init__(self, scale_x: float = 0.024, scale_y: float = 0.034):
        self.scale_x = scale_x  # Mac 3024 屏幕下，单格子横向比例
        self.scale_y = scale_y  # 单格子纵向比例

    def calculate_grid_delta(self, p_ratio: Tuple[float, float], t_ratio: Tuple[float, float]) -> Tuple[int, int]:
        """【尺子】将像素比例转换为相对网格数"""
        return (round((t_ratio[0] - p_ratio[0]) / self.scale_x), round((t_ratio[1] - p_ratio[1]) / self.scale_y))

    def find_path(
        self, start: Tuple[int, int], goal: Tuple[int, int], obstacles: List[Tuple[int, int]]
    ) -> Optional[List[Tuple[int, int]]]:
        """【A* 算法】在 40x40 的虚拟局部棋盘中寻路"""
        obstacle_set = set(obstacles)
        if goal in obstacle_set:
            return None

        open_set = []
        heapq.heappush(open_set, (0, start))
        came_from, g_score = {}, {start: 0}
        f_score = {start: abs(start[0] - goal[0]) + abs(start[1] - goal[1])}

        while open_set:
            _, current = heapq.heappop(open_set)
            if current == goal:
                path = []
                while current in came_from:
                    path.append(current)
                    current = came_from[current]
                path.reverse()
                return path

            for dx, dy in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
                neighbor = (current[0] + dx, current[1] + dy)
                if not (0 <= neighbor[0] < 40 and 0 <= neighbor[1] < 40) or neighbor in obstacle_set:
                    continue
                tentative_g_score = g_score[current] + 1
                if neighbor not in g_score or tentative_g_score < g_score[neighbor]:
                    came_from[neighbor] = current
                    g_score[neighbor] = tentative_g_score
                    f_score[neighbor] = tentative_g_score + (abs(neighbor[0] - goal[0]) + abs(neighbor[1] - goal[1]))
                    heapq.heappush(open_set, (f_score[neighbor], neighbor))
        return None
