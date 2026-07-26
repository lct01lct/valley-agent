import json
import os
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Literal, Protocol

from agent.action.valley_action.action_type import StardewAction, StardewCommand
from server.valley_server import StardewState
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
    ) -> ToolAftermathResult:
        blocking_menu_type, blocking_menu_text = self._read_blocking_menu(state, request.check_blocking_menu)
        target_change_state = self._build_target_change_state(request.target_tile_changed)
        nearby_loot_tiles = self._find_nearby_loot_tiles(state, request.target_tile)

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
            if self._tile_distance(debris.tile, target_tile) > max_distance:
                continue
            if debris.tile in seen_tiles:
                continue
            seen_tiles.add(debris.tile)
            nearby_tiles.append(debris.tile)
        return nearby_tiles

    def _tile_distance(self, start_tile: Tile, end_tile: Tile) -> int:
        return abs(start_tile.x - end_tile.x) + abs(start_tile.y - end_tile.y)

    def _log_inspection(
        self,
        state: StardewState,
        request: ToolAftermathRequest,
        blocking_menu_type: str | None,
        target_change_state: ToolTargetChangeState,
        generated_ladder_tile: Tile | None,
        ladder_query_status: bool | None,
        nearby_loot_tiles: list[Tile],
    ) -> None:
        if ToolAftermathService._debug_logger is None:
            return

        debris_list = list(getattr(state, "debris", []))
        target_text = self._format_tile(request.target_tile)
        nearby_text = [self._format_tile(tile) for tile in nearby_loot_tiles]
        sample_text = self._format_debris_samples(debris_list)
        ToolAftermathService._debug_logger.log(
            "观察工具动作副作用: "
            f"owner={request.owner}, action={request.action_name}, location={state.location_name}, "
            f"target={target_text}, target_change={target_change_state}, "
            f"blocking_menu={blocking_menu_type}, ladder_query={ladder_query_status}, "
            f"generated_ladder={self._format_tile(generated_ladder_tile)}, "
            f"has_debris_snapshot={getattr(state, 'has_debris_snapshot', None)}, "
            f"debris_count={len(debris_list)}, nearby_loot_tiles={nearby_text}, debris_samples={sample_text}"
        )

    def _format_debris_samples(self, debris_list: list[Any], limit: int = 8) -> list[str]:
        samples: list[str] = []
        for debris in debris_list[:limit]:
            tile = self._format_tile(getattr(debris, "tile", None))
            position = getattr(debris, "position", None)
            name = getattr(debris, "name", "")
            display_name = getattr(debris, "display_name", "")
            qualified_item_id = getattr(debris, "qualified_item_id", "")
            stack = getattr(debris, "stack", 0)
            source = getattr(debris, "source", "")
            samples.append(
                f"tile={tile}, position={position}, name={name}, display_name={display_name}, "
                f"qualified_item_id={qualified_item_id}, stack={stack}, source={source}"
            )
        return samples

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
