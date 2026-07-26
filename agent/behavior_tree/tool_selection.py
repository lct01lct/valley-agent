from typing import Literal, Sequence

from agent.action.combat.weapon_selection import WeaponSelector
from agent.action.valley_action.action_type import KeyType
from agent.action.valley_action.clearance_policy import normalize_obstacle_type
from server.valley_server import InventoryItem, StardewState
from server.type import Tile

type ClearObstacleOwner = Literal["Route", "Farm", "Mining", "Guard"]

TOOLBAR_SIZE = 12
TOOLBAR_KEYS: tuple[KeyType, ...] = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "-", "=")
TOOL_AREA_TREE1_RISK_LAYERS: tuple[str, ...] = ("Tree1", "FruitTree1")
weapon_selector = WeaponSelector()

CLEAR_OBSTACLE_REQUIRED_TOOLS: dict[str, str] = {
    "stone": "Pickaxe",
    "Stone": "Pickaxe",
    "weeds": "Axe",
    "Weeds": "Axe",
    "twig": "Axe",
    "Twig": "Axe",
    "grass": "Scythe",
    "Grass": "Scythe",
    "tree": "Axe",
    "Tree": "Axe",
}

TOOL_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "Axe": ("Axe",),
    "Hoe": ("Hoe",),
    "Pickaxe": ("Pickaxe",),
    "Scythe": ("Scythe",),
    "Watering Can": ("Watering Can",),
    "Parsnip Seeds": ("Parsnip Seeds", "Parsnip Seed"),
}


def get_required_tool_for_obstacle(obstacle_type: str | None) -> str | None:
    if obstacle_type is None:
        return None
    return CLEAR_OBSTACLE_REQUIRED_TOOLS.get(obstacle_type)


def select_required_tool_for_obstacle(
    state: StardewState,
    obstacle_type: str | None,
    target_tile: Tile,
    owner: ClearObstacleOwner,
) -> str | None:
    if obstacle_type is None:
        return None

    normalized_obstacle_type = normalize_obstacle_type(obstacle_type)
    if normalized_obstacle_type == "weeds":
        return _select_tool_for_weeds(state, target_tile, owner)

    if normalized_obstacle_type == "grass":
        return _select_first_available_tool(state, ("Scythe",))

    if normalized_obstacle_type == "twig":
        return _select_first_available_tool(state, ("Axe",))

    if normalized_obstacle_type == "stone":
        return _select_first_available_tool(state, ("Pickaxe",))

    if normalized_obstacle_type == "tree":
        return _select_first_available_tool(state, ("Axe",))

    return get_required_tool_for_obstacle(obstacle_type)


def has_tool_area_tree1_risk(state: StardewState, target_tile: Tile, tool_name: str | None) -> bool:
    if not _is_area_clear_tool(tool_name):
        return False

    risk_tiles = _get_estimated_area_clear_range_tiles(state.player_tile, target_tile)
    for layer_name in TOOL_AREA_TREE1_RISK_LAYERS:
        if state.layers.get(layer_name, set()).intersection(risk_tiles):
            return True
    return False


def has_scythe_tree_seed_risk(state: StardewState, target_tile: Tile) -> bool:
    return has_tool_area_tree1_risk(state, target_tile, "Scythe")


def find_tool_item(state: StardewState, required_tool: str) -> InventoryItem | None:
    aliases = TOOL_NAME_ALIASES.get(required_tool, (required_tool,))
    for item in state.inventory.items:
        if matches_inventory_item(item, aliases):
            return item
    return None


def count_inventory_items(state: StardewState, item_name: str) -> int:
    aliases = TOOL_NAME_ALIASES.get(item_name, (item_name,))
    total_count = 0
    for item in state.inventory.items:
        if matches_inventory_item(item, aliases):
            total_count += max(item.stack, 1)
    return total_count


def matches_inventory_item(item: InventoryItem, aliases: Sequence[str]) -> bool:
    return (
        _matches_tool_name(item.name, aliases)
        or _matches_tool_name(item.display_name, aliases)
        or _matches_qualified_item_id(item.qualified_item_id, aliases)
    )


def _select_tool_for_weeds(state: StardewState, target_tile: Tile, owner: ClearObstacleOwner) -> str | None:
    sword = weapon_selector.select_best_sword(state)
    if sword is not None and _can_use_area_clear_tool_for_weeds(state, target_tile, sword.name, owner):
        return sword.name

    has_tree1_risk = has_tool_area_tree1_risk(state, target_tile, "Scythe")

    if owner == "Route":
        if not has_tree1_risk and find_tool_item(state, "Scythe") is not None:
            return "Scythe"
        return _select_first_available_tool(state, ("Axe", "Pickaxe", "Hoe", "Scythe"))

    if owner == "Mining":
        return _select_first_available_tool(state, ("Pickaxe", "Axe", "Scythe"))

    if find_tool_item(state, "Hoe") is not None:
        return "Hoe"
    if not has_tree1_risk and find_tool_item(state, "Scythe") is not None:
        return "Scythe"
    return _select_first_available_tool(state, ("Axe", "Pickaxe", "Scythe"))


def _can_use_area_clear_tool_for_weeds(
    state: StardewState,
    target_tile: Tile,
    tool_name: str,
    owner: ClearObstacleOwner,
) -> bool:
    if owner == "Farm":
        return True
    return not has_tool_area_tree1_risk(state, target_tile, tool_name)


def _select_first_available_tool(state: StardewState, tools: Sequence[str]) -> str | None:
    for tool_name in tools:
        if find_tool_item(state, tool_name) is not None:
            return tool_name
    return tools[0] if tools else None


def _is_area_clear_tool(tool_name: str | None) -> bool:
    if tool_name is None:
        return False
    normalized_tool_name = _normalize_tool_text(tool_name)
    return (
        "scythe" in normalized_tool_name
        or "sword" in normalized_tool_name
        or "blade" in normalized_tool_name
        or "saber" in normalized_tool_name
        or "cutlass" in normalized_tool_name
        or "katana" in normalized_tool_name
        or "claymore" in normalized_tool_name
        or "rapier" in normalized_tool_name
        or "slasher" in normalized_tool_name
    )


def _get_estimated_area_clear_range_tiles(player_tile: Tile, target_tile: Tile) -> set[Tile]:
    # 当前没有从 SMAPI 直接导出镰刀/剑的实际命中范围；这里用玩家格和目标格周围 1 格做保守估计。
    # Tree0 不需要保护；Tree1 / FruitTree1 开始保护，避免范围工具误伤成长中的树。
    range_tiles: set[Tile] = set()
    for center_tile in (player_tile, target_tile):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                range_tiles.add(Tile(center_tile.x + dx, center_tile.y + dy))
    return range_tiles


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


def get_toolbar_key(slot_index: int) -> KeyType | None:
    key_index = slot_index % TOOLBAR_SIZE
    if key_index < 0 or key_index >= len(TOOLBAR_KEYS):
        return None
    return TOOLBAR_KEYS[key_index]


def _matches_tool_name(tool_name: str, aliases: Sequence[str]) -> bool:
    normalized_name = _normalize_tool_text(tool_name)
    if not normalized_name:
        return False

    compact_name = _compact_tool_text(normalized_name)
    for alias in aliases:
        normalized_alias = _normalize_tool_text(alias)
        if not normalized_alias:
            continue

        compact_alias = _compact_tool_text(normalized_alias)
        if normalized_name == normalized_alias or compact_name == compact_alias:
            return True

        # 支持 Copper Axe / Steel Axe 这类升级工具名，但避免 Pickaxe 被误判成 Axe。
        if normalized_name.endswith(f" {normalized_alias}"):
            return True

    return False


def _matches_qualified_item_id(qualified_item_id: str, aliases: Sequence[str]) -> bool:
    normalized_item_id = _normalize_qualified_item_id(qualified_item_id)
    if not normalized_item_id:
        return False

    compact_item_id = _compact_tool_text(normalized_item_id)
    for alias in aliases:
        normalized_alias = _normalize_tool_text(alias)
        if not normalized_alias:
            continue

        if compact_item_id == _compact_tool_text(normalized_alias):
            return True

    return False


def _normalize_qualified_item_id(qualified_item_id: str) -> str:
    normalized_item_id = _normalize_tool_text(qualified_item_id)
    if ")" in normalized_item_id:
        normalized_item_id = normalized_item_id.rsplit(")", 1)[1].strip()
    return normalized_item_id


def _normalize_tool_text(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _compact_tool_text(value: str) -> str:
    return value.replace(" ", "")
