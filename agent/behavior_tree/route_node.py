import threading
import time
from collections import deque
from typing import List, Optional, Tuple


from matplotlib.pylab import True_

from agent.action.location.location import Location
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.player_context import PlayerContext
from agent.base_task import BaseTask, TaskType
from agent.action.valley_action.AStar import astar_solver
from server.valley_server import StardewState, async_render


class RouteNode(BTNode):
    def __init__(self):
        self.route_start_time = None
        self.stardew_map = HardcodedStardewMap()
        self.global_current_path = []
        self.routes: List[Location] | None = None
        self.route_idx = -1
        self.is_doing = False  # 标记是否正在执行寻路任务

        self.IMAGE_FILE = "server/img/stardew_live_map.png"

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:

        if not blackboard.macro_plan or blackboard.current_step_index >= len(blackboard.macro_plan):
            self.route_start_time = None
            return "FAILURE"

        current_task = blackboard.macro_plan[blackboard.current_step_index]

        if not isinstance(current_task, RouteTask):
            self.route_start_time = None
            return "FAILURE"

        game_state = context.state

        # 进入 RouteNode 后，先检查当前玩家是否已经在目标地点了，如果是，就直接流转到下一个任务
        if not self.is_doing:
            if game_state:
                if game_state.location_name == current_task.target_loc:
                    blackboard.current_step_index += 1
                    self.route_start_time = None
                    self.routes = []
                    self.route_idx = -1
                    self.global_current_path = []
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
                        self.route_start_time = None
                        self.routes = []
                        self.route_idx = -1
                        self.global_current_path = []
                        self.is_doing = False

                        print(
                            f"\n🏆 [RouteNode] 耗时 {current_run_duration:.2f}s，双脚成功踩中目的地: 【{current_task.target_loc}】！"
                        )
                        return "SUCCESS"

                    target_location_name = self.routes[self.route_idx]
                current_blocked_tiles = astar_solver._get_blocked_tiles(game_state)

                target_warp_passable = True
                target_warp_tile = None
                for warp in game_state.warps:
                    if warp.target_location == target_location_name:
                        target_warp_passable = getattr(warp, "is_passable", True)
                        target_warp_tile = (warp.tile_x, warp.tile_y)
                        break
                is_deviated = False
                is_path_blocked = False

                if self.global_current_path:
                    first_path_tile = astar_solver.get_path_coords(self.global_current_path[0])
                    # 1. 基础偏航判定：如果当前玩家所处的格子，离路径规划的第一格相差超过 2 个网格，视为严重偏航
                    if (
                        abs(game_state.player_tile_x - first_path_tile[0]) > 2
                        or abs(game_state.player_tile_y - first_path_tile[1]) > 2
                    ):
                        is_deviated = True

                    # 2. 动态过期判定（核心修复）：检查缓存路径的未来 3 步之内，是否有格子在最新视野中变成了障碍物
                    # 如果未来要踩雷，说明路径已过期，必须立刻唤醒 A* 动态绕路！
                    look_ahead_steps = min(3, len(self.global_current_path))
                    for i in range(look_ahead_steps):
                        future_tile = astar_solver.get_path_coords(self.global_current_path[i])
                        # 如果未来这个格子刚好是不可通行的门，那这本身就是我们规划好的，不视作异常阻挡
                        if future_tile == target_warp_tile and not target_warp_passable:
                            continue
                        if future_tile in current_blocked_tiles:
                            # print(
                            #     f"👁️‍🗨️ [视野更新] 发现已规划的未来格子 {global_current_path[i]} 刷新了障碍物！激活 A* 动态绕路。"
                            # )
                            is_path_blocked = True
                            break
                # 防止到目的地后的空路径无限重算
                # 判定条件：如果路径空了，但我们人其实已经站在不可通行大门前（倒数第二格）了，那就坚决不重复调用 A*
                should_trigger_astar = False
                if not self.global_current_path:
                    # 如果大门不可通行，且我们已经在门口
                    is_already_at_blocked_door = (not target_warp_passable) and (
                        target_warp_tile is not None
                        and abs(game_state.player_tile_x - target_warp_tile[0]) <= 1
                        and abs(game_state.player_tile_y - target_warp_tile[1]) <= 1
                    )
                    if not is_already_at_blocked_door:
                        should_trigger_astar = True
                elif is_deviated or is_path_blocked:
                    should_trigger_astar = True

                # 用于标记这一帧寻路是否陷入了绝路
                is_dead_end = False

                # 如果路径空了、偏航了、或者被新视野下的障碍物堵死了，才允许运行 A*
                if should_trigger_astar:
                    toal_tiles = astar_solver.get_goal_tiles(game_state, target_location_name)
                    new_path = astar_solver.find_path_to_warp_zone(
                        game_state, (game_state.player_tile_x, game_state.player_tile_y), toal_tiles
                    )

                    # 当发现目标被包裹、被障碍物堵死或无路可走时
                    if new_path is None:
                        if not toal_tiles:
                            print(
                                f"❌ [绝路停机] 无法在当前场景 {game_state.location_name} 中找到去往目标地点 [{target_location_name}] 的任何传送门！"
                            )
                        else:
                            print(
                                f"⚠️ [绝路停机] 视野内推断出目标 {toal_tiles} 已被障碍物彻底包裹，无法前往！执行紧急切停。"
                            )
                        # 1. 强行清空当前的全局记忆路径，防止继续消费过期的残余路径
                        self.global_current_path = []
                        # 2. 覆盖当前帧的 command，直接原地大推 IDLE 静止
                        command = StardewCommand(action=StardewAction.IDLE, key=[])
                        is_dead_end = True

                    else:
                        # 过滤试图开倒车的 A* 路径
                        # 如果旧路径已经被控制器推进切短了（比如此时第一格是 3），而新算出来的路径第一格却退回到 4
                        if self.global_current_path and new_path:
                            if astar_solver.get_path_coords(
                                self.global_current_path[0]
                            ) != astar_solver.get_path_coords(new_path[0]) and len(new_path) > len(
                                self.global_current_path
                            ):
                                # 判定新路径的下一步是不是在倒退回我们刚刚切掉的那个格子
                                if astar_solver.get_path_coords(new_path[0]) == (
                                    game_state.player_tile_x,
                                    game_state.player_tile_y,
                                ) and astar_solver.get_path_coords(new_path[1]) == astar_solver.get_path_coords(
                                    self.global_current_path[0]
                                ):
                                    # print("🛑 [拦截] 阻挡重算 A* 试图塞回已消费格子，强行抛弃新路径防止原地抽搐！")
                                    new_path = None

                        if new_path is not None:
                            self.global_current_path = astar_solver.annotate_path_points(
                                new_path,
                                target_warp_passable=target_warp_passable,
                                target_warp_tile=target_warp_tile,
                            )

                # 如果上面 new_path 成功算出来，它会正常走下面的 get_next_move_command
                # 如果上面 new_path 是 None 触发了“绝路停机”，因为 global_current_path 被清空，
                # 下面控制器也会安全返回 IDLE，双重保险保障角色绝对钉在原地不动。
                # 只有在非绝路停机状态下，才允许让控制器去接管驱动逻辑，防止 command 覆盖冲突
                if not is_dead_end:
                    command, self.global_current_path, is_blocked_door = astar_solver.get_next_move_command(
                        state=game_state,
                        current_path=self.global_current_path,
                        target_warp_passable=target_warp_passable,
                    )

                    # 需要开门
                    if is_blocked_door and len(self.global_current_path) == 0:
                        blackboard.require_open_door = True

                        self.route_start_time = None
                        self.routes = []
                        self.route_idx = -1
                        self.global_current_path = []
                        self.is_doing = False
                        print(f"🟡 [RouteNode] 发现前方有不可通行大门，触发开门节点！")
                        return "SUCCESS"
                    else:
                        blackboard.require_open_door = False
                context.executor_client.send_command(command)

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


class RouteTask(BaseTask):
    def __init__(self, task_type: TaskType, desc: str, target_loc: Location):
        super().__init__(task_type=task_type, desc=desc)
        self.target_loc: Location = target_loc


def stardew_survival_cost_function(
    current: Tuple[int, int], neighbor: Tuple[int, int], state: StardewState, base_cost: float
) -> Tuple[bool, float, str]:
    """
    return: (is_passable: bool, total_cost: float, action_type: str)
    """

    # 1. 🛑 提取硬阻挡图层 (死墙、静态不可交互障碍)
    # 这些格子属于“绝对不可逾越”，哪怕满级工具也破坏不了，直接熔断
    absolute_walls = state.layers.get("WALL", set()).union(state.layers.get("BUSH", set()))
    if neighbor in absolute_walls:
        return False, float("inf"), "blocked"

    # 2. 🚪 识别大门图层与交互开关
    # 假设你的图层里有大门或者带有锁性质的建筑物
    doors = state.layers.get("OBJECT", set())  # 游戏里许多门和障碍在 OBJECT 层
    # 这里你可以结合你 state 里的具体门禁状态判断，如果是关着的门
    if neighbor in doors and getattr(state, "is_door_at_tile", lambda x: False)(neighbor):
        # 门可以通过，但是需要开门成本 (例如多花费 2.0 秒动作成本)，标记为 "open_door"
        return True, base_cost + 2.0, "open_door"

    # 3. 🪓 识别可破坏的硬图层 (树、石头、大树桩)
    trees = state.layers.get("TREE_STUMP", set()).union(
        state.layers.get("T1", set()), state.layers.get("T2", set()), state.layers.get("T3", set())
    )
    stones = state.layers.get("STONE", set())

    # ------------------ 🌳 砍树决策细分 ------------------
    if neighbor in trees:
        # 门禁 1：检查工具。手里没斧头，物理上不可能砍开
        if not getattr(state, "player_has_tool", lambda x: False)("AXE"):
            return False, float("inf"), "blocked"

        # 门禁 2：检查体力。体力濒临耗尽（比如小于 15 点），拒绝砍树，逼迫算法绕行
        if getattr(state, "player_current_stamina", 100) < 15:
            return False, float("inf"), "blocked"

        # 门禁 3：🎒 背包格子与掉落物经济评估
        backpack_full = getattr(state, "is_backpack_full", lambda: False)()
        has_wood_stack = getattr(state, "has_item_stack", lambda x: False)("Wood")  # 包里是否有木头格子可堆叠

        # 算经济账：
        if not backpack_full or has_wood_stack:
            # 包没满，或者可以完美吸附堆叠。掉落的木头是有效资源，产生正向收益，抵消部分砍树痛苦
            reward_bonus = 3.0
        else:
            # 包满了且无法堆叠！掉落物掉在地上捡不起来，白白浪费。给予高额痛苦惩罚成本
            reward_bonus = -5.0

        # 砍树固定消耗高昂的时间与耐久成本 (比如 8.0)
        destroy_tree_cost = 8.0 - reward_bonus
        return True, base_cost + destroy_tree_cost, "destroy"

    # ------------------ 🪨 砸石头决策细分 ------------------
    if neighbor in stones:
        # 门禁 1：没稿子，物理上砸不开
        if not getattr(state, "player_has_tool", lambda x: False)("PICKAXE"):
            return False, float("inf"), "blocked"

        # 门禁 2：防累晕保险
        if getattr(state, "player_current_stamina", 100) < 10:
            return False, float("inf"), "blocked"

        # 门禁 3：🎒 检查石头掉落物容纳格子
        backpack_full = getattr(state, "is_backpack_full", lambda: False)()
        has_stone_stack = getattr(state, "has_item_stack", lambda x: False)("Stone")

        if not backpack_full or has_stone_stack:
            reward_bonus = 2.0  # 石头价值稍低，给予小幅度收益抵消
        else:
            reward_bonus = -4.0  # 满包惩罚

        destroy_stone_cost = 5.0 - reward_bonus
        return True, base_cost + destroy_stone_cost, "destroy"

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
