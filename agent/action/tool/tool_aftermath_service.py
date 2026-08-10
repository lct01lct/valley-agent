import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, ClassVar, Literal, Protocol

from agent.action.valley_action.action_type import StardewAction, StardewCommand
from server.valley_server import IGNORED_DEBRIS_QUALIFIED_ITEM_IDS, StardewState
from server.type import Tile


type ToolAftermathOwner = Literal[
    "Route",  # 寻路清障等移动过程中触发的工具动作
    "Farm",  # 农业锄地、浇水、清障等工具动作
    "Mining",  # 采矿挥镐、破石、破矿等工具动作
]


type ToolTargetChangeState = Literal[
    "CHANGED",  # 目标地块已经发生业务关心的变化，例如石头/障碍消失
    "UNCHANGED",  # 目标地块仍保持原状态，业务节点通常需要重试或继续等待
    "UNKNOWN",  # 无法可靠判断，业务节点需要按自身策略兜底
]


type ToolEffectAction = Literal[
    "CLEAR_OBSTACLE",  # 清理单个可破坏障碍物，预期目标地块障碍消失
    "AREA_CLEAR",  # 范围工具清理 Grass/Weeds，预期目标或范围内障碍减少
    "BREAK_STONE",  # 挖矿/破石，预期石头消失、耐久降低或刷新梯子
    "HOE_TILE",  # 锄地，预期目标地块变为 HoeDirt
    "WATER_TILE",  # 浇水，预期作物或耕地进入已浇水状态
    "PLANT_SEED",  # 播种，预期目标地块出现作物
    "BREAK_CONTAINER",  # 破坏矿井木桶/木箱等容器，预期容器消失并可能产生掉落物。
]


type ToolEffectStatus = Literal[
    "SUCCESS",  # 预期效果已经被最新 state 证明
    "WAITING",  # 工具已收招，但 state 还没有刷新出预期效果
    "TIMEOUT",  # 超过保护窗口仍没有观察到预期效果
    "BLOCKED",  # 阻塞 UI 或菜单打断了后处理流程
    "UNKNOWN",  # 当前计划没有可靠验证器，只能交给业务节点兜底
]


class ToolAftermathContext(Protocol):
    executor_client: Any


@dataclass(frozen=True)
class ToolAftermathRequest:
    owner: ToolAftermathOwner
    action_name: str
    target_tile: Tile | None = None
    check_blocking_menu: bool = True
    check_ladder_at_target_tile: bool = False
    target_tile_changed: bool | None = None


@dataclass(frozen=True)
class ToolAftermathResult:
    has_blocking_menu: bool = False
    blocking_menu_type: str | None = None
    blocking_menu_text: str = ""
    feedback_event_type: str | None = None
    target_change_state: ToolTargetChangeState = "UNKNOWN"
    generated_ladder_tile: Tile | None = None
    ladder_query_status: bool | None = None
    nearby_loot_tiles: list[Tile] = field(default_factory=list)
    should_wait_next_tick: bool = False
    reason: str = ""


@dataclass(frozen=True)
class ToolEffectPlan:
    """
    一次工具动作的预期效果计划。

    业务节点负责在发出工具命令时创建计划；工具动作收招后，本服务
    根据最新 state 验证预期效果，并统一观察阻塞 UI、掉落物、梯子等副作用。
    """

    owner: ToolAftermathOwner
    action_name: ToolEffectAction
    target_tile: Tile | None = None
    effect_checker: Callable[[StardewState], bool | None] | None = None
    side_effect_checker: Callable[[StardewState, ToolAftermathResult], bool | None] | None = None
    target_change_checker: Callable[[StardewState], bool | None] | None = None
    check_blocking_menu: bool = True
    check_ladder_at_target_tile: bool = False
    loot_scan_distance: int = 2
    effect_timeout_seconds: float = 1.0
    started_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolEffectResult:
    status: ToolEffectStatus
    aftermath: ToolAftermathResult
    elapsed_seconds: float
    effect_satisfied: bool | None = None
    reason: str = ""


class ToolAftermathDebugLogger:
    """
    非阻塞工具后处理诊断日志。

    行为树 tick 只把观察结果放入内存队列；后台 daemon 线程负责写文件，
    避免工具收招后的高频状态检查被磁盘 IO 卡住。
    """

    def __init__(self, log_path: str = "logs/tool_aftermath_debug.log"):
        self.log_path = log_path
        self._queue: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._clear_log_file()
        self._thread = threading.Thread(target=self._write_loop, daemon=True)
        self._thread.start()

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self._queue.put(f"[{timestamp}] [ToolAftermathService] {message}")

    def _write_loop(self) -> None:
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)

        with open(self.log_path, "a", encoding="utf-8", buffering=1) as log_file:
            while True:
                message = self._queue.get()
                log_file.write(message + "\n")

    def _clear_log_file(self) -> None:
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with open(self.log_path, "w", encoding="utf-8"):
            pass


class ToolAftermathService:
    """
    工具动作后处理的通用观察层。

    本服务只回答“工具动作收招后，世界出现了哪些可观察副作用”。
    业务节点仍负责解释这些结果，例如 Mining 决定是否立刻转向梯子，
    ClearObstacle 决定是否重试清障，未来 CollectLootNode 决定是否拾取掉落物。
    """

    _debug_logger: ClassVar[ToolAftermathDebugLogger | None] = None

    def __init__(self):
        if ToolAftermathService._debug_logger is None:
            ToolAftermathService._debug_logger = ToolAftermathDebugLogger()

    def inspect_after_tool_action(
        self,
        context: ToolAftermathContext,
        state: StardewState,
        request: ToolAftermathRequest,
        loot_scan_distance: int = 2,
    ) -> ToolAftermathResult:
        blocking_menu_type, blocking_menu_text = self._read_blocking_menu(state, request.check_blocking_menu)
        target_change_state = self._build_target_change_state(request.target_tile_changed)
        nearby_loot_tiles = self._find_nearby_loot_tiles(state, request.target_tile, max_distance=loot_scan_distance)

        generated_ladder_tile: Tile | None = None
        ladder_query_status: bool | None = None
        ladder_reason = ""
        if request.check_ladder_at_target_tile and request.target_tile is not None:
            ladder_query_status, generated_ladder_tile, ladder_reason = self._query_ladder_at_tile(
                context,
                request.target_tile,
            )

        reason_parts: list[str] = []
        if blocking_menu_type:
            reason_parts.append(f"blocking_menu={blocking_menu_type}")
        if request.check_ladder_at_target_tile:
            reason_parts.append(f"ladder_query={ladder_query_status}:{ladder_reason}")
        if nearby_loot_tiles:
            reason_parts.append(f"nearby_loot_tiles={[(tile.x, tile.y) for tile in nearby_loot_tiles]}")
        reason_parts.append(f"target_change={target_change_state}")
        self._log_inspection(
            state=state,
            request=request,
            blocking_menu_type=blocking_menu_type,
            target_change_state=target_change_state,
            generated_ladder_tile=generated_ladder_tile,
            ladder_query_status=ladder_query_status,
            nearby_loot_tiles=nearby_loot_tiles,
            loot_scan_distance=loot_scan_distance,
        )

        return ToolAftermathResult(
            has_blocking_menu=blocking_menu_type is not None,
            blocking_menu_type=blocking_menu_type,
            blocking_menu_text=blocking_menu_text,
            target_change_state=target_change_state,
            generated_ladder_tile=generated_ladder_tile,
            ladder_query_status=ladder_query_status,
            nearby_loot_tiles=nearby_loot_tiles,
            should_wait_next_tick=blocking_menu_type is not None,
            reason=", ".join(reason_parts),
        )

    def inspect_tool_effect(
        self,
        context: ToolAftermathContext,
        state: StardewState,
        plan: ToolEffectPlan,
    ) -> ToolEffectResult:
        """
        状态驱动的工具后效果检查。

        该方法只在工具动作已经收招后调用。它不会阻塞等待，也不会 sleep；
        若预期效果尚未出现在 state 中，则返回 WAITING，由行为树下一帧继续检查。
        """

        effect_satisfied = self._run_effect_checker(state, plan)
        target_tile_changed = self._run_target_change_checker(state, plan, effect_satisfied)
        aftermath_result = self.inspect_after_tool_action(
            context,
            state,
            ToolAftermathRequest(
                owner=plan.owner,
                action_name=plan.action_name,
                target_tile=plan.target_tile,
                check_blocking_menu=plan.check_blocking_menu,
                check_ladder_at_target_tile=plan.check_ladder_at_target_tile,
                target_tile_changed=target_tile_changed,
            ),
            loot_scan_distance=plan.loot_scan_distance,
        )
        elapsed_seconds = time.time() - plan.started_at
        side_effect_satisfied = self._run_side_effect_checker(state, plan, aftermath_result)

        if aftermath_result.has_blocking_menu:
            status: ToolEffectStatus = "BLOCKED"
            reason = f"blocking_menu={aftermath_result.blocking_menu_type}"
        elif effect_satisfied is True:
            status = "SUCCESS"
            reason = "effect_satisfied"
        elif side_effect_satisfied is True:
            status = "SUCCESS"
            reason = "side_effect_satisfied"
        elif effect_satisfied is None:
            status = "UNKNOWN"
            reason = "missing_or_unknown_effect_checker"
        elif elapsed_seconds >= plan.effect_timeout_seconds:
            status = "TIMEOUT"
            reason = f"effect_timeout={elapsed_seconds:.2f}s/{plan.effect_timeout_seconds:.2f}s"
        else:
            status = "WAITING"
            reason = f"waiting_effect={elapsed_seconds:.2f}s/{plan.effect_timeout_seconds:.2f}s"

        self._log_tool_effect(plan, state, status, elapsed_seconds, effect_satisfied, reason, aftermath_result)
        return ToolEffectResult(
            status=status,
            aftermath=aftermath_result,
            elapsed_seconds=elapsed_seconds,
            effect_satisfied=effect_satisfied,
            reason=reason,
        )

    def _run_effect_checker(self, state: StardewState, plan: ToolEffectPlan) -> bool | None:
        if plan.effect_checker is None:
            return None

        try:
            return plan.effect_checker(state)
        except Exception as exc:
            self._log(
                f"工具效果验证器异常: action={plan.action_name}, "
                f"target={self._format_tile(plan.target_tile)}, error={exc}"
            )
            return None

    def _run_side_effect_checker(
        self,
        state: StardewState,
        plan: ToolEffectPlan,
        aftermath_result: ToolAftermathResult,
    ) -> bool | None:
        if plan.side_effect_checker is None:
            return None

        try:
            return plan.side_effect_checker(state, aftermath_result)
        except Exception as exc:
            self._log(
                f"工具副作用验证器异常: action={plan.action_name}, "
                f"target={self._format_tile(plan.target_tile)}, error={exc}"
            )
            return None

    def _run_target_change_checker(
        self,
        state: StardewState,
        plan: ToolEffectPlan,
        effect_satisfied: bool | None,
    ) -> bool | None:
        if plan.target_change_checker is None:
            return effect_satisfied

        try:
            return plan.target_change_checker(state)
        except Exception as exc:
            self._log(
                f"工具目标变化验证器异常: action={plan.action_name}, "
                f"target={self._format_tile(plan.target_tile)}, error={exc}"
            )
            return effect_satisfied

    def _read_blocking_menu(self, state: StardewState, enabled: bool) -> tuple[str | None, str]:
        if not enabled:
            return None, ""
        menu_state = state.menu_state
        if not menu_state.is_menu_open:
            return None, ""
        if menu_state.menu_type != "DialogueBox":
            return None, ""
        return menu_state.menu_type, menu_state.text.strip()

    def _build_target_change_state(self, target_tile_changed: bool | None) -> ToolTargetChangeState:
        if target_tile_changed is None:
            return "UNKNOWN"
        if target_tile_changed:
            return "CHANGED"
        return "UNCHANGED"

    def _find_nearby_loot_tiles(self, state: StardewState, target_tile: Tile | None, max_distance: int = 2) -> list[Tile]:
        if target_tile is None:
            return []

        nearby_tiles: list[Tile] = []
        seen_tiles: set[Tile] = set()
        for debris in getattr(state, "debris", []):
            if not self._is_collectible_debris(debris):
                continue
            if self._tile_chebyshev_distance(debris.tile, target_tile) > max_distance:
                continue
            if debris.tile in seen_tiles:
                continue
            seen_tiles.add(debris.tile)
            nearby_tiles.append(debris.tile)
        return nearby_tiles

    def _is_collectible_debris(self, debris: Any) -> bool:
        qualified_item_id = str(getattr(debris, "qualified_item_id", "") or "").strip()
        return bool(
            qualified_item_id
            and getattr(debris, "name", "")
            and getattr(debris, "display_name", "")
            and qualified_item_id not in IGNORED_DEBRIS_QUALIFIED_ITEM_IDS
        )

    def _tile_distance(self, start_tile: Tile, end_tile: Tile) -> int:
        return abs(start_tile.x - end_tile.x) + abs(start_tile.y - end_tile.y)

    def _tile_chebyshev_distance(self, start_tile: Tile, end_tile: Tile) -> int:
        return max(abs(start_tile.x - end_tile.x), abs(start_tile.y - end_tile.y))

    def _log_inspection(
        self,
        state: StardewState,
        request: ToolAftermathRequest,
        blocking_menu_type: str | None,
        target_change_state: ToolTargetChangeState,
        generated_ladder_tile: Tile | None,
        ladder_query_status: bool | None,
        nearby_loot_tiles: list[Tile],
        loot_scan_distance: int,
    ) -> None:
        if ToolAftermathService._debug_logger is None:
            return

        debris_list = list(getattr(state, "debris", []))
        target_text = self._format_tile(request.target_tile)
        nearby_text = [self._format_tile(tile) for tile in nearby_loot_tiles]
        filtered_collectible_text = self._format_filtered_collectible_debris(
            debris_list,
            request.target_tile,
            loot_scan_distance,
        )
        sample_text = self._format_debris_samples(debris_list)
        ToolAftermathService._debug_logger.log(
            "观察工具动作副作用: "
            f"owner={request.owner}, action={request.action_name}, location={state.location_name}, "
            f"target={target_text}, target_change={target_change_state}, "
            f"blocking_menu={blocking_menu_type}, ladder_query={ladder_query_status}, "
            f"generated_ladder={self._format_tile(generated_ladder_tile)}, "
            f"has_debris_snapshot={getattr(state, 'has_debris_snapshot', None)}, "
            f"debris_count={len(debris_list)}, loot_scan_metric=chebyshev, "
            f"loot_scan_distance={loot_scan_distance}, nearby_loot_tiles={nearby_text}, "
            f"filtered_collectible_debris={filtered_collectible_text}, debris_samples={sample_text}"
        )

    def _log_tool_effect(
        self,
        plan: ToolEffectPlan,
        state: StardewState,
        status: ToolEffectStatus,
        elapsed_seconds: float,
        effect_satisfied: bool | None,
        reason: str,
        aftermath_result: ToolAftermathResult,
    ) -> None:
        self._log(
            "验证工具动作预期效果: "
            f"owner={plan.owner}, action={plan.action_name}, location={state.location_name}, "
            f"target={self._format_tile(plan.target_tile)}, status={status}, "
            f"effect_satisfied={effect_satisfied}, elapsed={elapsed_seconds:.3f}s, "
            f"timeout={plan.effect_timeout_seconds:.3f}s, reason={reason}, "
            f"aftermath={aftermath_result.reason}, metadata={self._format_metadata(plan.metadata)}"
        )

    def _format_metadata(self, metadata: dict[str, Any]) -> str:
        if not metadata:
            return "{}"

        try:
            return json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        except TypeError:
            return str(metadata)

    def _log(self, message: str) -> None:
        if ToolAftermathService._debug_logger is None:
            return
        ToolAftermathService._debug_logger.log(message)

    def _format_debris_samples(self, debris_list: list[Any], limit: int = 8) -> list[str]:
        samples: list[str] = []
        for debris in debris_list[:limit]:
            tile = self._format_tile(getattr(debris, "tile", None))
            position = getattr(debris, "position", None)
            name = getattr(debris, "name", "")
            display_name = getattr(debris, "display_name", "")
            qualified_item_id = getattr(debris, "qualified_item_id", "")
            is_collectible = getattr(debris, "is_collectible", False)
            stack = getattr(debris, "stack", 0)
            source = getattr(debris, "source", "")
            samples.append(
                f"tile={tile}, position={position}, name={name}, display_name={display_name}, "
                f"qualified_item_id={qualified_item_id}, is_collectible={is_collectible}, "
                f"stack={stack}, source={source}"
            )
        return samples

    def _format_filtered_collectible_debris(
        self,
        debris_list: list[Any],
        target_tile: Tile | None,
        max_distance: int,
        limit: int = 8,
    ) -> list[str]:
        if target_tile is None:
            return []

        filtered: list[str] = []
        for debris in debris_list:
            if not self._is_collectible_debris(debris):
                continue
            debris_tile = getattr(debris, "tile", None)
            if not isinstance(debris_tile, Tile):
                continue
            chebyshev_distance = self._tile_chebyshev_distance(debris_tile, target_tile)
            if chebyshev_distance <= max_distance:
                continue
            manhattan_distance = self._tile_distance(debris_tile, target_tile)
            filtered.append(
                f"tile={self._format_tile(debris_tile)}, chebyshev={chebyshev_distance}, "
                f"manhattan={manhattan_distance}, max={max_distance}, name={getattr(debris, 'name', '')}, "
                f"source={getattr(debris, 'source', '')}"
            )
            if len(filtered) >= limit:
                break
        return filtered

    def _format_tile(self, tile: Tile | None) -> str:
        if tile is None:
            return "None"
        return f"({tile.x}, {tile.y})"

    def _query_ladder_at_tile(
        self,
        context: ToolAftermathContext,
        tile: Tile,
    ) -> tuple[bool | None, Tile | None, str]:
        response = context.executor_client.send_command(
            StardewCommand(
                action=StardewAction.QUERY_LADDER_AT_TILE,
                tile=(tile.x, tile.y),
            )
        )
        if response in (None, "TIMEOUT"):
            return None, None, str(response)

        try:
            payload = json.loads(response)
        except json.JSONDecodeError as exc:
            return None, None, f"INVALID_JSON:{exc}:{response}"

        status = payload.get("status")
        if status != "SUCCESS":
            return None, None, str(payload.get("reason") or status)

        has_ladder = bool(payload.get("has_ladder"))
        reason = str(payload.get("reason") or "")
        if not has_ladder:
            return False, None, reason

        ladder_obj = payload.get("ladder") if isinstance(payload.get("ladder"), dict) else {}
        raw_tile = ladder_obj.get("Tile") if isinstance(ladder_obj, dict) else None
        if not isinstance(raw_tile, list) or len(raw_tile) < 2:
            raw_tile = payload.get("tile")
        if not isinstance(raw_tile, list) or len(raw_tile) < 2:
            return True, tile, reason

        return True, Tile(int(raw_tile[0]), int(raw_tile[1])), reason
