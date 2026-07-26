import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

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


class ToolAftermathService:
    """
    工具动作后处理的通用观察层。

    本服务只回答“工具动作收招后，世界出现了哪些可观察副作用”。
    业务节点仍负责解释这些结果，例如 Mining 决定是否立刻转向梯子，
    ClearObstacle 决定是否重试清障，未来 CollectLootNode 决定是否拾取掉落物。
    """

    def inspect_after_tool_action(
        self,
        context: ToolAftermathContext,
        state: StardewState,
        request: ToolAftermathRequest,
    ) -> ToolAftermathResult:
        blocking_menu_type, blocking_menu_text = self._read_blocking_menu(state, request.check_blocking_menu)
        target_change_state = self._build_target_change_state(request.target_tile_changed)

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
        reason_parts.append(f"target_change={target_change_state}")

        return ToolAftermathResult(
            has_blocking_menu=blocking_menu_type is not None,
            blocking_menu_type=blocking_menu_type,
            blocking_menu_text=blocking_menu_text,
            target_change_state=target_change_state,
            generated_ladder_tile=generated_ladder_tile,
            ladder_query_status=ladder_query_status,
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
