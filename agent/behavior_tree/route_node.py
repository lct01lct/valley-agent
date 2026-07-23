import asyncio
import time
from typing import List, Set, Tuple

from agent.action.location.location import Location
from agent.action.map.map import HardcodedStardewMap
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.action.valley_action.clearance_policy import ORDINARY_TREE_LAYERS, decide_clear_obstacle
from agent.action.valley_action.tool_targeting import build_tool_target_face_command
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.route_debug_logger import RouteDebugLogger
from agent.behavior_tree.tool_selection import has_scythe_tree_seed_risk, select_required_tool_for_obstacle
from agent.base_task import BaseTask, TaskType
from agent.action.valley_action.move_controller import MoveController
from agent.action.valley_action.AStar import RouteActionType, RouteTile, astar_solver
from server.valley_server import StardewState
from server.type import Tile

CLEARABLE_ROUTE_TYPES: set[RouteActionType] = {"weeds", "twig", "stone", "tree"}
NEAR_REPLAN_DISTANCE = 2
MOVE_DEBUG_LOG_INTERVAL_SECONDS = 0.5
ROUTE_PROGRESS_PRINT_INTERVAL_SECONDS = 0.2
MOVE_INTERVAL_SPIKE_SECONDS = 0.08
EXECUTOR_SLOW_SEND_SECONDS = 0.05


class RouteNode(BTNode):
    def __init__(self):
        self.route_start_time = None
        self.stardew_map = HardcodedStardewMap()
        self.global_current_path: List[RouteTile] = []
        self.path_index = 1
        self.move_controller = MoveController()
        self.routes: List[Location] | None = None
        self.route_idx = -1
        self.is_doing = False  # 标记是否正在执行寻路任务
        self.should_trigger_astar = True
        self.toal_tiles: Set[RouteTile] = set()
        self.current_task_signature: tuple[int, Location] | None = None
        self.last_location_name: Location | None = None
        self.last_move_command_at: float | None = None
        self.last_command_interval: float | None = None
        self.last_send_duration: float | None = None
        self.last_move_debug_log_at: float | None = None
        self.last_move_debug_signature: tuple[StardewAction, int, int] | None = None
        self.last_progress_print_at: float | None = None
        self.route_debug_logger = RouteDebugLogger()
        self.clear_obstacle_debug_logger = RouteDebugLogger("logs/clear_obstacle_debug.log")
        self.last_replan_distance: int | None = None
        self.pending_astar_task: asyncio.Task[List[RouteTile] | None] | None = None
        self.pending_astar_reason: str | None = None
        self.pending_astar_target_location: Location | None = None
        self.pending_astar_started_at: float | None = None
        self.failed_route_signature: tuple[int, Location, Location, Location] | None = None
        self.last_clear_obstacle_debug_signature: tuple | None = None
        self.scene_warp_distance_cache: dict[Location, dict[Location, float]] = {}

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
            self._update_scene_warp_distance_cache(game_state)

            if not self.routes:
                self.routes = self._select_best_scene_route(game_state, current_task.target_loc)
                self.route_idx = 0
                if self.routes:
                    self.routes = self.routes[1:]

            if self.routes:
                target_location_name = self.routes[self.route_idx]
                if game_state.location_name == target_location_name:
                    self.route_idx += 1

                    if self.route_idx == len(self.routes):
                        context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
                        blackboard.current_step_index += 1
                        self._reset_route_state()

                        print(
                            f"\n🏆 [RouteNode] 耗时 {current_run_duration:.2f}s，双脚成功踩中目的地: 【{current_task.target_loc}】！"
                        )
                        return "SUCCESS"

                    target_location_name = self.routes[self.route_idx]
                    self.global_current_path = []
                    self.path_index = 1
                    self.should_trigger_astar = True
                    self.toal_tiles = set()

                self._consume_pending_astar_result(game_state, target_location_name)
                replan_reason = self._get_replan_reason(game_state, blackboard)
                failure_signature = (
                    blackboard.current_step_index,
                    current_task.target_loc,
                    game_state.location_name,
                    target_location_name,
                )

                if self.failed_route_signature == failure_signature:
                    return "FAILURE"

                if replan_reason and not self.should_trigger_astar and self._should_replan_in_background():
                    self._start_background_astar(game_state, target_location_name, blackboard, replan_reason)
                    replan_reason = None

                if (
                    replan_reason
                    and not self.should_trigger_astar
                    and self._has_pending_astar()
                    and self._should_wait_for_pending_replan()
                ):
                    self._log_route_debug(
                        f"障碍已接近，等待后台 A*: reason={replan_reason}, "
                        f"distance={self.last_replan_distance}, path={self.path_index}/{len(self.global_current_path)}"
                    )
                    context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
                    return "RUNNING"

                if self.should_trigger_astar or replan_reason:
                    astar_reason = replan_reason or "should_trigger_astar"
                    astar_start_time = time.perf_counter()
                    if len(self.toal_tiles) == 0:
                        self.toal_tiles = astar_solver.get_goal_tiles(game_state, target_location_name)

                    self._log_route_debug(
                        f"触发 A*: reason={astar_reason}, "
                        f"loc={game_state.location_name}, target={target_location_name}, "
                        f"routes={self.routes}, route_idx={self.route_idx}, final_target={current_task.target_loc}, "
                        f"player={game_state.player_tile}, path_index={self.path_index}, "
                        f"path_len={len(self.global_current_path)}"
                    )

                    def cost_fn(
                        current: Tile, neighbor: Tile, state: StardewState, base_cost: float
                    ) -> Tuple[bool, float, RouteActionType]:
                        if (neighbor.x, neighbor.y) in blackboard.failed_clear_obstacles:
                            return False, float("inf"), "blocked"
                        return route_cost_function(current, neighbor, state, base_cost)

                    new_path = astar_solver.find_path_to_warp_zone(
                        game_state,
                        RouteTile(*game_state.player_tile, type="walk"),
                        self.toal_tiles,
                        cost_fn,
                    )
                    astar_duration_ms = (time.perf_counter() - astar_start_time) * 1000

                    if new_path is None:
                        if not self.toal_tiles:
                            failure_reason = (
                                f"无法在当前场景 {game_state.location_name} 中找到去往目标地点 "
                                f"[{target_location_name}] 的任何传送门"
                            )
                            print(f"\n ❌ [绝路停机] {failure_reason}！")
                        else:
                            failure_reason = f"视野内推断出目标 {self.toal_tiles} 已被障碍物彻底包裹，无法前往"
                            print(f"\n ⚠️ [绝路停机] {failure_reason}！执行紧急切停。")

                        self._stop_route_on_failure(
                            context=context,
                            blackboard=blackboard,
                            failure_signature=failure_signature,
                            reason=failure_reason,
                            target_location_name=target_location_name,
                            current_task=current_task,
                        )
                        return "FAILURE"

                    else:
                        if self._is_backtracking_path(game_state, new_path):
                            new_path = None

                        if new_path is not None:
                            self.should_trigger_astar = False
                            self.global_current_path = new_path
                            self.path_index = 1 if len(new_path) > 1 else 0
                            self.last_location_name = game_state.location_name
                            self._log_route_debug(
                                f"A* 完成: cost={astar_duration_ms:.1f}ms, "
                                f"path_len={len(new_path)}, next={self._get_next_path_tile()}"
                            )
                            self._log_clear_obstacles_in_path(game_state, new_path, "A* 完成")
                        elif self.global_current_path:
                            self.should_trigger_astar = False
                            self._log_route_debug(
                                f"A* 结果被丢弃，继续沿用旧路径: "
                                f"cost={astar_duration_ms:.1f}ms, path_index={self.path_index}, "
                                f"path_len={len(self.global_current_path)}"
                            )

                if not self.should_trigger_astar:
                    if blackboard.is_opening_door:
                        return "RUNNING"

                    clear_obstacle_tile = self._get_next_reachable_clear_obstacle_tile(game_state, blackboard)
                    if clear_obstacle_tile is not None:
                        target_tile = Tile(clear_obstacle_tile.x, clear_obstacle_tile.y)
                        required_tool = select_required_tool_for_obstacle(
                            game_state,
                            clear_obstacle_tile.type,
                            target_tile,
                            "Route",
                        )
                        context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
                        blackboard.require_switch_tool = required_tool is not None
                        blackboard.is_switching_tool = required_tool is not None
                        blackboard.required_tool_owner = "Route" if required_tool is not None else None
                        blackboard.required_tool = required_tool
                        blackboard.require_clear_obstacle = True
                        blackboard.clear_obstacle_owner = "Route"
                        blackboard.clear_obstacle_tile = target_tile
                        blackboard.clear_obstacle_type = clear_obstacle_tile.type

                        self.should_trigger_astar = True
                        self.toal_tiles = set()
                        self._log_clear_obstacle_debug(
                            f"触发清障节点: tile={clear_obstacle_tile}, type={clear_obstacle_tile.type}, "
                            f"required_tool={required_tool}, player={game_state.player_tile}, "
                            f"scythe_tree_seed_risk={has_scythe_tree_seed_risk(game_state, target_tile)}, "
                            f"path_index={self.path_index}, path_len={len(self.global_current_path)}, "
                            f"CurrentToolIndex={game_state.inventory.current_tool_index}, "
                            f"CurrentToolbarIndex={game_state.inventory.current_toolbar_index}"
                        )
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

                    command, self.path_index, is_path_finished = self.move_controller.get_next_move_command(
                        state=game_state,
                        tile_path=self.global_current_path,
                        path_index=self.path_index,
                    )

                    blackboard.require_open_door = False

                    if is_path_finished:
                        self.should_trigger_astar = True

                    now = time.perf_counter()
                    if self.last_move_command_at is not None:
                        self.last_command_interval = now - self.last_move_command_at
                        if self.last_command_interval > MOVE_INTERVAL_SPIKE_SECONDS:
                            self._log_route_debug(
                                f"移动命令间隔偏高: "
                                f"interval={self.last_command_interval * 1000:.1f}ms, "
                                f"cmd={command.action}, path_index={self.path_index}, "
                                f"path_len={len(self.global_current_path)}"
                            )
                    self.last_move_command_at = now

                    send_start_time = time.perf_counter()
                    context.executor_client.send_command(command)
                    self.last_send_duration = time.perf_counter() - send_start_time
                    if self.last_send_duration > EXECUTOR_SLOW_SEND_SECONDS:
                        self._log_route_debug(
                            f"Executor 响应偏慢: " f"send={self.last_send_duration * 1000:.1f}ms, cmd={command.action}"
                        )
                    if self._should_log_move_debug(command, now):
                        self._log_route_debug(
                            f"移动命令: cmd={command.action}, "
                            f"cmdHz={self._format_command_frequency()}, "
                            f"send={self._format_send_duration()}, "
                            f"path={self.path_index}/{len(self.global_current_path)}, "
                            f"player={game_state.player_tile}"
                        )

                    if self._should_print_progress(now):
                        print(
                            f"\r🏃‍♂️ [RouteNode] 正在前往 【{current_task.target_loc}】 途中... "
                            f"已奔跑 {current_run_duration:.2f}s",
                            end="",
                        )

                return "RUNNING"
            else:
                raise ValueError(
                    f"❌ [RouteNode]：从【{game_state.location_name}】到【{current_task.target_loc}】无法寻路！"
                )
        else:
            return "RUNNING"

    def _is_next_tile_door(self) -> bool:
        next_tile = self._get_next_path_tile()
        return next_tile is not None and next_tile.type == "door"

    def _build_door_command(self, game_state: StardewState) -> tuple[StardewCommand, bool]:
        door_tile = self._get_next_path_tile()
        if door_tile is None:
            return StardewCommand(action=StardewAction.IDLE), False

        anchor_index = max(0, self.path_index - 1)
        anchor_tile = self.global_current_path[anchor_index]

        if not self.move_controller.is_player_inside_tile(game_state, anchor_tile):
            return self.move_controller.build_move_command_to_tile(game_state, anchor_tile), False

        print(f"\n🏁 [Door] 已站到门前格，面向门 {door_tile} 并触发开门节点。")
        return build_tool_target_face_command(anchor_tile, door_tile), True

    def _select_best_scene_route(self, game_state: StardewState, target_location: Location) -> List[Location] | None:
        candidate_routes = self.stardew_map.find_candidate_routes(
            game_state.location_name,
            target_location,
        )
        if not candidate_routes:
            return None

        scored_routes = [self._score_scene_route(game_state, route) for route in candidate_routes]
        selected_route = min(scored_routes, key=lambda scored_route: scored_route["score"])

        score_lines = []
        for scored_route in sorted(scored_routes, key=lambda item: item["score"]):
            route_text = "->".join(scored_route["route"])
            score_lines.append(
                f"route={route_text}, hops={scored_route['hops']}, "
                f"first={scored_route['first_edge_distance']:.1f}, "
                f"known_sum={scored_route['known_distance_sum']:.1f}, "
                f"unknown={scored_route['unknown_edge_count']}, score={scored_route['score']}"
            )

        self._log_route_debug(
            "候选跨场景路线评分: " + " | ".join(score_lines) + f" | selected={'->'.join(selected_route['route'])}"
        )
        return selected_route["route"]

    def _score_scene_route(self, game_state: StardewState, route: List[Location]) -> dict:
        hops = max(0, len(route) - 1)
        edge_distances: list[float] = []
        unknown_edge_count = 0

        for index in range(hops):
            source_location = route[index]
            target_location = route[index + 1]
            edge_distance = self._get_scene_edge_distance(
                game_state=game_state,
                source_location=source_location,
                target_location=target_location,
            )
            if edge_distance is None:
                unknown_edge_count += 1
                edge_distance = 0.0
            edge_distances.append(edge_distance)

        first_edge_distance = edge_distances[0] if edge_distances else 0.0
        known_distance_sum = sum(edge_distances)

        if unknown_edge_count > 0:
            score = (hops, first_edge_distance, known_distance_sum, unknown_edge_count)
        else:
            score = (hops, known_distance_sum, first_edge_distance, unknown_edge_count)

        return {
            "route": route,
            "hops": hops,
            "first_edge_distance": first_edge_distance,
            "known_distance_sum": known_distance_sum,
            "unknown_edge_count": unknown_edge_count,
            "score": score,
        }

    def _get_scene_edge_distance(
        self,
        game_state: StardewState,
        source_location: Location,
        target_location: Location,
    ) -> float | None:
        if source_location == game_state.location_name:
            return self._get_current_scene_warp_distance(game_state, target_location)

        return self.scene_warp_distance_cache.get(source_location, {}).get(target_location)

    def _update_scene_warp_distance_cache(self, game_state: StardewState) -> None:
        if not game_state.warps:
            return

        scene_distances = self.scene_warp_distance_cache.setdefault(game_state.location_name, {})
        for warp in game_state.warps:
            distance = self._get_tile_distance(game_state.player_tile, warp.tile)
            previous_distance = scene_distances.get(warp.target_location)
            if previous_distance is None or distance < previous_distance:
                scene_distances[warp.target_location] = distance

    def _get_current_scene_warp_distance(
        self,
        game_state: StardewState,
        target_location: Location,
    ) -> float | None:
        candidate_distances = [
            self._get_tile_distance(game_state.player_tile, warp.tile)
            for warp in game_state.warps
            if warp.target_location == target_location
        ]
        if not candidate_distances:
            return None
        return min(candidate_distances)

    def _get_tile_distance(self, start_tile: Tile, end_tile: Tile) -> float:
        return abs(start_tile.x - end_tile.x) + abs(start_tile.y - end_tile.y)

    def _reset_route_state(self) -> None:
        self.route_start_time = None
        self.routes = []
        self.route_idx = -1
        self.global_current_path = []
        self.path_index = 1
        self.move_controller.reset()
        self.is_doing = False
        self.should_trigger_astar = True
        self.toal_tiles = set()
        self.current_task_signature = None
        self.last_location_name = None
        self.last_move_command_at = None
        self.last_command_interval = None
        self.last_send_duration = None
        self.last_move_debug_log_at = None
        self.last_move_debug_signature = None
        self.last_progress_print_at = None
        self.last_replan_distance = None
        if self.pending_astar_task is not None and not self.pending_astar_task.done():
            self.pending_astar_task.cancel()
        self.pending_astar_task = None
        self.pending_astar_reason = None
        self.pending_astar_target_location = None
        self.pending_astar_started_at = None
        self.failed_route_signature = None
        self.last_clear_obstacle_debug_signature = None

    def _get_replan_reason(self, game_state: StardewState, blackboard: AgentBlackboard) -> str | None:
        self.last_replan_distance = None

        if not self.global_current_path:
            return "路径为空"

        if len(self.global_current_path) < 2:
            return "路径过短"

        if self.last_location_name is not None and game_state.location_name != self.last_location_name:
            self.global_current_path = []
            self.path_index = 1
            self.toal_tiles = set()
            return "场景变化"

        anchor_index = max(0, min(self.path_index - 1, len(self.global_current_path) - 1))
        first_path_tile = self.global_current_path[anchor_index]
        if (
            abs(game_state.player_tile.x - first_path_tile.x) > 2
            or abs(game_state.player_tile.y - first_path_tile.y) > 2
        ):
            return f"玩家偏离路径: player={game_state.player_tile}, path={first_path_tile}"

        look_ahead_end = min(self.path_index + 5, len(self.global_current_path))
        for future_index in range(self.path_index, look_ahead_end):
            previous_tile = self.global_current_path[future_index - 1]
            future_tile = self.global_current_path[future_index]
            replan_distance = future_index - self.path_index

            if future_tile.type == "door":
                continue
            if future_tile in self.toal_tiles:
                continue
            if future_tile.type in CLEARABLE_ROUTE_TYPES:
                continue
            if (future_tile.x, future_tile.y) in blackboard.failed_clear_obstacles:
                self.last_replan_distance = replan_distance
                return f"未来路径包含已失败清障点: {future_tile}"

            is_passable, _, current_action_type = route_cost_function(previous_tile, future_tile, game_state, 1.0)
            if not is_passable:
                self.last_replan_distance = replan_distance
                return f"未来路径被阻挡: {future_tile}"
            if current_action_type in CLEARABLE_ROUTE_TYPES and future_tile.type != current_action_type:
                self.last_replan_distance = replan_distance
                signature = (
                    "dynamic_clear_obstacle",
                    game_state.location_name,
                    game_state.player_tile.x,
                    game_state.player_tile.y,
                    self.path_index,
                    future_tile.x,
                    future_tile.y,
                    current_action_type,
                )
                if signature != self.last_clear_obstacle_debug_signature:
                    self.last_clear_obstacle_debug_signature = signature
                    self._log_clear_obstacle_debug(
                        f"未来路径动态发现可清障障碍: action_type={current_action_type}, "
                        f"path_tile={future_tile}, path_tile_type={future_tile.type}, "
                        f"player={game_state.player_tile}, path_index={self.path_index}, "
                        f"future_index={future_index}, distance={replan_distance}, "
                        f"CurrentToolIndex={game_state.inventory.current_tool_index}, "
                        f"CurrentToolbarIndex={game_state.inventory.current_toolbar_index}"
                    )
                return f"未来路径出现新清障点: {current_action_type} @ {future_tile}"

        return None

    def _get_next_reachable_clear_obstacle_tile(
        self, game_state: StardewState, blackboard: AgentBlackboard
    ) -> RouteTile | None:
        look_ahead_end = min(self.path_index + 5, len(self.global_current_path))
        for route_tile in self.global_current_path[self.path_index : look_ahead_end]:
            if route_tile.type not in CLEARABLE_ROUTE_TYPES:
                continue
            if (route_tile.x, route_tile.y) in blackboard.failed_clear_obstacles:
                self._log_clear_obstacle_candidate(
                    game_state=game_state,
                    route_tile=route_tile,
                    look_ahead_end=look_ahead_end,
                    reason="已标记为清障失败，跳过",
                    is_reachable=False,
                )
                continue
            if self._is_player_cardinally_next_to_tile(game_state.player_tile, route_tile):
                self._log_clear_obstacle_candidate(
                    game_state=game_state,
                    route_tile=route_tile,
                    look_ahead_end=look_ahead_end,
                    reason="玩家已在上下左右相邻格，可触发清障",
                    is_reachable=True,
                )
                return route_tile
            self._log_clear_obstacle_candidate(
                game_state=game_state,
                route_tile=route_tile,
                look_ahead_end=look_ahead_end,
                reason="玩家尚未到达相邻格，继续移动靠近",
                is_reachable=False,
            )
            return None

        return None

    def _log_clear_obstacles_in_path(
        self,
        game_state: StardewState,
        path: List[RouteTile],
        reason: str,
    ) -> None:
        clear_obstacle_tiles = [route_tile for route_tile in path if route_tile.type in CLEARABLE_ROUTE_TYPES]
        if not clear_obstacle_tiles:
            return

        preview = ", ".join(
            f"{route_tile.type}@({route_tile.x},{route_tile.y})" for route_tile in clear_obstacle_tiles[:8]
        )
        self._log_clear_obstacle_debug(
            f"路径包含可清障障碍: reason={reason}, loc={game_state.location_name}, "
            f"player={game_state.player_tile}, path_index={self.path_index}, "
            f"count={len(clear_obstacle_tiles)}, preview=[{preview}]"
        )

    def _log_clear_obstacle_candidate(
        self,
        game_state: StardewState,
        route_tile: RouteTile,
        look_ahead_end: int,
        reason: str,
        is_reachable: bool,
    ) -> None:
        target_tile = Tile(route_tile.x, route_tile.y)
        required_tool = select_required_tool_for_obstacle(game_state, route_tile.type, target_tile, "Route")
        signature = (
            game_state.location_name,
            game_state.player_tile.x,
            game_state.player_tile.y,
            self.path_index,
            route_tile.x,
            route_tile.y,
            route_tile.type,
            reason,
        )
        if signature == self.last_clear_obstacle_debug_signature:
            return

        self.last_clear_obstacle_debug_signature = signature
        self._log_clear_obstacle_debug(
            f"清障候选: tile={route_tile}, type={route_tile.type}, required_tool={required_tool}, "
            f"scythe_tree_seed_risk={has_scythe_tree_seed_risk(game_state, target_tile)}, "
            f"is_reachable={is_reachable}, reason={reason}, player={game_state.player_tile}, "
            f"path_index={self.path_index}, lookahead_end={look_ahead_end}, "
            f"path_len={len(self.global_current_path)}, CurrentToolIndex={game_state.inventory.current_tool_index}, "
            f"CurrentToolbarIndex={game_state.inventory.current_toolbar_index}"
        )

    def _get_next_path_tile(self) -> RouteTile | None:
        if self.path_index >= len(self.global_current_path):
            return None
        return self.global_current_path[self.path_index]

    def _get_remaining_path(self) -> List[RouteTile]:
        start_index = max(0, self.path_index - 1)
        return self.global_current_path[start_index:].copy()

    def _format_command_frequency(self) -> str:
        if self.last_command_interval is None or self.last_command_interval <= 0:
            return "--"
        return f"{1 / self.last_command_interval:.1f}Hz"

    def _format_send_duration(self) -> str:
        if self.last_send_duration is None:
            return "--"
        return f"{self.last_send_duration * 1000:.1f}ms"

    def _should_log_move_debug(self, command: StardewCommand, now: float) -> bool:
        current_signature = (command.action, self.path_index, len(self.global_current_path))
        is_first_log = self.last_move_debug_log_at is None
        is_signature_changed = current_signature != self.last_move_debug_signature
        is_interval_due = (
            self.last_move_debug_log_at is not None
            and now - self.last_move_debug_log_at >= MOVE_DEBUG_LOG_INTERVAL_SECONDS
        )

        if not is_first_log and not is_signature_changed and not is_interval_due:
            return False

        self.last_move_debug_log_at = now
        self.last_move_debug_signature = current_signature
        return True

    def _should_print_progress(self, now: float) -> bool:
        if self.last_progress_print_at is None:
            self.last_progress_print_at = now
            return True
        if now - self.last_progress_print_at < ROUTE_PROGRESS_PRINT_INTERVAL_SECONDS:
            return False

        self.last_progress_print_at = now
        return True

    def _log_route_debug(self, message: str) -> None:
        self.route_debug_logger.log(f"[RouteNode] {message}")

    def _log_clear_obstacle_debug(self, message: str) -> None:
        self.clear_obstacle_debug_logger.log(f"[RouteNode] {message}")

    def _stop_route_on_failure(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        failure_signature: tuple[int, Location, Location, Location],
        reason: str,
        target_location_name: Location,
        current_task: "RouteTask",
    ) -> None:
        context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
        if self.pending_astar_task is not None and not self.pending_astar_task.done():
            self.pending_astar_task.cancel()
        self.pending_astar_task = None
        self.pending_astar_reason = None
        self.pending_astar_target_location = None
        self.pending_astar_started_at = None
        self.global_current_path = []
        self.path_index = 1
        self.should_trigger_astar = False
        self.toal_tiles = set()
        self.failed_route_signature = failure_signature
        blackboard.prompt = (
            f"RouteNode 寻路失败: {reason}; "
            f"current_loc={failure_signature[2]}, target_loc={target_location_name}, "
            f"final_target={current_task.target_loc}, routes={self.routes}"
        )
        self._log_route_debug(
            f"绝路停机: reason={reason}, signature={failure_signature}, "
            f"routes={self.routes}, route_idx={self.route_idx}, final_target={current_task.target_loc}"
        )

    def _should_replan_in_background(self) -> bool:
        if self.last_replan_distance is None:
            return False
        return self.last_replan_distance > NEAR_REPLAN_DISTANCE

    def _should_wait_for_pending_replan(self) -> bool:
        if self.last_replan_distance is None:
            return False
        return self.last_replan_distance <= NEAR_REPLAN_DISTANCE

    def _has_pending_astar(self) -> bool:
        return self.pending_astar_task is not None and not self.pending_astar_task.done()

    def _start_background_astar(
        self,
        game_state: StardewState,
        target_location_name: Location,
        blackboard: AgentBlackboard,
        replan_reason: str,
    ) -> None:
        if self._has_pending_astar():
            return

        goal_tiles = astar_solver.get_goal_tiles(game_state, target_location_name)
        if not goal_tiles:
            return

        failed_clear_obstacles = blackboard.failed_clear_obstacles.copy()
        start_tile = RouteTile(*game_state.player_tile, type="walk")
        self.pending_astar_reason = replan_reason
        self.pending_astar_target_location = target_location_name
        self.pending_astar_started_at = time.perf_counter()
        self.pending_astar_task = asyncio.create_task(
            asyncio.to_thread(
                self._calculate_astar_path,
                game_state,
                start_tile,
                goal_tiles,
                failed_clear_obstacles,
            )
        )
        self._log_route_debug(
            f"后台 A* 启动: reason={replan_reason}, distance={self.last_replan_distance}, "
            f"loc={game_state.location_name}, target={target_location_name}, "
            f"player={game_state.player_tile}, old_path={self.path_index}/{len(self.global_current_path)}"
        )

    def _consume_pending_astar_result(self, game_state: StardewState, target_location_name: Location) -> None:
        if self.pending_astar_task is None or not self.pending_astar_task.done():
            return

        task = self.pending_astar_task
        started_at = self.pending_astar_started_at
        pending_target = self.pending_astar_target_location
        pending_reason = self.pending_astar_reason
        self.pending_astar_task = None
        self.pending_astar_started_at = None
        self.pending_astar_target_location = None
        self.pending_astar_reason = None

        if pending_target != target_location_name:
            self._log_route_debug(
                f"后台 A* 结果丢弃: target_changed old={pending_target}, current={target_location_name}"
            )
            return

        try:
            new_path = task.result()
        except Exception as exc:
            self._log_route_debug(f"后台 A* 失败: reason={pending_reason}, error={exc}")
            return

        duration_ms = (time.perf_counter() - started_at) * 1000 if started_at is not None else 0.0
        if new_path is None:
            self._log_route_debug(f"后台 A* 无路: reason={pending_reason}, cost={duration_ms:.1f}ms")
            return
        if self._is_backtracking_path(game_state, new_path):
            self._log_route_debug(f"后台 A* 结果被丢弃: backtracking, cost={duration_ms:.1f}ms")
            return

        aligned_path = self._align_pending_path_to_current_player(game_state, new_path)
        if aligned_path is None:
            self._log_route_debug(
                f"后台 A* 结果被丢弃: stale_start, cost={duration_ms:.1f}ms, "
                f"player={game_state.player_tile}, path_start={new_path[0] if new_path else None}"
            )
            return

        self.global_current_path = aligned_path
        self.path_index = 1 if len(aligned_path) > 1 else 0
        self.should_trigger_astar = False
        self.last_location_name = game_state.location_name
        self.toal_tiles = astar_solver.get_goal_tiles(game_state, target_location_name)
        self._log_route_debug(
            f"后台 A* 切换成功: reason={pending_reason}, cost={duration_ms:.1f}ms, "
            f"path_len={len(aligned_path)}, next={self._get_next_path_tile()}"
        )
        self._log_clear_obstacles_in_path(game_state, aligned_path, "后台 A* 切换成功")

    def _align_pending_path_to_current_player(
        self,
        game_state: StardewState,
        new_path: List[RouteTile],
    ) -> List[RouteTile] | None:
        if not new_path:
            return None

        player_tile = game_state.player_tile
        look_ahead_end = min(8, len(new_path))
        selected_index: int | None = None

        for index in range(look_ahead_end):
            route_tile = new_path[index]
            if route_tile == player_tile:
                selected_index = index
                continue
            if self._is_player_next_to_tile(player_tile, route_tile):
                selected_index = index

        if selected_index is None:
            if self._is_player_next_to_tile(player_tile, new_path[0]):
                return [RouteTile(player_tile.x, player_tile.y, type="walk"), *new_path]
            return None

        selected_tile = new_path[selected_index]
        if selected_tile == player_tile:
            return new_path[selected_index:]

        return [RouteTile(player_tile.x, player_tile.y, type="walk"), *new_path[selected_index:]]

    def _calculate_astar_path(
        self,
        game_state: StardewState,
        start_tile: RouteTile,
        goal_tiles: Set[RouteTile],
        failed_clear_obstacles: set[tuple[int, int]],
    ) -> List[RouteTile] | None:
        def cost_fn(
            current: Tile, neighbor: Tile, state: StardewState, base_cost: float
        ) -> Tuple[bool, float, RouteActionType]:
            if (neighbor.x, neighbor.y) in failed_clear_obstacles:
                return False, float("inf"), "blocked"
            return route_cost_function(current, neighbor, state, base_cost)

        return astar_solver.find_path_to_warp_zone(
            game_state,
            start_tile,
            goal_tiles,
            cost_fn,
        )

    def _is_player_next_to_tile(self, player_tile: Tile, target_tile: Tile) -> bool:
        distance_x = abs(player_tile.x - target_tile.x)
        distance_y = abs(player_tile.y - target_tile.y)
        return max(distance_x, distance_y) == 1

    def _is_player_cardinally_next_to_tile(self, player_tile: Tile, target_tile: Tile) -> bool:
        distance_x = abs(player_tile.x - target_tile.x)
        distance_y = abs(player_tile.y - target_tile.y)
        return distance_x + distance_y == 1

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
    absolute_walls = state.layers.get("Wall", set()).union(
        state.layers.get("Bush", set()),
        state.layers.get("TreeStump", set()),
    )
    for growth_stage in range(0, 6):
        absolute_walls.update(state.layers.get(f"FruitTree{growth_stage}", set()))

    if neighbor in absolute_walls:
        return False, float("inf"), "blocked"

    # # 2. 🚪 识别大门图层与交互开关
    # # 假设你的图层里有大门或者带有锁性质的建筑物
    # doors = state.layers.get("OBJECT", set())  # 游戏里许多门和障碍在 OBJECT 层
    # # 这里你可以结合你 state 里的具体门禁状态判断，如果是关着的门
    # if neighbor in doors and getattr(state, "is_door_at_tile", lambda x: False)(neighbor):
    #     # 门可以通过，但是需要开门成本 (例如多花费 2.0 秒动作成本)，标记为 "open_door"
    #     return True, base_cost + 2.0, "open_door"

    # 3. 🪓 识别可破坏的硬图层。
    stones = state.layers.get("Stone", set())
    weeds = state.layers.get("Weeds", set())
    twigs = state.layers.get("Twig", set())
    trees = set()
    for layer_name in ORDINARY_TREE_LAYERS:
        trees.update(state.layers.get(layer_name, set()))

    if neighbor in weeds:
        return True, base_cost + 4.0, "weeds"

    if neighbor in twigs:
        return True, base_cost + 6.0, "twig"

    if neighbor in trees:
        decision = decide_clear_obstacle(state, neighbor, "tree", "Route")
        if decision.can_clear:
            return True, base_cost + decision.cost, "tree"
        return False, float("inf"), "blocked"

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

        return True, base_cost + 8.0, "stone"

    # 4. 🟢 顺畅空地放行
    # 没有任何硬图层阻挡，完全是康庄大道，保持原本的 A* 基础位移时间代价 (base_cost 是 1.0 或 1.414)
    return True, base_cost, "walk"
