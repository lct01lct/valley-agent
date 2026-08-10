import heapq
import time
from typing import Literal

from agent.action.combat.combat_tactical_resolver import (
    CombatTacticalResolver,
    MiningObjectiveContext,
    MiningObjectiveType,
    TacticalDecision,
)
from agent.action.combat.weapon_selection import WeaponSelector
from agent.action.location.location import Location
from agent.action.combat.monster_threat import MonsterThreat, MonsterThreatEvaluator
from agent.action.mining.mine_target import MineOpportunitySelector, MineTarget, MineTargetSelector
from agent.action.mining.mining_opportunity_policy import (
    MiningOpportunityPolicy,
    OpportunityDecision,
    OpportunityPolicyConfig,
)
from agent.action.mining.mining_target_resolver import MiningTargetResolver, MiningThreatContext
from agent.action.tool.loot_policy_service import LootPolicyService
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
type MineOpportunityTargetType = Literal[
    "COLLECTIBLE",  # 徒手采集物，例如石英、地晶、冰泪、火水晶、洞穴萝卜等。
    "BREAKABLE_CONTAINER",  # 矿井木箱/木桶，通常用武器打破并处理掉落物。
    "MINING_NODE",  # 路线附近低成本矿点，例如铜矿、宝石矿等。
]
type MiningPhase = Literal[
    "ENTER_MINE",  # 在矿洞大厅寻找入口并进入第一层
    "FIND_LADDER",  # 在矿层中寻找可交互的下层梯子
    "OPPORTUNITY",  # 冲层途中短暂处理低成本顺手资源
    "EXPLORE_STONE",  # 当前连通区域没有可挖石头时，先向剩余石头方向探索接近
    "BREAK_STONE",  # 没有梯子时，选择 Stone / MiningNode 并用镐子破坏
    "DONE",  # 已进入目标矿层，任务完成
]


PICKAXE_TOOL_NAME = "Pickaxe"
MINE_NODE_TIMEOUT_SECONDS = 90.0
MINE_INTERACT_RETRY_INTERVAL_SECONDS = 0.45
MINE_TOOL_START_GRACE_SECONDS = 0.35
MINE_TOOL_FINISH_TIMEOUT_SECONDS = 3.0
MAX_STONE_ATTEMPTS = 8
MAX_OPPORTUNITY_ATTEMPTS = 6
MINE_INTERACT_CLOSE_EDGE_MARGIN = 0.0
MINE_INTERACT_CLOSE_EDGE_DEAD_ZONE = 4.0
ENABLE_MINING_MONSTER_TACTICS = True
LAST_TOOL_SOURCE_LOOT_SCAN_GRACE_SECONDS = 1.2
LAST_TOOL_SOURCE_LOOT_SCAN_DISTANCE = 4


class MiningTask(BaseTask):
    def __init__(
        self,
        task_type: TaskType,
        desc: str,
        mine_action: MiningAction,
        target_loc: Location = "Mine",
        target_mine_level: int = 2,
        max_stones_to_break: int = 60,
        collect_opportunity_resources: bool = False,
        opportunity_target_types: list[MineOpportunityTargetType] | None = None,
        max_opportunity_detour_tiles: int = 10,
    ) -> None:
        super().__init__(task_type=task_type, desc=desc)
        self.mine_action = mine_action
        self.target_loc = target_loc
        self.target_mine_level = target_mine_level
        self.max_stones_to_break = max_stones_to_break
        self.collect_opportunity_resources = collect_opportunity_resources
        self.opportunity_target_types = opportunity_target_types or []
        self.max_opportunity_detour_tiles = max_opportunity_detour_tiles


class MineNode(BTNode):
    """
    Mining P0：进入矿洞第一层，并找到/制造通往第二层的入口。
    """

    def __init__(self) -> None:
        self.positioning_controller = PositioningController()
        self.approach_positioning_controller = PositioningController()
        self.threat_evaluator = MonsterThreatEvaluator()
        self.tactical_resolver = CombatTacticalResolver()
        self.weapon_selector = WeaponSelector()
        self.mine_target_selector = MineTargetSelector()
        self.mine_opportunity_selector = MineOpportunitySelector()
        self.mining_opportunity_policy = MiningOpportunityPolicy()
        self.mining_target_resolver = MiningTargetResolver(
            target_selector=self.mine_target_selector,
            opportunity_selector=self.mine_opportunity_selector,
            opportunity_policy=self.mining_opportunity_policy,
        )
        self.loot_policy_service = LootPolicyService()
        self.tool_aftermath_service = ToolAftermathService()
        self.tool_action_tracker = ToolActionTracker(
            start_grace_seconds=MINE_TOOL_START_GRACE_SECONDS,
            finish_timeout_seconds=MINE_TOOL_FINISH_TIMEOUT_SECONDS,
        )
        self.mining_debug_logger = MiningDebugLogger()
        self._phase: MiningPhase | None = None
        self._task_signature: tuple[int, int, str, bool, tuple[MineOpportunityTargetType, ...]] | None = None
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
        self._active_opportunity_target: MineTarget | None = None
        self._opportunity_anchor_target: MineTarget | None = None
        self._opportunity_anchor_decision: OpportunityDecision | None = None
        self._opportunity_anchor_corridor_breaks_used = 0
        self._explore_stone_tile: Tile | None = None
        self._handled_opportunity_tiles: set[Tile] = set()
        self._skipped_opportunity_tiles: set[Tile] = set()
        self._opportunity_actions_used = 0
        self._pre_ladder_opportunity_actions_used = 0
        self._active_opportunity_is_pre_ladder = False
        self._opportunity_attempt_count = 0
        self._last_opportunity_interact_at = 0.0
        self._last_interact_at = 0.0
        self._has_logged_task = False
        self._last_debug_heartbeat_at = 0.0
        self._active_tool_effect_plan: ToolEffectPlan | None = None
        self._last_tool_source_tile: Tile | None = None
        self._last_tool_source_type: str | None = None
        self._last_tool_finished_at = 0.0

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
        self.loot_policy_service.refresh_deferred_loot(blackboard, game_state)

        task_signature = (
            blackboard.current_step_index,
            current_task.target_mine_level,
            current_task.mine_action,
            current_task.collect_opportunity_resources,
            tuple(current_task.opportunity_target_types),
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
                f"target_mine_level={current_task.target_mine_level}, "
                f"collect_opportunity_resources={current_task.collect_opportunity_resources}"
            )
            self._log(
                "收到采矿任务: "
                f"action={current_task.mine_action}, target_loc={current_task.target_loc}, "
                f"target_mine_level={current_task.target_mine_level}, "
                f"collect_opportunity_resources={current_task.collect_opportunity_resources}, "
                f"opportunity_target_types={current_task.opportunity_target_types}, "
                f"max_opportunity_detour_tiles={current_task.max_opportunity_detour_tiles}, "
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
            self._active_opportunity_target = None
            self._opportunity_anchor_target = None
            self._opportunity_anchor_decision = None
            self._opportunity_anchor_corridor_breaks_used = 0
            self._explore_stone_tile = None
            self._detected_ladder_tile = None
            self._ladder_pursuit_tile = None
            self._approach_stand_tile = None
            self._corridor_ladder_tile = None
            self._handled_opportunity_tiles = set()
            self._skipped_opportunity_tiles = set()
            self._opportunity_actions_used = 0
            self._pre_ladder_opportunity_actions_used = 0
            self._active_opportunity_is_pre_ladder = False
            self._opportunity_attempt_count = 0
            self._last_opportunity_interact_at = 0.0
            self._last_interact_at = 0.0

        if self._phase == "ENTER_MINE":
            self._phase = "FIND_LADDER"
            print(f"\n⛏️ [MineNode] 已进入矿层: MineLevel={game_state.mine_level}，开始寻找下一层。")
            self._log(
                f"已进入矿层: MineLevel={game_state.mine_level}, player={game_state.player_tile}, "
                f"return_prompt_tiles={self._format_tiles(self._return_prompt_tiles)}"
            )

        if self._should_continue_break_stone_phase(game_state):
            return self._run_break_stone_phase(context, blackboard, game_state, current_task)

        if self._phase == "OPPORTUNITY" and self._active_opportunity_target is not None:
            return self._run_opportunity_phase(context, blackboard, game_state, current_task)

        anchored_opportunity_status = self._try_start_or_continue_opportunity_anchor(
            context,
            blackboard,
            game_state,
            current_task,
            is_pre_ladder=self._detected_ladder_tile is not None or self._has_next_level_ladder(game_state),
            allow_create=False,
        )
        if anchored_opportunity_status is not None:
            return anchored_opportunity_status

        if self._detected_ladder_tile is not None:
            if self._promote_deferred_loot_before_ladder(
                blackboard=blackboard,
                game_state=game_state,
                reason="破石后已发现梯子，进入下一层前必须先拾取当前层掉落物",
            ):
                return "RUNNING"
            pre_ladder_status = self._try_start_pre_ladder_opportunity_or_corridor(
                context,
                blackboard,
                game_state,
                current_task,
                ladder_tile=self._detected_ladder_tile,
            )
            if pre_ladder_status is not None:
                return pre_ladder_status
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

        ladder = self._select_next_level_ladder(game_state)
        if ladder is not None:
            if self._promote_deferred_loot_before_ladder(
                blackboard=blackboard,
                game_state=game_state,
                reason="准备进入下一层前必须先拾取当前层掉落物",
            ):
                return "RUNNING"
            pre_ladder_status = self._try_start_pre_ladder_opportunity_or_corridor(
                context,
                blackboard,
                game_state,
                current_task,
                ladder_tile=ladder.tile,
            )
            if pre_ladder_status is not None:
                return pre_ladder_status
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

        if self._phase == "EXPLORE_STONE":
            return self._run_explore_toward_stone_phase(context, blackboard, game_state, current_task)

        anchored_opportunity_status = self._try_start_or_continue_opportunity_anchor(
            context,
            blackboard,
            game_state,
            current_task,
            is_pre_ladder=False,
        )
        if anchored_opportunity_status is not None:
            return anchored_opportunity_status

        self._phase = "BREAK_STONE"
        return self._run_break_stone_phase(context, blackboard, game_state, current_task)

    def _should_continue_break_stone_phase(self, game_state: StardewState) -> bool:
        if self._phase != "BREAK_STONE" or self._target_tile is None:
            return False

        if not self.tool_action_tracker.is_idle():
            if not self._is_stone_tile(game_state, self._target_tile):
                self._log(
                    f"目标石头已变化但工具动作仍在追踪中，继续完成破石后处理: "
                    f"target={self._target_tile}, has_ladder={self._has_ladder_at_tile(game_state, self._target_tile)}, "
                    f"tracker={self.tool_action_tracker.get_debug_snapshot()}"
                )
            return True

        return self._is_stone_tile(game_state, self._target_tile) or self._has_ladder_at_tile(
            game_state,
            self._target_tile,
        )

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

    def _run_opportunity_phase(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: MiningTask,
    ) -> NodeStatus:
        target = self._active_opportunity_target
        if target is None:
            self._finish_opportunity_target(success=False, reason="没有活跃机会目标")
            return "RUNNING"

        if self._is_opportunity_target_done(game_state, target):
            if target.action in ("ATTACK_WEAPON", "USE_PICKAXE") and not self.tool_action_tracker.is_idle():
                return self._run_tool_opportunity(context, blackboard, game_state, target)
            if target.action in ("ATTACK_WEAPON", "USE_PICKAXE"):
                return self._finish_destroyed_opportunity_target(
                    context,
                    blackboard,
                    game_state,
                    target,
                    "目标已从 state 消失",
                )
            self._finish_opportunity_target(success=True, reason="目标已从 state 消失")
            return "RUNNING"

        if target.action == "INTERACT":
            return self._run_interact_opportunity(context, game_state, target)

        if target.action in ("ATTACK_WEAPON", "USE_PICKAXE"):
            return self._run_tool_opportunity(context, blackboard, game_state, target)

        self._finish_opportunity_target(success=False, reason=f"暂不支持的机会目标动作: action={target.action}")
        return "RUNNING"

    def _run_interact_opportunity(
        self,
        context: PlayerContext,
        game_state: StardewState,
        target: MineTarget,
    ) -> NodeStatus:
        positioning_result = self._tick_positioning_for_target(game_state, context, target)
        if positioning_result.status == "FAILED":
            if target.target_type == "COLLECTIBLE" and self._should_approach_distant_target(game_state, target.tile):
                approach_result = self._tick_approach_distant_target(game_state, context, target.tile)
                if approach_result.status == "READY":
                    self._ladder_pursuit_tile = None
                    self._approach_stand_tile = None
                    self.approach_positioning_controller.reset()
                    return "RUNNING"
                if approach_result.status != "FAILED":
                    self._log(
                        f"采集物较远且当前视野无法直接规划贴近站位，先复用梯子接近逻辑: "
                        f"target={self._format_mine_target(target)}, approach_status={approach_result.status}, "
                        f"reason={approach_result.reason}"
                    )
                    return "RUNNING"

                self._approach_stand_tile = None
                self.approach_positioning_controller.reset()
            self._finish_opportunity_target(success=False, reason=f"机会目标站位失败: {positioning_result.reason}")
            return "RUNNING"

        if positioning_result.status in ("MOVING", "FACING"):
            return "RUNNING"

        now = time.time()
        if now - self._last_opportunity_interact_at < MINE_INTERACT_RETRY_INTERVAL_SECONDS:
            return "RUNNING"

        if self._opportunity_attempt_count >= MAX_OPPORTUNITY_ATTEMPTS:
            self._finish_opportunity_target(success=False, reason="机会目标交互重试耗尽")
            return "RUNNING"

        self._last_opportunity_interact_at = now
        self._opportunity_attempt_count += 1
        response = context.executor_client.send_command(
            StardewCommand(
                action=StardewAction.INTERACT_TILE,
                key=["x"],
                tile=(target.tile.x, target.tile.y),
            )
        )
        self._log(
            f"发送机会目标 INTERACT_TILE: target={self._format_mine_target(target)}, "
            f"response={response}, attempt={self._opportunity_attempt_count}/{MAX_OPPORTUNITY_ATTEMPTS}, "
            f"player={game_state.player_tile}"
        )
        return "RUNNING"

    def _run_tool_opportunity(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        target: MineTarget,
    ) -> NodeStatus:
        required_tool = self._resolve_opportunity_required_tool(game_state, target)
        if required_tool is None:
            self._finish_opportunity_target(success=False, reason=f"机会目标缺少可用工具: target={target.target_type}")
            return "RUNNING"

        if not is_current_tool(game_state, required_tool):
            blackboard.require_switch_tool = True
            blackboard.is_switching_tool = True
            blackboard.required_tool_owner = "Mining"
            blackboard.required_tool = required_tool
            print(f"\n🟡 [MineNode] 当前工具不是 {required_tool}，等待切换工具后再处理顺手机会目标。")
            self._log(f"机会目标请求切换工具: target={self._format_mine_target(target)}, required_tool={required_tool}")
            return "SUCCESS"

        if not self.tool_action_tracker.is_idle():
            tool_status = self.tool_action_tracker.tick(game_state)
            self._log(
                f"等待机会目标工具动作收招: target={self._format_mine_target(target)}, "
                f"status={tool_status}, tracker={self.tool_action_tracker.get_debug_snapshot()}"
            )
            if tool_status == "FINISHED":
                self.tool_action_tracker.reset()
                self._remember_last_tool_source_finished(target.tile, target.target_type)
                effect_result = self.tool_aftermath_service.inspect_tool_effect(
                    context,
                    game_state,
                    self._active_tool_effect_plan or self._build_opportunity_effect_plan(target),
                )
                aftermath_result = effect_result.aftermath
                if aftermath_result.has_blocking_menu:
                    self._log(
                        f"机会目标工具动作后发现阻塞 UI，等待 Guard 处理: "
                        f"target={self._format_mine_target(target)}, menu={aftermath_result.blocking_menu_type}"
                    )
                    return "RUNNING"

                target_destroyed = self._is_opportunity_target_done(game_state, target)
                self._log(
                    f"机会目标工具收招完成: target={self._format_mine_target(target)}, "
                    f"destroyed={target_destroyed}, effect_status={effect_result.status}, "
                    f"loot_tiles={self._format_tile_list(aftermath_result.nearby_loot_tiles)}, "
                    f"reason={effect_result.reason}"
                )
                if target_destroyed:
                    if aftermath_result.generated_ladder_tile is not None:
                        self._detected_ladder_tile = aftermath_result.generated_ladder_tile
                    self._request_collect_loot_after_target_destroyed(
                        blackboard,
                        game_state,
                        target,
                        aftermath_result.nearby_loot_tiles,
                        "机会目标已破坏",
                    )
                    self._active_tool_effect_plan = None
                    self._finish_opportunity_target(success=True, reason=f"工具效果完成: {effect_result.reason}")
                    return "RUNNING"

                if self._opportunity_attempt_count >= MAX_OPPORTUNITY_ATTEMPTS:
                    self._finish_opportunity_target(success=False, reason=f"工具效果失败: {effect_result.reason}")
                    return "RUNNING"

                self._active_tool_effect_plan = None
                self._log(
                    f"机会目标本次工具动作未完成目标，准备重试: target={self._format_mine_target(target)}, "
                    f"status={effect_result.status}, destroyed={target_destroyed}, skip_loot=True, "
                    f"reason=目标仍存在，暂不登记掉落物; effect_reason={effect_result.reason}, "
                    f"attempt={self._opportunity_attempt_count}/{MAX_OPPORTUNITY_ATTEMPTS}"
                )
                return "RUNNING"

            if tool_status == "TIMEOUT":
                self.tool_action_tracker.reset()
                self._active_tool_effect_plan = None
                self._finish_opportunity_target(success=False, reason="机会目标工具动作等待超时")
                return "RUNNING"
            return "RUNNING"

        positioning_result = self._tick_positioning_for_target(game_state, context, target)
        if positioning_result.status == "FAILED":
            self._finish_opportunity_target(success=False, reason=f"机会目标站位失败: {positioning_result.reason}")
            return "RUNNING"

        if positioning_result.status in ("MOVING", "FACING"):
            return "RUNNING"

        if self._opportunity_attempt_count >= MAX_OPPORTUNITY_ATTEMPTS:
            self._finish_opportunity_target(success=False, reason="机会目标工具重试耗尽")
            return "RUNNING"

        command_action = StardewAction.ATTACK_WEAPON if target.action == "ATTACK_WEAPON" else StardewAction.USE_TOOL
        response = context.executor_client.send_command(StardewCommand(action=command_action, key=["c"]))
        if response == "BUSY":
            self._log(f"机会目标工具命令 BUSY，等待下一帧: target={self._format_mine_target(target)}")
            return "RUNNING"
        if response == "TIMEOUT" or response is None:
            self._log(
                f"机会目标工具命令异常，等待下一帧重试: target={self._format_mine_target(target)}, response={response}"
            )
            return "RUNNING"

        self._opportunity_attempt_count += 1
        self._active_tool_effect_plan = self._build_opportunity_effect_plan(target)
        self._remember_last_tool_source_started(target.tile, target.target_type)
        self.tool_action_tracker.start()
        print(
            f"\n💎 [MineNode] 处理顺手机会目标: "
            f"{target.target_type} @ {target.tile}, attempt={self._opportunity_attempt_count}"
        )
        self._log(
            f"发送机会目标工具命令: target={self._format_mine_target(target)}, "
            f"action={command_action}, response={response}, "
            f"attempt={self._opportunity_attempt_count}/{MAX_OPPORTUNITY_ATTEMPTS}, player={game_state.player_tile}"
        )
        return "RUNNING"

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
                return self._run_explore_toward_stone_phase(context, blackboard, game_state, current_task)
            print(f"\n⛏️ [MineNode] 没有发现梯子，准备破坏石头: target={self._target_tile}")
            self._log(
                f"选择破坏石头: target={self._target_tile}, player={game_state.player_tile}, "
                f"elapsed={select_elapsed:.3f}s"
            )

        if not self.tool_action_tracker.is_idle():
            return self._run_pending_break_stone_tool_action(context, blackboard, game_state)

        if self._promote_deferred_loot_if_not_covered(
            blackboard,
            game_state,
            self._build_stone_continuation_tiles(game_state, self._target_tile),
            "下一块石头路径无法顺路覆盖掉落物",
        ):
            return "RUNNING"

        if not is_current_tool(game_state, PICKAXE_TOOL_NAME):
            blackboard.require_switch_tool = True
            blackboard.is_switching_tool = True
            blackboard.required_tool_owner = "Mining"
            blackboard.required_tool = PICKAXE_TOOL_NAME
            print(f"\n🟡 [MineNode] 当前工具不是 {PICKAXE_TOOL_NAME}，等待切换工具后再挖矿。")
            return "SUCCESS"

        if self._target_tile is not None and (
            self._is_current_stone_done(game_state, self._target_tile)
            or self._has_ladder_at_tile(game_state, self._target_tile)
        ):
            self._remember_last_tool_source_finished(self._target_tile, "Stone")
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
                self._log("目标已变化，回到主决策层重新评估梯子、顺手资源和下一块石头。")
                return "RUNNING"
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
        self._remember_last_tool_source_started(self._target_tile, "Stone")
        self.tool_action_tracker.start()
        print(f"\n⛏️ [MineNode] 使用镐子破坏石头: target={self._target_tile}, attempt={self._stone_attempt_count}")
        self._log(
            f"发送 USE_TOOL 挥镐: target={self._target_tile}, response={response}, "
            f"attempt={self._stone_attempt_count}/{MAX_STONE_ATTEMPTS}, player={game_state.player_tile}"
        )
        return "RUNNING"

    def _run_pending_break_stone_tool_action(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
    ) -> NodeStatus:
        tool_status = self.tool_action_tracker.tick(game_state)
        self._log(
            f"等待挥镐收招: target={self._target_tile}, status={tool_status}, "
            f"tracker={self.tool_action_tracker.get_debug_snapshot()}"
        )
        if tool_status == "FINISHED":
            self.tool_action_tracker.reset()
            self._remember_last_tool_source_finished(self._target_tile, "Stone")
            aftermath_result = self._inspect_current_stone_aftermath(context, game_state)
            self._active_tool_effect_plan = None
            if aftermath_result.has_blocking_menu:
                self._log(
                    f"挥镐收招后发现阻塞 UI，等待 Guard 处理: target={self._target_tile}, "
                    f"menu={aftermath_result.blocking_menu_type}, text={aftermath_result.blocking_menu_text}"
                )
                return "RUNNING"

            self._request_collect_loot(blackboard, self._target_tile, "Stone", aftermath_result.nearby_loot_tiles)
            self._log(
                f"破石收招后完成统一后处理: target={self._target_tile}, "
                f"loot={self._format_tile_list(aftermath_result.nearby_loot_tiles)}, "
                f"ladder={aftermath_result.generated_ladder_tile}, "
                f"target_change={aftermath_result.target_change_state}"
            )
            if aftermath_result.target_change_state == "CHANGED" or aftermath_result.generated_ladder_tile is not None:
                self._mark_current_stone_done_after_aftermath(game_state, aftermath_result)
                if self._detected_ladder_tile is None and self._target_tile is None:
                    self._log("石头处理完成，回到主决策层重新评估梯子、顺手资源和下一块石头。")
                    return "RUNNING"
            elif self._stone_attempt_count >= MAX_STONE_ATTEMPTS:
                if self._target_tile is not None:
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

    def _run_explore_toward_stone_phase(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: MiningTask,
    ) -> NodeStatus:
        self._phase = "EXPLORE_STONE"
        stone_tile = self._select_exploration_stone_tile(game_state)
        if stone_tile is None:
            return self._fail(context, blackboard, current_task, "当前矿层没有发现剩余 Stone / MiningNode")

        if self._explore_stone_tile != stone_tile:
            self._explore_stone_tile = stone_tile
            self._approach_stand_tile = None
            self.approach_positioning_controller.reset()
            self._log(
                f"当前连通区域没有可达石头，开始向剩余石头探索接近: "
                f"target={stone_tile}, player={game_state.player_tile}"
            )

        approach_result = self._tick_approach_distant_target(game_state, context, stone_tile)
        if approach_result.status == "READY":
            self._log(
                f"探索接近点已到达，重新选择可挖石头: target={stone_tile}, "
                f"stand={approach_result.stand_tile}, player={game_state.player_tile}"
            )
            self._phase = "BREAK_STONE"
            self._explore_stone_tile = None
            self._approach_stand_tile = None
            self.approach_positioning_controller.reset()
            return "RUNNING"

        if approach_result.status == "FAILED":
            self._deferred_stone_tiles.add(stone_tile)
            self._log(
                f"无法向剩余石头方向探索，暂缓该石头: target={stone_tile}, "
                f"reason={approach_result.reason}"
            )
            self._explore_stone_tile = None
            self._approach_stand_tile = None
            self.approach_positioning_controller.reset()
            if self._select_exploration_stone_tile(game_state) is None:
                return self._fail(
                    context,
                    blackboard,
                    current_task,
                    "当前矿层剩余 Stone / MiningNode 都无法接近",
                )
            return "RUNNING"

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
        if not ENABLE_MINING_MONSTER_TACTICS:
            return set()

        snapshot = threat_snapshot or self.threat_evaluator.evaluate(game_state)
        blocked_tiles = set(snapshot.blocking_tiles)
        for tile, risk_score in snapshot.risk_tiles.items():
            if risk_score >= 2.0:
                blocked_tiles.add(tile)
        blocked_tiles.discard(game_state.player_tile)
        return blocked_tiles

    def _get_threat_risk_score(self, threat_snapshot, tile: Tile) -> float:
        if threat_snapshot is None:
            return 0.0
        return threat_snapshot.risk_tiles.get(tile, 0.0)

    def _build_mining_threat_context(self, threat_snapshot=None) -> MiningThreatContext:
        if threat_snapshot is None:
            return MiningThreatContext()
        nearest_threat_distance = (
            None
            if threat_snapshot.nearest_threat is None
            else threat_snapshot.nearest_threat.distance_to_player
        )
        return MiningThreatContext(
            blocked_tiles=frozenset(threat_snapshot.blocking_tiles),
            risk_by_tile=dict(threat_snapshot.risk_tiles),
            nearest_threat_distance=nearest_threat_distance,
        )

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

    def _try_start_pre_ladder_opportunity_or_corridor(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: MiningTask,
        ladder_tile: Tile,
    ) -> NodeStatus | None:
        if not current_task.collect_opportunity_resources:
            return None

        return self._try_start_or_continue_opportunity_anchor(
            context,
            blackboard,
            game_state,
            current_task,
            is_pre_ladder=True,
            ladder_tile=ladder_tile,
            reason=f"已发现梯子 {ladder_tile}，下楼前先完成价值资源锚点",
        )

    def _try_start_or_continue_opportunity_anchor(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: MiningTask,
        is_pre_ladder: bool,
        allow_create: bool = True,
        ladder_tile: Tile | None = None,
        reason: str | None = None,
    ) -> NodeStatus | None:
        if not current_task.collect_opportunity_resources:
            return None

        if self._opportunity_anchor_target is None:
            if not allow_create:
                return None

            anchor_decision = self._select_opportunity_anchor_decision(game_state, current_task, ladder_tile)
            if anchor_decision is None:
                return None

            anchor_target = anchor_decision.target
            self._opportunity_anchor_target = anchor_target
            self._opportunity_anchor_decision = anchor_decision
            self._opportunity_anchor_corridor_breaks_used = 0
            self._log(
                f"锁定价值资源锚点，后续挖石优先朝该方向推进: "
                f"target={self._format_mine_target(anchor_target)}, "
                f"is_pre_ladder={is_pre_ladder}, "
                f"decision={self._format_opportunity_decision(anchor_decision)}, "
                f"reason={reason or '-'}"
            )

        anchor_target = self._opportunity_anchor_target
        if anchor_target is None:
            return None

        if self._is_opportunity_target_done(game_state, anchor_target):
            self._clear_opportunity_anchor(f"锚点资源已完成或消失: target={self._format_mine_target(anchor_target)}")
            return None

        path = self._build_path_to_opportunity_target(game_state, anchor_target)
        if path:
            if self._promote_deferred_loot_if_not_covered(
                blackboard,
                game_state,
                self._build_opportunity_continuation_tiles(game_state, anchor_target),
                "价值资源锚点路径无法顺路覆盖掉落物",
            ):
                return "RUNNING"
            self._start_opportunity_target(
                anchor_target,
                current_task,
                is_pre_ladder=is_pre_ladder,
                reason=reason or "价值资源锚点已可达，优先完成后再回到下楼/挖石主线",
            )
            return self._run_opportunity_phase(context, blackboard, game_state, current_task)

        max_anchor_breaks = self._build_opportunity_policy_config(current_task).max_corridor_break_count
        if self._opportunity_anchor_corridor_breaks_used >= max_anchor_breaks:
            self._skipped_opportunity_tiles.add(anchor_target.tile)
            self._clear_opportunity_anchor(
                f"价值资源锚点通路破石成本过高，放弃: target={self._format_mine_target(anchor_target)}, "
                f"used_breaks={self._opportunity_anchor_corridor_breaks_used}/{max_anchor_breaks}"
            )
            return None

        corridor = self._find_first_breakable_stone_for_blocked_target(
            game_state,
            anchor_target,
            max_breaks=max_anchor_breaks - self._opportunity_anchor_corridor_breaks_used,
        )
        if corridor is None:
            self._skipped_opportunity_tiles.add(anchor_target.tile)
            self._clear_opportunity_anchor(
                f"价值资源锚点当前无法规划通路石头，跳过: target={self._format_mine_target(anchor_target)}"
            )
            return None

        stone_tile, required_breaks, path_length = corridor
        if self._promote_deferred_loot_if_not_covered(
            blackboard,
            game_state,
            self._build_stone_continuation_tiles(game_state, stone_tile),
            "价值资源锚点通路石头路径无法顺路覆盖掉落物",
        ):
            return "RUNNING"

        self._phase = "BREAK_STONE"
        self._target_tile = stone_tile
        self._stone_attempt_count = 0
        self._corridor_ladder_tile = None
        self.positioning_controller.reset()
        self.tool_action_tracker.reset()
        self._active_tool_effect_plan = None
        self._opportunity_anchor_corridor_breaks_used += 1
        self._log(
            f"价值资源锚点不可达，优先破石打通资源方向: "
            f"target={self._format_mine_target(anchor_target)}, stone={stone_tile}, "
            f"required_breaks={required_breaks}, path_length={path_length}, "
            f"used_breaks={self._opportunity_anchor_corridor_breaks_used}/{max_anchor_breaks}, "
            f"is_pre_ladder={is_pre_ladder}, player={game_state.player_tile}"
        )
        return self._run_break_stone_phase(context, blackboard, game_state, current_task)

    def _start_opportunity_target(
        self,
        opportunity_target: MineTarget,
        current_task: MiningTask,
        is_pre_ladder: bool = False,
        reason: str | None = None,
    ) -> None:
        self._phase = "OPPORTUNITY"
        self._active_opportunity_target = opportunity_target
        self._active_opportunity_is_pre_ladder = is_pre_ladder
        self._target_tile = opportunity_target.tile
        self._opportunity_attempt_count = 0
        self._last_opportunity_interact_at = 0.0
        self.positioning_controller.reset()
        self.tool_action_tracker.reset()
        self._active_tool_effect_plan = None
        print(
            f"\n💎 [MineNode] 发现顺手机会目标: "
            f"{opportunity_target.target_type} @ {opportunity_target.tile}"
        )
        self._log(
            f"发现顺手机会目标: target={self._format_mine_target(opportunity_target)}, "
            f"handled={self._opportunity_actions_used}, "
            f"pre_ladder_handled={self._pre_ladder_opportunity_actions_used}, "
            f"policy={self._format_opportunity_decision(self._opportunity_anchor_decision)}, "
            f"is_pre_ladder={is_pre_ladder}, reason={reason or '-'}"
        )

    def _select_opportunity_anchor_decision(
        self,
        game_state: StardewState,
        current_task: MiningTask,
        ladder_tile: Tile | None = None,
    ) -> OpportunityDecision | None:
        allowed_target_types = set(current_task.opportunity_target_types)
        if not allowed_target_types:
            return None

        ignored_tiles = self._handled_opportunity_tiles | self._skipped_opportunity_tiles
        direct_ladder_path = self._build_path_to_ladder_tile(game_state, ladder_tile) if ladder_tile is not None else []
        direct_ladder_path_tiles = self._route_path_to_tile_list(direct_ladder_path)
        self._log_opportunity_anchor_diagnostics(
            game_state=game_state,
            current_task=current_task,
            allowed_target_types=allowed_target_types,
            ignored_tiles=ignored_tiles,
            direct_ladder_path_tiles=direct_ladder_path_tiles,
        )
        threat_snapshot = self.threat_evaluator.evaluate(game_state) if ENABLE_MINING_MONSTER_TACTICS else None
        resolution = self.mining_target_resolver.resolve_opportunity_anchor(
            state=game_state,
            allowed_target_types=allowed_target_types,
            ignored_tiles=ignored_tiles,
            max_visible_resource_distance=current_task.max_opportunity_detour_tiles,
            target_path_builder=lambda target: self._route_path_to_tile_list(
                self._build_path_to_opportunity_target(game_state, target, threat_snapshot)
            ),
            corridor_finder=lambda target, max_breaks: self._find_first_breakable_stone_for_blocked_target(
                game_state,
                target,
                max_breaks,
            ),
            direct_ladder_path_tiles=direct_ladder_path_tiles or None,
            ladder_tile=ladder_tile,
            threat_context=self._build_mining_threat_context(threat_snapshot),
        )
        self._skipped_opportunity_tiles.update(resolution.skipped_tiles)
        self._log(
            f"MiningTargetResolver 机会资源决策: objective={resolution.objective}, "
            f"target={self._format_mine_target(resolution.target)}, "
            f"corridor={resolution.corridor_stone_tile}, reason={resolution.reason}"
        )
        if resolution.objective not in ("COLLECT_RESOURCE", "BREAK_CORRIDOR"):
            return None
        return resolution.opportunity_decision

    def _log_opportunity_anchor_diagnostics(
        self,
        game_state: StardewState,
        current_task: MiningTask,
        allowed_target_types: set[MineOpportunityTargetType],
        ignored_tiles: set[Tile],
        direct_ladder_path_tiles: list[Tile] | None = None,
    ) -> None:
        raw_targets: list[MineTarget] = []
        if "COLLECTIBLE" in allowed_target_types:
            raw_targets.extend(self.mine_target_selector.build_collectible_targets(game_state))
        if "BREAKABLE_CONTAINER" in allowed_target_types:
            raw_targets.extend(self.mine_target_selector.build_breakable_container_targets(game_state))
        if "MINING_NODE" in allowed_target_types:
            raw_targets.extend(
                target
                for target in self.mine_target_selector.build_breakable_rock_targets(game_state)
                if target.target_type == "MINING_NODE"
            )

        if not raw_targets:
            self._log(
                "机会资源候选诊断: raw=0, "
                f"allowed={sorted(allowed_target_types)}, player={game_state.player_tile}, "
                f"max_detour={current_task.max_opportunity_detour_tiles}, "
                f"direct_ladder_path={self._format_tile_list(direct_ladder_path_tiles or [])}"
            )
            return

        sorted_targets = sorted(
            raw_targets,
            key=lambda target: (
                self._tile_distance(game_state.player_tile, target.tile),
                target.target_type,
                target.tile.y,
                target.tile.x,
            ),
        )
        diagnostic_items: list[str] = []
        for target in sorted_targets[:16]:
            distance = self._tile_distance(game_state.player_tile, target.tile)
            path_nearby_distance = (
                None
                if not direct_ladder_path_tiles
                else min(self._tile_distance(target.tile, path_tile) for path_tile in direct_ladder_path_tiles)
            )
            is_ignored = target.tile in ignored_tiles
            is_in_range = (
                distance <= current_task.max_opportunity_detour_tiles
                or (
                    path_nearby_distance is not None
                    and path_nearby_distance <= self._build_opportunity_policy_config(current_task).path_nearby_distance
                )
            )
            is_resource_mining_node = (
                target.target_type != "MINING_NODE"
                or self.mine_opportunity_selector.is_resource_mining_node(target)
            )

            reason = "candidate"
            path_length_text = "-"
            corridor_text = "-"
            if is_ignored:
                reason = "ignored"
            elif not is_in_range:
                reason = "out_of_range"
            elif not is_resource_mining_node:
                reason = "non_resource_mining_node"
            else:
                path = self._build_path_to_opportunity_target(game_state, target)
                if path:
                    path_length = max(0, len(path) - 1)
                    path_length_text = str(path_length)
                    reason = "reachable" if path_length <= current_task.max_opportunity_detour_tiles else "path_over_budget"
                else:
                    corridor = self._find_first_breakable_stone_for_blocked_target(
                        game_state,
                        target,
                        max_breaks=max(1, current_task.max_opportunity_detour_tiles),
                    )
                    if corridor is None:
                        reason = "unreachable"
                    else:
                        first_stone, required_breaks, corridor_path_length = corridor
                        corridor_text = f"{first_stone}/{required_breaks}/{corridor_path_length}"
                        reason = "blocked_by_stone"

            diagnostic_items.append(
                (
                    f"{self._format_mine_target(target)}"
                    f"/dist={distance}"
                    f"/path_near={path_nearby_distance if path_nearby_distance is not None else '-'}"
                    f"/resource={is_resource_mining_node}"
                    f"/path={path_length_text}"
                    f"/corridor={corridor_text}"
                    f"/reason={reason}"
                )
            )

        self._log(
            "机会资源候选诊断: "
            f"raw={len(raw_targets)}, shown={len(diagnostic_items)}, "
            f"allowed={sorted(allowed_target_types)}, player={game_state.player_tile}, "
            f"max_detour={current_task.max_opportunity_detour_tiles}, "
            f"direct_ladder_path={self._format_tile_list(direct_ladder_path_tiles or [])}, "
            f"ignored={len(ignored_tiles)}, candidates=["
            + "; ".join(diagnostic_items)
            + "]"
        )

    def _clear_opportunity_anchor(self, reason: str) -> None:
        self._log(reason)
        self._opportunity_anchor_target = None
        self._opportunity_anchor_decision = None
        self._opportunity_anchor_corridor_breaks_used = 0

    def _find_first_breakable_stone_for_blocked_target(
        self,
        game_state: StardewState,
        target: MineTarget,
        max_breaks: int,
    ) -> tuple[Tile, int, int] | None:
        map_width, map_height = game_state.map_size
        player_tile = game_state.player_tile
        soft_stones = self._get_candidate_stone_tiles(game_state, self._failed_stone_tiles | self._deferred_stone_tiles)
        soft_stones.discard(target.tile)
        soft_stones.discard(player_tile)
        if not soft_stones:
            return None

        hard_blocked_tiles = astar_solver._get_blocked_tiles(game_state) - soft_stones
        if target.blocks_movement:
            hard_blocked_tiles.add(target.tile)
        hard_blocked_tiles.discard(player_tile)

        goal_tiles = {
            tile
            for tile in target.candidate_stand_tiles
            if 0 <= tile.x < map_width
            if 0 <= tile.y < map_height
            if tile not in hard_blocked_tiles
        }
        if not goal_tiles:
            return None

        queue: list[tuple[int, int, int, Tile, list[Tile]]] = []
        sequence = 0
        heapq.heappush(queue, (0, 0, sequence, player_tile, []))
        best_seen: dict[Tile, tuple[int, int]] = {player_tile: (0, 0)}
        directions = [
            (0, 1),
            (0, -1),
            (1, 0),
            (-1, 0),
        ]

        while queue:
            break_count, path_length, _, current_tile, broken_path = heapq.heappop(queue)
            if current_tile in goal_tiles and broken_path:
                return broken_path[0], break_count, path_length

            for dx, dy in directions:
                next_tile = Tile(current_tile.x + dx, current_tile.y + dy)
                if next_tile.x < 0 or next_tile.y < 0 or next_tile.x >= map_width or next_tile.y >= map_height:
                    continue
                if next_tile in hard_blocked_tiles:
                    continue

                next_break_count = break_count + (1 if next_tile in soft_stones else 0)
                if next_break_count > max_breaks:
                    continue

                next_path_length = path_length + 1
                previous_best = best_seen.get(next_tile)
                if previous_best is not None and previous_best <= (next_break_count, next_path_length):
                    continue

                next_broken_path = [*broken_path, next_tile] if next_tile in soft_stones else broken_path
                best_seen[next_tile] = (next_break_count, next_path_length)
                sequence += 1
                heapq.heappush(queue, (next_break_count, next_path_length, sequence, next_tile, next_broken_path))

        return None

    def _build_path_to_opportunity_target(
        self,
        game_state: StardewState,
        target: MineTarget,
        threat_snapshot=None,
    ) -> list[RouteTile]:
        extra_blocked_tiles = self._get_tactical_blocked_tiles(game_state, threat_snapshot)
        if target.blocks_movement:
            extra_blocked_tiles.add(target.tile)
        extra_blocked_tiles.discard(game_state.player_tile)
        return self._build_path_to_tiles(game_state, target.candidate_stand_tiles, extra_blocked_tiles)

    def _build_path_to_ladder_tile(self, game_state: StardewState, ladder_tile: Tile | None) -> list[RouteTile]:
        if ladder_tile is None:
            return []
        extra_blocked_tiles = {ladder_tile}
        extra_blocked_tiles.update(self._get_tactical_blocked_tiles(game_state))
        extra_blocked_tiles.discard(game_state.player_tile)
        return self._build_path_to_tiles(
            game_state,
            self._build_cardinal_neighbor_tiles(ladder_tile),
            extra_blocked_tiles,
        )

    def _route_path_to_tile_list(self, path: list[RouteTile]) -> list[Tile]:
        return [Tile(route_tile.x, route_tile.y) for route_tile in path]

    def _build_opportunity_policy_config(self, current_task: MiningTask) -> OpportunityPolicyConfig:
        return OpportunityPolicyConfig(
            max_visible_resource_distance=current_task.max_opportunity_detour_tiles,
        )

    def _format_opportunity_decision(self, decision: OpportunityDecision | None) -> str:
        if decision is None:
            return "-"
        return (
            f"score={decision.score:.1f}, value={decision.resource_value:.1f}, "
            f"direct={decision.direct_ladder_cost if decision.direct_ladder_cost is not None else '-'}, "
            f"resource_cost={decision.resource_cost:.1f}, "
            f"extra={decision.extra_path_cost if decision.extra_path_cost is not None else '-'}, "
            f"effective_extra={decision.effective_extra_path_cost if decision.effective_extra_path_cost is not None else '-'}, "
            f"path_near={decision.path_nearby_distance if decision.path_nearby_distance is not None else '-'}, "
            f"near_bonus={decision.near_player_bonus:.1f}, "
            f"action={decision.action_cost:.1f}, break={decision.break_cost:.1f}, "
            f"risk={decision.risk_cost:.1f}, should_take={decision.should_take}, reason={decision.reason}"
        )

    def _tick_positioning_for_target(
        self,
        game_state: StardewState,
        context: PlayerContext,
        target: MineTarget,
    ) -> PositioningResult:
        return self._tick_positioning(
            game_state,
            context,
            target.tile,
            allow_standing_on_target=target.can_stand_on_target,
            require_tool_target=target.require_tool_target,
            block_target=target.blocks_movement,
            forced_stand_tiles=target.candidate_stand_tiles,
            require_close_to_target=target.require_close_to_target,
        )

    def _is_opportunity_target_done(self, game_state: StardewState, target: MineTarget) -> bool:
        if target.target_type == "COLLECTIBLE":
            return target.tile not in game_state.mine_collectibles_by_tile
        if target.target_type == "BREAKABLE_CONTAINER":
            return target.tile not in game_state.mine_breakable_containers_by_tile
        if target.target_type == "MINING_NODE":
            return target.tile not in game_state.mining_nodes_by_tile
        return False

    def _resolve_opportunity_required_tool(self, game_state: StardewState, target: MineTarget) -> str | None:
        if target.action == "USE_PICKAXE":
            return PICKAXE_TOOL_NAME
        if target.action == "ATTACK_WEAPON":
            weapon = self.weapon_selector.select_best_weapon(game_state)
            return None if weapon is None else weapon.name
        return target.required_tool

    def _build_opportunity_effect_plan(self, target: MineTarget) -> ToolEffectPlan:
        if target.action == "USE_PICKAXE":
            action_name = "BREAK_STONE"
        else:
            action_name = "BREAK_CONTAINER"

        return ToolEffectPlan(
            owner="Mining",
            action_name=action_name,
            target_tile=target.tile,
            effect_checker=lambda state: self._is_opportunity_target_done(state, target),
            target_change_checker=lambda state: self._is_opportunity_target_done(state, target),
            check_ladder_at_target_tile=target.action == "USE_PICKAXE",
            loot_scan_distance=4,
            effect_timeout_seconds=0.0,
            metadata={
                "phase": "OPPORTUNITY",
                "target_type": target.target_type,
                "target_name": target.name,
                "expected_effect": "opportunity_target_removed",
            },
        )

    def _finish_destroyed_opportunity_target(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        target: MineTarget,
        reason: str,
    ) -> NodeStatus:
        effect_result = self.tool_aftermath_service.inspect_tool_effect(
            context,
            game_state,
            self._active_tool_effect_plan or self._build_opportunity_effect_plan(target),
        )
        aftermath_result = effect_result.aftermath
        if aftermath_result.has_blocking_menu:
            self._log(
                f"机会目标已消失但发现阻塞 UI，等待 Guard 处理: "
                f"target={self._format_mine_target(target)}, menu={aftermath_result.blocking_menu_type}"
            )
            return "RUNNING"

        if aftermath_result.generated_ladder_tile is not None:
            self._detected_ladder_tile = aftermath_result.generated_ladder_tile

        self._request_collect_loot_after_target_destroyed(
            blackboard,
            game_state,
            target,
            aftermath_result.nearby_loot_tiles,
            reason,
        )
        self._active_tool_effect_plan = None
        self._finish_opportunity_target(success=True, reason=reason)
        return "RUNNING"

    def _request_collect_loot_after_target_destroyed(
        self,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        target: MineTarget,
        loot_tiles: list[Tile],
        reason: str,
    ) -> None:
        resolved_loot_tiles = loot_tiles or self._find_collectible_loot_tiles_near_source(
            game_state,
            target.tile,
            LAST_TOOL_SOURCE_LOOT_SCAN_DISTANCE,
        )
        self._log(
            f"机会目标已破坏，扫描掉落物: target={self._format_mine_target(target)}, "
            f"reason={reason}, loot_tiles={self._format_tile_list(resolved_loot_tiles)}"
        )
        self._request_collect_loot(
            blackboard,
            target.tile,
            target.target_type,
            resolved_loot_tiles,
        )

    def _finish_opportunity_target(self, success: bool, reason: str) -> None:
        target = self._active_opportunity_target
        if target is not None:
            if success:
                self._handled_opportunity_tiles.add(target.tile)
                if self._active_opportunity_is_pre_ladder:
                    self._pre_ladder_opportunity_actions_used += 1
                else:
                    self._opportunity_actions_used += 1
            else:
                self._skipped_opportunity_tiles.add(target.tile)
            self._log(
                f"机会目标结束: success={success}, target={self._format_mine_target(target)}, "
                f"reason={reason}, used={self._opportunity_actions_used}, "
                f"pre_ladder_used={self._pre_ladder_opportunity_actions_used}, "
                f"is_pre_ladder={self._active_opportunity_is_pre_ladder}"
            )
            if self._opportunity_anchor_target is not None and self._opportunity_anchor_target.tile == target.tile:
                self._clear_opportunity_anchor(f"价值资源锚点已结束: success={success}, target={self._format_mine_target(target)}")

        self._active_opportunity_target = None
        self._active_opportunity_is_pre_ladder = False
        self._target_tile = None
        self._phase = "FIND_LADDER"
        self._opportunity_attempt_count = 0
        self._last_opportunity_interact_at = 0.0
        self.positioning_controller.reset()
        self.tool_action_tracker.reset()
        self._active_tool_effect_plan = None

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
        resolution = self.mining_target_resolver.resolve_ladder(game_state, self._return_prompt_tiles)
        if len(game_state.ladders) > 0 and resolution.target is None:
            self._log(
                "过滤矿层返回入口梯子: "
                f"return_prompt_tiles={self._format_tiles(self._return_prompt_tiles)}, "
                f"raw_ladders={self._format_targets(game_state.ladders)}, reason={resolution.reason}"
            )
        return resolution.target

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
        threat_snapshot = self.threat_evaluator.evaluate(game_state) if ENABLE_MINING_MONSTER_TACTICS else None
        tactical_blocked_tiles = self._get_tactical_blocked_tiles(game_state, threat_snapshot)
        resolution = self.mining_target_resolver.resolve_break_search_stone(
            state=game_state,
            excluded_tiles=excluded_tiles,
            stand_path_builder=lambda candidate_tiles: self._route_path_to_tile_list(
                self._build_path_to_tiles(game_state, candidate_tiles, tactical_blocked_tiles)
            ),
            threat_context=self._build_mining_threat_context(threat_snapshot),
        )
        if resolution.target is None and self._deferred_stone_tiles:
            self._log(f"没有非暂缓石头，清空暂缓集合后重新选择: deferred={self._format_tiles(self._deferred_stone_tiles)}")
            self._deferred_stone_tiles = set()
            return self._select_nearest_stone_tile(game_state)
        self._log(
            f"MiningTargetResolver 普通破石决策: objective={resolution.objective}, "
            f"target={self._format_mine_target(resolution.target)}, reason={resolution.reason}"
        )
        return None if resolution.target is None else resolution.target.tile

    def _select_exploration_stone_tile(self, game_state: StardewState) -> Tile | None:
        excluded_tiles = self._failed_stone_tiles | self._deferred_stone_tiles
        threat_snapshot = self.threat_evaluator.evaluate(game_state) if ENABLE_MINING_MONSTER_TACTICS else None
        resolution = self.mining_target_resolver.resolve_exploration_stone(
            state=game_state,
            excluded_tiles=excluded_tiles,
            threat_context=self._build_mining_threat_context(threat_snapshot),
        )
        if resolution.target is None:
            if self._deferred_stone_tiles:
                self._log(
                    f"剩余石头都在暂缓集合中，清空暂缓集合后重新探索: "
                    f"deferred={self._format_tiles(self._deferred_stone_tiles)}"
                )
                self._deferred_stone_tiles = set()
                resolution = self.mining_target_resolver.resolve_exploration_stone(
                    state=game_state,
                    excluded_tiles=self._failed_stone_tiles,
                    threat_context=self._build_mining_threat_context(threat_snapshot),
                )
        self._log(
            f"MiningTargetResolver 探索石头决策: objective={resolution.objective}, "
            f"target={self._format_mine_target(resolution.target)}, reason={resolution.reason}"
        )
        return None if resolution.target is None else resolution.target.tile

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

        threat_snapshot = self.threat_evaluator.evaluate(game_state) if ENABLE_MINING_MONSTER_TACTICS else None
        safe_nearby_stones = [
            tile
            for tile in nearby_stones
            if threat_snapshot is None or tile not in threat_snapshot.blocking_tiles
        ]
        if not safe_nearby_stones:
            return None

        return min(
            safe_nearby_stones,
            key=lambda tile: (
                self._get_threat_risk_score(threat_snapshot, tile),
                self._tile_distance(tile, current_target),
            ),
        )

    def _select_corridor_stone_toward_tile(self, game_state: StardewState, target_tile: Tile) -> Tile | None:
        excluded_tiles = self._failed_stone_tiles | self._deferred_stone_tiles | {target_tile}
        stone_tiles = self._get_candidate_stone_tiles(game_state, excluded_tiles)
        if not stone_tiles:
            return None

        threat_snapshot = self.threat_evaluator.evaluate(game_state) if ENABLE_MINING_MONSTER_TACTICS else None
        current_distance_to_target = self._tile_distance(game_state.player_tile, target_tile)
        scored_stones: list[tuple[tuple[float, int, int, int], Tile]] = []
        for stone_tile in stone_tiles:
            if threat_snapshot is not None and stone_tile in threat_snapshot.blocking_tiles:
                continue
            path = self._build_path_to_stone_stand_tiles(game_state, stone_tile, threat_snapshot)
            if not path:
                continue

            stone_distance_to_target = self._tile_distance(stone_tile, target_tile)
            opens_toward_target = stone_distance_to_target < current_distance_to_target
            score = (
                self._get_threat_risk_score(threat_snapshot, stone_tile),
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
        self._explore_stone_tile = None
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
            loot_scan_distance=4,
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

        self.loot_policy_service.register_deferred_loot(
            blackboard=blackboard,
            owner="Mining",
            source_tile=source_tile,
            source_type=source_type,
            loot_tiles=loot_tiles,
        )
        self._log(
            f"发现破石掉落物，登记延迟拾取: source={source_tile}, source_type={source_type}, "
            f"loot_tiles={self._format_tile_list(loot_tiles)}, "
            f"deferred={self._format_deferred_loot_records(blackboard)}"
        )

    def _remember_last_tool_source_started(self, source_tile: Tile | None, source_type: str) -> None:
        if source_tile is None:
            return
        self._last_tool_source_tile = source_tile
        self._last_tool_source_type = source_type
        self._last_tool_finished_at = 0.0

    def _remember_last_tool_source_finished(self, source_tile: Tile | None, source_type: str) -> None:
        if source_tile is None:
            return
        self._last_tool_source_tile = source_tile
        self._last_tool_source_type = source_type
        self._last_tool_finished_at = time.time()

    def _request_visible_loot_from_last_tool_source(
        self,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        reason: str,
    ) -> bool:
        if self._last_tool_source_tile is None or self._last_tool_source_type is None:
            return False
        if self._last_tool_finished_at <= 0:
            return False
        if time.time() - self._last_tool_finished_at > LAST_TOOL_SOURCE_LOOT_SCAN_GRACE_SECONDS:
            return False

        loot_tiles = self._find_collectible_loot_tiles_near_source(
            game_state,
            self._last_tool_source_tile,
            LAST_TOOL_SOURCE_LOOT_SCAN_DISTANCE,
        )
        if not loot_tiles:
            self._log(
                f"下梯子前兜底扫描最后工具来源，未发现可拾取掉落物: reason={reason}, "
                f"source={self._last_tool_source_tile}, source_type={self._last_tool_source_type}, "
                f"scan_distance={LAST_TOOL_SOURCE_LOOT_SCAN_DISTANCE}"
            )
            return False

        self._request_collect_loot(blackboard, self._last_tool_source_tile, self._last_tool_source_type, loot_tiles)
        promoted = self.loot_policy_service.promote_deferred_loot(blackboard, game_state, "Mining")
        self._log(
            f"下梯子前兜底发现最后工具来源附近掉落物，先拾取: reason={reason}, "
            f"source={self._last_tool_source_tile}, source_type={self._last_tool_source_type}, "
            f"loot_tiles={self._format_tile_list(loot_tiles)}, promoted={promoted}, "
            f"pending={self._format_tile_list(blackboard.pending_loot_tiles)}"
        )
        return promoted or (blackboard.require_collect_loot and blackboard.collect_loot_owner == "Mining")

    def _find_collectible_loot_tiles_near_source(
        self,
        game_state: StardewState,
        source_tile: Tile,
        max_distance: int,
    ) -> list[Tile]:
        loot_tiles: list[Tile] = []
        seen_tiles: set[tuple[int, int]] = set()
        for debris in getattr(game_state, "debris", []):
            if not bool(getattr(debris, "is_collectible", False)):
                continue
            debris_tile = getattr(debris, "tile", None)
            if not isinstance(debris_tile, Tile):
                continue
            if self._tile_chebyshev_distance(source_tile, debris_tile) > max_distance:
                continue
            tile_key = (debris_tile.x, debris_tile.y)
            if tile_key in seen_tiles:
                continue
            seen_tiles.add(tile_key)
            loot_tiles.append(debris_tile)

        return sorted(loot_tiles, key=lambda tile: self._tile_distance(game_state.player_tile, tile))

    def _promote_deferred_loot_if_not_covered(
        self,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        continuation_tiles: set[Tile],
        reason: str,
    ) -> bool:
        if not blackboard.deferred_loot_records:
            return False

        has_expired_deferred_loot = self.loot_policy_service.has_expired_deferred_loot(blackboard, "Mining")
        has_missed_expected_cover = self.loot_policy_service.has_missed_expected_cover_for_owner(
            blackboard,
            game_state,
            "Mining",
        )
        should_promote = self.loot_policy_service.should_promote_deferred_loot(
            blackboard=blackboard,
            state=game_state,
            owner="Mining",
            continuation_tiles=continuation_tiles,
        )
        if not should_promote:
            self._log(
                f"延迟拾取继续等待顺路磁吸: reason={reason}, "
                f"continuation={self._format_tiles(continuation_tiles)}, "
                f"deferred={self._format_deferred_loot_records(blackboard)}"
            )
            return False

        promoted = self.loot_policy_service.promote_deferred_loot(blackboard, game_state, "Mining")
        if promoted:
            if has_expired_deferred_loot:
                promote_reason = "延迟拾取超过等待窗口，立刻主动拾取"
            elif has_missed_expected_cover:
                promote_reason = "已经过预计磁吸覆盖地块但掉落物仍存在，立刻主动拾取"
            else:
                promote_reason = reason
            self._log(
                f"延迟拾取转为主动拾取: reason={promote_reason}, "
                f"continuation={self._format_tiles(continuation_tiles)}, "
                f"pending={self._format_tile_list(blackboard.pending_loot_tiles)}"
            )
        return promoted

    def _promote_deferred_loot_before_ladder(
        self,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        reason: str,
    ) -> bool:
        if blackboard.require_collect_loot and blackboard.collect_loot_owner == "Mining":
            self._log(
                f"下梯子前已有主动拾取请求，等待 CollectLootNode 处理: reason={reason}, "
                f"pending={self._format_tile_list(blackboard.pending_loot_tiles)}"
            )
            return True

        if self._request_visible_loot_from_last_tool_source(blackboard, game_state, reason):
            return True

        if not blackboard.deferred_loot_records:
            return False

        promoted = self.loot_policy_service.promote_deferred_loot(blackboard, game_state, "Mining")
        if promoted:
            self._log(
                f"进入下一层前延迟拾取转为主动拾取: reason={reason}, "
                f"pending={self._format_tile_list(blackboard.pending_loot_tiles)}"
            )
        return promoted

    def _build_stone_continuation_tiles(self, game_state: StardewState, stone_tile: Tile | None) -> set[Tile]:
        if stone_tile is None:
            return set()

        path = self._build_path_to_stone_stand_tiles(game_state, stone_tile)
        return self._route_path_to_tiles(path) | self._build_cardinal_neighbor_tiles(stone_tile)

    def _build_opportunity_continuation_tiles(self, game_state: StardewState, target: MineTarget) -> set[Tile]:
        path = self._build_path_to_opportunity_target(game_state, target)
        return self._route_path_to_tiles(path) | target.candidate_stand_tiles

    def _build_interact_continuation_tiles(self, game_state: StardewState, target_tile: Tile) -> set[Tile]:
        candidate_tiles = self._build_cardinal_neighbor_tiles(target_tile)
        path = self._build_path_to_tiles(game_state, candidate_tiles, {target_tile})
        return self._route_path_to_tiles(path) | candidate_tiles

    def _route_path_to_tiles(self, path: list[RouteTile]) -> set[Tile]:
        return {Tile(route_tile.x, route_tile.y) for route_tile in path}

    def _format_deferred_loot_records(self, blackboard: AgentBlackboard) -> str:
        return str(
            [
                {
                    "owner": record.owner,
                    "source": (record.source_tile.x, record.source_tile.y),
                    "source_type": record.source_type,
                    "loot": [(tile.x, tile.y) for tile in record.loot_tiles],
                    "expected_cover": [(tile.x, tile.y) for tile in record.expected_cover_tiles],
                }
                for record in blackboard.deferred_loot_records
            ]
        )

    def _has_reached_target_level(self, game_state: StardewState, current_task: MiningTask) -> bool:
        return game_state.mine_level is not None and game_state.mine_level >= current_task.target_mine_level

    def _finish(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        current_task: MiningTask,
    ) -> NodeStatus:
        if context.state is not None and self.loot_policy_service.promote_deferred_loot(blackboard, context.state, "Mining"):
            self._log(
                f"Mining 完成前仍有延迟掉落物，先转为主动拾取: "
                f"pending={self._format_tile_list(blackboard.pending_loot_tiles)}"
            )
            return "RUNNING"

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
        self._active_opportunity_target = None
        self._opportunity_anchor_target = None
        self._opportunity_anchor_decision = None
        self._opportunity_anchor_corridor_breaks_used = 0
        self._explore_stone_tile = None
        self._handled_opportunity_tiles = set()
        self._skipped_opportunity_tiles = set()
        self._opportunity_actions_used = 0
        self._pre_ladder_opportunity_actions_used = 0
        self._active_opportunity_is_pre_ladder = False
        self._opportunity_attempt_count = 0
        self._last_opportunity_interact_at = 0.0
        self._last_interact_at = 0.0
        self._has_logged_task = False
        self._last_debug_heartbeat_at = 0.0
        self._last_tool_source_tile = None
        self._last_tool_source_type = None
        self._last_tool_finished_at = 0.0

    def _resolve_mining_tactical_decision(
        self,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        objective_type: MiningObjectiveType,
        target_tile: Tile,
        candidate_stand_tiles: set[Tile],
    ) -> TacticalDecision:
        if not ENABLE_MINING_MONSTER_TACTICS:
            blackboard.combat_tactical_decision = None
            return TacticalDecision(
                decision_type="IGNORE",
                target_threat=None,
                reason="Mining 临时关闭怪物战术判断",
                expires_at=time.time() + 0.1,
            )

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

    def _tile_chebyshev_distance(self, start_tile: Tile, end_tile: Tile) -> int:
        return max(abs(start_tile.x - end_tile.x), abs(start_tile.y - end_tile.y))

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
            f"explore_stone={self._explore_stone_tile}, "
            f"opportunity={self._format_mine_target(self._active_opportunity_target)}, "
            f"opportunity_handled={self._opportunity_actions_used}, "
            f"pre_ladder_opportunity_handled={self._pre_ladder_opportunity_actions_used}, "
            f"opportunity_decision={self._format_opportunity_decision(self._opportunity_anchor_decision)}, "
            f"detected_ladder={self._detected_ladder_tile}, "
            f"ladder_pursuit={self._ladder_pursuit_tile}, "
            f"approach_stand={self._approach_stand_tile}, "
            f"corridor_ladder={self._corridor_ladder_tile}, "
            f"stone_attempt={self._stone_attempt_count}, broken={self._broken_stone_count}, "
            f"ladders={self._format_targets(game_state.ladders)}, "
            f"entrances={self._format_targets(game_state.mine_entrances)}, "
            f"mining_nodes={len(game_state.mining_nodes)}, stone_layer={len(game_state.layers.get('Stone', set()))}, "
            f"collectibles={len(game_state.mine_collectibles)}, "
            f"breakable_containers={len(game_state.mine_breakable_containers)}, "
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

    def _format_mine_target(self, target: MineTarget | None) -> str:
        if target is None:
            return "None"
        return (
            f"{target.target_type}@{target.tile}"
            f"/name={target.name or '-'}"
            f"/source={target.source or '-'}"
            f"/qid={target.qualified_item_id or '-'}"
            f"/kind={target.mining_node_kind or '-'}"
            f"/parent={target.parent_sheet_index}"
            f"/hits={target.estimated_hits_to_break}"
            f"/action={target.action}"
        )

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
