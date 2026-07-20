from collections import deque
from typing import List, Optional

from agent.action.location.location import Location


class HardcodedStardewMap:
    """
    🗺️ 完全静态写死的星露谷物语场景连通图 (基于官方 Warp 数据建模)
    不需要任何动态解析，运行效率极高，专为 Agent 长途寻路提供数据支撑。
    """

    # 将去重后的邻接拓扑网完全写死在类属性中
    GRAPH: dict[Location, set[Location]] = {
        "AdventureGuild": {"Town"},
        "AnimalShop": {"Forest"},
        "ArchaeologyHouse": {"Town"},
        "Backwoods": {"Farm", "Mountain"},
        "Beach": {"Town", "FishShop"},
        "Blacksmith": {"Town"},
        "BusStop": {"Desert", "Tunnel", "Town", "Mountain", "Farm"},
        "CommunityCenter": {"Town"},
        "Desert": {"BusStop"},
        "ElliottHouse": {"Beach"},
        "Farm": {
            "FarmHouse",
            "Greenhouse",
            "BusStop",
            "Backwoods",
            "Forest",
        },
        "FarmCave": {"Backwoods"},
        "FarmHouse": {"Farm"},
        "FishShop": {"Beach"},
        "Forest": {"Town", "Farm", "AnimalShop", "WizardHouse", "LeahHouse", "Woods"},
        "Greenhouse": {"Farm"},
        "HaleyHouse": {"Town"},
        "Hospital": {"Town"},
        "JojaMart": {"Town"},
        "JoshHouse": {"Town"},
        "LeahHouse": {"Forest"},
        "LockedDoorWarp": {"ScienceHouse"},
        "ManorHouse": {"Town"},
        "Mine": {"Mountain", "Town"},
        "Mountain": {
            "Backwoods",
            "Town",
            "Mine",
            "Railroad",
            "Tent",
            "ScienceHouse",
        },
        "Railroad": {"Mountain", "Backwoods"},
        "Saloon": {"Town"},
        "SamHouse": {"Town"},
        "ScienceHouse": {"Mountain", "LockedDoorWarp"},
        "SeedShop": {"Town"},
        "Tent": {"Mountain", "Town"},
        "Town": {
            "BusStop",
            "Mountain",
            "Forest",
            "Beach",
            "CommunityCenter",
            "JojaMart",
            "Hospital",
            "SeedShop",
            "JoshHouse",
            "Trailer",
            "Saloon",
            "Blacksmith",
            "SamHouse",
            "ManorHouse",
            "HaleyHouse",
            "ArchaeologyHouse",
            "ElliottHouse",
            "AdventureGuild",
            "Mine",
            "Tent",
        },
        "Trailer": {"Town"},
        "Tunnel": {"BusStop"},
        "WizardHouse": {"Forest"},
        "Woods": {"Forest"},
    }

    @classmethod
    def find_route(cls, start: Location, end: Location) -> Optional[List[Location]]:
        """
        🔍 根据静态写死的地图，寻找从起点到终点过门次数最少的最短场景路径
        :param start: 起点场景名 (如 "Farm")
        :param end: 终点场景名 (如 "SeedShop")
        :return: 路径场景名列表，如果无法连通则返回 None
        """
        # 门禁：防止传入不存在的场景名
        if start not in cls.GRAPH or end not in cls.GRAPH:
            print(f"❌ [MapService] 导航失败：找不到场景 【{start}】 或 【{end}】")
            return None

        if start == end:
            return [start]

        # BFS 队列：存储元组 (当前场景, 走到当前场景经历的路径数组)
        queue: deque[tuple[Location, List[Location]]] = deque([(start, [start])])
        # 履历集合：防止回溯死循环
        visited = {start}

        while queue:
            current_loc, current_path = queue.popleft()

            # 遍历当前场景写死的邻居们
            for neighbor in cls.GRAPH[current_loc]:
                if neighbor == end:
                    # 抓到终点，路径拼接完成，直接返回
                    return current_path + [neighbor]

                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, current_path + [neighbor]))

        return None

    @classmethod
    def find_candidate_routes(
        cls,
        start: Location,
        end: Location,
        max_routes: int = 12,
    ) -> List[List[Location]]:
        """
        枚举最少场景跳数的候选路线。

        距离评分依赖运行时 state，属于 RouteNode 的职责；这里仅负责静态场景图搜索。
        """
        if start not in cls.GRAPH or end not in cls.GRAPH:
            print(f"❌ [MapService] 导航失败：找不到场景 【{start}】 或 【{end}】")
            return []

        if start == end:
            return [[start]]

        candidate_routes: List[List[Location]] = []
        shortest_hops: int | None = None
        queue: deque[tuple[Location, List[Location]]] = deque([(start, [start])])

        while queue:
            current_location, current_path = queue.popleft()
            current_hops = len(current_path) - 1

            if shortest_hops is not None and current_hops >= shortest_hops:
                continue

            for neighbor in sorted(cls.GRAPH[current_location]):
                if neighbor in current_path:
                    continue

                next_path = current_path + [neighbor]
                next_hops = len(next_path) - 1

                if neighbor == end:
                    if shortest_hops is None:
                        shortest_hops = next_hops
                    if next_hops == shortest_hops:
                        candidate_routes.append(next_path)
                    if len(candidate_routes) >= max_routes:
                        return candidate_routes
                    continue

                if shortest_hops is not None and next_hops >= shortest_hops:
                    continue

                queue.append((neighbor, next_path))

        return candidate_routes
