import time
from typing import Literal

from agent.action.location.location import Location
from agent.action.valley_action.positioning_controller import PositioningController, PositioningGoal, PositioningResult
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.action.valley_action.tool_targeting import format_tool_target
from agent.base_task import BaseTask, TaskType
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.farm_debug_logger import FarmDebugLogger
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.tool_selection import is_current_tool
from server.valley_server import StardewState
from server.type import Tile


type FarmAction = Literal["PLANT", "WATER", "PLANT_AND_WATER"]

WATERING_CAN_TOOL_NAME = "Watering Can"
WATER_ACTION_TIMEOUT_SECONDS = 12.0
MAX_WATER_ATTEMPTS = 3
STATE_SETTLE_TICKS = 8
WATER_TOOL_VERIFY_DELAY_SECONDS = 0.75
WATER_SUCCESS_SETTLE_SECONDS = 0.9
POSITIONING_STUCK_TIMEOUT_SECONDS = 0.45
FAILED_WATER_RETRY_DELAY_SECONDS = 1.0
MAX_FAILED_WATER_RETRY_COUNT = 3


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
    ):
        super().__init__(task_type=task_type, desc=desc)
        self.farm_action = farm_action
        self.target_loc = target_loc
        self.seed_name = seed_name
        self.count = count
        self.target_tiles = target_tiles or []


class FarmNode(BTNode):
    """
    FARM 任务的确定性执行入口。

    当前先打通“指定地块浇水”闭环：
    找到目标地块 -> 移动到上下左右相邻格 -> 切换水壶 -> 面向地块 -> 使用工具 -> 验证 IsWatered。
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
        self._last_use_tool_at: float | None = None
        self._last_debug_heartbeat_at = 0.0
        self._debug_tick_count = 0
        self._last_positioning_position: tuple[float, float] | None = None
        self._positioning_stuck_started_at: float | None = None
        self._next_action_available_at = 0.0
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

        if current_task.farm_action in ("PLANT", "PLANT_AND_WATER"):
            self._fail(context, blackboard, f"当前 FarmNode 尚未实现 {current_task.farm_action}，只支持 farm_action='WATER'。")
            return "SUCCESS"

        if game_state.location_name != current_task.target_loc:
            self._fail(
                context,
                blackboard,
                f"当前场景不是农业任务目标场景: current={game_state.location_name}, target={current_task.target_loc}",
            )
            return "SUCCESS"

        if self._is_waiting_after_water_success(game_state):
            return "RUNNING"

        if self._target_tile is not None:
            farm_tile_state = game_state.farm_tiles_by_tile.get(self._target_tile)
            if farm_tile_state is not None and farm_tile_state.is_watered:
                print(f"\n💧 [FarmNode] 目标地块已浇水: {self._target_tile}")
                self._log(f"目标地块已浇水，跳过: {self._format_farm_tile_state(farm_tile_state)}")
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

        if self._last_use_tool_at is not None:
            elapsed_after_use_tool = time.time() - self._last_use_tool_at
            if elapsed_after_use_tool < WATER_TOOL_VERIFY_DELAY_SECONDS:
                self._log(
                    f"等待浇水动作后的状态验证窗口: target={self._target_tile}, "
                    f"elapsed={elapsed_after_use_tool:.2f}s, required={WATER_TOOL_VERIFY_DELAY_SECONDS:.2f}s, "
                    f"attempt={self._attempt_count}/{MAX_WATER_ATTEMPTS}, "
                    f"farm_tile_state={self._format_farm_tile_state(game_state.farm_tiles_by_tile.get(self._target_tile))}"
                )
                return "RUNNING"
            self._last_use_tool_at = None

        if self._attempt_count >= MAX_WATER_ATTEMPTS:
            return self._skip_or_fail_current_target(
                context,
                blackboard,
                current_task,
                f"浇水重试次数耗尽: target={self._target_tile}, attempts={self._attempt_count}",
            )

        self._wait_ticks = 0
        self._attempt_count += 1
        self._last_use_tool_at = time.time()
        print(f"\n💧 [FarmNode] 使用水壶浇水: target={self._target_tile}, attempt={self._attempt_count}")
        self._log(
            f"发送 USE_TOOL 浇水: target={self._target_tile}, attempt={self._attempt_count}, "
            f"{self._format_watering_stance(game_state.player_tile, self._target_tile)}, "
            f"tool_target={format_tool_target(game_state.tool_target)}, "
            f"farm_tile_state={self._format_farm_tile_state(game_state.farm_tiles_by_tile.get(self._target_tile))}, "
            f"blackboard={self._format_blackboard_state(blackboard)}"
        )
        self._send_command(
            context,
            StardewCommand(action=StardewAction.USE_TOOL, key=["c"]),
            game_state,
            "use_tool_water",
        )
        return "RUNNING"

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
                self._log(f"检查指定浇水目标: target={target_tile}, state={self._format_farm_tile_state(farm_tile_state)}")
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
        self._log(f"确认地块已浇水: target={target_tile}, completed={len(self._watered_tiles)}, count={current_task.count}")

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
        blackboard.required_tool = None
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
        blackboard.required_tool = None
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

    def _pause_after_water_success(self, context: PlayerContext, game_state: StardewState, target_tile: Tile) -> None:
        self._next_action_available_at = time.time() + WATER_SUCCESS_SETTLE_SECONDS
        self._send_command(
            context,
            StardewCommand(action=StardewAction.IDLE),
            game_state,
            f"water_success_settle target={target_tile}",
        )
        self._log(
            f"浇水成功后等待工具动画结束: target={target_tile}, "
            f"settle={WATER_SUCCESS_SETTLE_SECONDS:.2f}s"
        )

    def _is_waiting_after_water_success(self, game_state: StardewState) -> bool:
        remaining_seconds = self._next_action_available_at - time.time()
        if remaining_seconds <= 0:
            return False

        self._log(
            f"等待上一次浇水动作收尾: remaining={remaining_seconds:.2f}s, "
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
    ) -> None:
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
        self._log(
            f"心跳: tick={self._debug_tick_count}, "
            f"task_action={current_task.farm_action}, count={current_task.count}, "
            f"step={blackboard.current_step_index}/{len(blackboard.macro_plan)}, "
            f"target={self._target_tile}, completed={len(self._watered_tiles)}, "
            f"failed={sorted(self._failed_water_tiles, key=lambda tile: (tile.x, tile.y))}, "
            f"failed_retry={self._format_failed_water_retry_state()}, "
            f"attempt={self._attempt_count}/{MAX_WATER_ATTEMPTS}, wait_ticks={self._wait_ticks}, "
            f"last_use_tool_at={self._last_use_tool_at}, "
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
            f"required_tool={blackboard.required_tool}, "
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

        preview = ", ".join(self._format_farm_tile_state(farm_tile_state) for farm_tile_state in game_state.farm_tiles[:20])
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
