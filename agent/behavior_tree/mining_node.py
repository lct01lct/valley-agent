import time
from typing import Literal

from agent.action.combat.combat_tactical_resolver import (
    CombatTacticalResolver,
    MiningObjectiveContext,
    MiningObjectiveType,
    TacticalDecision,
)
from agent.action.location.location import Location
from agent.action.combat.monster_threat import MonsterThreat, MonsterThreatEvaluator
from agent.action.mining.mine_target import MineTarget, MineTargetSelector
from agent.action.tool.tool_aftermath_service import ToolAftermathResult, ToolAftermathService, ToolEffectPlan
from agent.action.valley_action.AStar import RouteTile, astar_solver
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.action.valley_action.positioning_controller import PositioningController, PositioningGoal, PositioningResult
from agent.base_task import BaseTask, TaskType
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.mining_debug_logger import MiningDebugLogger
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.tool_action_tracker import ToolActionTracker
from agent.behavior_tree.tool_selection import is_current_tool
from server.valley_server import MineInteractTargetState, StardewState
from server.type import Tile


type MiningAction = Literal[
    "FIND_NEXT_LEVEL",  # 找到并进入下一层；若当前层没有梯子，则挖石头直到梯子出现
]
type MiningPhase = Literal[
    "ENTER_MINE",  # 在矿洞大厅寻找入口并进入第一层
    "FIND_LADDER",  # 在矿层中寻找可交互的下层梯子
    "BREAK_STONE",  # 没有梯子时，选择 Stone / MiningNode 并用镐子破坏
    "DONE",  # 已进入目标矿层，任务完成
]


PICKAXE_TOOL_NAME = "Pickaxe"
MINE_NODE_TIMEOUT_SECONDS = 90.0
MINE_INTERACT_RETRY_INTERVAL_SECONDS = 0.45
MINE_TOOL_START_GRACE_SECONDS = 0.35
MINE_TOOL_FINISH_TIMEOUT_SECONDS = 3.0
MAX_STONE_ATTEMPTS = 8
MINE_INTERACT_CLOSE_EDGE_MARGIN = 0.0
MINE_INTERACT_CLOSE_EDGE_DEAD_ZONE = 4.0


class MiningTask(BaseTask):
    def __init__(
        self,
        task_type: TaskType,
        desc: str,
        mine_action: MiningAction,
        target_loc: Location = "Mine",
        target_mine_level: int = 2,
        max_stones_to_break: int = 60,
    ) -> None:
        super().__init__(task_type=task_type, desc=desc)
        self.mine_action = mine_action
        self.target_loc = target_loc
        self.target_mine_level = target_mine_level
        self.max_stones_to_break = max_stones_to_break


class MineNode(BTNode):
    """
    Mining P0：进入矿洞第一层，并找到/制造通往第二层的入口。
    """

    def __init__(self) -> None:
        self.positioning_controller = PositioningController()
        self.approach_positioning_controller = PositioningController()
        self.threat_evaluator = MonsterThreatEvaluator()
        self.tactical_resolver = CombatTacticalResolver()
        self.mine_target_selector = MineTargetSelector()
        self.tool_aftermath_service = ToolAftermathService()
        self.tool_action_tracker = ToolActionTracker(
            start_grace_seconds=MINE_TOOL_START_GRACE_SECONDS,
            finish_timeout_seconds=MINE_TOOL_FINISH_TIMEOUT_SECONDS,
        )
        self.mining_debug_logger = MiningDebugLogger()
        self._phase: MiningPhase | None = None
        self._task_signature: tuple[int, int, str] | None = None
        self._started_at: float | None = None
        self._target_tile: Tile | None = None
        self._detected_ladder_tile: Tile | None = None
        self._ladder_pursuit_tile: Tile | None = None
        self._approach_stand_tile: Tile | None = None
        self._corridor_ladder_tile: Tile | None = None
        self._active_mine_level: int | None = None
        self._return_prompt_tiles: set[Tile] = set()
        self._stone_attempt_count = 0
        self._broken_stone_count = 0
        self._failed_stone_tiles: set[Tile] = set()
        self._deferred_stone_tiles: set[Tile] = set()
        self._last_interact_at = 0.0
        self._has_logged_task = False
        self._last_debug_heartbeat_at = 0.0
        self._active_tool_effect_plan: ToolEffectPlan | None = None

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        if not blackboard.macro_plan or blackboard.current_step_index >= len(blackboard.macro_plan):
            self._reset()
            return "FAILURE"

        current_task = blackboard.macro_plan[blackboard.current_step_index]
        if not isinstance(current_task, MiningTask):
            self._reset()
            return "FAILURE"

        if current_task.task_type != "MINE":
            self._reset()
            return "FAILURE"

        game_state = context.state
        if game_state is None:
            return "RUNNING"

        task_signature = (
            blackboard.current_step_index,
            current_task.target_mine_level,
            current_task.mine_action,
        )
        if self._task_signature != task_signature:
            self._reset()
            self._task_signature = task_signature
            self._phase = "ENTER_MINE"
            self._started_at = time.time()

        self._log_debug_heartbeat(game_state, current_task)
        if not self._has_logged_task:
            self._has_logged_task = True
            print(
                "\n⛏️ [MineNode] 收到采矿任务: "
                f"action={current_task.mine_action}, target_loc={current_task.target_loc}, "
                f"target_mine_level={current_task.target_mine_level}"
            )
            self._log(
                "收到采矿任务: "
                f"action={current_task.mine_action}, target_loc={current_task.target_loc}, "
                f"target_mine_level={current_task.target_mine_level}, "
                f"location={game_state.location_name}, mine_level={game_state.mine_level}, "
                f"player={game_state.player_tile}"
            )

        if self._started_at is not None and time.time() - self._started_at > MINE_NODE_TIMEOUT_SECONDS:
            return self._fail(context, blackboard, current_task, "Mining P0 超时")

        if current_task.mine_action != "FIND_NEXT_LEVEL":
            return self._fail(context, blackboard, current_task, f"暂不支持的采矿动作: {current_task.mine_action}")

        if self._has_reached_target_level(game_state, current_task):
            return self._finish(context, blackboard, current_task)

        if game_state.mine_level is None:
            return self._run_enter_mine_phase(context, blackboard, game_state, current_task)

        if self._active_mine_level != game_state.mine_level:
            self._active_mine_level = game_state.mine_level
            self._record_return_prompt_tiles(game_state)
            self.positioning_controller.reset()
            self.approach_positioning_controller.reset()
            self._target_tile = None
            self._detected_ladder_tile = None
            self._ladder_pursuit_tile = None
            self._approach_stand_tile = None
            self._corridor_ladder_tile = None
            self._last_interact_at = 0.0

        if self._phase == "ENTER_MINE":
            self._phase = "FIND_LADDER"
            print(f"\n⛏️ [MineNode] 已进入矿层: MineLevel={game_state.mine_level}，开始寻找下一层。")
            self._log(
                f"已进入矿层: MineLevel={game_state.mine_level}, player={game_state.player_tile}, "
                f"return_prompt_tiles={self._format_tiles(self._return_prompt_tiles)}"
            )

        if self._detected_ladder_tile is not None:
            self._phase = "FIND_LADDER"
            return self._run_interact_target(
                context=context,
                blackboard=blackboard,
                game_state=game_state,
                current_task=current_task,
                target_tile=self._detected_ladder_tile,
                target_name="破石后出现的梯子",
                require_tool_target=True,
                require_close_to_target=True,
            )

        if self._phase == "BREAK_STONE" and self._target_tile is not None and self._is_stone_tile(
            game_state,
            self._target_tile,
        ):
            return self._run_break_stone_phase(context, blackboard, game_state, current_task)

        ladder = self._select_next_level_ladder(game_state)
        if ladder is not None:
            self._phase = "FIND_LADDER"
            return self._run_interact_target(
                context=context,
                blackboard=blackboard,
                game_state=game_state,
                current_task=current_task,
                target_tile=ladder.tile,
                target_name="梯子",
                require_tool_target=ladder.require_tool_target,
                require_close_to_target=ladder.require_close_to_target,
            )

        self._phase = "BREAK_STONE"
        return self._run_break_stone_phase(context, blackboard, game_state, current_task)

    def _run_enter_mine_phase(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: MiningTask,
    ) -> NodeStatus:
        if game_state.location_name != current_task.target_loc:
            return self._fail(
                context,
                blackboard,
                current_task,
                f"当前场景不是矿洞入口场景: current={game_state.location_name}, target={current_task.target_loc}",
            )

        entrance = self._select_mine_level_entrance(game_state)
        if entrance is None:
            return self._fail(
                context,
                blackboard,
                current_task,
                f"当前矿洞大厅没有找到可进入第一层的矿洞入口: entrances={self._format_targets(game_state.mine_entrances)}",
            )

        self._phase = "ENTER_MINE"
        return self._run_interact_target(
            context=context,
            blackboard=blackboard,
            game_state=game_state,
            current_task=current_task,
            target_tile=entrance.tile,
            target_name="矿洞入口",
            require_tool_target=entrance.require_tool_target,
            forced_stand_tiles=entrance.candidate_stand_tiles,
            require_close_to_target=entrance.require_close_to_target,
        )

    def _run_interact_target(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: MiningTask,
        target_tile: Tile,
        target_name: str,
        allow_standing_on_target: bool = False,
        require_tool_target: bool = True,
        stand_on_target_only: bool = False,
        forced_stand_tiles: set[Tile] | None = None,
        require_close_to_target: bool = False,
    ) -> NodeStatus:
        if self._target_tile != target_tile:
            self._target_tile = target_tile
            self.positioning_controller.reset()
            if self._ladder_pursuit_tile != target_tile:
                self.approach_positioning_controller.reset()
                self._approach_stand_tile = None
            self._last_interact_at = 0.0
            print(f"\n⛏️ [MineNode] 准备交互{target_name}: target={target_tile}")
            stand_tiles_text = (
                f", forced_stand_tiles={self._format_tiles(forced_stand_tiles)}"
                if forced_stand_tiles is not None
                else ""
            )
            self._log(f"准备交互{target_name}: target={target_tile}, player={game_state.player_tile}{stand_tiles_text}")

        candidate_stand_tiles = self._build_candidate_stand_tiles(
            target_tile=target_tile,
            allow_standing_on_target=allow_standing_on_target,
            stand_on_target_only=stand_on_target_only,
            forced_stand_tiles=forced_stand_tiles,
        )
        tactical_decision = self._resolve_mining_tactical_decision(
            blackboard=blackboard,
            game_state=game_state,
            objective_type=self._get_interact_objective_type(target_name),
            target_tile=target_tile,
            candidate_stand_tiles=candidate_stand_tiles,
        )
        if tactical_decision.decision_type in ("ENGAGE", "AVOID"):
            return "RUNNING"

        if target_name in ("梯子", "破石后出现的梯子") and self._ladder_pursuit_tile == target_tile:
            if self._should_approach_distant_target(game_state, target_tile):
                approach_result = self._tick_approach_distant_target(game_state, context, target_tile)
                if approach_result.status == "READY":
                    self._ladder_pursuit_tile = None
                    self._approach_stand_tile = None
                    self.approach_positioning_controller.reset()
                elif approach_result.status != "FAILED":
                    self._log(
                        f"延续梯子接近意图: target={target_tile}, "
                        f"approach_status={approach_result.status}, reason={approach_result.reason}"
                    )
                    return "RUNNING"
                else:
                    self._ladder_pursuit_tile = None
                    self._approach_stand_tile = None
                    self.approach_positioning_controller.reset()

            else:
                self._ladder_pursuit_tile = None
                self._approach_stand_tile = None
                self.approach_positioning_controller.reset()

        positioning_result = self._tick_positioning(
            game_state,
            context,
            target_tile,
            allow_standing_on_target=allow_standing_on_target,
            require_tool_target=require_tool_target,
            block_target=not allow_standing_on_target,
            stand_on_target_only=stand_on_target_only,
            forced_stand_tiles=forced_stand_tiles,
            require_close_to_target=require_close_to_target,
            close_edge_margin=MINE_INTERACT_CLOSE_EDGE_MARGIN,
            close_edge_dead_zone=MINE_INTERACT_CLOSE_EDGE_DEAD_ZONE,
        )
        if positioning_result.status == "FAILED":
            if target_name in ("梯子", "破石后出现的梯子") and self._should_approach_distant_target(
                game_state,
                target_tile,
            ):
                approach_result = self._tick_approach_distant_target(game_state, context, target_tile)
                if approach_result.status == "READY":
                    self._ladder_pursuit_tile = None
                    self._approach_stand_tile = None
                    self.approach_positioning_controller.reset()
                    return "RUNNING"
                if approach_result.status != "FAILED":
                    self._log(
                        f"梯子较远且当前视野无法直接规划站位，先接近目标: target={target_tile}, "
                        f"approach_status={approach_result.status}, reason={approach_result.reason}"
                    )
                    self._ladder_pursuit_tile = target_tile
                    return "RUNNING"

            self._ladder_pursuit_tile = None
            self._approach_stand_tile = None
            self.approach_positioning_controller.reset()

            if target_name in ("梯子", "破石后出现的梯子"):
                self._ladder_pursuit_tile = None
                self._approach_stand_tile = None
                self.approach_positioning_controller.reset()
                corridor_stone = self._select_corridor_stone_toward_tile(game_state, target_tile)
                if corridor_stone is not None:
                    self._phase = "BREAK_STONE"
                    self._target_tile = corridor_stone
                    self._corridor_ladder_tile = target_tile
                    self._stone_attempt_count = 0
                    self.positioning_controller.reset()
                    self.tool_action_tracker.reset()
                    self._log(
                        f"梯子不可达，先挖通路石头: ladder={target_tile}, "
                        f"stone={corridor_stone}, player={game_state.player_tile}"
                    )
                    return "RUNNING"

            return self._fail(
                context,
                blackboard,
                current_task,
                f"无法移动到{target_name}旁: target={target_tile}, reason={positioning_result.reason}",
            )

        if positioning_result.status in ("MOVING", "FACING"):
            self._ladder_pursuit_tile = None
            self._approach_stand_tile = None
            self.approach_positioning_controller.reset()
            return "RUNNING"

        self._ladder_pursuit_tile = None
        self._approach_stand_tile = None
        self.approach_positioning_controller.reset()

        now = time.time()
        if now - self._last_interact_at < MINE_INTERACT_RETRY_INTERVAL_SECONDS:
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
            return "RUNNING"

        self._last_interact_at = now
        response = context.executor_client.send_command(
            StardewCommand(
                action=StardewAction.INTERACT_TILE,
                key=["x"],
                tile=(target_tile.x, target_tile.y),
            )
        )
        self._log(
            f"发送 INTERACT_TILE: target_name={target_name}, target={target_tile}, "
            f"response={response}, mine_level={game_state.mine_level}, player={game_state.player_tile}"
        )
        return "RUNNING"

    def _run_break_stone_phase(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: MiningTask,
    ) -> NodeStatus:
        if self._broken_stone_count >= current_task.max_stones_to_break:
            return self._fail(
                context,
                blackboard,
                current_task,
                f"已达到最大破石数量仍未发现梯子: broken={self._broken_stone_count}",
            )

        if self._target_tile is None:
            select_started_at = time.time()
            self._target_tile = self._select_nearest_stone_tile(game_state)
            select_elapsed = time.time() - select_started_at
            self._stone_attempt_count = 0
            self.positioning_controller.reset()
            self.tool_action_tracker.reset()
            if self._target_tile is None:
                return self._fail(context, blackboard, current_task, "当前矿层没有发现可挖 Stone / MiningNode")
            print(f"\n⛏️ [MineNode] 没有发现梯子，准备破坏石头: target={self._target_tile}")
            self._log(
                f"选择破坏石头: target={self._target_tile}, player={game_state.player_tile}, "
                f"elapsed={select_elapsed:.3f}s"
            )

        if not is_current_tool(game_state, PICKAXE_TOOL_NAME):
            blackboard.require_switch_tool = True
            blackboard.is_switching_tool = True
            blackboard.required_tool_owner = "Mining"
            blackboard.required_tool = PICKAXE_TOOL_NAME
            print(f"\n🟡 [MineNode] 当前工具不是 {PICKAXE_TOOL_NAME}，等待切换工具后再挖矿。")
            return "SUCCESS"

        if not self.tool_action_tracker.is_idle():
            tool_status = self.tool_action_tracker.tick(game_state)
            self._log(
                f"等待挥镐收招: target={self._target_tile}, status={tool_status}, "
                f"tracker={self.tool_action_tracker.get_debug_snapshot()}"
            )
            if tool_status == "FINISHED":
                self.tool_action_tracker.reset()
                aftermath_result = self._inspect_current_stone_aftermath(context, game_state)
                self._active_tool_effect_plan = None
                if aftermath_result.has_blocking_menu:
                    self._log(
                        f"挥镐收招后发现阻塞 UI，等待 Guard 处理: target={self._target_tile}, "
                        f"menu={aftermath_result.blocking_menu_type}, text={aftermath_result.blocking_menu_text}"
                    )
                    return "RUNNING"
                self._request_collect_loot(blackboard, self._target_tile, "Stone", aftermath_result.nearby_loot_tiles)
                if aftermath_result.target_change_state == "CHANGED" or aftermath_result.generated_ladder_tile is not None:
                    self._mark_current_stone_done_after_aftermath(game_state, aftermath_result)
                    if self._detected_ladder_tile is None and self._target_tile is None:
                        return self._run_break_stone_phase(context, blackboard, game_state, current_task)
                elif self._stone_attempt_count >= MAX_STONE_ATTEMPTS:
                    self._failed_stone_tiles.add(self._target_tile)
                    self._log(f"石头重试耗尽，加入失败集合: target={self._target_tile}")
                    self._target_tile = None
                    self._corridor_ladder_tile = None
                    self.positioning_controller.reset()
                return "RUNNING"
            if tool_status == "TIMEOUT":
                self.tool_action_tracker.reset()
                self._active_tool_effect_plan = None
                if self._target_tile is not None:
                    self._failed_stone_tiles.add(self._target_tile)
                self._log(f"挥镐等待超时，换下一个石头: target={self._target_tile}")
                self._target_tile = None
                self._corridor_ladder_tile = None
                self.positioning_controller.reset()
                return "RUNNING"
            return "RUNNING"

        if self._target_tile is not None and (
            self._is_current_stone_done(game_state, self._target_tile)
            or self._has_ladder_at_tile(game_state, self._target_tile)
        ):
            aftermath_result = self._inspect_current_stone_aftermath(context, game_state)
            if aftermath_result.has_blocking_menu:
                self._log(
                    f"目标变化后发现阻塞 UI，等待 Guard 处理: target={self._target_tile}, "
                    f"menu={aftermath_result.blocking_menu_type}, text={aftermath_result.blocking_menu_text}"
                )
                return "RUNNING"
            self._request_collect_loot(blackboard, self._target_tile, "Stone", aftermath_result.nearby_loot_tiles)
            self._mark_current_stone_done_after_aftermath(game_state, aftermath_result)
            if self._detected_ladder_tile is None and self._target_tile is None:
                return self._run_break_stone_phase(context, blackboard, game_state, current_task)
            return "RUNNING"

        interrupt_stone = None
        if self._corridor_ladder_tile is None:
            interrupt_stone = self._select_nearby_interrupt_stone(game_state, self._target_tile)
        if interrupt_stone is not None:
            self._log(
                f"移动途中发现更近可挖石头，切换目标: old={self._target_tile}, "
                f"new={interrupt_stone}, player={game_state.player_tile}"
            )
            self._target_tile = interrupt_stone
            self._corridor_ladder_tile = None
            self._stone_attempt_count = 0
            self.positioning_controller.reset()
            self.tool_action_tracker.reset()
            self._active_tool_effect_plan = None
        tactical_decision = self._resolve_mining_tactical_decision(
            blackboard=blackboard,
            game_state=game_state,
            objective_type="STONE",
            target_tile=self._target_tile,
            candidate_stand_tiles=self._build_cardinal_neighbor_tiles(self._target_tile),
        )
        if tactical_decision.decision_type in ("ENGAGE", "AVOID"):
            return "RUNNING"
        if tactical_decision.decision_type == "DEFER_OBJECTIVE":
            self._deferred_stone_tiles.add(self._target_tile)
            self._log(
                f"战术层暂缓当前石头，换下一个目标: target={self._target_tile}, "
                f"reason={tactical_decision.reason}"
            )
            self._target_tile = None
            self._corridor_ladder_tile = None
            self.positioning_controller.reset()
            self.tool_action_tracker.reset()
            self._active_tool_effect_plan = None
            return "RUNNING"

        positioning_result = self._tick_positioning(game_state, context, self._target_tile)
        if positioning_result.status == "FAILED":
            self._failed_stone_tiles.add(self._target_tile)
            self._log(f"石头站位失败，换下一个: target={self._target_tile}, reason={positioning_result.reason}")
            self._target_tile = None
            self._corridor_ladder_tile = None
            self.positioning_controller.reset()
            return "RUNNING"

        if positioning_result.status in ("MOVING", "FACING"):
            return "RUNNING"

        response = context.executor_client.send_command(
            StardewCommand(action=StardewAction.USE_TOOL, key=["c"])
        )
        if response == "BUSY":
            self._log(f"挥镐被 C# 判定 BUSY，等待下一帧: target={self._target_tile}")
            return "RUNNING"
        if response == "TIMEOUT" or response is None:
            self._log(f"挥镐命令异常，等待下一帧重试: target={self._target_tile}, response={response}")
            return "RUNNING"

        self._stone_attempt_count += 1
        self._active_tool_effect_plan = self._build_break_stone_effect_plan(self._target_tile)
        self.tool_action_tracker.start()
        print(f"\n⛏️ [MineNode] 使用镐子破坏石头: target={self._target_tile}, attempt={self._stone_attempt_count}")
        self._log(
            f"发送 USE_TOOL 挥镐: target={self._target_tile}, response={response}, "
            f"attempt={self._stone_attempt_count}/{MAX_STONE_ATTEMPTS}, player={game_state.player_tile}"
        )
        return "RUNNING"

    def _tick_positioning(
        self,
        game_state: StardewState,
        context: PlayerContext,
        target_tile: Tile,
        allow_standing_on_target: bool = False,
        require_tool_target: bool = True,
        block_target: bool = True,
        stand_on_target_only: bool = False,
        forced_stand_tiles: set[Tile] | None = None,
        require_close_to_target: bool = False,
        close_edge_margin: float = 2.0,
        close_edge_dead_zone: float = 4.0,
    ) -> PositioningResult:
        candidate_stand_tiles = self._build_candidate_stand_tiles(
            target_tile=target_tile,
            allow_standing_on_target=allow_standing_on_target,
            stand_on_target_only=stand_on_target_only,
            forced_stand_tiles=forced_stand_tiles,
        )
        extra_blocked_tiles = {target_tile} if block_target else set()
        extra_blocked_tiles.update(self._get_tactical_blocked_tiles(game_state))
        extra_blocked_tiles.discard(game_state.player_tile)

        positioning_result = self.positioning_controller.tick(
            game_state,
            PositioningGoal(
                candidate_stand_tiles=candidate_stand_tiles,
                tool_target_tile=target_tile if require_tool_target else None,
                extra_blocked_tiles=extra_blocked_tiles,
                allowed_blocked_tiles={target_tile} if allow_standing_on_target else set(),
                require_close_to_target=require_close_to_target,
                close_edge_margin=close_edge_margin,
                close_edge_dead_zone=close_edge_dead_zone,
            ),
        )

        if positioning_result.command is not None:
            context.executor_client.send_command(positioning_result.command)

        self._log(
            f"站位结果: target={target_tile}, status={positioning_result.status}, "
            f"stand={positioning_result.stand_tile}, reason={positioning_result.reason}, "
            f"player={game_state.player_tile}, tool_target={game_state.tool_target.tile}, "
            f"positioning={self.positioning_controller.get_debug_snapshot()}"
        )
        return positioning_result

    def _should_approach_distant_target(self, game_state: StardewState, target_tile: Tile) -> bool:
        scan_range = game_state.scan_range or 0
        if scan_range <= 0:
            return False
        return self._tile_distance(game_state.player_tile, target_tile) > max(6, scan_range // 2)

    def _tick_approach_distant_target(
        self,
        game_state: StardewState,
        context: PlayerContext,
        target_tile: Tile,
    ) -> PositioningResult:
        if self._approach_stand_tile == game_state.player_tile:
            return PositioningResult(status="READY", stand_tile=self._approach_stand_tile, reason="已到达锁定接近点")

        candidate_tiles = self._build_approach_candidate_tiles(game_state, target_tile)
        if not candidate_tiles:
            return PositioningResult(status="FAILED", reason="没有可用接近点")

        threat_snapshot = self.threat_evaluator.evaluate(game_state)
        extra_blocked_tiles = self._get_tactical_blocked_tiles(game_state, threat_snapshot)
        if self._approach_stand_tile is None:
            self._approach_stand_tile = self._select_approach_stand_tile(
                game_state,
                target_tile,
                candidate_tiles,
                extra_blocked_tiles,
            )
            self.approach_positioning_controller.reset()

        if self._approach_stand_tile is None:
            return PositioningResult(status="FAILED", reason="无法规划到任何接近点")

        positioning_result = self.approach_positioning_controller.tick(
            game_state,
            PositioningGoal(
                candidate_stand_tiles={self._approach_stand_tile},
                tool_target_tile=None,
                extra_blocked_tiles=extra_blocked_tiles,
            ),
        )
        if positioning_result.status == "FAILED":
            self._log(
                f"锁定接近点不可达，等待下一帧重选: target={target_tile}, "
                f"locked={self._approach_stand_tile}, reason={positioning_result.reason}"
            )
            self._approach_stand_tile = None
            self.approach_positioning_controller.reset()

        if positioning_result.command is not None:
            context.executor_client.send_command(positioning_result.command)

        self._log(
            f"远距离接近目标: target={target_tile}, candidate_count={len(candidate_tiles)}, "
            f"locked={self._approach_stand_tile}, status={positioning_result.status}, "
            f"stand={positioning_result.stand_tile}, "
            f"reason={positioning_result.reason}, positioning={self.approach_positioning_controller.get_debug_snapshot()}"
        )
        return positioning_result

    def _select_approach_stand_tile(
        self,
        game_state: StardewState,
        target_tile: Tile,
        candidate_tiles: set[Tile],
        extra_blocked_tiles: set[Tile],
    ) -> Tile | None:
        sorted_candidates = sorted(
            candidate_tiles,
            key=lambda tile: (
                self._tile_distance(tile, target_tile),
                self._tile_distance(game_state.player_tile, tile),
                tile.y,
                tile.x,
            ),
        )
        for tile in sorted_candidates:
            if self._build_path_to_tiles(game_state, {tile}, extra_blocked_tiles):
                return tile
        return None

    def _build_approach_candidate_tiles(self, game_state: StardewState, target_tile: Tile) -> set[Tile]:
        scan_range = game_state.scan_range or 10
        map_width, map_height = game_state.map_size
        player_tile = game_state.player_tile
        dx = target_tile.x - player_tile.x
        dy = target_tile.y - player_tile.y
        max_axis_distance = max(abs(dx), abs(dy))
        if max_axis_distance == 0:
            return set()

        candidates: set[Tile] = set()
        current_distance_to_target = self._tile_distance(player_tile, target_tile)
        max_approach_distance = max(3, min(scan_range - 3, max_axis_distance - 2))
        approach_distances = sorted(
            {
                3,
                max_approach_distance,
                max(3, max_approach_distance // 3),
                max(3, (max_approach_distance * 2) // 3),
            }
        )

        for approach_distance in approach_distances:
            approach_ratio = approach_distance / max_axis_distance
            center_tile = Tile(
                player_tile.x + round(dx * approach_ratio),
                player_tile.y + round(dy * approach_ratio),
            )

            for offset_x in (-2, -1, 0, 1, 2):
                for offset_y in (-2, -1, 0, 1, 2):
                    tile = Tile(center_tile.x + offset_x, center_tile.y + offset_y)
                    if tile.x < 0 or tile.y < 0 or tile.x >= map_width or tile.y >= map_height:
                        continue
                    if tile == player_tile:
                        continue
                    if tile == target_tile:
                        continue
                    if self._tile_distance(tile, target_tile) >= current_distance_to_target:
                        continue
                    candidates.add(tile)
        return candidates

    def _get_tactical_blocked_tiles(self, game_state: StardewState, threat_snapshot=None) -> set[Tile]:
        snapshot = threat_snapshot or self.threat_evaluator.evaluate(game_state)
        blocked_tiles = set(snapshot.blocking_tiles)
        for tile, risk_score in snapshot.risk_tiles.items():
            if risk_score >= 2.0:
                blocked_tiles.add(tile)
        blocked_tiles.discard(game_state.player_tile)
        return blocked_tiles

    def _build_cardinal_neighbor_tiles(self, target_tile: Tile) -> set[Tile]:
        return {
            Tile(target_tile.x + 1, target_tile.y),
            Tile(target_tile.x - 1, target_tile.y),
            Tile(target_tile.x, target_tile.y + 1),
            Tile(target_tile.x, target_tile.y - 1),
        }

    def _build_candidate_stand_tiles(
        self,
        target_tile: Tile,
        allow_standing_on_target: bool = False,
        stand_on_target_only: bool = False,
        forced_stand_tiles: set[Tile] | None = None,
    ) -> set[Tile]:
        if forced_stand_tiles is not None:
            return forced_stand_tiles
        if stand_on_target_only:
            return {target_tile}

        candidate_stand_tiles = self._build_cardinal_neighbor_tiles(target_tile)
        if allow_standing_on_target:
            candidate_stand_tiles.add(target_tile)
        return candidate_stand_tiles

    def _select_mine_level_entrance(self, game_state: StardewState) -> MineTarget | None:
        entrance_targets = self.mine_target_selector.build_mine_entrance_targets(game_state)
        exact_mine_entrances = [
            target
            for target, raw_target in zip(entrance_targets, game_state.mine_entrances)
            if self._normalize_action(raw_target.action) == "mine"
        ]
        if exact_mine_entrances:
            return self.mine_target_selector.select_nearest_target(game_state, exact_mine_entrances)

        fallback_entrances = [
            target
            for target, raw_target in zip(entrance_targets, game_state.mine_entrances)
            if self._is_possible_mine_level_entrance(raw_target)
        ]
        return self.mine_target_selector.select_nearest_target(game_state, fallback_entrances)

    def _select_next_level_ladder(self, game_state: StardewState) -> MineTarget | None:
        next_level_ladders = self.mine_target_selector.build_ladder_targets(game_state, self._return_prompt_tiles)
        if len(next_level_ladders) != len(game_state.ladders):
            self._log(
                "过滤矿层返回入口梯子: "
                f"return_prompt_tiles={self._format_tiles(self._return_prompt_tiles)}, "
                f"raw_ladders={self._format_targets(game_state.ladders)}, "
                f"next_level_ladders={self._format_mine_targets(next_level_ladders)}"
            )
        return self.mine_target_selector.select_nearest_target(game_state, next_level_ladders)

    def _record_return_prompt_tiles(self, game_state: StardewState) -> None:
        player_tile = game_state.player_tile
        self._return_prompt_tiles = {
            player_tile,
            Tile(player_tile.x, player_tile.y - 1),
        }
        self._log(
            f"记录矿层返回入口提示区: mine_level={game_state.mine_level}, "
            f"return_prompt_tiles={self._format_tiles(self._return_prompt_tiles)}"
        )

    def _select_nearest_stone_tile(self, game_state: StardewState) -> Tile | None:
        excluded_tiles = self._failed_stone_tiles | self._deferred_stone_tiles
        stone_tiles = self._get_candidate_stone_tiles(game_state, excluded_tiles)
        if not stone_tiles and self._deferred_stone_tiles:
            self._log(f"没有非暂缓石头，清空暂缓集合后重新选择: deferred={self._format_tiles(self._deferred_stone_tiles)}")
            self._deferred_stone_tiles = set()
            return self._select_nearest_stone_tile(game_state)

        threat_snapshot = self.threat_evaluator.evaluate(game_state)
        stone_tiles = {tile for tile in stone_tiles if tile not in threat_snapshot.blocking_tiles}
        if not stone_tiles:
            return None

        reachable_stone = self._select_reachable_stone_with_single_astar(game_state, stone_tiles, threat_snapshot)
        if reachable_stone is None:
            return min(
                stone_tiles,
                key=lambda tile: (
                    threat_snapshot.risk_tiles.get(tile, 0.0),
                    self._tile_distance(game_state.player_tile, tile),
                ),
            )

        return reachable_stone

    def _select_reachable_stone_with_single_astar(
        self,
        game_state: StardewState,
        stone_tiles: set[Tile],
        threat_snapshot: MonsterThreat,
    ) -> Tile | None:
        """
        用一次多目标 A* 选择最近可挖石头。

        旧逻辑会对每块石头分别跑一次 A*；矿层较大时，破坏一块石头后会明显卡顿。
        这里把所有石头的上下左右候选站位合并成目标集合，只跑一次 A*，再反查该站位对应的石头。
        """
        map_width, map_height = game_state.map_size
        tactical_blocked_tiles = self._get_tactical_blocked_tiles(game_state, threat_snapshot)
        blocked_tiles = astar_solver._get_blocked_tiles(game_state) | tactical_blocked_tiles
        blocked_tiles.discard(game_state.player_tile)

        stand_to_stones: dict[Tile, list[Tile]] = {}
        for stone_tile in stone_tiles:
            for stand_tile in self._build_cardinal_neighbor_tiles(stone_tile):
                if not 0 <= stand_tile.x < map_width or not 0 <= stand_tile.y < map_height:
                    continue
                if stand_tile in blocked_tiles:
                    continue
                stand_to_stones.setdefault(stand_tile, []).append(stone_tile)

        if not stand_to_stones:
            return None

        path = self._build_path_to_tiles(
            game_state,
            set(stand_to_stones),
            tactical_blocked_tiles,
        )
        if not path:
            return None

        reached_stand_tile = Tile(path[-1].x, path[-1].y)
        candidate_stones = stand_to_stones.get(reached_stand_tile, [])
        if not candidate_stones:
            return None

        return min(
            candidate_stones,
            key=lambda tile: (
                threat_snapshot.risk_tiles.get(tile, 0.0),
                self._tile_distance(reached_stand_tile, tile),
                self._tile_distance(game_state.player_tile, tile),
            ),
        )

    def _select_nearby_interrupt_stone(self, game_state: StardewState, current_target: Tile | None) -> Tile | None:
        if current_target is None:
            return None
        if self._tile_distance(game_state.player_tile, current_target) <= 2:
            return None

        excluded_tiles = self._failed_stone_tiles | self._deferred_stone_tiles | {current_target}
        nearby_stones = [
            tile
            for tile in self._build_cardinal_neighbor_tiles(game_state.player_tile)
            if tile not in excluded_tiles and self._is_stone_tile(game_state, tile)
        ]
        if not nearby_stones:
            return None

        threat_snapshot = self.threat_evaluator.evaluate(game_state)
        safe_nearby_stones = [tile for tile in nearby_stones if tile not in threat_snapshot.blocking_tiles]
        if not safe_nearby_stones:
            return None

        return min(
            safe_nearby_stones,
            key=lambda tile: (
                threat_snapshot.risk_tiles.get(tile, 0.0),
                self._tile_distance(tile, current_target),
            ),
        )

    def _select_corridor_stone_toward_tile(self, game_state: StardewState, target_tile: Tile) -> Tile | None:
        excluded_tiles = self._failed_stone_tiles | self._deferred_stone_tiles | {target_tile}
        stone_tiles = self._get_candidate_stone_tiles(game_state, excluded_tiles)
        if not stone_tiles:
            return None

        threat_snapshot = self.threat_evaluator.evaluate(game_state)
        current_distance_to_target = self._tile_distance(game_state.player_tile, target_tile)
        scored_stones: list[tuple[tuple[float, int, int, int], Tile]] = []
        for stone_tile in stone_tiles:
            if stone_tile in threat_snapshot.blocking_tiles:
                continue
            path = self._build_path_to_stone_stand_tiles(game_state, stone_tile, threat_snapshot)
            if not path:
                continue

            stone_distance_to_target = self._tile_distance(stone_tile, target_tile)
            opens_toward_target = stone_distance_to_target < current_distance_to_target
            score = (
                threat_snapshot.risk_tiles.get(stone_tile, 0.0),
                0 if opens_toward_target else 1,
                len(path),
                stone_distance_to_target,
            )
            scored_stones.append((score, stone_tile))

        if not scored_stones:
            return None

        return min(scored_stones, key=lambda item: item[0])[1]

    def _get_candidate_stone_tiles(self, game_state: StardewState, excluded_tiles: set[Tile]) -> set[Tile]:
        return {
            target.tile
            for target in self.mine_target_selector.build_breakable_rock_targets(game_state, excluded_tiles)
        }

    def _is_stone_tile(self, game_state: StardewState, tile: Tile) -> bool:
        return tile in game_state.mining_nodes_by_tile or tile in game_state.layers.get("Stone", set())

    def _build_path_to_stone_stand_tiles(self, game_state: StardewState, stone_tile: Tile, threat_snapshot=None) -> list[RouteTile]:
        candidate_stand_tiles = self._build_cardinal_neighbor_tiles(stone_tile)
        extra_blocked_tiles = {stone_tile}
        extra_blocked_tiles.update(self._get_tactical_blocked_tiles(game_state, threat_snapshot))
        extra_blocked_tiles.discard(game_state.player_tile)
        return self._build_path_to_tiles(game_state, candidate_stand_tiles, extra_blocked_tiles)

    def _build_path_to_tiles(
        self,
        game_state: StardewState,
        candidate_tiles: set[Tile],
        extra_blocked_tiles: set[Tile] | None = None,
    ) -> list[RouteTile]:
        map_width, map_height = game_state.map_size
        extra_blocked_tiles = extra_blocked_tiles or set()
        blocked_tiles = astar_solver._get_blocked_tiles(game_state) | extra_blocked_tiles
        goal_tiles = {
            RouteTile(tile.x, tile.y, type="walk")
            for tile in candidate_tiles
            if 0 <= tile.x < map_width
            if 0 <= tile.y < map_height
            if tile not in blocked_tiles
        }
        if not goal_tiles:
            return []

        start = RouteTile(*game_state.player_tile, type="walk")

        def mining_positioning_cost_func(curr, neigh, st, base_c):
            if neigh != start and neigh in blocked_tiles:
                return False, float("inf"), "blocked"
            return True, base_c, "walk"

        path = astar_solver.find_path_to_warp_zone(
            game_state,
            start,
            goal_tiles,
            cost_function=mining_positioning_cost_func,
        )
        return path or []

    def _is_current_stone_done(self, game_state: StardewState, target_tile: Tile) -> bool:
        return target_tile not in game_state.mining_nodes_by_tile and target_tile not in game_state.layers.get("Stone", set())

    def _has_ladder_at_tile(self, game_state: StardewState, target_tile: Tile) -> bool:
        return any(ladder.tile == target_tile for ladder in game_state.ladders)

    def _has_next_level_ladder(self, game_state: StardewState) -> bool:
        return any(ladder.tile not in self._return_prompt_tiles for ladder in game_state.ladders)

    def _mark_current_stone_done(self, game_state: StardewState) -> None:
        if self._target_tile is None:
            return
        finished_tile = self._target_tile
        self._broken_stone_count += 1
        print(f"\n⛏️ [MineNode] 石头已消失或梯子已出现: target={finished_tile}, broken={self._broken_stone_count}")
        self._log(
            f"石头处理完成: target={finished_tile}, broken={self._broken_stone_count}, "
            f"ladders={self._format_targets(game_state.ladders)}"
        )
        self._target_tile = None
        self._corridor_ladder_tile = None
        self._stone_attempt_count = 0
        self.positioning_controller.reset()
        self.approach_positioning_controller.reset()
        self.tool_action_tracker.reset()
        self._active_tool_effect_plan = None

    def _inspect_current_stone_aftermath(
        self,
        context: PlayerContext,
        game_state: StardewState,
    ) -> ToolAftermathResult:
        target_tile = self._target_tile
        effect_result = self.tool_aftermath_service.inspect_tool_effect(
            context,
            game_state,
            self._active_tool_effect_plan or self._build_break_stone_effect_plan(target_tile),
        )
        result = effect_result.aftermath
        self._log(
            f"工具后处理结果: target={target_tile}, effect_status={effect_result.status}, "
            f"effect_satisfied={effect_result.effect_satisfied}, "
            f"change_state={result.target_change_state}, generated_ladder={result.generated_ladder_tile}, "
            f"ladder_query={result.ladder_query_status}, blocking_menu={result.has_blocking_menu}, "
            f"reason={result.reason}, effect_reason={effect_result.reason}"
        )
        return result

    def _build_break_stone_effect_plan(self, target_tile: Tile | None) -> ToolEffectPlan:
        return ToolEffectPlan(
            owner="Mining",
            action_name="BREAK_STONE",
            target_tile=target_tile,
            effect_checker=lambda state: self._is_break_stone_effect_observed(state, target_tile),
            target_change_checker=lambda state: self._is_break_stone_target_changed(state, target_tile),
            check_ladder_at_target_tile=target_tile is not None,
            effect_timeout_seconds=0.0,
            metadata={
                "phase": "BREAK_STONE",
                "expected_effect": "stone_removed_or_ladder_detected_or_multi_hit_progress_unknown",
            },
        )

    def _is_break_stone_effect_observed(self, state: StardewState, target_tile: Tile | None) -> bool | None:
        if target_tile is None:
            return None
        if self._is_current_stone_done(state, target_tile) or self._has_ladder_at_tile(state, target_tile):
            return True
        return None

    def _is_break_stone_target_changed(self, state: StardewState, target_tile: Tile | None) -> bool | None:
        if target_tile is None:
            return None
        return self._is_current_stone_done(state, target_tile) or self._has_ladder_at_tile(state, target_tile)

    def _mark_current_stone_done_after_aftermath(
        self,
        game_state: StardewState,
        aftermath_result: ToolAftermathResult,
    ) -> None:
        if self._target_tile is None:
            return

        finished_tile = self._target_tile
        if aftermath_result.generated_ladder_tile is not None:
            self._detected_ladder_tile = aftermath_result.generated_ladder_tile
            self._broken_stone_count += 1
            print(f"\n⛏️ [MineNode] 破石后发现梯子: target={finished_tile}, ladder={self._detected_ladder_tile}")
            self._log(
                f"破石后工具后处理发现梯子: target={finished_tile}, ladder={self._detected_ladder_tile}, "
                f"reason={aftermath_result.reason}, broken={self._broken_stone_count}"
            )
            self._target_tile = None
            self._corridor_ladder_tile = None
            self._stone_attempt_count = 0
            self.positioning_controller.reset()
            self.approach_positioning_controller.reset()
            self.tool_action_tracker.reset()
            return

        if aftermath_result.ladder_query_status is None:
            self._log(f"破石后梯子查询失败，按石头已处理继续: target={finished_tile}, reason={aftermath_result.reason}")
        else:
            self._log(f"破石后未发现梯子: target={finished_tile}, reason={aftermath_result.reason}")
        self._mark_current_stone_done(game_state)

    def _request_collect_loot(
        self,
        blackboard: AgentBlackboard,
        source_tile: Tile | None,
        source_type: str,
        loot_tiles: list[Tile],
    ) -> None:
        if source_tile is None or not loot_tiles:
            return

        known_tiles = {(tile.x, tile.y) for tile in blackboard.pending_loot_tiles}
        for loot_tile in loot_tiles:
            if (loot_tile.x, loot_tile.y) in known_tiles:
                continue
            blackboard.pending_loot_tiles.append(loot_tile)
            known_tiles.add((loot_tile.x, loot_tile.y))

        blackboard.require_collect_loot = bool(blackboard.pending_loot_tiles)
        blackboard.collect_loot_owner = "Mining"
        blackboard.collect_loot_source_tile = source_tile
        blackboard.collect_loot_source_type = source_type
        self._log(
            f"发现破石掉落物，触发自动拾取: source={source_tile}, source_type={source_type}, "
            f"loot_tiles={self._format_tile_list(loot_tiles)}, "
            f"pending={self._format_tile_list(blackboard.pending_loot_tiles)}"
        )

    def _has_reached_target_level(self, game_state: StardewState, current_task: MiningTask) -> bool:
        return game_state.mine_level is not None and game_state.mine_level >= current_task.target_mine_level

    def _finish(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        current_task: MiningTask,
    ) -> NodeStatus:
        context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
        blackboard.current_step_index += 1
        print(f"\n🏆 [MineNode] 已进入目标矿层: MineLevel={current_task.target_mine_level}，Mining P0 完成！")
        self._log(f"任务完成: target_mine_level={current_task.target_mine_level}")
        self._reset()
        return "SUCCESS"

    def _fail(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        current_task: MiningTask,
        reason: str,
    ) -> NodeStatus:
        context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
        blackboard.prompt = (
            f"MineNode 执行失败: {reason}; "
            f"target_loc={current_task.target_loc}, target_mine_level={current_task.target_mine_level}"
        )
        print(f"\n🔴 [MineNode] {reason}")
        self._log(f"任务失败: reason={reason}")
        self._reset()
        return "FAILURE"

    def _reset(self) -> None:
        self.positioning_controller.reset()
        self.approach_positioning_controller.reset()
        self.tool_action_tracker.reset()
        self._active_tool_effect_plan = None
        self._phase = None
        self._task_signature = None
        self._started_at = None
        self._target_tile = None
        self._detected_ladder_tile = None
        self._ladder_pursuit_tile = None
        self._approach_stand_tile = None
        self._corridor_ladder_tile = None
        self._active_mine_level = None
        self._return_prompt_tiles = set()
        self._stone_attempt_count = 0
        self._broken_stone_count = 0
        self._failed_stone_tiles = set()
        self._deferred_stone_tiles = set()
        self._last_interact_at = 0.0
        self._has_logged_task = False
        self._last_debug_heartbeat_at = 0.0

    def _resolve_mining_tactical_decision(
        self,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        objective_type: MiningObjectiveType,
        target_tile: Tile,
        candidate_stand_tiles: set[Tile],
    ) -> TacticalDecision:
        threat_snapshot = self.threat_evaluator.evaluate(game_state)
        decision = self.tactical_resolver.resolve_for_mining(
            game_state,
            threat_snapshot,
            MiningObjectiveContext(
                objective_type=objective_type,
                target_tile=target_tile,
                candidate_stand_tiles=candidate_stand_tiles,
            ),
        )

        if decision.decision_type in ("ENGAGE", "AVOID"):
            blackboard.combat_tactical_decision = decision
            context_text = (
                f"objective={objective_type}, target={target_tile}, decision={decision.decision_type}, "
                f"reason={decision.reason}, threat={self._format_threat(decision.target_threat)}"
            )
            self._log(f"写入战术决策并让 Guard 接管: {context_text}")
        elif decision.decision_type in ("REROUTE", "DEFER_OBJECTIVE"):
            blackboard.combat_tactical_decision = None
            self._log(
                f"战术层调整采矿目标: objective={objective_type}, target={target_tile}, "
                f"decision={decision.decision_type}, reason={decision.reason}, "
                f"threat={self._format_threat(decision.target_threat)}"
            )

        return decision

    def _get_interact_objective_type(self, target_name: str) -> MiningObjectiveType:
        if target_name == "矿洞入口":
            return "MINE_ENTRANCE"
        return "LADDER"

    def _tile_distance(self, start_tile: Tile, end_tile: Tile) -> int:
        return abs(start_tile.x - end_tile.x) + abs(start_tile.y - end_tile.y)

    def _normalize_action(self, action: str | None) -> str:
        if action is None:
            return ""
        return " ".join(action.strip().split()).lower()

    def _is_possible_mine_level_entrance(self, target: MineInteractTargetState) -> bool:
        action = self._normalize_action(target.action)
        if not action.startswith("mine"):
            return False
        if "minecart" in action or "elevator" in action:
            return False
        return True

    def _log_debug_heartbeat(self, game_state: StardewState, current_task: MiningTask) -> None:
        now = time.time()
        if now - self._last_debug_heartbeat_at < 0.25:
            return

        self._last_debug_heartbeat_at = now
        self._log(
            f"心跳: phase={self._phase}, task={current_task.mine_action}, "
            f"loc={game_state.location_name}, mine_level={game_state.mine_level}, "
            f"player={game_state.player_tile}, target={self._target_tile}, "
            f"detected_ladder={self._detected_ladder_tile}, "
            f"ladder_pursuit={self._ladder_pursuit_tile}, "
            f"approach_stand={self._approach_stand_tile}, "
            f"corridor_ladder={self._corridor_ladder_tile}, "
            f"stone_attempt={self._stone_attempt_count}, broken={self._broken_stone_count}, "
            f"ladders={self._format_targets(game_state.ladders)}, "
            f"entrances={self._format_targets(game_state.mine_entrances)}, "
            f"mining_nodes={len(game_state.mining_nodes)}, stone_layer={len(game_state.layers.get('Stone', set()))}, "
            f"using_tool={game_state.using_tool}, can_move={game_state.can_move}, "
            f"tracker={self.tool_action_tracker.get_debug_snapshot()}"
        )

    def _format_targets(self, targets: list[MineInteractTargetState]) -> str:
        preview = [
            (
                f"{target.type}@{target.tile}"
                f"/source={target.source or '-'}"
                f"/qid={target.qualified_item_id or '-'}"
                f"/action={target.action or '-'}"
            )
            for target in targets[:8]
        ]
        return "[" + ", ".join(preview) + "]"

    def _format_mine_targets(self, targets: list[MineTarget]) -> str:
        preview = [
            (
                f"{target.target_type}@{target.tile}"
                f"/source={target.source or '-'}"
                f"/qid={target.qualified_item_id or '-'}"
                f"/action={target.action}"
            )
            for target in targets[:8]
        ]
        return "[" + ", ".join(preview) + "]"

    def _format_tiles(self, tiles: set[Tile]) -> str:
        ordered_tiles = sorted(tiles, key=lambda tile: (tile.x, tile.y))
        return "[" + ", ".join(str(tile) for tile in ordered_tiles) + "]"

    def _format_tile_list(self, tiles: list[Tile]) -> str:
        return "[" + ", ".join(str(tile) for tile in tiles) + "]"

    def _format_threat(self, threat: MonsterThreat | None) -> str:
        if threat is None:
            return "None"
        monster = threat.monster
        return (
            f"name={monster.name}, tile={monster.tile}, focused={monster.focused_on_farmer}, "
            f"search={monster.search_array_size}, health={monster.health}, damage={monster.damage_to_farmer}, "
            f"distance={threat.distance_to_player}, score={threat.threat_score:.2f}, level={threat.threat_level}"
        )

    def _log(self, message: str) -> None:
        self.mining_debug_logger.log(f"[MineNode] {message}")
