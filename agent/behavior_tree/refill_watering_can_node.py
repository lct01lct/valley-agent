import json
import time

from agent.action.location.location import Location
from agent.action.tool.tool_aftermath_service import ToolAftermathService, ToolEffectPlan
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.action.valley_action.positioning_controller import PositioningController, PositioningGoal, PositioningResult
from agent.action.valley_action.tool_targeting import format_tool_target
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.farm_debug_logger import FarmDebugLogger
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.tool_action_tracker import ToolActionTracker
from agent.behavior_tree.tool_selection import is_current_tool
from agent.memory.map_knowledge_cache import MapFact
from server.valley_server import InventoryItem, StardewState
from server.type import Tile


WATERING_CAN_TOOL_NAME = "Watering Can"
REFILL_ACTION_TIMEOUT_SECONDS = 12.0
MAX_REFILL_ATTEMPTS = 3
TOOL_START_GRACE_SECONDS = 0.35
TOOL_FINISH_TIMEOUT_SECONDS = 3.0
REFILL_EFFECT_TIMEOUT_SECONDS = 1.0


class RefillWateringCanNode(BTNode):
    """
    Farm 分支的水壶补水节点。

    该节点只处理 FarmNode 通过 blackboard 暴露的补水需求：
    先读取运行期地图知识缓存中的水源；若没有缓存，则低频查询 C# Executor 并写入缓存。
    """

    def __init__(self, owner: str = "Farm") -> None:
        self.owner = owner
        self.positioning_controller = PositioningController()
        self.tool_action_tracker = ToolActionTracker(
            start_grace_seconds=TOOL_START_GRACE_SECONDS,
            finish_timeout_seconds=TOOL_FINISH_TIMEOUT_SECONDS,
        )
        self.farm_debug_logger = FarmDebugLogger()
        self._started_at: float | None = None
        self._attempt_count = 0
        self._target_water_source_tile: Tile | None = None
        self._failed_water_source_tiles: set[Tile] = set()
        self._has_queried_water_sources = False
        self._active_tool_effect_plan: ToolEffectPlan | None = None
        self.tool_aftermath_service = ToolAftermathService()

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        if not blackboard.require_refill_watering_can:
            self._reset()
            return "SUCCESS"

        if blackboard.refill_watering_can_owner is not None and blackboard.refill_watering_can_owner != self.owner:
            self._reset()
            return "SUCCESS"

        game_state = context.state
        if game_state is None:
            return "RUNNING"

        if self._started_at is None:
            self._start()

        watering_can_item = self._get_watering_can_item(game_state)
        if watering_can_item is None:
            return self._fail(context, blackboard, "背包中没有找到水壶，无法补水")

        if watering_can_item.water_left is None:
            return self._fail(context, blackboard, "水壶缺少 WaterLeft state，无法判断是否需要补水")

        if watering_can_item.water_left > 0:
            print(f"\n💧 [RefillWateringCanNode] 水壶已有水: WaterLeft={watering_can_item.water_left}")
            self._log(f"水壶已有水，补水完成: {self._format_watering_can(watering_can_item)}")
            self._finish(blackboard)
            return "SUCCESS"

        if self._started_at is not None and time.time() - self._started_at > REFILL_ACTION_TIMEOUT_SECONDS:
            return self._fail(context, blackboard, "水壶补水超时")

        if not is_current_tool(game_state, WATERING_CAN_TOOL_NAME):
            blackboard.required_tool = WATERING_CAN_TOOL_NAME
            blackboard.required_tool_owner = self.owner
            blackboard.require_switch_tool = True
            blackboard.is_switching_tool = True
            print(f"\n🟡 [RefillWateringCanNode] 当前工具不是 {WATERING_CAN_TOOL_NAME}，等待切换后补水。")
            self._log(f"等待切换水壶: {self._format_watering_can(watering_can_item)}")
            return "RUNNING"

        if self._is_waiting_for_refill_action(context, game_state, watering_can_item):
            return "RUNNING"

        if not self._ensure_water_sources_cached(context, game_state):
            return self._fail(context, blackboard, f"无法查询到可用水源: location={game_state.location_name}")

        target_water_source_tile = self._select_water_source_tile(context, game_state)
        if target_water_source_tile is None:
            return self._fail(context, blackboard, f"无法找到可达水源: location={game_state.location_name}")

        if self._target_water_source_tile != target_water_source_tile:
            self._target_water_source_tile = target_water_source_tile
            self.positioning_controller.reset()
            self._log(f"选择补水水源: target={target_water_source_tile}")

        positioning_result = self._tick_refill_positioning(game_state, context, target_water_source_tile)
        if positioning_result.status == "FAILED":
            self._failed_water_source_tiles.add(target_water_source_tile)
            self._target_water_source_tile = None
            self.positioning_controller.reset()
            self._log(f"补水站位失败，尝试下一个水源: target={target_water_source_tile}, reason={positioning_result.reason}")
            return "RUNNING"

        if positioning_result.status in ("MOVING", "FACING"):
            return "RUNNING"

        if self._attempt_count >= MAX_REFILL_ATTEMPTS:
            return self._fail(context, blackboard, f"补水重试次数耗尽: target={target_water_source_tile}")

        self._attempt_count += 1
        print(f"\n💧 [RefillWateringCanNode] 使用水壶接水: target={target_water_source_tile}, attempt={self._attempt_count}")
        self._log(
            f"发送 USE_TOOL 补水: target={target_water_source_tile}, attempt={self._attempt_count}, "
            f"tool_target={format_tool_target(game_state.tool_target)}, {self._format_watering_can(watering_can_item)}"
        )
        response = context.executor_client.send_command(StardewCommand(action=StardewAction.USE_TOOL, key=["c"]))
        self._log(f"补水 USE_TOOL 返回: response={response}, target={target_water_source_tile}")
        if response == "BUSY":
            self._attempt_count -= 1
            return "RUNNING"

        self._active_tool_effect_plan = self._build_refill_effect_plan(target_water_source_tile)
        self.tool_action_tracker.start()
        return "RUNNING"

    def _start(self) -> None:
        self._started_at = time.time()
        self._attempt_count = 0
        self._target_water_source_tile = None
        self._failed_water_source_tiles = set()
        self._has_queried_water_sources = False
        self._active_tool_effect_plan = None
        self.positioning_controller.reset()
        self.tool_action_tracker.reset()
        print("\n💧 [RefillWateringCanNode] 水壶没水，准备前往水源补水。")
        self._log("开始补水流程")

    def _ensure_water_sources_cached(self, context: PlayerContext, game_state: StardewState) -> bool:
        cached_water_sources = context.map_knowledge_cache.get_water_sources(game_state.location_name)
        if cached_water_sources:
            return True

        if self._has_queried_water_sources:
            return False

        self._has_queried_water_sources = True
        response = context.executor_client.send_command(
            StardewCommand(action=StardewAction.QUERY_WATER_SOURCES, location_name=game_state.location_name)
        )
        self._log(f"查询水源返回: response={response}")
        facts = self._parse_water_source_response(game_state.location_name, response)
        if not facts:
            return False

        context.map_knowledge_cache.remember_query_result(game_state.location_name, "WATER_SOURCE", facts)
        print(f"\n💧 [RefillWateringCanNode] 已缓存水源数量: {len(facts)}")
        return True

    def _parse_water_source_response(self, location_name: Location, response: str | None) -> list[MapFact]:
        if not response:
            return []

        try:
            response_obj = json.loads(response)
        except json.JSONDecodeError:
            return []

        if response_obj.get("status") != "SUCCESS":
            return []

        facts: list[MapFact] = []
        for raw_water_source in response_obj.get("water_sources", []):
            raw_tile = raw_water_source.get("Tile")
            if not isinstance(raw_tile, list) or len(raw_tile) < 2:
                continue
            facts.append(
                MapFact.create(
                    location_name=location_name,
                    tile=Tile(int(raw_tile[0]), int(raw_tile[1])),
                    fact_type="WATER_SOURCE",
                    name=raw_water_source.get("Source", "Map Water"),
                    source="QUERY_WATER_SOURCES",
                    status="VERIFIED",
                    confidence=1.0,
                )
            )
        return facts

    def _select_water_source_tile(self, context: PlayerContext, game_state: StardewState) -> Tile | None:
        water_sources = context.map_knowledge_cache.get_water_sources(game_state.location_name)
        candidates = [tile for tile in water_sources if tile not in self._failed_water_source_tiles]
        if not candidates:
            return None

        return sorted(
            candidates,
            key=lambda tile: self._get_tile_distance(game_state.player_tile, tile),
        )[0]

    def _tick_refill_positioning(
        self,
        game_state: StardewState,
        context: PlayerContext,
        water_source_tile: Tile,
    ) -> PositioningResult:
        candidate_stand_tiles = self._get_cardinal_neighbor_tiles(water_source_tile)
        result = self.positioning_controller.tick(
            game_state,
            PositioningGoal(
                candidate_stand_tiles=candidate_stand_tiles,
                tool_target_tile=water_source_tile,
                extra_blocked_tiles={water_source_tile},
            ),
        )

        if result.command is not None:
            response = context.executor_client.send_command(result.command)
            self._log(
                f"补水站位命令: status={result.status}, action={result.command.action}, key={result.command.key}, "
                f"response={response}, player={game_state.player_tile}, water_source={water_source_tile}, "
                f"tool_target={format_tool_target(game_state.tool_target)}"
            )

        return result

    def _is_waiting_for_refill_action(
        self,
        context: PlayerContext,
        game_state: StardewState,
        watering_can_item: InventoryItem,
    ) -> bool:
        if self.tool_action_tracker.is_idle():
            return False

        status = self.tool_action_tracker.tick(game_state)
        self._log(
            f"等待补水动作完成: status={status}, tracker={self.tool_action_tracker.get_debug_snapshot()}, "
            f"{self._format_watering_can(watering_can_item)}"
        )

        if status in ("WAITING_STARTED", "WAITING_FINISHED"):
            return True

        if status == "TIMEOUT":
            self._active_tool_effect_plan = None
            self.tool_action_tracker.reset()
            self._log("补水动作等待超时，允许下一帧重试")
            return False

        effect_result = self.tool_aftermath_service.inspect_tool_effect(
            context,
            game_state,
            self._active_tool_effect_plan or self._build_refill_effect_plan(self._target_water_source_tile),
        )
        if effect_result.status == "WAITING":
            self._log(
                f"等待补水预期效果刷新: target={self._target_water_source_tile}, "
                f"elapsed={effect_result.elapsed_seconds:.3f}s, reason={effect_result.reason}, "
                f"{self._format_watering_can(watering_can_item)}"
            )
            return True

        self._active_tool_effect_plan = None
        self.tool_action_tracker.reset()
        if effect_result.status == "TIMEOUT":
            self._log(
                f"补水预期效果超时，允许下一帧重试: target={self._target_water_source_tile}, "
                f"reason={effect_result.reason}, aftermath={effect_result.aftermath.reason}, "
                f"{self._format_watering_can(watering_can_item)}"
            )
            return False

        if effect_result.status == "BLOCKED":
            self._log(
                f"补水动作后发现阻塞 UI，等待 Guard 处理: target={self._target_water_source_tile}, "
                f"menu={effect_result.aftermath.blocking_menu_type}, text={effect_result.aftermath.blocking_menu_text}"
            )
            return True

        self._log(
            f"补水预期效果成立，等待下一帧统一完成: target={self._target_water_source_tile}, "
            f"effect={effect_result.reason}, aftermath={effect_result.aftermath.reason}, "
            f"{self._format_watering_can(watering_can_item)}"
        )
        return True

    def _finish(self, blackboard: AgentBlackboard) -> None:
        blackboard.require_refill_watering_can = False
        blackboard.refill_watering_can_owner = None
        blackboard.refill_water_source_tile = None
        self._reset()

    def _fail(self, context: PlayerContext, blackboard: AgentBlackboard, reason: str) -> NodeStatus:
        context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
        print(f"\n🔴 [RefillWateringCanNode] {reason}")
        self._log(f"补水失败: reason={reason}")
        blackboard.prompt = f"RefillWateringCanNode 补水失败: {reason}"
        blackboard.require_refill_watering_can = False
        blackboard.refill_watering_can_owner = None
        blackboard.refill_water_source_tile = None
        self._reset()
        return "FAILURE"

    def _reset(self) -> None:
        self._started_at = None
        self._attempt_count = 0
        self._target_water_source_tile = None
        self._failed_water_source_tiles = set()
        self._has_queried_water_sources = False
        self._active_tool_effect_plan = None
        self.positioning_controller.reset()
        self.tool_action_tracker.reset()

    def _build_refill_effect_plan(self, target_water_source_tile: Tile | None) -> ToolEffectPlan:
        return ToolEffectPlan(
            owner="Farm",
            action_name="WATER_TILE",
            target_tile=target_water_source_tile,
            effect_checker=lambda state: self._is_watering_can_refilled(state),
            effect_timeout_seconds=REFILL_EFFECT_TIMEOUT_SECONDS,
            metadata={
                "phase": "REFILL_WATERING_CAN",
            },
        )

    def _is_watering_can_refilled(self, state: StardewState) -> bool:
        watering_can_item = self._get_watering_can_item(state)
        return watering_can_item is not None and watering_can_item.water_left is not None and watering_can_item.water_left > 0

    def _get_watering_can_item(self, game_state: StardewState) -> InventoryItem | None:
        for item in game_state.inventory.items:
            if item.name == WATERING_CAN_TOOL_NAME:
                return item
        return None

    def _get_cardinal_neighbor_tiles(self, tile: Tile) -> set[Tile]:
        return {
            Tile(tile.x + 1, tile.y),
            Tile(tile.x - 1, tile.y),
            Tile(tile.x, tile.y + 1),
            Tile(tile.x, tile.y - 1),
        }

    def _get_tile_distance(self, start_tile: Tile, end_tile: Tile) -> int:
        return abs(start_tile.x - end_tile.x) + abs(start_tile.y - end_tile.y)

    def _format_watering_can(self, watering_can_item: InventoryItem) -> str:
        return (
            f"WaterLeft={watering_can_item.water_left}, WaterCapacity={watering_can_item.water_capacity}, "
            f"index={watering_can_item.index}, name={watering_can_item.name}"
        )

    def _log(self, message: str) -> None:
        self.farm_debug_logger.log(f"[RefillWateringCanNode] {message}")
