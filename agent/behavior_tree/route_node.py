import threading
import time
from collections import deque
from typing import List, Optional, Set, Tuple


from agent.action.location.location import Location
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.player_context import PlayerContext
from agent.base_task import BaseTask, TaskType
from agent.action.valley_action.AStar import RouteActionType, RouteTile, astar_solver
from server.valley_server import StardewState, async_render
from server.type import Tile

CLEARABLE_ROUTE_TYPES: set[RouteActionType] = {"weeds", "twig", "stone"}


class RouteNode(BTNode):
    def __init__(self):
        self.route_start_time = None
        self.stardew_map = HardcodedStardewMap()
        self.global_current_path = []
        self.routes: List[Location] | None = None
        self.route_idx = -1
        self.is_doing = False  # 标记是否正在执行寻路任务
        self.should_trigger_astar = True  # 优化寻路性能，只有到了下一个格子才出触发
        self.toal_tiles: Set[RouteTile] = set()
        self.current_task_signature: tuple[int, Location] | None = None
        self.last_location_name: Location | None = None

        self.IMAGE_FILE = "server/img/stardew_live_map.png"

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:

        if not blackboard.macro_plan or blackboard.current_step_index >= len(blackboard.macro_plan):
            self._reset_route_state()
            return "FAILURE"

        current_task = blackboard.macro_plan[blackboard.current_step_index]

        if not isinstance(current_task, RouteTask):
            self._reset_route_state()
            return "FAILURE"

        if blackboard.should_reset_route:
            blackboard.should_reset_route = False
            self._reset_route_state()

        task_signature = (blackboard.current_step_index, current_task.target_loc)
        if self.current_task_signature != task_signature:
            self._reset_route_state()
            self.current_task_signature = task_signature

        game_state = context.state

        # 进入 RouteNode 后，先检查当前玩家是否已经在目标地点了，如果是，就直接流转到下一个任务
        if not self.is_doing:
            if game_state:
                if game_state.location_name == current_task.target_loc:
                    blackboard.current_step_index += 1
                    self._reset_route_state()
                    print(f"\n🏆 [RouteNode] 当前已经在【{current_task.target_loc}】，流转到下一个任务！")
                    return "SUCCESS"
            else:
                return "RUNNING"

        self.is_doing = True
        if self.route_start_time is None:
            self.route_start_time = time.time()
            print(f"\n🏃‍♂️ [RouteNode] 开始寻路任务，开始全速前往目的地: 【{current_task.target_loc}】...")

        if game_state:
            current_run_duration = time.time() - self.route_start_time

            if not self.routes:
                self.routes = self.stardew_map.find_route(game_state.location_name, current_task.target_loc)
                self.route_idx = 0
                if self.routes:
                    self.routes = self.routes[1:]

            if self.routes:
                target_location_name = self.routes[self.route_idx]
                if game_state.location_name == target_location_name:
                    self.route_idx += 1

                    if self.route_idx == len(self.routes):
                        blackboard.current_step_index += 1
                        self._reset_route_state()

                        print(
                            f"\n🏆 [RouteNode] 耗时 {current_run_duration:.2f}s，双脚成功踩中目的地: 【{current_task.target_loc}】！"
                        )
                        return "SUCCESS"

                    target_location_name = self.routes[self.route_idx]
                    self.global_current_path = []
                    self.should_trigger_astar = True
                    self.toal_tiles = set()

                replan_reason = self._get_replan_reason(game_state)
                if self.should_trigger_astar or replan_reason:
                    if len(self.toal_tiles) == 0:
                        self.toal_tiles = astar_solver.get_goal_tiles(game_state, target_location_name)

                    # def cost_fn(
                    #     current: Tile, neighbor: Tile, state: StardewState, base_cost: float
                    # ) -> Tuple[bool, float, RouteActionType]:
                    #     if (neighbor.x, neighbor.y) in blackboard.failed_clear_obstacles:
                    #         return False, float("inf"), "blocked"
                    #     return route_cost_function(current, neighbor, state, base_cost)

                    new_path = astar_solver.find_path_to_warp_zone(
                        game_state,
                        RouteTile(*game_state.player_tile, type="walk"),
                        self.toal_tiles,
                        # cost_fn,
                    )

                    if new_path is None:
                        if not self.toal_tiles:
                            print(
                                f"❌ [绝路停机] 无法在当前场景 {game_state.location_name} 中找到去往目标地点 [{target_location_name}] 的任何传送门！"
                            )
                        else:
                            print(
                                f"⚠️ [绝路停机] 视野内推断出目标 {self.toal_tiles} 已被障碍物彻底包裹，无法前往！执行紧急切停。"
                            )
                        self.global_current_path: List[RouteTile] = []

                        return "FAILURE"

                    else:
                        if self._is_backtracking_path(game_state, new_path):
                            new_path = None

                        if new_path is not None:
                            self.should_trigger_astar = False
                            self.global_current_path = new_path
                            self.last_location_name = game_state.location_name
                        elif self.global_current_path:
                            self.should_trigger_astar = False

                if not self.should_trigger_astar:
                    if blackboard.is_opening_door:
                        return "RUNNING"

                    clear_obstacle_tile = self._get_next_reachable_clear_obstacle_tile(game_state, blackboard)
                    if clear_obstacle_tile is not None:
                        blackboard.require_clear_obstacle = True
                        blackboard.clear_obstacle_tile = Tile(clear_obstacle_tile.x, clear_obstacle_tile.y)
                        blackboard.clear_obstacle_type = clear_obstacle_tile.type

                        self.should_trigger_astar = True
                        self.toal_tiles = set()
                        print(
                            f"\n🟡 [RouteNode] 发现必要清障点: {clear_obstacle_tile.type} @ {clear_obstacle_tile}，触发清障节点！"
                        )
                        return "SUCCESS"

                    if self._is_next_tile_door():
                        command, is_ready_to_open_door = self._build_door_command(game_state)
                        context.executor_client.send_command(command)
                        if is_ready_to_open_door:
                            blackboard.require_open_door = True
                            blackboard.is_opening_door = True
                            self.should_trigger_astar = True
                            self.toal_tiles = set()
                            print(f"🟡 [RouteNode] 发现前方有不可通行大门，触发开门节点！")
                            return "RUNNING"

                        return "RUNNING"

                    command, self.global_current_path, _should_trigger_astar = astar_solver.get_next_move_command(
                        state=game_state,
                        current_path=self.global_current_path,
                    )

                    blackboard.require_open_door = False

                    if _should_trigger_astar:
                        self.should_trigger_astar = len(self.global_current_path) < 2

                    context.executor_client.send_command(command)
                    print(command.action)

                    print(
                        f"\r🏃‍♂️ [RouteNode] 正在前往 【{current_task.target_loc}】 途中... 已奔跑 {current_run_duration:.2f}s",
                        end="",
                    )
                    if "render_thread" not in locals() or not render_thread.is_alive():  # type: ignore
                        render_thread = threading.Thread(
                            target=async_render,
                            args=(game_state, self.IMAGE_FILE, 40, self.global_current_path.copy()),
                            daemon=True,
                        )
                        render_thread.start()

                return "RUNNING"
            else:
                raise ValueError(
                    f"❌ [RouteNode]：从【{game_state.location_name}】到【{current_task.target_loc}】无法寻路！"
                )
        else:
            return "RUNNING"

    def _is_next_tile_door(self) -> bool:
        return len(self.global_current_path) >= 2 and self.global_current_path[1].type == "door"

    def _build_door_command(self, game_state: StardewState) -> tuple[StardewCommand, bool]:
        anchor_tile = self.global_current_path[0]
        door_tile = self.global_current_path[1]

        if game_state.player_tile != anchor_tile:
            return astar_solver._build_move_command_to_tile(game_state, anchor_tile), False

        print(f"\n🏁 [Door] 已站到门前格，面向门 {door_tile} 并触发开门节点。")
        return astar_solver._build_face_command(anchor_tile, door_tile), True

    def _reset_route_state(self) -> None:
        self.route_start_time = None
        self.routes = []
        self.route_idx = -1
        self.global_current_path = []
        self.is_doing = False
        self.should_trigger_astar = True
        self.toal_tiles = set()
        self.current_task_signature = None
        self.last_location_name = None

    def _get_replan_reason(self, game_state: StardewState) -> str | None:
        if not self.global_current_path:
            return "路径为空"

        if len(self.global_current_path) < 2:
            return "路径过短"

        if self.last_location_name is not None and game_state.location_name != self.last_location_name:
            self.global_current_path = []
            self.toal_tiles = set()
            return "场景变化"

        first_path_tile = self.global_current_path[0]
        if (
            abs(game_state.player_tile.x - first_path_tile.x) > 2
            or abs(game_state.player_tile.y - first_path_tile.y) > 2
        ):
            return f"玩家偏离路径: player={game_state.player_tile}, path={first_path_tile}"

        blocked_tiles = astar_solver._get_blocked_tiles(game_state)
        look_ahead_steps = min(5, len(self.global_current_path))
        for future_tile in self.global_current_path[:look_ahead_steps]:
            if future_tile.type == "door":
                continue
            if future_tile.type in CLEARABLE_ROUTE_TYPES:
                continue
            if future_tile in blocked_tiles:
                return f"未来路径被阻挡: {future_tile}"

        return None

    def _get_next_reachable_clear_obstacle_tile(
        self, game_state: StardewState, blackboard: AgentBlackboard
    ) -> RouteTile | None:
        for route_tile in self.global_current_path[1:6]:
            if route_tile.type not in CLEARABLE_ROUTE_TYPES:
                continue
            if (route_tile.x, route_tile.y) in blackboard.failed_clear_obstacles:
                continue
            if self._is_player_next_to_tile(game_state.player_tile, route_tile):
                return route_tile
            return None

        return None

    def _is_player_next_to_tile(self, player_tile: Tile, target_tile: Tile) -> bool:
        distance_x = abs(player_tile.x - target_tile.x)
        distance_y = abs(player_tile.y - target_tile.y)
        return max(distance_x, distance_y) == 1

    def _is_backtracking_path(self, game_state: StardewState, new_path: List[RouteTile]) -> bool:
        if not self.global_current_path or len(new_path) < 2:
            return False

        if self.global_current_path[0] == new_path[0] or len(new_path) <= len(self.global_current_path):
            return False

        return new_path[0] == game_state.player_tile and new_path[1] == self.global_current_path[0]


class RouteTask(BaseTask):
    def __init__(self, task_type: TaskType, desc: str, target_loc: Location):
        super().__init__(task_type=task_type, desc=desc)
        self.target_loc: Location = target_loc


def route_cost_function(
    current: Tile, neighbor: Tile, state: StardewState, base_cost: float
) -> Tuple[bool, float, RouteActionType]:
    """
    return: (is_passable: bool, total_cost: float, action_type: str)
    """

    # 1. 🛑 提取硬阻挡图层 (死墙、静态不可交互障碍)
    # 这些格子属于“绝对不可逾越”，哪怕满级工具也破坏不了，直接熔断
    absolute_walls = state.layers.get("Wall", set()).union(state.layers.get("Bush", set()))
    if neighbor in absolute_walls:
        return False, float("inf"), "blocked"

    # # 2. 🚪 识别大门图层与交互开关
    # # 假设你的图层里有大门或者带有锁性质的建筑物
    # doors = state.layers.get("OBJECT", set())  # 游戏里许多门和障碍在 OBJECT 层
    # # 这里你可以结合你 state 里的具体门禁状态判断，如果是关着的门
    # if neighbor in doors and getattr(state, "is_door_at_tile", lambda x: False)(neighbor):
    #     # 门可以通过，但是需要开门成本 (例如多花费 2.0 秒动作成本)，标记为 "open_door"
    #     return True, base_cost + 2.0, "open_door"

    # 3. 🪓 识别可破坏的硬图层 (树、石头、大树桩)
    trees = state.layers.get("Tree5", set()).union(
        state.layers.get("Tree1", set()),
        state.layers.get("Tree2", set()),
        state.layers.get("Tree3", set()),
        state.layers.get("Tree4", set()),
    )
    stones = state.layers.get("Stone", set())
    weeds = state.layers.get("Weeds", set())
    twigs = state.layers.get("Twig", set())

    if neighbor in weeds:
        return True, base_cost, "weeds"

    if neighbor in twigs:
        return True, base_cost, "twig"

    # if neighbor in trees:
    #     return True, base_cost + 30.0, "tree"

    # ------------------ 🌳 砍树决策细分 ------------------
    # if neighbor in trees:
    #     # 门禁 1：检查工具。手里没斧头，物理上不可能砍开
    #     if not getattr(state, "player_has_tool", lambda x: False)("AXE"):
    #         return False, float("inf"), "blocked"

    #     # 门禁 2：检查体力。体力濒临耗尽（比如小于 15 点），拒绝砍树，逼迫算法绕行
    #     if getattr(state, "player_current_stamina", 100) < 15:
    #         return False, float("inf"), "blocked"

    #     # 门禁 3：🎒 背包格子与掉落物经济评估
    #     backpack_full = getattr(state, "is_backpack_full", lambda: False)()
    #     has_wood_stack = getattr(state, "has_item_stack", lambda x: False)("Wood")  # 包里是否有木头格子可堆叠

    #     # 算经济账：
    #     if not backpack_full or has_wood_stack:
    #         # 包没满，或者可以完美吸附堆叠。掉落的木头是有效资源，产生正向收益，抵消部分砍树痛苦
    #         reward_bonus = 3.0
    #     else:
    #         # 包满了且无法堆叠！掉落物掉在地上捡不起来，白白浪费。给予高额痛苦惩罚成本
    #         reward_bonus = -5.0

    #     # 砍树固定消耗高昂的时间与耐久成本 (比如 8.0)
    #     destroy_tree_cost = 8.0 - reward_bonus
    #     return True, base_cost + destroy_tree_cost, "destroy"

    # ------------------ 🪨 砸石头决策细分 ------------------
    if neighbor in stones:
        # # 门禁 1：没稿子，物理上砸不开
        # if not getattr(state, "player_has_tool", lambda x: False)("PICKAXE"):
        #     return False, float("inf"), "blocked"

        # # 门禁 2：防累晕保险
        # if getattr(state, "player_current_stamina", 100) < 10:
        #     return False, float("inf"), "blocked"

        # # 门禁 3：🎒 检查石头掉落物容纳格子
        # backpack_full = getattr(state, "is_backpack_full", lambda: False)()
        # has_stone_stack = getattr(state, "has_item_stack", lambda x: False)("Stone")

        # if not backpack_full or has_stone_stack:
        #     reward_bonus = 2.0  # 石头价值稍低，给予小幅度收益抵消
        # else:
        #     reward_bonus = -4.0  # 满包惩罚

        # destroy_stone_cost = 5.0 - reward_bonus
        # return True, base_cost + destroy_stone_cost, "destroy"

        return True, base_cost, "stone"

    # 4. 🟢 顺畅空地放行
    # 没有任何硬图层阻挡，完全是康庄大道，保持原本的 A* 基础位移时间代价 (base_cost 是 1.0 或 1.414)
    return True, base_cost, "walk"


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
            # "Forest",
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
            "FishShop",
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
