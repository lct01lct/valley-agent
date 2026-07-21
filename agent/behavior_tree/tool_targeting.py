from agent.action.valley_action.action_type import StardewAction, StardewCommand
from server.valley_server import StardewState, ToolTargetState
from server.type import Tile


def is_tool_targeting(state: StardewState, target_tile: Tile) -> bool:
    return state.tool_target.is_targeting(target_tile)


def build_tool_target_face_command(player_tile: Tile, target_tile: Tile) -> StardewCommand:
    if target_tile.x > player_tile.x:
        return StardewCommand(action=StardewAction.FACE_DIRECTION, key=["d"])
    if target_tile.x < player_tile.x:
        return StardewCommand(action=StardewAction.FACE_DIRECTION, key=["a"])
    if target_tile.y > player_tile.y:
        return StardewCommand(action=StardewAction.FACE_DIRECTION, key=["s"])
    if target_tile.y < player_tile.y:
        return StardewCommand(action=StardewAction.FACE_DIRECTION, key=["w"])
    return StardewCommand(action=StardewAction.IDLE)


def format_tool_target(tool_target: ToolTargetState | None) -> str:
    if tool_target is None:
        return "None"

    return (
        "{"
        f"Source={tool_target.source}, "
        f"Tile={tool_target.tile}, "
        f"PlayerTile={tool_target.player_tile}, "
        f"FacingDirection={tool_target.facing_direction}, "
        f"SelectedItemName={tool_target.selected_item_name}, "
        f"IsCardinalNeighbor={tool_target.is_cardinal_neighbor}, "
        f"IsStandingOnTarget={tool_target.is_standing_on_target}"
        "}"
    )
