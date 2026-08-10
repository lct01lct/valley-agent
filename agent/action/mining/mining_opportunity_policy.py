from dataclasses import dataclass

from agent.action.mining.mine_target import MineTarget
from agent.action.mining.mining_risk_evaluator import MiningRiskEvaluator
from server.valley_server import StardewState
from server.type import Tile


@dataclass(frozen=True)
class OpportunityPolicyConfig:
    """
    Mining 机会资源策略参数。

    这些参数表达“额外代价是否值得”，不是“每层最多拿几个资源”的硬上限。
    """

    max_visible_resource_distance: int = 10
    max_corridor_break_count: int = 2
    path_nearby_distance: int = 3
    path_nearby_cost_ratio: float = 0.6
    min_score: float = 0.0
    collectible_value: float = 10.0
    mining_node_value: float = 8.0
    breakable_container_value: float = 7.0
    near_player_distance: int = 10
    near_player_bonus: float = 10.0
    very_near_player_distance: int = 5
    very_near_player_bonus: float = 16.0
    corridor_break_cost: float = 2.0
    mining_node_hit_cost: float = 1.0
    breakable_container_action_cost: float = 1.0
    collectible_action_cost: float = 0.0


@dataclass(frozen=True)
class OpportunityDecision:
    target: MineTarget
    should_take: bool
    score: float
    resource_value: float
    direct_ladder_cost: float | None
    resource_cost: float
    extra_path_cost: float | None
    effective_extra_path_cost: float | None
    path_nearby_distance: int | None
    near_player_bonus: float
    break_cost: float
    action_cost: float
    risk_cost: float
    reason: str


class MiningOpportunityPolicy:
    """
    用 Utility 风格评分判断 Mining 机会资源是否值得处理。

    MineNode 负责执行；这里负责回答“当前目标值不值得拿”。
    当前风险项由 MiningRiskEvaluator 预留，默认 risk_cost=0。
    """

    def __init__(
        self,
        risk_evaluator: MiningRiskEvaluator | None = None,
        config: OpportunityPolicyConfig | None = None,
    ) -> None:
        self.risk_evaluator = risk_evaluator or MiningRiskEvaluator()
        self.config = config or OpportunityPolicyConfig()

    def with_config(self, config: OpportunityPolicyConfig) -> "MiningOpportunityPolicy":
        return MiningOpportunityPolicy(risk_evaluator=self.risk_evaluator, config=config)

    def evaluate(
        self,
        state: StardewState,
        target: MineTarget,
        resource_path_tiles: list[Tile],
        corridor_break_count: int = 0,
        direct_ladder_path_tiles: list[Tile] | None = None,
        ladder_tile: Tile | None = None,
    ) -> OpportunityDecision:
        resource_value = self._get_resource_value(target)
        action_cost = self._get_action_cost(target)
        break_cost = corridor_break_count * self.config.corridor_break_cost
        risk_cost = self.risk_evaluator.calculate_path_risk(state, target, resource_path_tiles)
        resource_path_cost = max(0, len(resource_path_tiles) - 1)
        near_player_bonus = self._get_near_player_bonus(state, target)

        direct_ladder_cost: float | None = None
        extra_path_cost: float | None = None
        effective_extra_path_cost: float | None = None
        path_nearby_distance: int | None = None
        resource_to_ladder_cost = 0
        if direct_ladder_path_tiles is not None and ladder_tile is not None:
            direct_ladder_cost = max(0, len(direct_ladder_path_tiles) - 1)
            resource_to_ladder_cost = self._tile_distance(target.tile, ladder_tile)
            raw_extra_path_cost = resource_path_cost + resource_to_ladder_cost - direct_ladder_cost
            extra_path_cost = max(0.0, float(raw_extra_path_cost))
            path_nearby_distance = self._distance_to_path(target.tile, direct_ladder_path_tiles)
            if path_nearby_distance <= self.config.path_nearby_distance:
                effective_extra_path_cost = extra_path_cost * self.config.path_nearby_cost_ratio
            else:
                effective_extra_path_cost = extra_path_cost
        else:
            effective_extra_path_cost = float(resource_path_cost)

        resource_cost = (
            float(resource_path_cost)
            + float(resource_to_ladder_cost)
            + action_cost
            + break_cost
            + risk_cost
        )
        score = resource_value + near_player_bonus - effective_extra_path_cost - action_cost - break_cost - risk_cost

        should_take, reason = self._build_reason(
            target=target,
            score=score,
            corridor_break_count=corridor_break_count,
            effective_extra_path_cost=effective_extra_path_cost,
            path_nearby_distance=path_nearby_distance,
        )
        return OpportunityDecision(
            target=target,
            should_take=should_take,
            score=score,
            resource_value=resource_value,
            direct_ladder_cost=direct_ladder_cost,
            resource_cost=resource_cost,
            extra_path_cost=extra_path_cost,
            effective_extra_path_cost=effective_extra_path_cost,
            path_nearby_distance=path_nearby_distance,
            near_player_bonus=near_player_bonus,
            break_cost=break_cost,
            action_cost=action_cost,
            risk_cost=risk_cost,
            reason=reason,
        )

    def is_candidate_in_scope(
        self,
        state: StardewState,
        target: MineTarget,
        direct_ladder_path_tiles: list[Tile] | None = None,
    ) -> bool:
        if self._tile_distance(state.player_tile, target.tile) <= self.config.max_visible_resource_distance:
            return True
        if direct_ladder_path_tiles is None:
            return False
        return self._distance_to_path(target.tile, direct_ladder_path_tiles) <= self.config.path_nearby_distance

    def _build_reason(
        self,
        target: MineTarget,
        score: float,
        corridor_break_count: int,
        effective_extra_path_cost: float | None,
        path_nearby_distance: int | None,
    ) -> tuple[bool, str]:
        if corridor_break_count > self.config.max_corridor_break_count:
            return False, f"通路破石成本过高: breaks={corridor_break_count}/{self.config.max_corridor_break_count}"
        if score < self.config.min_score:
            return False, f"机会评分不足: score={score:.1f}/{self.config.min_score:.1f}"

        nearby_text = "" if path_nearby_distance is None else f", path_nearby={path_nearby_distance}"
        return True, f"机会评分通过: target={target.target_type}, score={score:.1f}{nearby_text}"

    def _get_resource_value(self, target: MineTarget) -> float:
        if target.target_type == "COLLECTIBLE":
            return self.config.collectible_value
        if target.target_type == "BREAKABLE_CONTAINER":
            return self.config.breakable_container_value
        if target.target_type == "MINING_NODE":
            return self.config.mining_node_value
        return 0.0

    def _get_action_cost(self, target: MineTarget) -> float:
        if target.target_type == "COLLECTIBLE":
            return self.config.collectible_action_cost
        if target.target_type == "BREAKABLE_CONTAINER":
            return self.config.breakable_container_action_cost
        if target.target_type == "MINING_NODE":
            return max(1, target.estimated_hits_to_break) * self.config.mining_node_hit_cost
        return 0.0

    def _get_near_player_bonus(self, state: StardewState, target: MineTarget) -> float:
        distance = self._tile_distance(state.player_tile, target.tile)
        if distance <= self.config.very_near_player_distance:
            return self.config.very_near_player_bonus
        if distance > self.config.near_player_distance:
            return 0.0
        return self.config.near_player_bonus

    def _distance_to_path(self, tile: Tile, path_tiles: list[Tile]) -> int:
        if not path_tiles:
            return 0
        return min(self._tile_distance(tile, path_tile) for path_tile in path_tiles)

    def _tile_distance(self, start_tile: Tile, end_tile: Tile) -> int:
        return abs(start_tile.x - end_tile.x) + abs(start_tile.y - end_tile.y)
