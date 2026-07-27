from dataclasses import dataclass
from typing import Literal

from server.valley_server import StardewState
from server.type import Tile


type ClearanceOwner = Literal["Route", "Farm"]

ORDINARY_TREE_LAYERS: tuple[str, ...] = tuple(f"Tree{growth_stage}" for growth_stage in range(6))
PASSABLE_TREE_SEED_LAYERS: tuple[str, ...] = ("Tree0",)
BLOCKING_ORDINARY_TREE_LAYERS: tuple[str, ...] = tuple(f"Tree{growth_stage}" for growth_stage in range(1, 6))
FRUIT_TREE_LAYERS: tuple[str, ...] = tuple(f"FruitTree{growth_stage}" for growth_stage in range(6))


@dataclass(frozen=True)
class ClearDecision:
    can_clear: bool
    obstacle_type: str | None
    required_tool: str | None
    cost: float
    should_skip_tile: bool
    reason: str


def decide_clear_obstacle(state: StardewState, target_tile: Tile, obstacle_type: str | None, owner: ClearanceOwner) -> ClearDecision:
    """
    清障策略层：判断某个障碍是否允许被清理。

    这里承接未来 Agent Skill / Planner 的策略输入，例如保护某棵树、延迟到成熟后再砍等。
    当前尚未接入外部策略记忆，因此默认规则是：
    - Tree0：树种/幼苗，可以通行，不作为自动清障目标；若未来显式清理，只允许斧子/镐子这类点对点工具。
    - 普通 Tree1~Tree5：Route 和 Farm 都允许清理；Farm 的规划区域视为 Agent 已授权。
    - FruitTree0~FruitTree5：暂不清理。
    - TreeStump：暂不清理。
    - Stone / Weeds / Twig / Grass：允许清理。
    """
    normalized_obstacle_type = normalize_obstacle_type(obstacle_type)

    if normalized_obstacle_type is None:
        return ClearDecision(
            can_clear=False,
            obstacle_type=None,
            required_tool=None,
            cost=float("inf"),
            should_skip_tile=False,
            reason="目标地块没有可识别障碍",
        )

    if normalized_obstacle_type == "fruit_tree":
        return ClearDecision(
            can_clear=False,
            obstacle_type=normalized_obstacle_type,
            required_tool=None,
            cost=float("inf"),
            should_skip_tile=True,
            reason="果树暂不纳入自动清理",
        )

    if normalized_obstacle_type == "tree_stump":
        return ClearDecision(
            can_clear=False,
            obstacle_type=normalized_obstacle_type,
            required_tool=None,
            cost=float("inf"),
            should_skip_tile=True,
            reason="TreeStump 暂不纳入自动清理",
        )

    if normalized_obstacle_type == "tree_seed":
        return ClearDecision(
            can_clear=False,
            obstacle_type=normalized_obstacle_type,
            required_tool=None,
            cost=0.0,
            should_skip_tile=False,
            reason="Tree0 树种/幼苗可通行，不作为自动清障目标",
        )

    if normalized_obstacle_type == "tree":
        return ClearDecision(
            can_clear=True,
            obstacle_type="tree",
            required_tool="Axe",
            cost=50.0,
            should_skip_tile=False,
            reason=f"{owner} 允许清理普通树；未来可由 Agent Skill 覆盖该策略",
        )

    required_tool_by_obstacle = {
        "grass": "Scythe",
        "weeds": None,
        "twig": "Axe",
        "stone": "Pickaxe",
    }
    cost_by_obstacle = {
        "grass": 2.0,
        "weeds": 4.0,
        "twig": 6.0,
        "stone": 8.0,
    }

    if normalized_obstacle_type in required_tool_by_obstacle:
        return ClearDecision(
            can_clear=True,
            obstacle_type=normalized_obstacle_type,
            required_tool=required_tool_by_obstacle[normalized_obstacle_type],
            cost=cost_by_obstacle[normalized_obstacle_type],
            should_skip_tile=False,
            reason="基础可清障碍",
        )

    return ClearDecision(
        can_clear=False,
        obstacle_type=normalized_obstacle_type,
        required_tool=None,
        cost=float("inf"),
        should_skip_tile=True,
        reason=f"未知或暂不支持的障碍类型: {obstacle_type}",
    )


def normalize_obstacle_type(obstacle_type: str | None) -> str | None:
    if obstacle_type is None:
        return None

    normalized_type = obstacle_type.strip()
    if not normalized_type:
        return None

    lower_type = normalized_type.lower()
    if lower_type.startswith("fruittree"):
        return "fruit_tree"
    if lower_type == "treestump":
        return "tree_stump"
    if lower_type == "tree0":
        return "tree_seed"
    if lower_type.startswith("tree"):
        return "tree"
    if lower_type in ("grass", "weeds", "twig", "stone"):
        return lower_type
    return lower_type


def get_obstacle_type_at_tile(state: StardewState, target_tile: Tile) -> str | None:
    for layer_name in ("Grass", "Weeds", "Twig", "Stone", *BLOCKING_ORDINARY_TREE_LAYERS, "TreeStump", *FRUIT_TREE_LAYERS):
        if target_tile in state.layers.get(layer_name, set()):
            return layer_name
    return None
