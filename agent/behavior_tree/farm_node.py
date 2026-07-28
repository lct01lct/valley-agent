import time
from collections import deque
from typing import Literal

from agent.action.location.location import Location
from agent.action.tool.tool_aftermath_service import ToolEffectAction, ToolAftermathService, ToolEffectPlan
from agent.action.valley_action.AStar import astar_solver
from agent.action.valley_action.clearance_policy import (
    FRUIT_TREE_LAYERS,
    decide_clear_obstacle,
    get_obstacle_type_at_tile,
    normalize_obstacle_type,
)
from agent.action.valley_action.positioning_controller import PositioningController, PositioningGoal, PositioningResult
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.action.valley_action.tool_targeting import format_tool_target
from agent.base_task import BaseTask, TaskType
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.farm_debug_logger import FarmDebugLogger
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.tool_action_tracker import ToolActionTracker
from agent.behavior_tree.tool_selection import (
    has_tool_area_tree1_risk,
    is_current_tool,
    select_required_tool_for_obstacle,
)
from server.valley_server import InventoryItem, StardewState
from server.type import Tile

type FarmAction = Literal["PLANT", "WATER", "PLANT_AND_WATER"]
type FarmWorkPhase = Literal["HOE", "PLANT", "WATER", "DONE"]
type FarmBatchPhase = Literal[
    "CLEAR_OBSTACLES",  # 批量清理 7x7 区域内可清障碍
    "HOE_TILES",  # 批量把候选地块锄成 HoeDirt
    "PLANT_SEEDS",  # 批量播种指定 seed_name
    "WATER_TILES",  # 批量给已播种作物浇水
    "DONE",  # 批处理任务已完成
]

HOE_TOOL_NAME = "Hoe"
WATERING_CAN_TOOL_NAME = "Watering Can"
WATER_ACTION_TIMEOUT_SECONDS = 12.0
FARM_ACTION_TIMEOUT_SECONDS = 12.0
MAX_WATER_ATTEMPTS = 3
MAX_FARM_ACTION_ATTEMPTS = 3
STATE_SETTLE_TICKS = 3
WATER_TOOL_VERIFY_DELAY_SECONDS = 0.75
HOE_TOOL_VERIFY_DELAY_SECONDS = 1.2
PLANT_ITEM_VERIFY_DELAY_SECONDS = 0.9
FARM_TOOL_START_GRACE_SECONDS = 0.35
FARM_TOOL_FINISH_TIMEOUT_SECONDS = 3.0
WATER_SUCCESS_SETTLE_SECONDS = 0.2
WATER_RESULT_GRACE_SECONDS = 0.25
FARM_ACTION_FAILURE_SETTLE_SECONDS = 0.45
FARM_POSITIONING_STUCK_SETTLE_SECONDS = 0.45
POSITIONING_SOFT_RECOVER_SETTLE_SECONDS = 0.08
POSITIONING_STUCK_TIMEOUT_SECONDS = 1.2
MAX_POSITIONING_SOFT_RECOVERIES = 2
FAILED_WATER_RETRY_DELAY_SECONDS = 1.0
MAX_FAILED_WATER_RETRY_COUNT = 3
FARM_EFFECT_TIMEOUT_SECONDS = 1.0


class FarmTask(BaseTask):
    def __init__(
        self,
        task_type: TaskType,
        desc: str,
        farm_action: FarmAction,
        target_loc: Location = "Farm",
        seed_name: str | None = None,
        count: int = 1,
        target_tiles: list[Tile] | None = None,
        area_origin: Tile | None = None,
        area_width: int = 0,
        area_height: int = 0,
    ):
        super().__init__(task_type=task_type, desc=desc)
        self.farm_action = farm_action
        self.target_loc: Location = target_loc
        self.seed_name = seed_name
        self.count = count
        self.target_tiles = target_tiles or []
        self.area_origin = area_origin
        self.area_width = area_width
        self.area_height = area_height


class FarmNode(BTNode):
    """
    FARM 任务的确定性执行入口。

    P0 支持指定/自动选择作物浇水。
    P1 支持批处理流水线：规划候选地块 -> 批量清障 -> 批量锄地 -> 批量播种 -> 批量浇水。
    """

    def __init__(self) -> None:
        self.positioning_controller = PositioningController()
        self._target_tile: Tile | None = None
        self._started_at: float | None = None
        self._attempt_count = 0
        self._wait_ticks = 0
        self._has_faced_target = False
        self._has_logged_task = False
        self._watered_tiles: set[Tile] = set()
        self._failed_water_tiles: set[Tile] = set()
        self._failed_water_retry_count_by_tile: dict[Tile, int] = {}
        self._failed_water_retry_at_by_tile: dict[Tile, float] = {}
        self._failed_water_reason_by_tile: dict[Tile, str] = {}
        self._completed_plant_tiles: set[Tile] = set()
        self._failed_plant_tiles: set[Tile] = set()
        self._ignored_plant_tiles: set[Tile] = set()
        self._temporarily_unreachable_clear_tiles: set[Tile] = set()
        self._target_phase: FarmWorkPhase | None = None
        self._batch_phase: FarmBatchPhase | None = None
        self._planned_plant_tiles: list[Tile] = []
        self._plant_task_signature: tuple[int, str, int, int, int, int, int, int] | None = None
        self._last_use_tool_at: float | None = None
        self._last_water_attempt_at: float | None = None
        self._active_tool_effect_plan: ToolEffectPlan | None = None
        self._last_debug_heartbeat_at = 0.0
        self._debug_tick_count = 0
        self._last_positioning_position: tuple[float, float] | None = None
        self._positioning_stuck_started_at: float | None = None
        self._positioning_recovery_count = 0
        self._next_action_available_at = 0.0
        self.tool_action_tracker = ToolActionTracker(
            start_grace_seconds=FARM_TOOL_START_GRACE_SECONDS,
            finish_timeout_seconds=FARM_TOOL_FINISH_TIMEOUT_SECONDS,
        )
        self.tool_aftermath_service = ToolAftermathService()
        self.farm_debug_logger = FarmDebugLogger()

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        if not blackboard.macro_plan or blackboard.current_step_index >= len(blackboard.macro_plan):
            self._reset()
            return "FAILURE"

        current_task = blackboard.macro_plan[blackboard.current_step_index]
        if not isinstance(current_task, FarmTask):
            self._reset()
            return "FAILURE"

        if current_task.task_type != "FARM":
            self._reset()
            return "FAILURE"

        game_state = context.state
        if game_state is None:
            return "RUNNING"

        self._debug_tick_count += 1
        self._log_debug_heartbeat(game_state, blackboard, current_task)

        if not self._has_logged_task:
            self._has_logged_task = True
            print(
                "\n🌱 [FarmNode] 收到农业任务: "
                f"action={current_task.farm_action}, seed={current_task.seed_name}, "
                f"count={current_task.count}, target_loc={current_task.target_loc}, "
                f"target_tiles={current_task.target_tiles}"
            )
            self._log(
                "收到农业任务: "
                f"action={current_task.farm_action}, seed={current_task.seed_name}, "
                f"count={current_task.count}, target_loc={current_task.target_loc}, "
                f"target_tiles={current_task.target_tiles}, "
                f"location={game_state.location_name}, player_tile={game_state.player_tile}, "
                f"farm_tiles_count={len(game_state.farm_tiles)}"
            )
            self._log_farm_tiles_snapshot(game_state)

        if game_state.location_name != current_task.target_loc:
            self._fail(
                context,
                blackboard,
                f"当前场景不是农业任务目标场景: current={game_state.location_name}, target={current_task.target_loc}",
            )
            return "SUCCESS"

        if self._is_waiting_for_external_player_action(game_state):
            return "RUNNING"

        if current_task.farm_action in ("PLANT", "PLANT_AND_WATER"):
            return self._run_plant_task(blackboard, context, game_state, current_task)

        if self._is_waiting_after_farm_action(game_state):
            return "RUNNING"

        if self._target_tile is not None:
            farm_tile_state = game_state.farm_tiles_by_tile.get(self._target_tile)
            if farm_tile_state is not None and farm_tile_state.is_watered:
                print(f"\n💧 [FarmNode] 目标地块已浇水: {self._target_tile}")
                self._log(f"目标地块已浇水，跳过: {self._format_farm_tile_state(farm_tile_state)}")
                self._last_water_attempt_at = None
                self._mark_watered(self._target_tile, current_task)
                if self._has_reached_water_count(current_task):
                    print(f"\n🟢 [FarmNode] 已完成本次浇水数量: {len(self._watered_tiles)}/{current_task.count}")
                    self._finish(context, blackboard)
                    return "SUCCESS"
                self._pause_after_water_success(context, game_state, self._target_tile)
                self._reset_target()
                return "RUNNING"

        target_tile = self._select_next_water_target(game_state, current_task)
        if target_tile is None:
            if self._has_pending_water_retry(game_state, current_task):
                return "RUNNING"
            self._finish_water_task(game_state, blackboard, current_task)
            self._finish(context, blackboard)
            return "SUCCESS"

        if self._target_tile != target_tile:
            self._start(target_tile)

        if self._target_tile is None:
            return "RUNNING"

        farm_tile_state = game_state.farm_tiles_by_tile.get(self._target_tile)

        if game_state.is_tile_inside_current_scan(self._target_tile):
            if farm_tile_state is None:
                return self._skip_or_fail_current_target(
                    context,
                    blackboard,
                    current_task,
                    f"目标地块不是可浇水耕地: target={self._target_tile}",
                )
            if not farm_tile_state.has_crop:
                return self._skip_or_fail_current_target(
                    context,
                    blackboard,
                    current_task,
                    f"目标地块没有作物，不执行浇水: {self._format_farm_tile_state(farm_tile_state)}",
                )

        if self._started_at is not None and time.time() - self._started_at > WATER_ACTION_TIMEOUT_SECONDS:
            return self._skip_or_fail_current_target(
                context,
                blackboard,
                current_task,
                f"指定地块浇水超时: target={self._target_tile}",
            )

        positioning_result = self._tick_watering_positioning(game_state, context)
        if positioning_result.status == "FAILED":
            return self._skip_or_fail_current_target(
                context,
                blackboard,
                current_task,
                f"无法移动并面向浇水目标: target={self._target_tile}, reason={positioning_result.reason}",
            )

        if positioning_result.status in ("MOVING", "FACING"):
            if positioning_result.status == "MOVING" and self._is_positioning_stuck(game_state, positioning_result):
                return self._skip_or_fail_current_target(
                    context,
                    blackboard,
                    current_task,
                    f"站位移动疑似卡住: target={self._target_tile}, reason={positioning_result.reason}",
                )
            self._wait_ticks = 0
            return "RUNNING"

        self._reset_positioning_stuck_detection()

        self._log(
            "已处于浇水站位: "
            f"{self._format_watering_stance(game_state.player_tile, self._target_tile)}, "
            "strategy=站在作物地块上下左右相邻格浇水，不站在作物地块上"
        )

        if not is_current_tool(game_state, WATERING_CAN_TOOL_NAME):
            blackboard.required_tool = WATERING_CAN_TOOL_NAME
            blackboard.required_tool_owner = "Farm"
            blackboard.require_switch_tool = True
            blackboard.is_switching_tool = True
            print(f"\n🟡 [FarmNode] 当前工具不是 {WATERING_CAN_TOOL_NAME}，等待切换工具后再浇水。")
            self._log(
                f"等待切换水壶: target={self._target_tile}, "
                f"CurrentToolIndex={game_state.inventory.current_tool_index}, "
                f"CurrentToolbarIndex={game_state.inventory.current_toolbar_index}, "
                f"blackboard={self._format_blackboard_state(blackboard)}"
            )
            return "RUNNING"

        self._wait_ticks += 1
        if self._wait_ticks < STATE_SETTLE_TICKS:
            return "RUNNING"

        if self._is_waiting_for_tool_action_completion(context, game_state, "WATER", self._target_tile):
            return "RUNNING"

        if self._is_watering_can_empty(game_state):
            if self._should_wait_for_recent_water_result(game_state, self._target_tile):
                return "RUNNING"
            self._request_refill_watering_can(context, blackboard, game_state, self._target_tile)
            return "RUNNING"

        if self._attempt_count >= MAX_WATER_ATTEMPTS:
            return self._skip_or_fail_current_target(
                context,
                blackboard,
                current_task,
                f"浇水重试次数耗尽: target={self._target_tile}, attempts={self._attempt_count}",
            )

        self._wait_ticks = 0
        self._attempt_count += 1
        self._last_water_attempt_at = time.time()
        print(f"\n💧 [FarmNode] 使用水壶浇水: target={self._target_tile}, attempt={self._attempt_count}")
        self._log(
            f"发送 USE_TOOL 浇水: target={self._target_tile}, attempt={self._attempt_count}, "
            f"{self._format_watering_stance(game_state.player_tile, self._target_tile)}, "
            f"tool_target={format_tool_target(game_state.tool_target)}, "
            f"UsingTool={game_state.using_tool}, CanMove={game_state.can_move}, "
            f"farm_tile_state={self._format_farm_tile_state(game_state.farm_tiles_by_tile.get(self._target_tile))}, "
            f"blackboard={self._format_blackboard_state(blackboard)}"
        )
        response = self._send_command(
            context,
            StardewCommand(action=StardewAction.USE_TOOL, key=["c"]),
            game_state,
            "use_tool_water",
        )
        if response == "BUSY":
            self._attempt_count -= 1
            self._last_water_attempt_at = None
            self._log(f"C# Executor 忙碌，浇水 USE_TOOL 未执行，等待下一帧: target={self._target_tile}")
            return "RUNNING"

        self._active_tool_effect_plan = self._build_farm_effect_plan("WATER", self._target_tile)
        self.tool_action_tracker.start()
        return "RUNNING"

    def _run_plant_task(
        self,
        blackboard: AgentBlackboard,
        context: PlayerContext,
        game_state: StardewState,
        current_task: FarmTask,
    ) -> NodeStatus:
        if not current_task.seed_name:
            self._fail(context, blackboard, "种植任务缺少 seed_name，无法选择种子。")
            return "SUCCESS"

        self._ensure_batch_plan(game_state, blackboard, current_task)

        if self._is_waiting_after_farm_action(game_state):
            return "RUNNING"

        if self._batch_phase == "DONE":
            self._finish_plant_task(game_state, current_task)
            self._finish(context, blackboard)
            return "SUCCESS"

        if self._target_tile is not None and self._is_batch_target_done(game_state, current_task, self._target_tile):
            self._mark_batch_target_done(game_state, current_task, self._target_tile)
            self._reset_target()

        target_tile = self._select_next_batch_target(game_state, blackboard, current_task)
        if (
            target_tile is None
            and self._batch_phase == "WATER_TILES"
            and self._has_pending_batch_water_retry(game_state)
        ):
            return "RUNNING"

        while target_tile is None and self._advance_batch_phase(game_state, current_task):
            target_tile = self._select_next_batch_target(game_state, blackboard, current_task)
            if (
                target_tile is None
                and self._batch_phase == "WATER_TILES"
                and self._has_pending_batch_water_retry(game_state)
            ):
                return "RUNNING"

        if target_tile is None or self._batch_phase == "DONE":
            self._finish_plant_task(game_state, current_task)
            self._finish(context, blackboard)
            return "SUCCESS"

        if self._target_tile != target_tile:
            self._start_farm_target(target_tile)

        if self._batch_phase == "CLEAR_OBSTACLES":
            return self._request_batch_clear_obstacle(blackboard, context, game_state, target_tile)

        self._set_target_phase(self._get_work_phase_for_batch_phase())
        invalid_reason = self._get_invalid_farm_action_reason(game_state, target_tile, self._target_phase)
        if invalid_reason is not None:
            self._mark_plant_tile_failed(target_tile, invalid_reason)
            self._reset_target()
            return "RUNNING"

        if self._started_at is not None and time.time() - self._started_at > FARM_ACTION_TIMEOUT_SECONDS:
            self._pause_after_farm_action(
                context,
                game_state,
                target_tile,
                "farm_action_timeout",
                FARM_ACTION_FAILURE_SETTLE_SECONDS,
            )
            self._mark_batch_action_failed(
                target_tile,
                f"农业阶段超时: phase={self._target_phase}, target={self._target_tile}",
            )
            self._reset_target()
            return "RUNNING"

        positioning_result = self._tick_watering_positioning(game_state, context)
        if positioning_result.status == "FAILED":
            self._pause_after_farm_action(
                context,
                game_state,
                target_tile,
                "positioning_failed",
                FARM_POSITIONING_STUCK_SETTLE_SECONDS,
            )
            self._mark_batch_action_failed(
                target_tile,
                f"无法移动并面向农业目标: phase={self._target_phase}, reason={positioning_result.reason}",
            )
            self._reset_target()
            return "RUNNING"

        if positioning_result.status in ("MOVING", "FACING"):
            if positioning_result.status == "MOVING" and self._is_positioning_stuck(game_state, positioning_result):
                if self._soft_recover_positioning_stuck(context, game_state, target_tile, positioning_result):
                    return "RUNNING"

                self._pause_after_farm_action(
                    context,
                    game_state,
                    target_tile,
                    "positioning_stuck",
                    FARM_POSITIONING_STUCK_SETTLE_SECONDS,
                )
                self._mark_batch_action_failed(
                    target_tile,
                    f"农业站位移动确认卡住: phase={self._target_phase}, reason={positioning_result.reason}",
                )
                self._reset_target()
            return "RUNNING"

        required_tool = self._get_required_tool_for_phase(current_task, self._target_phase)
        if required_tool is None:
            self._mark_plant_tile_failed(target_tile, f"无法判断农业阶段所需工具: phase={self._target_phase}")
            self._reset_target()
            return "RUNNING"

        if not is_current_tool(game_state, required_tool):
            blackboard.required_tool = required_tool
            blackboard.required_tool_owner = "Farm"
            blackboard.require_switch_tool = True
            blackboard.is_switching_tool = True
            print(f"\n🟡 [FarmNode] 当前工具不是 {required_tool}，等待 Farm 分支切换工具。")
            self._log(
                f"等待 Farm 切换工具: phase={self._target_phase}, required_tool={required_tool}, target={self._target_tile}"
            )
            return "RUNNING"

        self._wait_ticks += 1
        if self._wait_ticks < STATE_SETTLE_TICKS:
            return "RUNNING"

        if self._target_phase == "PLANT":
            if self._is_waiting_for_plant_item_result(context, game_state, target_tile):
                return "RUNNING"
        elif self._is_waiting_for_tool_action_completion(context, game_state, self._target_phase, target_tile):
            return "RUNNING"

        if self._target_phase == "WATER" and self._is_watering_can_empty(game_state):
            if self._should_wait_for_recent_water_result(game_state, target_tile):
                return "RUNNING"
            self._request_refill_watering_can(context, blackboard, game_state, target_tile)
            return "RUNNING"

        max_attempts = self._get_max_attempts_for_phase(self._target_phase)
        if self._attempt_count >= max_attempts:
            self._pause_after_farm_action(
                context,
                game_state,
                target_tile,
                "farm_action_attempts_exhausted",
                FARM_ACTION_FAILURE_SETTLE_SECONDS,
            )
            self._mark_batch_action_failed(
                target_tile,
                f"农业动作重试次数耗尽: phase={self._target_phase}, attempts={self._attempt_count}/{max_attempts}",
            )
            self._reset_target()
            return "RUNNING"

        self._wait_ticks = 0
        self._attempt_count += 1
        if self._target_phase == "PLANT":
            self._last_use_tool_at = time.time()
        if self._target_phase == "WATER":
            self._last_water_attempt_at = time.time()
        print(
            f"\n🌱 [FarmNode] 执行农业动作: phase={self._target_phase}, "
            f"target={self._target_tile}, tool={required_tool}, attempt={self._attempt_count}"
        )
        self._log(
            f"发送农业动作命令: phase={self._target_phase}, target={self._target_tile}, "
            f"required_tool={required_tool}, attempt={self._attempt_count}, "
            f"UsingTool={game_state.using_tool}, CanMove={game_state.can_move}, "
            f"state={self._format_farm_tile_state(game_state.farm_tiles_by_tile.get(self._target_tile))}, "
            f"tool_target={format_tool_target(game_state.tool_target)}"
        )
        response = self._send_farm_action_command(context, game_state, self._target_phase)
        if response == "BUSY":
            self._attempt_count -= 1
            self._last_use_tool_at = None
            if self._target_phase == "WATER":
                self._last_water_attempt_at = None
            self._log(
                f"C# Executor 忙碌，农业动作未执行，等待下一帧: phase={self._target_phase}, "
                f"target={self._target_tile}"
            )
            return "RUNNING"

        if self._target_phase != "PLANT":
            self._active_tool_effect_plan = self._build_farm_effect_plan(self._target_phase, target_tile)
            self.tool_action_tracker.start()
        else:
            self._active_tool_effect_plan = self._build_farm_effect_plan(
                self._target_phase,
                target_tile,
                started_at=self._last_use_tool_at,
            )
        return "RUNNING"

    def _ensure_batch_plan(
        self,
        game_state: StardewState,
        blackboard: AgentBlackboard,
        current_task: FarmTask,
    ) -> None:
        task_signature = (
            blackboard.current_step_index,
            current_task.farm_action,
            current_task.area_origin.x if current_task.area_origin else -1,
            current_task.area_origin.y if current_task.area_origin else -1,
            current_task.area_width,
            current_task.area_height,
            len(current_task.target_tiles),
            current_task.count,
        )
        if self._plant_task_signature == task_signature:
            return

        self._plant_task_signature = task_signature
        self._reset_target()
        self._completed_plant_tiles = set()
        self._failed_plant_tiles = set()
        self._ignored_plant_tiles = set()
        self._temporarily_unreachable_clear_tiles = set()
        self._watered_tiles = set()
        self._planned_plant_tiles = self._build_batch_plan_tiles(game_state, current_task)
        self._batch_phase = "CLEAR_OBSTACLES"
        self._log(
            f"P1 批处理规划完成: phase={self._batch_phase}, "
            f"planned={self._format_tile_list(self._planned_plant_tiles)}, "
            f"ignored={self._format_tile_set(self._ignored_plant_tiles)}, count={current_task.count}"
        )
        print(
            f"\n🌾 [FarmNode] P1 批处理规划完成: planned={len(self._planned_plant_tiles)}, "
            f"ignored={len(self._ignored_plant_tiles)}"
        )

    def _build_batch_plan_tiles(self, game_state: StardewState, current_task: FarmTask) -> list[Tile]:
        planned_tiles: list[Tile] = []
        for target_tile in self._get_plant_candidate_tiles(current_task):
            if current_task.count > 0 and len(planned_tiles) >= current_task.count:
                break

            ignored_obstacle_type = self._get_ignored_obstacle_type(game_state, target_tile)
            if ignored_obstacle_type is not None:
                self._ignored_plant_tiles.add(target_tile)
                self._log(f"P1 批处理跳过不可清理障碍: target={target_tile}, obstacle={ignored_obstacle_type}")
                continue

            farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
            clearable_obstacle_type = self._get_clearable_obstacle_type(game_state, target_tile)
            if farm_tile_state is not None and clearable_obstacle_type is None:
                if farm_tile_state.has_crop or farm_tile_state.has_hoe_dirt or farm_tile_state.can_hoe:
                    planned_tiles.append(target_tile)
                    continue

                self._ignored_plant_tiles.add(target_tile)
                self._log(
                    f"P1 批处理跳过不可锄地块: target={target_tile}, "
                    f"state={self._format_farm_tile_state(farm_tile_state)}"
                )
                continue

            planned_tiles.append(target_tile)
        return planned_tiles

    def _select_next_batch_target(
        self,
        game_state: StardewState,
        blackboard: AgentBlackboard,
        current_task: FarmTask,
    ) -> Tile | None:
        if self._batch_phase == "CLEAR_OBSTACLES":
            return self._select_next_clear_obstacle_tile(game_state, blackboard)
        if self._batch_phase == "HOE_TILES":
            return self._select_next_hoe_tile(game_state, current_task)
        if self._batch_phase == "PLANT_SEEDS":
            return self._select_next_seed_tile(game_state, current_task)
        if self._batch_phase == "WATER_TILES":
            return self._select_next_batch_water_tile(game_state, current_task)
        return None

    def _advance_batch_phase(self, game_state: StardewState, current_task: FarmTask) -> bool:
        if self._batch_phase == "CLEAR_OBSTACLES":
            self._set_batch_phase("HOE_TILES")
            return True
        if self._batch_phase == "HOE_TILES":
            self._set_batch_phase("PLANT_SEEDS")
            return True
        if self._batch_phase == "PLANT_SEEDS":
            if current_task.farm_action == "PLANT_AND_WATER":
                self._set_batch_phase("WATER_TILES")
                return True
            self._set_batch_phase("DONE")
            return False
        if self._batch_phase == "WATER_TILES":
            self._set_batch_phase("DONE")
            return False
        return False

    def _set_batch_phase(self, batch_phase: FarmBatchPhase) -> None:
        if self._batch_phase == batch_phase:
            return
        self._batch_phase = batch_phase
        self._reset_target()
        print(f"\n🌾 [FarmNode] P1 批处理进入阶段: {batch_phase}")
        self._log(f"P1 批处理进入阶段: {batch_phase}")

    def _select_next_clear_obstacle_tile(
        self,
        game_state: StardewState,
        blackboard: AgentBlackboard,
    ) -> Tile | None:
        candidates: list[tuple[str, Tile]] = []
        unreachable_candidates: list[tuple[str, Tile]] = []
        blocked_tiles = astar_solver._get_blocked_tiles(game_state)
        reachable_tiles = self._get_reachable_tiles(game_state, blocked_tiles)
        for target_tile in self._planned_plant_tiles:
            if target_tile in self._failed_plant_tiles or target_tile in self._ignored_plant_tiles:
                continue
            obstacle_type = self._get_clearable_obstacle_type(game_state, target_tile)
            if obstacle_type is None:
                self._temporarily_unreachable_clear_tiles.discard(target_tile)
                continue
            if (target_tile.x, target_tile.y) in blackboard.failed_clear_obstacles:
                self._temporarily_unreachable_clear_tiles.add(target_tile)
                unreachable_candidates.append((obstacle_type, target_tile))
                self._log(
                    f"P1 清障候选暂缓，因为清障节点刚确认失败: target={target_tile}, obstacle={obstacle_type}"
                )
                continue
            if not self._has_reachable_clear_obstacle_stand_tile(game_state, target_tile, blocked_tiles, reachable_tiles):
                self._temporarily_unreachable_clear_tiles.add(target_tile)
                unreachable_candidates.append((obstacle_type, target_tile))
                self._log(
                    f"P1 清障候选暂不可达，先尝试其他目标: target={target_tile}, obstacle={obstacle_type}, "
                    f"player={game_state.player_tile}"
                )
                continue
            self._temporarily_unreachable_clear_tiles.discard(target_tile)
            candidates.append((obstacle_type, target_tile))

        if not candidates:
            for obstacle_type, target_tile in unreachable_candidates:
                self._mark_plant_tile_failed(target_tile, f"当前无法移动到清障站位: obstacle={obstacle_type}")
            return None

        def sort_key(item: tuple[str, Tile]) -> tuple[int, int, int]:
            obstacle_type, target_tile = item
            return (
                self._get_obstacle_tool_group_order(obstacle_type),
                0 if self._is_preferred_area_clear_target(game_state, obstacle_type, target_tile) else 1,
                self._get_tile_distance(game_state.player_tile, target_tile),
            )

        return sorted(candidates, key=sort_key)[0][1]

    def _has_reachable_clear_obstacle_stand_tile(
        self,
        game_state: StardewState,
        target_tile: Tile,
        blocked_tiles: set[Tile],
        reachable_tiles: set[Tile],
    ) -> bool:
        map_width, map_height = game_state.map_size

        for stand_tile in self._get_cardinal_neighbor_tiles(target_tile):
            if stand_tile.x < 0 or stand_tile.y < 0 or stand_tile.x >= map_width or stand_tile.y >= map_height:
                continue
            if stand_tile == game_state.player_tile:
                return True
            if stand_tile in blocked_tiles:
                continue
            if stand_tile in reachable_tiles:
                return True

        return False

    def _get_reachable_tiles(self, game_state: StardewState, blocked_tiles: set[Tile]) -> set[Tile]:
        map_width, map_height = game_state.map_size
        start_tile = game_state.player_tile
        reachable_tiles: set[Tile] = {start_tile}
        queue: deque[Tile] = deque([start_tile])
        directions = (
            (0, 1),
            (0, -1),
            (1, 0),
            (-1, 0),
            (1, 1),
            (1, -1),
            (-1, 1),
            (-1, -1),
        )

        while queue:
            current_tile = queue.popleft()
            for dx, dy in directions:
                next_tile = Tile(current_tile.x + dx, current_tile.y + dy)
                if next_tile.x < 0 or next_tile.y < 0 or next_tile.x >= map_width or next_tile.y >= map_height:
                    continue
                if next_tile in reachable_tiles or next_tile in blocked_tiles:
                    continue
                if dx != 0 and dy != 0:
                    side_tile_1 = Tile(current_tile.x + dx, current_tile.y)
                    side_tile_2 = Tile(current_tile.x, current_tile.y + dy)
                    if side_tile_1 in blocked_tiles or side_tile_2 in blocked_tiles:
                        continue
                reachable_tiles.add(next_tile)
                queue.append(next_tile)

        return reachable_tiles

    def _select_next_hoe_tile(self, game_state: StardewState, current_task: FarmTask) -> Tile | None:
        candidates: list[Tile] = []
        for target_tile in self._planned_plant_tiles:
            if self._should_skip_batch_tile(target_tile):
                continue
            farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
            if farm_tile_state is not None and farm_tile_state.has_hoe_dirt:
                continue
            if farm_tile_state is not None and not farm_tile_state.can_hoe:
                clearable_obstacle_type = self._get_clearable_obstacle_type(game_state, target_tile)
                if clearable_obstacle_type is not None:
                    self._log(
                        f"锄地阶段发现仍需清障，留给清障阶段/后续重规划: "
                        f"target={target_tile}, obstacle={clearable_obstacle_type}, "
                        f"state={self._format_farm_tile_state(farm_tile_state)}"
                    )
                    continue

                self._ignored_plant_tiles.add(target_tile)
                self._log(
                    f"锄地阶段跳过不可锄地块: target={target_tile}, "
                    f"state={self._format_farm_tile_state(farm_tile_state)}"
                )
                continue
            candidates.append(target_tile)
        return self._select_best_farm_action_tile(game_state, candidates)

    def _select_next_seed_tile(self, game_state: StardewState, current_task: FarmTask) -> Tile | None:
        candidates: list[Tile] = []
        for target_tile in self._planned_plant_tiles:
            if self._should_skip_batch_tile(target_tile):
                continue
            farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
            if farm_tile_state is None:
                continue
            if farm_tile_state.has_crop:
                continue
            if not farm_tile_state.can_plant:
                self._ignored_plant_tiles.add(target_tile)
                self._log(
                    f"播种阶段跳过不可播种地块: target={target_tile}, "
                    f"state={self._format_farm_tile_state(farm_tile_state)}"
                )
                continue
            candidates.append(target_tile)
        return self._select_best_farm_action_tile(game_state, candidates)

    def _select_next_batch_water_tile(self, game_state: StardewState, current_task: FarmTask) -> Tile | None:
        candidates: list[Tile] = []
        for target_tile in self._planned_plant_tiles:
            if target_tile in self._ignored_plant_tiles or target_tile in self._completed_plant_tiles:
                continue
            farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
            if farm_tile_state is None or not farm_tile_state.has_crop or farm_tile_state.is_watered:
                continue
            if target_tile in self._failed_plant_tiles:
                self._failed_plant_tiles.discard(target_tile)
                self._log(
                    f"浇水阶段发现地块已有作物且未浇水，移出 P1 永久失败集合并重新纳入浇水: "
                    f"target={target_tile}, state={self._format_farm_tile_state(farm_tile_state)}"
                )
            if self._should_skip_failed_water_target(target_tile):
                continue
            candidates.append(target_tile)
        return self._select_best_farm_action_tile(game_state, candidates)

    def _select_best_farm_action_tile(self, game_state: StardewState, candidate_tiles: list[Tile]) -> Tile | None:
        if not candidate_tiles:
            return None

        candidate_set = set(candidate_tiles)
        tool_target_tile = game_state.tool_target.tile
        if tool_target_tile in candidate_set:
            self._log(f"优先选择当前工具目标地块: target={tool_target_tile}")
            return tool_target_tile

        player_neighbor_tiles = self._get_cardinal_neighbor_tiles(game_state.player_tile)
        actionable_tiles = [tile for tile in candidate_tiles if tile in player_neighbor_tiles]
        if actionable_tiles:
            selected_tile = sorted(actionable_tiles, key=lambda tile: self._get_tile_distance(tool_target_tile, tile))[
                0
            ]
            self._log(
                f"优先选择当前站位可直接操作的邻格: target={selected_tile}, "
                f"player_tile={game_state.player_tile}, tool_target={tool_target_tile}, "
                f"candidates={self._format_tile_list(actionable_tiles)}"
            )
            return selected_tile

        return self._select_nearest_tile_without_forcing_current_tile_first(game_state, candidate_tiles)

    def _select_nearest_tile(self, game_state: StardewState, candidate_tiles: list[Tile]) -> Tile | None:
        if not candidate_tiles:
            return None
        return sorted(candidate_tiles, key=lambda tile: self._get_tile_distance(game_state.player_tile, tile))[0]

    def _select_nearest_tile_without_forcing_current_tile_first(
        self,
        game_state: StardewState,
        candidate_tiles: list[Tile],
    ) -> Tile | None:
        if not candidate_tiles:
            return None

        return sorted(
            candidate_tiles,
            key=lambda tile: (
                tile == game_state.player_tile,
                self._get_tile_distance(game_state.player_tile, tile),
            ),
        )[0]

    def _request_batch_clear_obstacle(
        self,
        blackboard: AgentBlackboard,
        context: PlayerContext,
        game_state: StardewState,
        target_tile: Tile,
    ) -> NodeStatus:
        clear_obstacle_type = self._get_clearable_obstacle_type(game_state, target_tile)
        if clear_obstacle_type is None:
            self._reset_target()
            return "RUNNING"

        required_tool = select_required_tool_for_obstacle(game_state, clear_obstacle_type, target_tile, "Farm")
        if required_tool is None:
            self._mark_plant_tile_failed(target_tile, f"没有配置清障工具: obstacle={clear_obstacle_type}")
            self._reset_target()
            return "RUNNING"

        context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
        blackboard.require_clear_obstacle = True
        blackboard.clear_obstacle_owner = "Farm"
        blackboard.clear_obstacle_tile = target_tile
        blackboard.clear_obstacle_type = clear_obstacle_type
        blackboard.require_switch_tool = True
        blackboard.required_tool_owner = "Farm"
        blackboard.is_switching_tool = True
        blackboard.required_tool = required_tool
        self._log(
            f"P1 批量清障请求: target={target_tile}, obstacle={clear_obstacle_type}, "
            f"required_tool={required_tool}, "
            f"tool_area_tree1_risk={has_tool_area_tree1_risk(game_state, target_tile, required_tool)}"
        )
        print(f"\n🟡 [FarmNode] P1 批量清障: {clear_obstacle_type} @ {target_tile}")
        return "RUNNING"

    def _is_batch_target_done(self, game_state: StardewState, current_task: FarmTask, target_tile: Tile) -> bool:
        if self._batch_phase == "CLEAR_OBSTACLES":
            return self._get_clearable_obstacle_type(game_state, target_tile) is None
        if self._batch_phase == "HOE_TILES":
            farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
            return farm_tile_state is not None and farm_tile_state.has_hoe_dirt
        if self._batch_phase == "PLANT_SEEDS":
            farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
            return farm_tile_state is not None and farm_tile_state.has_crop
        if self._batch_phase == "WATER_TILES":
            farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
            return farm_tile_state is not None and farm_tile_state.has_crop and farm_tile_state.is_watered
        return False

    def _mark_batch_target_done(self, game_state: StardewState, current_task: FarmTask, target_tile: Tile) -> None:
        if self._batch_phase == "WATER_TILES" or (
            self._batch_phase == "PLANT_SEEDS" and current_task.farm_action == "PLANT"
        ):
            self._mark_plant_tile_done(target_tile, current_task)
        if self._batch_phase == "WATER_TILES":
            self._mark_watered(target_tile, current_task)
        self._log(
            f"P1 批处理目标完成: phase={self._batch_phase}, target={target_tile}, "
            f"state={self._format_farm_tile_state(game_state.farm_tiles_by_tile.get(target_tile))}"
        )

    def _should_skip_batch_tile(self, target_tile: Tile) -> bool:
        return (
            target_tile in self._failed_plant_tiles
            or target_tile in self._ignored_plant_tiles
            or target_tile in self._completed_plant_tiles
        )

    def _get_work_phase_for_batch_phase(self) -> FarmWorkPhase:
        if self._batch_phase == "HOE_TILES":
            return "HOE"
        if self._batch_phase == "PLANT_SEEDS":
            return "PLANT"
        if self._batch_phase == "WATER_TILES":
            return "WATER"
        return "DONE"

    def _get_invalid_farm_action_reason(
        self,
        game_state: StardewState,
        target_tile: Tile,
        target_phase: FarmWorkPhase | None,
    ) -> str | None:
        farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
        if farm_tile_state is None:
            return None

        if target_phase == "HOE":
            if farm_tile_state.has_hoe_dirt:
                return None
            if farm_tile_state.can_hoe:
                return None
            clearable_obstacle_type = self._get_clearable_obstacle_type(game_state, target_tile)
            if clearable_obstacle_type is not None:
                return f"锄地前仍有可清障碍，等待清障阶段处理: obstacle={clearable_obstacle_type}"
            return f"目标地块当前不可锄: {self._format_farm_tile_state(farm_tile_state)}"

        if target_phase == "PLANT":
            if farm_tile_state.can_plant:
                return None
            if farm_tile_state.has_crop:
                return None
            return f"目标地块当前不可播种: {self._format_farm_tile_state(farm_tile_state)}"

        if target_phase == "WATER":
            if farm_tile_state.has_crop:
                return None
            return f"目标地块没有作物，不能浇水: {self._format_farm_tile_state(farm_tile_state)}"

        return None

    def _send_farm_action_command(
        self,
        context: PlayerContext,
        game_state: StardewState,
        target_phase: FarmWorkPhase | None,
    ) -> str | None:
        if target_phase == "PLANT":
            return self._send_command(
                context,
                StardewCommand(action=StardewAction.USE_ITEM, key=["x"]),
                game_state,
                "use_item_farm_PLANT",
            )

        return self._send_command(
            context,
            StardewCommand(action=StardewAction.USE_TOOL, key=["c"]),
            game_state,
            f"use_tool_farm_{target_phase}",
        )

    def _get_max_attempts_for_phase(self, target_phase: FarmWorkPhase | None) -> int:
        if target_phase == "HOE":
            return 1
        if target_phase == "PLANT":
            return 1
        if target_phase == "WATER":
            return MAX_FARM_ACTION_ATTEMPTS
        return 1

    def _get_action_verify_delay_seconds(self, target_phase: FarmWorkPhase | None) -> float:
        if target_phase == "HOE":
            return HOE_TOOL_VERIFY_DELAY_SECONDS
        if target_phase == "PLANT":
            return PLANT_ITEM_VERIFY_DELAY_SECONDS
        if target_phase == "WATER":
            return WATER_TOOL_VERIFY_DELAY_SECONDS
        return WATER_TOOL_VERIFY_DELAY_SECONDS

    def _is_waiting_for_tool_action_completion(
        self,
        context: PlayerContext,
        game_state: StardewState,
        target_phase: FarmWorkPhase,
        target_tile: Tile,
    ) -> bool:
        tool_action_status = self.tool_action_tracker.tick(game_state)
        if tool_action_status in ("WAITING_STARTED", "WAITING_FINISHED"):
            self._log(
                f"等待农业工具动作收招: phase={target_phase}, target={target_tile}, "
                f"UsingTool={game_state.using_tool}, CanMove={game_state.can_move}, "
                f"tracker={self.tool_action_tracker.get_debug_snapshot()}, "
                f"state={self._format_farm_tile_state(game_state.farm_tiles_by_tile.get(target_tile))}"
            )
            return True

        if tool_action_status == "FINISHED":
            effect_result = self.tool_aftermath_service.inspect_tool_effect(
                context,
                game_state,
                self._active_tool_effect_plan or self._build_farm_effect_plan(target_phase, target_tile),
            )
            if effect_result.status == "WAITING":
                self._log(
                    f"等待农业工具预期效果刷新: phase={target_phase}, target={target_tile}, "
                    f"elapsed={effect_result.elapsed_seconds:.3f}s, reason={effect_result.reason}, "
                    f"state={self._format_farm_tile_state(game_state.farm_tiles_by_tile.get(target_tile))}"
                )
                return True

            if effect_result.status == "TIMEOUT":
                self._log(
                    f"农业工具预期效果超时，准备重试/失败判断: phase={target_phase}, target={target_tile}, "
                    f"elapsed={effect_result.elapsed_seconds:.3f}s, reason={effect_result.reason}, "
                    f"state={self._format_farm_tile_state(game_state.farm_tiles_by_tile.get(target_tile))}, "
                    f"aftermath={effect_result.aftermath.reason}"
                )
                self._active_tool_effect_plan = None
                self.tool_action_tracker.reset()
                return False

            if effect_result.status == "BLOCKED":
                self._log(
                    f"农业工具动作后发现阻塞 UI，等待 Guard 处理: phase={target_phase}, target={target_tile}, "
                    f"menu={effect_result.aftermath.blocking_menu_type}, text={effect_result.aftermath.blocking_menu_text}"
                )
                self._active_tool_effect_plan = None
                self.tool_action_tracker.reset()
                return True

            self._log(
                f"农业工具动作已收招且预期效果成立，等待下一帧统一推进: phase={target_phase}, target={target_tile}, "
                f"UsingTool={game_state.using_tool}, CanMove={game_state.can_move}, "
                f"state={self._format_farm_tile_state(game_state.farm_tiles_by_tile.get(target_tile))}, "
                f"effect={effect_result.reason}, aftermath={effect_result.aftermath.reason}"
            )
            self._active_tool_effect_plan = None
            self.tool_action_tracker.reset()
            return True

        if tool_action_status == "TIMEOUT":
            self._log(
                f"农业工具动作等待超时，准备进入重试/失败判断: phase={target_phase}, target={target_tile}, "
                f"UsingTool={game_state.using_tool}, CanMove={game_state.can_move}, "
                f"tracker={self.tool_action_tracker.get_debug_snapshot()}, "
                f"state={self._format_farm_tile_state(game_state.farm_tiles_by_tile.get(target_tile))}"
            )
            self._active_tool_effect_plan = None
            self.tool_action_tracker.reset()

        return False

    def _is_waiting_for_external_player_action(self, game_state: StardewState) -> bool:
        if not self.tool_action_tracker.is_idle():
            return False
        if not game_state.using_tool and game_state.can_move:
            return False

        self._log(
            f"等待上一轮动作释放控制权，暂不推进农业任务: "
            f"target={self._target_tile}, phase={self._target_phase}, "
            f"UsingTool={game_state.using_tool}, CanMove={game_state.can_move}, "
            f"player_tile={game_state.player_tile}, tool_target={format_tool_target(game_state.tool_target)}"
        )
        return True

    def _is_waiting_for_plant_item_result(
        self,
        context: PlayerContext,
        game_state: StardewState,
        target_tile: Tile,
    ) -> bool:
        if self._last_use_tool_at is None:
            return False

        effect_plan = self._active_tool_effect_plan or self._build_farm_effect_plan(
            "PLANT",
            target_tile,
            started_at=self._last_use_tool_at,
        )
        effect_result = self.tool_aftermath_service.inspect_tool_effect(context, game_state, effect_plan)
        if effect_result.status == "WAITING":
            self._log(
                f"等待播种预期效果刷新: target={target_tile}, "
                f"elapsed={effect_result.elapsed_seconds:.2f}s, required={effect_plan.effect_timeout_seconds:.2f}s, "
                f"attempt={self._attempt_count}/{self._get_max_attempts_for_phase('PLANT')}, "
                f"state={self._format_farm_tile_state(game_state.farm_tiles_by_tile.get(target_tile))}"
            )
            return True

        if effect_result.status == "BLOCKED":
            self._log(
                f"播种动作后发现阻塞 UI，等待 Guard 处理: target={target_tile}, "
                f"menu={effect_result.aftermath.blocking_menu_type}, text={effect_result.aftermath.blocking_menu_text}"
            )
            self._active_tool_effect_plan = None
            self._last_use_tool_at = None
            return True

        if effect_result.status == "TIMEOUT":
            self._log(
                f"播种预期效果超时，准备重试/失败判断: target={target_tile}, "
                f"elapsed={effect_result.elapsed_seconds:.2f}s, reason={effect_result.reason}, "
                f"state={self._format_farm_tile_state(game_state.farm_tiles_by_tile.get(target_tile))}, "
                f"aftermath={effect_result.aftermath.reason}"
            )
        else:
            self._log(
                f"播种预期效果成立，等待下一帧统一推进: target={target_tile}, "
                f"state={self._format_farm_tile_state(game_state.farm_tiles_by_tile.get(target_tile))}, "
                f"effect={effect_result.reason}, aftermath={effect_result.aftermath.reason}"
            )

        self._active_tool_effect_plan = None
        self._last_use_tool_at = None
        return effect_result.status == "SUCCESS"

    def _build_farm_effect_plan(
        self,
        target_phase: FarmWorkPhase | None,
        target_tile: Tile,
        started_at: float | None = None,
    ) -> ToolEffectPlan:
        action_name = self._get_tool_effect_action_for_phase(target_phase)
        return ToolEffectPlan(
            owner="Farm",
            action_name=action_name,
            target_tile=target_tile,
            effect_checker=lambda state: self._is_farm_effect_satisfied(state, target_phase, target_tile),
            effect_timeout_seconds=FARM_EFFECT_TIMEOUT_SECONDS,
            started_at=started_at or time.time(),
            metadata={
                "phase": target_phase,
            },
        )

    def _get_tool_effect_action_for_phase(self, target_phase: FarmWorkPhase | None) -> ToolEffectAction:
        if target_phase == "HOE":
            return "HOE_TILE"
        if target_phase == "PLANT":
            return "PLANT_SEED"
        if target_phase == "WATER":
            return "WATER_TILE"
        return "WATER_TILE"

    def _is_farm_effect_satisfied(
        self,
        state: StardewState,
        target_phase: FarmWorkPhase | None,
        target_tile: Tile,
    ) -> bool:
        farm_tile_state = state.farm_tiles_by_tile.get(target_tile)
        if farm_tile_state is None:
            return False

        if target_phase == "HOE":
            return farm_tile_state.has_hoe_dirt
        if target_phase == "PLANT":
            return farm_tile_state.has_crop
        if target_phase == "WATER":
            return farm_tile_state.has_crop and farm_tile_state.is_watered
        return False

    def _get_obstacle_tool_group_order(self, obstacle_type: str) -> int:
        normalized_obstacle_type = normalize_obstacle_type(obstacle_type)
        if normalized_obstacle_type == "grass":
            return 0
        if normalized_obstacle_type in ("weeds", "twig"):
            return 1
        if normalized_obstacle_type == "stone":
            return 2
        if normalized_obstacle_type == "tree":
            return 3
        return 9

    def _is_preferred_area_clear_target(
        self,
        game_state: StardewState,
        obstacle_type: str,
        target_tile: Tile,
    ) -> bool:
        normalized_obstacle_type = normalize_obstacle_type(obstacle_type)
        if normalized_obstacle_type not in ("grass", "weeds"):
            return False

        return target_tile in self._get_current_area_clear_focus_tiles(game_state)

    def _get_current_area_clear_focus_tiles(self, game_state: StardewState) -> set[Tile]:
        focus_centers = {game_state.player_tile}
        tool_target_tile = game_state.tool_target.tile
        if tool_target_tile is not None:
            focus_centers.add(tool_target_tile)

        focus_tiles: set[Tile] = set()
        for center_tile in focus_centers:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    focus_tiles.add(Tile(center_tile.x + dx, center_tile.y + dy))
        return focus_tiles

    def _select_next_plant_target(self, game_state: StardewState, current_task: FarmTask) -> Tile | None:
        if self._has_reached_plant_count(current_task):
            return None

        candidate_tiles = self._get_plant_candidate_tiles(current_task)
        for target_tile in sorted(
            candidate_tiles, key=lambda tile: self._get_tile_distance(game_state.player_tile, tile)
        ):
            if target_tile in self._completed_plant_tiles:
                continue
            if target_tile in self._failed_plant_tiles:
                continue
            if target_tile in self._ignored_plant_tiles:
                continue

            ignored_obstacle_type = self._get_ignored_obstacle_type(game_state, target_tile)
            if ignored_obstacle_type is not None:
                self._ignored_plant_tiles.add(target_tile)
                self._log(f"规划种植区域跳过不可清理障碍: target={target_tile}, obstacle={ignored_obstacle_type}")
                continue

            farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
            if farm_tile_state is not None and farm_tile_state.has_crop:
                if current_task.farm_action == "PLANT_AND_WATER" and not farm_tile_state.is_watered:
                    return target_tile
                self._completed_plant_tiles.add(target_tile)
                continue

            self._log(
                f"选择 P1 种植目标: target={target_tile}, "
                f"state={self._format_farm_tile_state(farm_tile_state)}, "
                f"obstacle={self._get_clearable_obstacle_type(game_state, target_tile)}"
            )
            return target_tile
        return None

    def _get_plant_candidate_tiles(self, current_task: FarmTask) -> list[Tile]:
        if current_task.target_tiles:
            return current_task.target_tiles

        if current_task.area_origin is not None and current_task.area_width > 0 and current_task.area_height > 0:
            return [
                Tile(current_task.area_origin.x + dx, current_task.area_origin.y + dy)
                for dy in range(current_task.area_height)
                for dx in range(current_task.area_width)
            ]

        return []

    def _get_required_plant_phase(
        self,
        game_state: StardewState,
        current_task: FarmTask,
        target_tile: Tile,
    ) -> FarmWorkPhase:
        farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
        if farm_tile_state is None:
            return "HOE"
        if not farm_tile_state.has_hoe_dirt:
            return "HOE"
        if not farm_tile_state.has_crop:
            return "PLANT"
        if current_task.farm_action == "PLANT_AND_WATER" and not farm_tile_state.is_watered:
            return "WATER"
        return "DONE"

    def _get_required_tool_for_phase(self, current_task: FarmTask, phase: FarmWorkPhase | None) -> str | None:
        if phase == "HOE":
            return HOE_TOOL_NAME
        if phase == "PLANT":
            return current_task.seed_name
        if phase == "WATER":
            return WATERING_CAN_TOOL_NAME
        return None

    def _mark_plant_tile_done(self, target_tile: Tile, current_task: FarmTask) -> None:
        if target_tile in self._completed_plant_tiles:
            return
        self._completed_plant_tiles.add(target_tile)
        self._failed_plant_tiles.discard(target_tile)
        print(f"\n🟢 [FarmNode] P1 地块完成: target={target_tile}, completed={len(self._completed_plant_tiles)}")
        self._log(
            f"P1 地块完成: target={target_tile}, completed={len(self._completed_plant_tiles)}, "
            f"count={current_task.count}"
        )

    def _mark_plant_tile_failed(self, target_tile: Tile, reason: str) -> None:
        self._failed_plant_tiles.add(target_tile)
        print(f"\n🔴 [FarmNode] P1 地块失败，跳过: target={target_tile}, reason={reason}")
        self._log(f"P1 地块失败，跳过: target={target_tile}, reason={reason}")

    def _mark_batch_action_failed(self, target_tile: Tile, reason: str) -> None:
        if self._batch_phase == "WATER_TILES" or self._target_phase == "WATER":
            self._mark_water_failed(target_tile, reason)
            print(f"\n🟡 [FarmNode] P1 浇水地块暂时失败，稍后重试: target={target_tile}, reason={reason}")
            self._log(f"P1 浇水地块进入重试队列: target={target_tile}, reason={reason}")
            return

        self._mark_plant_tile_failed(target_tile, reason)

    def _has_reached_plant_count(self, current_task: FarmTask) -> bool:
        if current_task.count <= 0:
            return False
        return len(self._completed_plant_tiles) >= current_task.count

    def _finish_plant_task(self, game_state: StardewState, current_task: FarmTask) -> None:
        self._refresh_completed_batch_tiles(game_state, current_task)
        completed_count = len(self._completed_plant_tiles)
        candidate_count = len(self._get_plant_candidate_tiles(current_task))
        self._log(
            f"P1 农业任务结束: batch_phase={self._batch_phase}, completed={completed_count}, count={current_task.count}, "
            f"candidate_count={candidate_count}, ignored={self._format_tile_set(self._ignored_plant_tiles)}, "
            f"failed={self._format_tile_set(self._failed_plant_tiles)}, "
            f"failed_water={self._format_tile_set(self._failed_water_tiles)}, "
            f"failed_water_retry={self._format_failed_water_retry_state()}, "
            f"location={game_state.location_name}"
        )
        print(
            f"\n🟢 [FarmNode] P1 农业任务结束: completed={completed_count}, "
            f"candidate={candidate_count}, ignored={len(self._ignored_plant_tiles)}, "
            f"failed={len(self._failed_plant_tiles)}, failed_water={len(self._failed_water_tiles)}"
        )

    def _refresh_completed_batch_tiles(self, game_state: StardewState, current_task: FarmTask) -> None:
        for target_tile in self._planned_plant_tiles:
            if self._should_skip_batch_tile(target_tile):
                continue
            farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
            if farm_tile_state is None or not farm_tile_state.has_crop:
                continue
            if current_task.farm_action == "PLANT_AND_WATER" and not farm_tile_state.is_watered:
                continue
            self._completed_plant_tiles.add(target_tile)

    def _start_farm_target(self, target_tile: Tile) -> None:
        self._target_tile = target_tile
        self._target_phase = None
        self._started_at = time.time()
        self._attempt_count = 0
        self._wait_ticks = 0
        self._has_faced_target = False
        self._last_use_tool_at = None
        self._last_water_attempt_at = None
        self._active_tool_effect_plan = None
        self._positioning_recovery_count = 0
        self.tool_action_tracker.reset()
        self._reset_positioning_stuck_detection()
        self.positioning_controller.reset()
        print(f"\n🟡 [FarmNode] 准备处理 P1 地块: target={target_tile}")
        self._log(f"开始处理 P1 地块: target={target_tile}")

    def _set_target_phase(self, target_phase: FarmWorkPhase) -> None:
        if self._target_phase == target_phase:
            return

        self._target_phase = target_phase
        self._started_at = time.time()
        self._attempt_count = 0
        self._wait_ticks = 0
        self._last_use_tool_at = None
        self._last_water_attempt_at = None
        self._active_tool_effect_plan = None
        self._positioning_recovery_count = 0
        self.tool_action_tracker.reset()
        self._reset_positioning_stuck_detection()
        self.positioning_controller.reset()
        self._log(f"切换 P1 地块阶段: target={self._target_tile}, phase={target_phase}")

    def _get_clearable_obstacle_type(self, game_state: StardewState, target_tile: Tile) -> str | None:
        farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
        if farm_tile_state is not None:
            decision = decide_clear_obstacle(game_state, target_tile, farm_tile_state.obstacle_type, "Farm")
            if decision.can_clear:
                return decision.obstacle_type

        obstacle_type = get_obstacle_type_at_tile(game_state, target_tile)
        decision = decide_clear_obstacle(game_state, target_tile, obstacle_type, "Farm")
        if decision.can_clear:
            return decision.obstacle_type
        return None

    def _get_ignored_obstacle_type(self, game_state: StardewState, target_tile: Tile) -> str | None:
        farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
        if farm_tile_state is not None:
            obstacle_type = farm_tile_state.obstacle_type
            if obstacle_type == "TreeStump":
                return "TreeStump"
            if normalize_obstacle_type(obstacle_type) == "fruit_tree":
                return obstacle_type
            if obstacle_type in ("Wall", "Worm", "Object", "Rug"):
                return obstacle_type

        if target_tile in game_state.layers.get("TreeStump", set()):
            return "TreeStump"
        for layer_name in FRUIT_TREE_LAYERS:
            if target_tile in game_state.layers.get(layer_name, set()):
                return layer_name
        return None

    def _format_tile_set(self, tiles: set[Tile]) -> str:
        return str(sorted(tiles, key=lambda tile: (tile.x, tile.y)))

    def _format_tile_list(self, tiles: list[Tile]) -> str:
        return str(sorted(tiles, key=lambda tile: (tile.x, tile.y)))

    def _select_next_water_target(self, game_state: StardewState, current_task: FarmTask) -> Tile | None:
        if self._has_reached_water_count(current_task):
            self._log(f"浇水数量已达成: completed={len(self._watered_tiles)}, count={current_task.count}")
            return None

        if current_task.target_tiles:
            candidate_tiles = sorted(
                current_task.target_tiles,
                key=lambda target_tile: self._get_tile_distance(game_state.player_tile, target_tile),
            )
            for target_tile in candidate_tiles:
                if target_tile in self._watered_tiles:
                    self._log(f"跳过指定目标，因为本任务已确认浇水: target={target_tile}")
                    continue
                if self._should_skip_failed_water_target(target_tile):
                    continue
                farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
                self._log(
                    f"检查指定浇水目标: target={target_tile}, state={self._format_farm_tile_state(farm_tile_state)}"
                )
                if farm_tile_state is not None and farm_tile_state.is_watered:
                    self._log(f"跳过指定目标，因为已浇水: target={target_tile}")
                    continue
                self._log(
                    f"选择指定浇水目标: target={target_tile}, "
                    f"distance={self._get_tile_distance(game_state.player_tile, target_tile)}, "
                    "stance_strategy=站在目标上下左右相邻格浇水"
                )
                return target_tile
            return None

        candidates = sorted(
            game_state.farm_tiles,
            key=lambda farm_tile_state: self._get_tile_distance(game_state.player_tile, farm_tile_state.tile),
        )
        for farm_tile_state in candidates:
            self._log(f"检查自动浇水候选: {self._format_farm_tile_state(farm_tile_state)}")
            if farm_tile_state.tile in self._watered_tiles:
                continue
            if self._should_skip_failed_water_target(farm_tile_state.tile):
                continue
            if not farm_tile_state.has_crop:
                continue
            if farm_tile_state.is_watered:
                continue
            self._log(
                "选择自动浇水目标: "
                f"{self._format_farm_tile_state(farm_tile_state)}, "
                f"distance={self._get_tile_distance(game_state.player_tile, farm_tile_state.tile)}, "
                "stance_strategy=站在目标上下左右相邻格浇水"
            )
            return farm_tile_state.tile

        return None

    def _mark_watered(self, target_tile: Tile, current_task: FarmTask) -> None:
        if target_tile in self._watered_tiles:
            return

        self._watered_tiles.add(target_tile)
        self._failed_water_tiles.discard(target_tile)
        self._failed_water_retry_count_by_tile.pop(target_tile, None)
        self._failed_water_retry_at_by_tile.pop(target_tile, None)
        self._failed_water_reason_by_tile.pop(target_tile, None)
        self._log(
            f"确认地块已浇水: target={target_tile}, completed={len(self._watered_tiles)}, count={current_task.count}"
        )

    def _mark_water_failed(self, target_tile: Tile, reason: str) -> None:
        retry_count = self._failed_water_retry_count_by_tile.get(target_tile, 0) + 1
        self._failed_water_tiles.add(target_tile)
        self._failed_water_retry_count_by_tile[target_tile] = retry_count
        self._failed_water_retry_at_by_tile[target_tile] = time.time() + FAILED_WATER_RETRY_DELAY_SECONDS
        self._failed_water_reason_by_tile[target_tile] = reason
        self._log(
            f"标记浇水失败地块，等待后续重试: target={target_tile}, "
            f"retry_count={retry_count}/{MAX_FAILED_WATER_RETRY_COUNT}, "
            f"retry_delay={FAILED_WATER_RETRY_DELAY_SECONDS:.2f}s, reason={reason}"
        )

    def _should_skip_failed_water_target(self, target_tile: Tile) -> bool:
        if target_tile not in self._failed_water_tiles:
            return False

        retry_count = self._failed_water_retry_count_by_tile.get(target_tile, 0)
        if retry_count >= MAX_FAILED_WATER_RETRY_COUNT:
            self._log(
                f"跳过浇水失败地块，因为重试次数已达上限: "
                f"target={target_tile}, retry_count={retry_count}/{MAX_FAILED_WATER_RETRY_COUNT}, "
                f"last_reason={self._failed_water_reason_by_tile.get(target_tile)}"
            )
            return True

        remaining_seconds = self._failed_water_retry_at_by_tile.get(target_tile, 0.0) - time.time()
        if remaining_seconds > 0:
            self._log(
                f"暂缓重试浇水失败地块: target={target_tile}, "
                f"retry_count={retry_count}/{MAX_FAILED_WATER_RETRY_COUNT}, "
                f"remaining={remaining_seconds:.2f}s"
            )
            return True

        self._failed_water_tiles.discard(target_tile)
        self._log(
            f"重试之前失败的浇水地块: target={target_tile}, "
            f"retry_count={retry_count}/{MAX_FAILED_WATER_RETRY_COUNT}, "
            f"last_reason={self._failed_water_reason_by_tile.get(target_tile)}"
        )
        return False

    def _has_pending_water_retry(self, game_state: StardewState, current_task: FarmTask) -> bool:
        retryable_tiles = self._get_retryable_water_tiles(game_state, current_task)
        if not retryable_tiles:
            return False

        next_retry_at = min(self._failed_water_retry_at_by_tile.get(tile, 0.0) for tile in retryable_tiles)
        remaining_seconds = max(0.0, next_retry_at - time.time())
        self._log(
            f"存在等待重试的未浇水地块，本 tick 不结束 FarmTask: "
            f"tiles={sorted(retryable_tiles, key=lambda tile: (tile.x, tile.y))}, "
            f"remaining={remaining_seconds:.2f}s"
        )
        return True

    def _has_pending_batch_water_retry(self, game_state: StardewState) -> bool:
        retryable_tiles = self._get_retryable_batch_water_tiles(game_state)
        if not retryable_tiles:
            return False

        next_retry_at = min(self._failed_water_retry_at_by_tile.get(tile, 0.0) for tile in retryable_tiles)
        remaining_seconds = max(0.0, next_retry_at - time.time())
        self._log(
            f"P1 WATER_TILES 存在等待重试的未浇水地块，本 tick 不结束批处理: "
            f"tiles={sorted(retryable_tiles, key=lambda tile: (tile.x, tile.y))}, "
            f"remaining={remaining_seconds:.2f}s"
        )
        return True

    def _get_retryable_batch_water_tiles(self, game_state: StardewState) -> set[Tile]:
        retryable_tiles: set[Tile] = set()

        for target_tile in self._planned_plant_tiles:
            if target_tile not in self._failed_water_tiles:
                continue
            if target_tile in self._ignored_plant_tiles or target_tile in self._completed_plant_tiles:
                continue
            if self._failed_water_retry_count_by_tile.get(target_tile, 0) >= MAX_FAILED_WATER_RETRY_COUNT:
                continue
            farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
            if farm_tile_state is None:
                continue
            if not farm_tile_state.has_crop or farm_tile_state.is_watered:
                continue
            retryable_tiles.add(target_tile)

        return retryable_tiles

    def _get_retryable_water_tiles(self, game_state: StardewState, current_task: FarmTask) -> set[Tile]:
        retryable_tiles: set[Tile] = set()
        candidate_tiles = (
            set(current_task.target_tiles)
            if current_task.target_tiles
            else {farm_tile_state.tile for farm_tile_state in game_state.farm_tiles}
        )

        for target_tile in candidate_tiles:
            if target_tile not in self._failed_water_tiles:
                continue
            if self._failed_water_retry_count_by_tile.get(target_tile, 0) >= MAX_FAILED_WATER_RETRY_COUNT:
                continue
            farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
            if farm_tile_state is None:
                continue
            if not farm_tile_state.has_crop or farm_tile_state.is_watered:
                continue
            retryable_tiles.add(target_tile)

        return retryable_tiles

    def _finish_water_task(
        self,
        game_state: StardewState,
        blackboard: AgentBlackboard,
        current_task: FarmTask,
    ) -> None:
        completed_count = len(self._watered_tiles)
        if self._has_reached_water_count(current_task):
            print(f"\n🟢 [FarmNode] 所有目标地块都已完成浇水: completed={completed_count}/{current_task.count}")
            self._log(f"浇水任务完成: completed={completed_count}, count={current_task.count}")
            return

        unwatered_crop_tiles = self._get_unwatered_crop_tiles(game_state, current_task)
        if current_task.count <= 0 and not unwatered_crop_tiles:
            print(f"\n🟢 [FarmNode] 所有未浇水作物都已完成浇水: completed={completed_count}")
            self._log(f"全部模式浇水任务完成: completed={completed_count}")
            return

        failed_tiles_text = ", ".join(
            str(tile) for tile in sorted(self._failed_water_tiles, key=lambda tile: (tile.x, tile.y))
        )
        target_count_text = "ALL" if current_task.count <= 0 else str(current_task.count)
        reason = (
            f"可浇水目标已耗尽，实际完成 {completed_count}/{target_count_text}，"
            f"仍未浇水地块={sorted(unwatered_crop_tiles, key=lambda tile: (tile.x, tile.y))}，"
            f"失败地块=[{failed_tiles_text}]"
        )
        print(f"\n🟡 [FarmNode] {reason}")
        self._log(f"浇水任务部分完成: {reason}")
        blackboard.prompt = f"农业任务部分完成，需要后续重新规划或人工检查农田状态：{reason}"

    def _has_reached_water_count(self, current_task: FarmTask) -> bool:
        if current_task.count <= 0:
            return False
        return len(self._watered_tiles) >= current_task.count

    def _get_tile_distance(self, start_tile: Tile, end_tile: Tile) -> int:
        return abs(start_tile.x - end_tile.x) + abs(start_tile.y - end_tile.y)

    def _get_unwatered_crop_tiles(self, game_state: StardewState, current_task: FarmTask) -> set[Tile]:
        candidate_tiles = (
            set(current_task.target_tiles)
            if current_task.target_tiles
            else {farm_tile_state.tile for farm_tile_state in game_state.farm_tiles}
        )
        unwatered_crop_tiles: set[Tile] = set()

        for target_tile in candidate_tiles:
            farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
            if farm_tile_state is None:
                continue
            if farm_tile_state.has_crop and not farm_tile_state.is_watered:
                unwatered_crop_tiles.add(target_tile)

        return unwatered_crop_tiles

    def _tick_watering_positioning(
        self,
        game_state: StardewState,
        context: PlayerContext,
    ) -> PositioningResult:
        if self._target_tile is None:
            return PositioningResult(status="FAILED", reason="缺少浇水目标")

        candidate_stand_tiles = self._get_watering_stand_tiles(game_state, self._target_tile)
        self._log(
            f"计算浇水候选站位: target={self._target_tile}, "
            f"candidate_stand_tiles={sorted(candidate_stand_tiles, key=lambda tile: (tile.x, tile.y))}"
        )
        result = self.positioning_controller.tick(
            game_state,
            PositioningGoal(
                candidate_stand_tiles=candidate_stand_tiles,
                tool_target_tile=self._target_tile,
                extra_blocked_tiles={self._target_tile},
            ),
        )

        if result.command is not None:
            self._send_command(context, result.command, game_state, f"positioning_{result.status.lower()}")

        if result.status == "MOVING":
            self._log(
                f"发送站位移动命令: command={result.command.action if result.command else None}, "
                f"key={result.command.key if result.command else None}, target={self._target_tile}, "
                f"stand_tile={result.stand_tile}, player={game_state.player_tile}, reason={result.reason}"
            )
        elif result.status == "FACING":
            self._has_faced_target = True
            print(f"\n🧭 [FarmNode] 面向目标地块: player={game_state.player_tile}, target={self._target_tile}")
            self._log(
                f"发送工具目标转向命令: command={result.command.action if result.command else None}, "
                f"key={result.command.key if result.command else None}, "
                f"{self._format_watering_stance(game_state.player_tile, self._target_tile)}, "
                f"tool_target={format_tool_target(game_state.tool_target)}"
            )
        elif result.status == "READY":
            self._log(
                f"站位控制器 READY: target={self._target_tile}, stand_tile={result.stand_tile}, "
                f"{self._format_watering_stance(game_state.player_tile, self._target_tile)}, "
                f"tool_target={format_tool_target(game_state.tool_target)}, "
                f"positioning={self.positioning_controller.get_debug_snapshot()}"
            )
        elif result.status == "FAILED":
            self._log(
                f"站位控制器 FAILED: target={self._target_tile}, reason={result.reason}, "
                f"player_tile={game_state.player_tile}, player_position={game_state.position}, "
                f"positioning={self.positioning_controller.get_debug_snapshot()}"
            )

        return result

    def _get_cardinal_neighbor_tiles(self, target_tile: Tile) -> set[Tile]:
        neighbor_tiles: set[Tile] = set()

        for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            neighbor_tiles.add(Tile(target_tile.x + dx, target_tile.y + dy))
        return neighbor_tiles

    def _get_watering_stand_tiles(self, game_state: StardewState, target_tile: Tile) -> set[Tile]:
        stand_tiles: set[Tile] = set()

        for candidate_tile in self._get_cardinal_neighbor_tiles(target_tile):
            if candidate_tile == target_tile:
                continue

            stand_tiles.add(candidate_tile)

        return stand_tiles

    def _start(self, target_tile: Tile) -> None:
        self._target_tile = target_tile
        self._started_at = time.time()
        self._attempt_count = 0
        self._wait_ticks = 0
        self._has_faced_target = False
        self._last_use_tool_at = None
        self._last_water_attempt_at = None
        self._active_tool_effect_plan = None
        self._positioning_recovery_count = 0
        self.tool_action_tracker.reset()
        self._reset_positioning_stuck_detection()
        self.positioning_controller.reset()
        print(f"\n🟡 [FarmNode] 准备给指定地块浇水: target={target_tile}")
        self._log(f"开始处理浇水目标: target={target_tile}")

    def _finish(self, context: PlayerContext, blackboard: AgentBlackboard) -> None:
        if context.state is not None:
            self._send_command(context, StardewCommand(action=StardewAction.IDLE), context.state, "finish")
        else:
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
        blackboard.current_step_index += 1
        blackboard.require_switch_tool = False
        blackboard.is_switching_tool = False
        blackboard.required_tool_owner = None
        blackboard.required_tool = None
        blackboard.require_clear_obstacle = False
        blackboard.clear_obstacle_owner = None
        blackboard.clear_obstacle_tile = None
        blackboard.clear_obstacle_type = None
        self._reset()

    def _fail(self, context: PlayerContext, blackboard: AgentBlackboard, reason: str) -> None:
        if context.state is not None:
            self._send_command(context, StardewCommand(action=StardewAction.IDLE), context.state, "fail")
        else:
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
        print(f"\n🔴 [FarmNode] {reason}")
        self._log(f"失败: {reason}")
        blackboard.prompt = f"农业任务失败，需要重新规划或人工检查农田状态：{reason}"
        blackboard.require_switch_tool = False
        blackboard.is_switching_tool = False
        blackboard.required_tool_owner = None
        blackboard.required_tool = None
        blackboard.require_clear_obstacle = False
        blackboard.clear_obstacle_owner = None
        blackboard.clear_obstacle_tile = None
        blackboard.clear_obstacle_type = None
        if blackboard.macro_plan and blackboard.current_step_index < len(blackboard.macro_plan):
            blackboard.current_step_index += 1
        self._reset()

    def _skip_or_fail_current_target(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        current_task: FarmTask,
        reason: str,
    ) -> NodeStatus:
        if self._target_tile is None:
            self._fail(context, blackboard, reason)
            return "SUCCESS"

        if current_task.target_tiles:
            self._fail(context, blackboard, reason)
            return "SUCCESS"

        self._mark_water_failed(self._target_tile, reason)
        if context.state is not None:
            self._send_command(context, StardewCommand(action=StardewAction.IDLE), context.state, "skip_failed_target")
        else:
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
        print(f"\n🔴 [FarmNode] {reason}，跳过当前自动浇水候选。")
        self._log(f"跳过自动浇水候选: target={self._target_tile}, reason={reason}")
        self._reset_target()
        return "RUNNING"

    def _reset_target(self) -> None:
        self._target_tile = None
        self._started_at = None
        self._attempt_count = 0
        self._wait_ticks = 0
        self._has_faced_target = False
        self._last_use_tool_at = None
        self._last_water_attempt_at = None
        self._positioning_recovery_count = 0
        self.tool_action_tracker.reset()
        self._reset_positioning_stuck_detection()
        self.positioning_controller.reset()

    def _reset(self) -> None:
        self._reset_target()
        self._has_logged_task = False
        self._watered_tiles = set()
        self._failed_water_tiles = set()
        self._failed_water_retry_count_by_tile = {}
        self._failed_water_retry_at_by_tile = {}
        self._failed_water_reason_by_tile = {}
        self._completed_plant_tiles = set()
        self._failed_plant_tiles = set()
        self._ignored_plant_tiles = set()
        self._temporarily_unreachable_clear_tiles = set()
        self._target_phase = None
        self._batch_phase = None
        self._planned_plant_tiles = []
        self._plant_task_signature = None
        self._last_debug_heartbeat_at = 0.0
        self._debug_tick_count = 0
        self._next_action_available_at = 0.0

    def _is_player_cardinally_next_to_tile(self, player_tile: Tile, target_tile: Tile) -> bool:
        distance_x = abs(player_tile.x - target_tile.x)
        distance_y = abs(player_tile.y - target_tile.y)
        return distance_x + distance_y == 1

    def _get_relative_direction(self, player_tile: Tile, target_tile: Tile) -> str:
        dx = target_tile.x - player_tile.x
        dy = target_tile.y - player_tile.y

        if dx == 1 and dy == 0:
            return "target_right"
        if dx == -1 and dy == 0:
            return "target_left"
        if dx == 0 and dy == 1:
            return "target_down"
        if dx == 0 and dy == -1:
            return "target_up"
        if dx == 0 and dy == 0:
            return "standing_on_target"
        return f"not_cardinal_neighbor(dx={dx},dy={dy})"

    def _format_watering_stance(self, player_tile: Tile, target_tile: Tile) -> str:
        return (
            f"player_tile={player_tile}, target_tile={target_tile}, "
            f"relative={self._get_relative_direction(player_tile, target_tile)}, "
            f"is_cardinal_neighbor={self._is_player_cardinally_next_to_tile(player_tile, target_tile)}, "
            f"is_standing_on_target={player_tile == target_tile}"
        )

    def _log(self, message: str) -> None:
        self.farm_debug_logger.log(f"[FarmNode] {message}")

    def _is_positioning_stuck(self, game_state: StardewState, positioning_result: PositioningResult) -> bool:
        command = positioning_result.command
        if command is None or not command.action.value.startswith("MOVE"):
            self._reset_positioning_stuck_detection()
            return False

        current_position = (game_state.position.x, game_state.position.y)
        now = time.time()
        if self._last_positioning_position is None:
            self._last_positioning_position = current_position
            self._positioning_stuck_started_at = now
            return False

        last_x, last_y = self._last_positioning_position
        is_position_changed = abs(current_position[0] - last_x) > 0.1 or abs(current_position[1] - last_y) > 0.1
        if is_position_changed:
            self._last_positioning_position = current_position
            self._positioning_stuck_started_at = now
            return False

        if self._positioning_stuck_started_at is None:
            self._positioning_stuck_started_at = now
            return False

        stuck_duration = now - self._positioning_stuck_started_at
        self._log(
            f"站位移动无位移: duration={stuck_duration:.2f}s, command={command.action}, key={command.key}, "
            f"position={current_position}, target={self._target_tile}, stand_tile={positioning_result.stand_tile}, "
            f"positioning={self.positioning_controller.get_debug_snapshot()}"
        )
        return stuck_duration >= POSITIONING_STUCK_TIMEOUT_SECONDS

    def _reset_positioning_stuck_detection(self) -> None:
        self._last_positioning_position = None
        self._positioning_stuck_started_at = None

    def _soft_recover_positioning_stuck(
        self,
        context: PlayerContext,
        game_state: StardewState,
        target_tile: Tile,
        positioning_result: PositioningResult,
    ) -> bool:
        if self._positioning_recovery_count >= MAX_POSITIONING_SOFT_RECOVERIES:
            self._log(
                f"站位软恢复次数耗尽，升级为确认卡住: target={target_tile}, "
                f"phase={self._target_phase}, recoveries={self._positioning_recovery_count}/"
                f"{MAX_POSITIONING_SOFT_RECOVERIES}, positioning={self.positioning_controller.get_debug_snapshot()}"
            )
            return False

        self._positioning_recovery_count += 1
        self._next_action_available_at = time.time() + POSITIONING_SOFT_RECOVER_SETTLE_SECONDS
        self._send_command(
            context,
            StardewCommand(action=StardewAction.IDLE),
            game_state,
            f"positioning_soft_recover target={target_tile}",
        )
        self.positioning_controller.reset()
        self._reset_positioning_stuck_detection()
        self._log(
            f"站位疑似卡住，执行软恢复并重新规划站位: target={target_tile}, "
            f"phase={self._target_phase}, recoveries={self._positioning_recovery_count}/"
            f"{MAX_POSITIONING_SOFT_RECOVERIES}, stand_tile={positioning_result.stand_tile}, "
            f"player_tile={game_state.player_tile}, player_position={game_state.position}, "
            f"next_wait={POSITIONING_SOFT_RECOVER_SETTLE_SECONDS:.2f}s"
        )
        return True

    def _pause_after_water_success(self, context: PlayerContext, game_state: StardewState, target_tile: Tile) -> None:
        self._pause_after_farm_action(
            context,
            game_state,
            target_tile,
            "water_success",
            WATER_SUCCESS_SETTLE_SECONDS,
        )

    def _pause_after_farm_action(
        self,
        context: PlayerContext,
        game_state: StardewState,
        target_tile: Tile,
        reason: str,
        settle_seconds: float,
    ) -> None:
        self._next_action_available_at = time.time() + settle_seconds
        self._send_command(
            context,
            StardewCommand(action=StardewAction.IDLE),
            game_state,
            f"farm_action_settle reason={reason} target={target_tile}",
        )
        self._log(f"农业动作后等待状态收尾: reason={reason}, target={target_tile}, " f"settle={settle_seconds:.2f}s")

    def _is_waiting_after_farm_action(self, game_state: StardewState) -> bool:
        remaining_seconds = self._next_action_available_at - time.time()
        if remaining_seconds <= 0:
            return False

        self._log(
            f"等待上一次农业动作收尾: remaining={remaining_seconds:.2f}s, "
            f"player_tile={game_state.player_tile}, player_position={game_state.position}, "
            f"tool_target={format_tool_target(game_state.tool_target)}"
        )
        return True

    def _send_command(
        self,
        context: PlayerContext,
        command: StardewCommand,
        game_state: StardewState,
        reason: str,
    ) -> str | None:
        self._log(
            f"发送命令: reason={reason}, action={command.action}, key={command.key}, "
            f"location={game_state.location_name}, player_tile={game_state.player_tile}, "
            f"player_position={game_state.position}, target={self._target_tile}, "
            f"tool_target={format_tool_target(game_state.tool_target)}"
        )
        response = context.executor_client.send_command(command)
        self._log(
            f"命令返回: reason={reason}, action={command.action}, response={response}, "
            f"target={self._target_tile}, player_tile={game_state.player_tile}, player_position={game_state.position}"
        )
        return response

    def _is_watering_can_empty(self, game_state: StardewState) -> bool:
        watering_can_item = self._get_watering_can_item(game_state)
        if watering_can_item is None or watering_can_item.water_left is None:
            return False

        return watering_can_item.water_left <= 0

    def _should_wait_for_recent_water_result(self, game_state: StardewState, target_tile: Tile | None) -> bool:
        if target_tile is None or self._attempt_count <= 0 or self._last_water_attempt_at is None:
            return False

        farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
        if farm_tile_state is not None and farm_tile_state.is_watered:
            return False

        elapsed_since_last_attempt = time.time() - self._last_water_attempt_at
        if elapsed_since_last_attempt > WATER_RESULT_GRACE_SECONDS:
            return False

        self._log(
            f"水壶已空但刚执行过浇水，等待目标结果刷新: target={target_tile}, "
            f"elapsed={elapsed_since_last_attempt:.2f}s, "
            f"state={self._format_farm_tile_state(farm_tile_state)}"
        )
        return True

    def _get_watering_can_item(self, game_state: StardewState) -> InventoryItem | None:
        for item in game_state.inventory.items:
            if item.name == WATERING_CAN_TOOL_NAME:
                return item
        return None

    def _request_refill_watering_can(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        target_tile: Tile | None,
    ) -> None:
        context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
        blackboard.require_refill_watering_can = True
        blackboard.refill_watering_can_owner = "Farm"
        watering_can_item = self._get_watering_can_item(game_state)
        self.tool_action_tracker.reset()
        self._active_tool_effect_plan = None
        self.positioning_controller.reset()
        print(f"\n💧 [FarmNode] 水壶没水，暂停当前浇水目标并请求补水: target={target_tile}")
        self._log(
            f"请求补水: target={target_tile}, watering_can={self._format_watering_can_item(watering_can_item)}, "
            f"location={game_state.location_name}, player={game_state.player_tile}, "
            f"blackboard={self._format_blackboard_state(blackboard)}"
        )

    def _format_watering_can_item(self, watering_can_item: InventoryItem | None) -> str:
        if watering_can_item is None:
            return "None"
        return (
            f"index={watering_can_item.index}, name={watering_can_item.name}, "
            f"WaterLeft={watering_can_item.water_left}, WaterCapacity={watering_can_item.water_capacity}"
        )

    def _log_debug_heartbeat(
        self,
        game_state: StardewState,
        blackboard: AgentBlackboard,
        current_task: FarmTask,
    ) -> None:
        now = time.time()
        if now - self._last_debug_heartbeat_at < 0.25:
            return

        self._last_debug_heartbeat_at = now
        farm_tile_state = None if self._target_tile is None else game_state.farm_tiles_by_tile.get(self._target_tile)
        max_attempts = self._get_max_attempts_for_phase(self._target_phase)
        self._log(
            f"心跳: tick={self._debug_tick_count}, "
            f"task_action={current_task.farm_action}, count={current_task.count}, "
            f"batch_phase={self._batch_phase}, planned={len(self._planned_plant_tiles)}, "
            f"step={blackboard.current_step_index}/{len(blackboard.macro_plan)}, "
            f"target={self._target_tile}, watered={len(self._watered_tiles)}, "
            f"plant_completed={len(self._completed_plant_tiles)}, "
            f"plant_failed={len(self._failed_plant_tiles)}, plant_ignored={len(self._ignored_plant_tiles)}, "
            f"failed={sorted(self._failed_water_tiles, key=lambda tile: (tile.x, tile.y))}, "
            f"failed_retry={self._format_failed_water_retry_state()}, "
            f"attempt={self._attempt_count}/{max_attempts}, wait_ticks={self._wait_ticks}, "
            f"last_use_tool_at={self._last_use_tool_at}, "
            f"UsingTool={game_state.using_tool}, CanMove={game_state.can_move}, "
            f"tool_action={self.tool_action_tracker.get_debug_snapshot()}, "
            f"location={game_state.location_name}, player_tile={game_state.player_tile}, "
            f"player_position={game_state.position}, current_tool={self._get_current_tool_name(game_state)}, "
            f"tool_target={format_tool_target(game_state.tool_target)}, "
            f"target_state={self._format_farm_tile_state(farm_tile_state)}, "
            f"blackboard={self._format_blackboard_state(blackboard)}, "
            f"positioning={self.positioning_controller.get_debug_snapshot()}"
        )

    def _format_blackboard_state(self, blackboard: AgentBlackboard) -> str:
        return (
            "{"
            f"require_switch_tool={blackboard.require_switch_tool}, "
            f"is_switching_tool={blackboard.is_switching_tool}, "
            f"required_tool_owner={blackboard.required_tool_owner}, "
            f"required_tool={blackboard.required_tool}, "
            f"require_refill_watering_can={blackboard.require_refill_watering_can}, "
            f"refill_watering_can_owner={blackboard.refill_watering_can_owner}, "
            f"refill_water_source_tile={blackboard.refill_water_source_tile}, "
            f"clear_obstacle_owner={blackboard.clear_obstacle_owner}, "
            f"clear_obstacle_tile={blackboard.clear_obstacle_tile}, "
            f"clear_obstacle_type={blackboard.clear_obstacle_type}, "
            f"prompt={blackboard.prompt}"
            "}"
        )

    def _format_failed_water_retry_state(self) -> str:
        if not self._failed_water_tiles:
            return "[]"

        now = time.time()
        retry_items = []
        for tile in sorted(self._failed_water_tiles, key=lambda failed_tile: (failed_tile.x, failed_tile.y)):
            retry_count = self._failed_water_retry_count_by_tile.get(tile, 0)
            retry_at = self._failed_water_retry_at_by_tile.get(tile, 0.0)
            remaining_seconds = max(0.0, retry_at - now)
            retry_items.append(
                f"{tile}:retry={retry_count}/{MAX_FAILED_WATER_RETRY_COUNT},remaining={remaining_seconds:.2f}s"
            )
        return "[" + ", ".join(retry_items) + "]"

    def _get_current_tool_name(self, game_state: StardewState) -> str:
        for item in game_state.inventory.items:
            if item.index == game_state.inventory.current_tool_index:
                return item.name or item.display_name or item.qualified_item_id
        return ""

    def _log_farm_tiles_snapshot(self, game_state: StardewState) -> None:
        if not game_state.farm_tiles:
            self._log("FarmTiles 快照为空。")
            return

        preview = ", ".join(
            self._format_farm_tile_state(farm_tile_state) for farm_tile_state in game_state.farm_tiles[:20]
        )
        self._log(f"FarmTiles 快照预览: count={len(game_state.farm_tiles)}, preview=[{preview}]")

    def _format_farm_tile_state(self, farm_tile_state) -> str:
        if farm_tile_state is None:
            return "None"

        return (
            f"tile={farm_tile_state.tile}, "
            f"TerrainFeatureType={farm_tile_state.terrain_feature_type}, "
            f"State={farm_tile_state.state}, "
            f"IsWatered={farm_tile_state.is_watered}, "
            f"HasCrop={farm_tile_state.has_crop}, "
            f"CanHoe={farm_tile_state.can_hoe}, "
            f"CanPlant={farm_tile_state.can_plant}, "
            f"HasHoeDirt={farm_tile_state.has_hoe_dirt}, "
            f"ObstacleType={farm_tile_state.obstacle_type}, "
            f"IsDiggable={farm_tile_state.is_diggable}, "
            f"HasNoSpawn={farm_tile_state.has_no_spawn}, "
            f"IsPassable={farm_tile_state.is_passable}, "
            f"RawHasCrop={farm_tile_state.raw_has_crop}, "
            f"HasCropPayload={farm_tile_state.has_crop_payload}, "
            f"Crop={self._format_crop_state(farm_tile_state.crop)}"
        )

    def _format_crop_state(self, crop_state) -> str:
        if crop_state is None:
            return "None"

        return (
            "{"
            f"NetSeedIndex={crop_state.net_seed_index}, "
            f"IndexOfHarvest={crop_state.index_of_harvest}, "
            f"CurrentPhase={crop_state.current_phase}, "
            f"Dead={crop_state.dead}, "
            f"ForageCrop={crop_state.forage_crop}"
            "}"
        )
