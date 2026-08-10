from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from agent.action.mining.mine_target import MineOpportunitySelector, MineTarget, MineTargetSelector, MineTargetType
from agent.action.mining.mining_opportunity_policy import (
    MiningOpportunityPolicy,
    OpportunityDecision,
    OpportunityPolicyConfig,
)
from server.valley_server import StardewState
from server.type import Tile


type MiningTargetObjective = Literal[
    "ENTER_LADDER",  # 前往并交互下一层梯子。
    "COLLECT_RESOURCE",  # 处理当前值得获取的价值资源。
    "BREAK_CORRIDOR",  # 破坏阻挡价值资源路径的第一块石头。
    "BREAK_SEARCH_STONE",  # 没有更高优先级目标时破石寻找梯子。
    "EXPLORE_STONE",  # 当前连通区域无可挖石头，向剩余石头方向探索。
    "NO_VALID_TARGET",  # 当前 state 中没有可安全执行的采矿目标。
]

type TargetPathBuilder = Callable[[MineTarget], list[Tile]]
type CorridorFinder = Callable[[MineTarget, int], tuple[Tile, int, int] | None]
type StandPathBuilder = Callable[[set[Tile]], list[Tile]]


@dataclass(frozen=True)
class MiningThreatContext:
    """目标决策所需的最小怪物风险视图，避免 Resolver 依赖战斗执行层。"""

    blocked_tiles: frozenset[Tile] = frozenset()
    risk_by_tile: dict[Tile, float] = field(default_factory=dict)
    nearest_threat_distance: int | None = None


@dataclass(frozen=True)
class MiningTargetDecision:
    """Mining 目标决策结果；只描述下一目标，不执行游戏动作。"""

    objective: MiningTargetObjective
    target: MineTarget | None = None
    corridor_stone_tile: Tile | None = None
    opportunity_decision: OpportunityDecision | None = None
    skipped_tiles: frozenset[Tile] = frozenset()
    reason: str = ""


class MiningTargetResolver:
    """
    Mining 目标决策层。

    Resolver 只读取候选目标、路径结果和策略参数，不发送命令、不推进任务，
    也不持有工具动作状态。MineNode 负责执行并通过新 state 验收结果。
    """

    def __init__(
        self,
        target_selector: MineTargetSelector | None = None,
        opportunity_selector: MineOpportunitySelector | None = None,
        opportunity_policy: MiningOpportunityPolicy | None = None,
    ) -> None:
        self.target_selector = target_selector or MineTargetSelector()
        self.opportunity_selector = opportunity_selector or MineOpportunitySelector()
        self.opportunity_policy = opportunity_policy or MiningOpportunityPolicy()

    def resolve_ladder(
        self,
        state: StardewState,
        excluded_tiles: set[Tile],
    ) -> MiningTargetDecision:
        ladders = self.target_selector.build_ladder_targets(state, excluded_tiles)
        target = self.target_selector.select_nearest_target(state, ladders)
        if target is None:
            return MiningTargetDecision(
                objective="NO_VALID_TARGET",
                reason="当前 state 中没有可用的下一层梯子",
            )
        return MiningTargetDecision(
            objective="ENTER_LADDER",
            target=target,
            reason=f"选择最近的下一层梯子: target={target.tile}",
        )

    def resolve_opportunity_anchor(
        self,
        state: StardewState,
        allowed_target_types: set[MineTargetType],
        ignored_tiles: set[Tile],
        max_visible_resource_distance: int,
        target_path_builder: TargetPathBuilder,
        corridor_finder: CorridorFinder,
        direct_ladder_path_tiles: list[Tile] | None = None,
        ladder_tile: Tile | None = None,
        threat_context: MiningThreatContext | None = None,
    ) -> MiningTargetDecision:
        if not allowed_target_types:
            return MiningTargetDecision(objective="NO_VALID_TARGET", reason="任务未启用任何机会目标类型")

        threat_context = threat_context or MiningThreatContext()
        if (
            threat_context.nearest_threat_distance is not None
            and threat_context.nearest_threat_distance <= 3
        ):
            return MiningTargetDecision(
                objective="NO_VALID_TARGET",
                reason=f"怪物距离过近，暂停创建价值资源锚点: distance={threat_context.nearest_threat_distance}",
            )

        targets = self.opportunity_selector.build_opportunity_targets(
            state,
            allowed_target_types,
            ignored_tiles=ignored_tiles,
            max_detour_tiles=None if ladder_tile is not None else max_visible_resource_distance,
        )
        if not targets:
            return MiningTargetDecision(objective="NO_VALID_TARGET", reason="当前没有机会资源候选")

        policy = self.opportunity_policy.with_config(
            OpportunityPolicyConfig(max_visible_resource_distance=max_visible_resource_distance)
        )
        accepted: list[tuple[OpportunityDecision, Tile | None]] = []
        rejected: list[OpportunityDecision] = []
        skipped_tiles: set[Tile] = set()

        for target in targets:
            if target.tile in threat_context.blocked_tiles:
                skipped_tiles.add(target.tile)
                continue
            if not policy.is_candidate_in_scope(state, target, direct_ladder_path_tiles):
                continue

            resource_path_tiles = target_path_builder(target)
            corridor_stone_tile: Tile | None = None
            corridor_break_count = 0
            if not resource_path_tiles:
                corridor = corridor_finder(target, policy.config.max_corridor_break_count)
                if corridor is None:
                    skipped_tiles.add(target.tile)
                    continue
                corridor_stone_tile, corridor_break_count, path_length = corridor
                # Policy 只依赖路径长度计算成本；不可达目标尚没有真实路径，使用等长占位序列。
                resource_path_tiles = [state.player_tile for _ in range(path_length + 1)]

            decision = policy.evaluate(
                state=state,
                target=target,
                resource_path_tiles=resource_path_tiles,
                corridor_break_count=corridor_break_count,
                direct_ladder_path_tiles=direct_ladder_path_tiles,
                ladder_tile=ladder_tile,
            )
            if decision.should_take:
                accepted.append((decision, corridor_stone_tile))
            else:
                rejected.append(decision)

        if not accepted:
            best_rejected = max(rejected, key=lambda item: item.score, default=None)
            reason = "当前没有值得处理的机会资源"
            if best_rejected is not None:
                reason = (
                    f"当前没有值得处理的机会资源: target={best_rejected.target.tile}, "
                    f"score={best_rejected.score:.1f}, detail={best_rejected.reason}"
                )
            return MiningTargetDecision(
                objective="NO_VALID_TARGET",
                opportunity_decision=best_rejected,
                skipped_tiles=frozenset(skipped_tiles),
                reason=reason,
            )

        selected_decision, corridor_stone_tile = max(
            accepted,
            key=lambda item: (
                item[0].score,
                -self._tile_distance(state.player_tile, item[0].target.tile),
                -item[0].break_cost,
                -item[0].action_cost,
            ),
        )
        objective: MiningTargetObjective = "BREAK_CORRIDOR" if corridor_stone_tile is not None else "COLLECT_RESOURCE"
        return MiningTargetDecision(
            objective=objective,
            target=selected_decision.target,
            corridor_stone_tile=corridor_stone_tile,
            opportunity_decision=selected_decision,
            skipped_tiles=frozenset(skipped_tiles),
            reason=(
                f"选择价值资源目标: target={selected_decision.target.tile}, "
                f"score={selected_decision.score:.1f}, corridor={corridor_stone_tile}"
            ),
        )

    def resolve_break_search_stone(
        self,
        state: StardewState,
        excluded_tiles: set[Tile],
        stand_path_builder: StandPathBuilder,
        threat_context: MiningThreatContext | None = None,
    ) -> MiningTargetDecision:
        threat_context = threat_context or MiningThreatContext()
        targets = self.target_selector.build_breakable_rock_targets(state, excluded_tiles)
        targets = [target for target in targets if target.tile not in threat_context.blocked_tiles]
        if not targets:
            return MiningTargetDecision(
                objective="EXPLORE_STONE",
                reason="当前没有未排除的 Stone / MiningNode",
            )

        map_width, map_height = state.map_size
        stand_to_targets: dict[Tile, list[MineTarget]] = {}
        for target in targets:
            for stand_tile in target.candidate_stand_tiles:
                if not 0 <= stand_tile.x < map_width or not 0 <= stand_tile.y < map_height:
                    continue
                if stand_tile in threat_context.blocked_tiles:
                    continue
                stand_to_targets.setdefault(stand_tile, []).append(target)

        path = stand_path_builder(set(stand_to_targets))
        if not path:
            return MiningTargetDecision(
                objective="EXPLORE_STONE",
                reason="当前连通区域没有可达石头站位",
            )

        reached_stand_tile = path[-1]
        candidate_targets = stand_to_targets.get(reached_stand_tile, [])
        if not candidate_targets:
            return MiningTargetDecision(
                objective="EXPLORE_STONE",
                reason=f"A* 终点没有关联石头目标: stand={reached_stand_tile}",
            )

        target = min(
            candidate_targets,
            key=lambda candidate: (
                threat_context.risk_by_tile.get(candidate.tile, 0.0),
                self._tile_distance(reached_stand_tile, candidate.tile),
                self._tile_distance(state.player_tile, candidate.tile),
                candidate.tile.y,
                candidate.tile.x,
            ),
        )
        return MiningTargetDecision(
            objective="BREAK_SEARCH_STONE",
            target=target,
            reason=f"选择当前连通区域最近可达石头: target={target.tile}, stand={reached_stand_tile}",
        )

    def resolve_exploration_stone(
        self,
        state: StardewState,
        excluded_tiles: set[Tile],
        threat_context: MiningThreatContext | None = None,
    ) -> MiningTargetDecision:
        threat_context = threat_context or MiningThreatContext()
        targets = self.target_selector.build_breakable_rock_targets(state, excluded_tiles)
        targets = [target for target in targets if target.tile not in threat_context.blocked_tiles]
        if not targets:
            return MiningTargetDecision(objective="NO_VALID_TARGET", reason="矿层没有剩余可探索石头")

        target = min(
            targets,
            key=lambda candidate: (
                threat_context.risk_by_tile.get(candidate.tile, 0.0),
                self._tile_distance(state.player_tile, candidate.tile),
                candidate.tile.y,
                candidate.tile.x,
            ),
        )
        return MiningTargetDecision(
            objective="EXPLORE_STONE",
            target=target,
            reason=f"选择最近的安全石头作为探索方向: target={target.tile}",
        )

    def _tile_distance(self, start_tile: Tile, end_tile: Tile) -> int:
        return abs(start_tile.x - end_tile.x) + abs(start_tile.y - end_tile.y)
