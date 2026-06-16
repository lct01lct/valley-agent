import heapq

from agent.action.valley_action.move import ValleyKeyCommand


class AStarParser:
    def __init__(self, tile_size: float = 128):
        """
        :param tile_size: 星露谷物语的网格步长（Scale），默认 128 像素/格
        """
        self.tile_size = tile_size

    def _pixel_to_tile(self, px, py):
        """将绝对像素坐标向下取整，转换为网格格子坐标"""
        return int(px // self.tile_size), int(py // self.tile_size)

    def _tile_to_pixel(self, tx, ty):
        """将网格格子坐标还原为该格子中心点的像素坐标，供驱动层使用"""
        center_offset = self.tile_size // 2
        return int(tx * self.tile_size + center_offset), int(ty * self.tile_size + center_offset)

    def _heuristic(self, a, b):
        """启发式函数：采用曼哈顿距离"""
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    def build_grid_world(self, player_px, target_px, real_obstacles):
        """
        将前置节点解析出来的像素级数据，降维映射成 A* 算法需要的虚拟格子世界。
        """
        # 1. 像素坐标转换成格子坐标
        start_tile = self._pixel_to_tile(*player_px)
        end_tile = self._pixel_to_tile(*target_px)

        # 2. 构建障碍物网格集合 (Set 查找时间复杂度为 O(1))
        obstacle_tiles = set()

        for obs in real_obstacles:
            xmin, ymin, xmax, ymax = obs["box"]

            # 算出这个障碍物像素框跨越了哪些格子索引范围
            tile_xmin, tile_ymin = self._pixel_to_tile(xmin, ymin)
            tile_xmax, tile_ymax = self._pixel_to_tile(xmax, ymax)

            # 【修复位】：严格控制双层循环变量为 grid_x 和 grid_y，防止污染
            for grid_x in range(tile_xmin, tile_xmax + 1):
                for grid_y in range(tile_ymin, tile_ymax + 1):
                    obstacle_tiles.add((grid_x, grid_y))

        # 3. A* 核心防御机制：如果模型标定有误差，强行将起点和终点从黑名单移出，防止自锁
        if start_tile in obstacle_tiles:
            obstacle_tiles.remove(start_tile)
        if end_tile in obstacle_tiles:
            obstacle_tiles.remove(end_tile)

        return start_tile, end_tile, obstacle_tiles

    def search_path(self, start, end, obstacles):
        """
        标准的 A* 寻路算法核心实现
        """
        open_set = []
        heapq.heappush(open_set, (0, start))

        came_from = {}
        g_score = {start: 0}

        # 允许的移动方向：上下左右（星露谷核心十字移动）
        neighbors = [(0, 1), (0, -1), (1, 0), (-1, 0)]

        while open_set:
            _, current = heapq.heappop(open_set)

            # 成功抵达终点，开始回溯路径
            if current == end:
                path = []
                while current in came_from:
                    path.append(current)
                    current = came_from[current]
                path.append(start)
                path.reverse()
                return path

            # 遍历四个邻居格子
            for dx, dy in neighbors:
                neighbor = (current[0] + dx, current[1] + dy)

                # 如果邻居是障碍物，直接忽略
                if neighbor in obstacles:
                    continue

                # 计算到达邻居格子的全新 G Score
                tentative_g_score = g_score[current] + 1

                if tentative_g_score < g_score.get(neighbor, float("inf")):
                    came_from[neighbor] = current
                    g_score[neighbor] = tentative_g_score
                    f_score = tentative_g_score + self._heuristic(neighbor, end)
                    heapq.heappush(open_set, (f_score, neighbor))

        return []  # 无路可走

    def plan_pixel_path(self, player_px, target_px, real_obstacles):
        """
        高级封装接口：直接输入还原后的【像素数据】，直接吐出给键盘执行层的【像素节点路径】
        """
        # 1. 空间降维映射
        start_tile, end_tile, obstacle_tiles = self.build_grid_world(player_px, target_px, real_obstacles)

        # 2. 在网格世界中跑 A*
        tile_path = self.search_path(start_tile, end_tile, obstacle_tiles)

        if not tile_path:
            print("❌ A* 算法评估：当前网格状态下，起点与终点之间被障碍物完全锁死，无法生成路径！")
            return []

        # 3. 升维还原：将格子链条翻译回绝对像素中心点坐标
        pixel_path = [self._tile_to_pixel(tx, ty) for tx, ty in tile_path]
        return pixel_path


import math
from typing import List, Tuple, Dict, Any


def convert_path_to_keyboard_commands(
    pixel_path: List[Tuple[int, int]], walk_speed_px_per_sec: float = 470.0, pixel_tolerance: int = 10
) -> List[ValleyKeyCommand]:
    """
    将 A* 算法生成的绝对像素路标点列表，精准翻译为键盘动作指令列表。

    :param pixel_path: A* 规划出的像素中心点列表，例如: [(1257, 1169), (1257, 1297), (1257, 1425)]
    :param walk_speed_px_per_sec: 游戏角色移动速度（像素/秒）。
                                  3024x1842 分辨率 & tile_size=128 环境下，
                                  未吃速度Buff常驻跑步推荐采用 470.0 像素/秒。
    :param pixel_tolerance: 像素容错阈值，小于该像素的微小位移将被忽略，防止抖动。
    :return: 包含键盘按键命令的列表，格式为 [{"key": "s", "duration": 0.272}, ...]
    """
    commands = []

    # 路径少于2个点，代表已经站在原地或者无路可走
    if not pixel_path or len(pixel_path) < 2:
        return commands

    # 循环遍历路径中的每一个步进线段
    for i in range(len(pixel_path) - 1):
        cx, cy = pixel_path[i]  # 当前路标点
        nx, ny = pixel_path[i + 1]  # 下一个路标点

        dx = nx - cx
        dy = ny - cy

        # 1. 处理水平 X 轴方向位移 (a=左, d=右)
        if abs(dx) > pixel_tolerance:
            key = "d" if dx > 0 else "a"
            duration = round(abs(dx) / walk_speed_px_per_sec, 3)
            commands.append({"key": key, "duration": duration})
            commands.append(ValleyKeyCommand(key=key, duration=duration))

        # 2. 处理垂直 Y 轴方向位移 (w=上, s=下)
        if abs(dy) > pixel_tolerance:
            key = "s" if dy > 0 else "w"  # 游戏画面下移代表往下走
            duration = round(abs(dy) / walk_speed_px_per_sec, 3)
            commands.append(ValleyKeyCommand(key=key, duration=duration))

    return commands


def to_box(box: tuple[float, float, float, float]):
    xmin, ymin = to_px(box[0], box[1])
    xmax, ymax = to_px(box[2], box[3])


IMAGE_WIDTH = 3024
IMAGE_HEIGHT = 1842

to_px = lambda nx, ny: (int((nx / 1000.0) * IMAGE_WIDTH), int((ny / 1000.0) * IMAGE_HEIGHT))

player_pixel_coordinate = to_px(416, 635)  # 约 (1257, 1169)
target_pixel_coordinate = to_px(416, 780)  # 约 (1257, 1436)


raw_obstacles_data = [
    {"name": "电视机", "box": (340, 610, 400, 670)},
    {"name": "床铺", "box": (435, 575, 500, 650)},
    {"name": "桌子", "box": (465, 465, 500, 535)},
    {"name": "椅子", "box": (435, 435, 495, 465)},
    {"name": "壁炉", "box": (560, 560, 640, 630)},
    {"name": "北墙", "box": (325, 325, 380, 675)},
    {"name": "西墙", "box": (325, 380, 635, 435)},
    {"name": "东墙", "box": (630, 380, 635, 675)},
]

real_obstacles = []
for obs in raw_obstacles_data:
    xmin, ymin = to_px(obs["box"][0], obs["box"][1])
    xmax, ymax = to_px(obs["box"][2], obs["box"][3])
    real_obstacles.append({"name": obs["name"], "box": (xmin, ymin, xmax, ymax)})

# ==========================================
# 2. 实例化并调用你的 AStarPlanner (tile_size=128)
# ==========================================
if __name__ == "__main__":
    print("▶️ 正在初始化 A* 寻路大脑...")
    # 显式传入 128 像素/格，对齐你刚才修改的类逻辑
    planner = AStarParser(tile_size=128)

    print("▶️ 正在规划绝对像素路径...")
    pixel_path = planner.plan_pixel_path(
        player_px=player_pixel_coordinate, target_px=target_pixel_coordinate, real_obstacles=real_obstacles
    )

    keyboard_action_list = convert_path_to_keyboard_commands(
        pixel_path=pixel_path, walk_speed_px_per_sec=470.0  # 游戏默认标准跑步速度
    )

    print(keyboard_action_list)

    # # ==========================================
    # # 3. 输出打印结果，供执行层消费
    # # ==========================================
    # print("\n" + "=" * 40 + "\n[调用运行结果]\n" + "=" * 40)
    # if pixel_path:
    #     print(f"🎉 寻路成功！A* 规划了一条包含 {len(pixel_path)} 个节点的运动路径。")
    #     print(f"起点像素: {player_pixel_coordinate} -> 终点像素: {target_pixel_coordinate}\n")
    #     for idx, waypoint in enumerate(pixel_path):
    #         print(f" 📍 节点 [{idx:02d}] -> 驱动层引导角色走向绝对像素坐标: {waypoint}")
    # else:
    #     print("❌ 寻路失败！在当前 128px 网格环境下，起点与终点之间被路障阻断。")
