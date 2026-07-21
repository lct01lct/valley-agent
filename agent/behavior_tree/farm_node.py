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
            return "FAILURE"

        if game_state.location_name != current_task.target_loc:
            self._fail(
                context,
                blackboard,
                f"当前场景不是农业任务目标场景: current={game_state.location_name}, target={current_task.target_loc}",
            )
            return "FAILURE"

        target_tile = self._select_next_water_target(game_state, current_task)
        if target_tile is None:
            print("\n🟢 [FarmNode] 所有目标地块都已完成浇水。")
            self._finish(blackboard)
            return "SUCCESS"

        if self._target_tile != target_tile:
            self._start(target_tile)

        if self._target_tile is None:
            return "RUNNING"

        farm_tile_state = game_state.farm_tiles_by_tile.get(self._target_tile)
        if farm_tile_state is not None and farm_tile_state.is_watered:
            print(f"\n💧 [FarmNode] 目标地块已浇水: {self._target_tile}")
            self._log(f"目标地块已浇水，跳过: {self._format_farm_tile_state(farm_tile_state)}")
            self._reset_target()
            return "RUNNING"

        if game_state.is_tile_inside_current_scan(self._target_tile):
            if farm_tile_state is None:
                self._fail(context, blackboard, f"目标地块不是可浇水耕地: target={self._target_tile}")
                return "FAILURE"
            if not farm_tile_state.has_crop:
                self._fail(
                    context,
                    blackboard,
                    f"目标地块没有作物，不执行浇水: {self._format_farm_tile_state(farm_tile_state)}",
                )
                return "FAILURE"

        if self._started_at is not None and time.time() - self._started_at > WATER_ACTION_TIMEOUT_SECONDS:
            self._fail(context, blackboard, f"指定地块浇水超时: target={self._target_tile}")
            return "FAILURE"

        positioning_result = self._tick_watering_positioning(game_state, context)
        if positioning_result.status == "FAILED":
            self._fail(context, blackboard, f"无法移动并面向浇水目标: target={self._target_tile}, reason={positioning_result.reason}")
            return "FAILURE"

        if positioning_result.status in ("MOVING", "FACING"):
            self._wait_ticks = 0
            return "RUNNING"

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
                f"CurrentToolbarIndex={game_state.inventory.current_toolbar_index}"
            )
            return "RUNNING"

        self._wait_ticks += 1
        if self._wait_ticks < STATE_SETTLE_TICKS:
            return "RUNNING"

        if self._attempt_count >= MAX_WATER_ATTEMPTS:
            self._fail(context, blackboard, f"浇水重试次数耗尽: target={self._target_tile}")
            return "FAILURE"

        self._wait_ticks = 0
        self._attempt_count += 1
        print(f"\n💧 [FarmNode] 使用水壶浇水: target={self._target_tile}, attempt={self._attempt_count}")
        self._log(
            f"发送 USE_TOOL 浇水: target={self._target_tile}, attempt={self._attempt_count}, "
            f"{self._format_watering_stance(game_state.player_tile, self._target_tile)}, "
            f"tool_target={format_tool_target(game_state.tool_target)}, "
            f"farm_tile_state={self._format_farm_tile_state(game_state.farm_tiles_by_tile.get(self._target_tile))}"
        )
        context.executor_client.send_command(StardewCommand(action=StardewAction.USE_TOOL, key=["c"]))
        return "RUNNING"

    def _select_next_water_target(self, game_state: StardewState, current_task: FarmTask) -> Tile | None:
        if current_task.target_tiles:
            for target_tile in current_task.target_tiles:
                farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
                self._log(f"检查指定浇水目标: target={target_tile}, state={self._format_farm_tile_state(farm_tile_state)}")
                if farm_tile_state is not None and farm_tile_state.is_watered:
                    self._log(f"跳过指定目标，因为已浇水: target={target_tile}")
                    continue
                self._log(f"选择指定浇水目标: target={target_tile}, stance_strategy=站在目标上下左右相邻格浇水")
                return target_tile
            return None

        for farm_tile_state in game_state.farm_tiles:
            self._log(f"检查自动浇水候选: {self._format_farm_tile_state(farm_tile_state)}")
            if not farm_tile_state.has_crop:
                continue
            if farm_tile_state.is_watered:
                continue
            self._log(
                "选择自动浇水目标: "
                f"{self._format_farm_tile_state(farm_tile_state)}, "
                "stance_strategy=站在目标上下左右相邻格浇水"
            )
            return farm_tile_state.tile

        return None

    def _tick_watering_positioning(
        self,
        game_state: StardewState,
        context: PlayerContext,
    ) -> PositioningResult:
        if self._target_tile is None:
            return PositioningResult(status="FAILED", reason="缺少浇水目标")

        candidate_stand_tiles = self._get_cardinal_neighbor_tiles(self._target_tile)
        result = self.positioning_controller.tick(
            game_state,
            PositioningGoal(candidate_stand_tiles=candidate_stand_tiles, tool_target_tile=self._target_tile),
        )

        if result.command is not None:
            context.executor_client.send_command(result.command)

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

        return result

    def _get_cardinal_neighbor_tiles(self, target_tile: Tile) -> set[Tile]:
        neighbor_tiles: set[Tile] = set()

        for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            neighbor_tiles.add(Tile(target_tile.x + dx, target_tile.y + dy))
        return neighbor_tiles

    def _start(self, target_tile: Tile) -> None:
        self._target_tile = target_tile
        self._started_at = time.time()
        self._attempt_count = 0
        self._wait_ticks = 0
        self._has_faced_target = False
        self.positioning_controller.reset()
        print(f"\n🟡 [FarmNode] 准备给指定地块浇水: target={target_tile}")
        self._log(f"开始处理浇水目标: target={target_tile}")

    def _finish(self, blackboard: AgentBlackboard) -> None:
        blackboard.current_step_index += 1
        blackboard.require_switch_tool = False
        blackboard.is_switching_tool = False
        blackboard.required_tool = None
        self._reset()

    def _fail(self, context: PlayerContext, blackboard: AgentBlackboard, reason: str) -> None:
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

    def _reset_target(self) -> None:
        self._target_tile = None
        self._started_at = None
        self._attempt_count = 0
        self._wait_ticks = 0
        self._has_faced_target = False
        self.positioning_controller.reset()

    def _reset(self) -> None:
        self._reset_target()
        self._has_logged_task = False

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
