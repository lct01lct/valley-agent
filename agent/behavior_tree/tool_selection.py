from typing import Sequence

from server.valley_server import InventoryItem, StardewState


TOOLBAR_SIZE = 12
TOOLBAR_KEYS: tuple[str, ...] = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "-", "=")

CLEAR_OBSTACLE_REQUIRED_TOOLS: dict[str, str] = {
    "stone": "Pickaxe",
    "weeds": "Axe",
    "twig": "Axe",
}

TOOL_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "Axe": ("Axe",),
    "Pickaxe": ("Pickaxe",),
}


def get_required_tool_for_obstacle(obstacle_type: str | None) -> str | None:
    if obstacle_type is None:
        return None
    return CLEAR_OBSTACLE_REQUIRED_TOOLS.get(obstacle_type)


def find_tool_item(state: StardewState, required_tool: str) -> InventoryItem | None:
    aliases = TOOL_NAME_ALIASES.get(required_tool, (required_tool,))
    for item in state.inventory.items:
        if not item.is_tool:
            continue
        if _matches_tool_name(item.name, aliases) or _matches_tool_name(item.display_name, aliases):
            return item
        if _matches_qualified_item_id(item.qualified_item_id, aliases):
            return item
    return None


def is_current_tool(state: StardewState, required_tool: str) -> bool:
    current_tool_index = state.inventory.current_tool_index
    for item in state.inventory.items:
        if item.index != current_tool_index:
            continue
        aliases = TOOL_NAME_ALIASES.get(required_tool, (required_tool,))
        return (
            _matches_tool_name(item.name, aliases)
            or _matches_tool_name(item.display_name, aliases)
            or _matches_qualified_item_id(item.qualified_item_id, aliases)
        )
    return False


def get_toolbar_index(slot_index: int) -> int:
    return slot_index // TOOLBAR_SIZE


def get_toolbar_key(slot_index: int) -> str | None:
    key_index = slot_index % TOOLBAR_SIZE
    if key_index < 0 or key_index >= len(TOOLBAR_KEYS):
        return None
    return TOOLBAR_KEYS[key_index]


def _matches_tool_name(tool_name: str, aliases: Sequence[str]) -> bool:
    normalized_name = tool_name.strip().lower()
    if not normalized_name:
        return False
    return any(normalized_name.endswith(alias.lower()) for alias in aliases)


def _matches_qualified_item_id(qualified_item_id: str, aliases: Sequence[str]) -> bool:
    normalized_item_id = qualified_item_id.strip().lower()
    if not normalized_item_id:
        return False
    return any(alias.lower() in normalized_item_id for alias in aliases)
