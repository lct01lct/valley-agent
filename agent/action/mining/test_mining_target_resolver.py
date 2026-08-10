import unittest
from types import SimpleNamespace

from agent.action.mining.mine_target import MineTarget
from agent.action.mining.mining_opportunity_policy import OpportunityDecision
from agent.action.mining.mining_target_resolver import MiningTargetResolver, MiningThreatContext
from server.type import Tile


class FakeTargetSelector:
    def __init__(self, ladders: list[MineTarget] | None = None, rocks: list[MineTarget] | None = None) -> None:
        self.ladders = ladders or []
        self.rocks = rocks or []

    def build_ladder_targets(self, state, excluded_tiles: set[Tile]) -> list[MineTarget]:
        return [target for target in self.ladders if target.tile not in excluded_tiles]

    def select_nearest_target(self, state, targets: list[MineTarget]) -> MineTarget | None:
        if not targets:
            return None
        return min(
            targets,
            key=lambda target: abs(state.player_tile.x - target.tile.x) + abs(state.player_tile.y - target.tile.y),
        )

    def build_breakable_rock_targets(self, state, excluded_tiles: set[Tile]) -> list[MineTarget]:
        return [target for target in self.rocks if target.tile not in excluded_tiles]


class FakeOpportunitySelector:
    def __init__(self, targets: list[MineTarget]) -> None:
        self.targets = targets

    def build_opportunity_targets(
        self,
        state,
        allowed_target_types,
        ignored_tiles: set[Tile],
        max_detour_tiles: int | None,
    ) -> list[MineTarget]:
        return [
            target
            for target in self.targets
            if target.target_type in allowed_target_types and target.tile not in ignored_tiles
        ]


class FakeOpportunityPolicy:
    config = SimpleNamespace(max_corridor_break_count=2)

    def with_config(self, config):
        return self

    def is_candidate_in_scope(self, state, target, direct_ladder_path_tiles=None) -> bool:
        return True

    def evaluate(
        self,
        state,
        target: MineTarget,
        resource_path_tiles: list[Tile],
        corridor_break_count: int = 0,
        direct_ladder_path_tiles: list[Tile] | None = None,
        ladder_tile: Tile | None = None,
    ) -> OpportunityDecision:
        return OpportunityDecision(
            target=target,
            should_take=True,
            score=target.priority,
            resource_value=target.priority,
            direct_ladder_cost=None,
            resource_cost=float(len(resource_path_tiles)),
            extra_path_cost=None,
            effective_extra_path_cost=None,
            path_nearby_distance=None,
            near_player_bonus=0.0,
            break_cost=float(corridor_break_count),
            action_cost=0.0,
            risk_cost=0.0,
            reason="测试策略通过",
        )


class MiningTargetResolverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = SimpleNamespace(player_tile=Tile(5, 5), map_size=(20, 20))

    def test_resolve_ladder_filters_return_prompt_and_selects_nearest(self) -> None:
        return_ladder = MineTarget(target_type="LADDER", tile=Tile(5, 4), action="INTERACT")
        near_ladder = MineTarget(target_type="LADDER", tile=Tile(7, 5), action="INTERACT")
        far_ladder = MineTarget(target_type="LADDER", tile=Tile(14, 14), action="INTERACT")
        resolver = MiningTargetResolver(
            target_selector=FakeTargetSelector(ladders=[return_ladder, near_ladder, far_ladder])
        )

        decision = resolver.resolve_ladder(self.state, {return_ladder.tile})

        self.assertEqual("ENTER_LADDER", decision.objective)
        self.assertEqual(near_ladder, decision.target)

    def test_resolve_break_search_stone_uses_astar_reached_stand_tile(self) -> None:
        left_rock = MineTarget(target_type="STONE", tile=Tile(3, 5), action="USE_PICKAXE")
        right_rock = MineTarget(target_type="STONE", tile=Tile(9, 5), action="USE_PICKAXE")
        resolver = MiningTargetResolver(target_selector=FakeTargetSelector(rocks=[left_rock, right_rock]))

        decision = resolver.resolve_break_search_stone(
            self.state,
            excluded_tiles=set(),
            stand_path_builder=lambda stand_tiles: [self.state.player_tile, Tile(4, 5)],
        )

        self.assertEqual("BREAK_SEARCH_STONE", decision.objective)
        self.assertEqual(left_rock, decision.target)

    def test_resolve_exploration_stone_excludes_threat_blocked_target(self) -> None:
        blocked_rock = MineTarget(target_type="STONE", tile=Tile(6, 5), action="USE_PICKAXE")
        safe_rock = MineTarget(target_type="STONE", tile=Tile(8, 5), action="USE_PICKAXE")
        resolver = MiningTargetResolver(target_selector=FakeTargetSelector(rocks=[blocked_rock, safe_rock]))

        decision = resolver.resolve_exploration_stone(
            self.state,
            excluded_tiles=set(),
            threat_context=MiningThreatContext(blocked_tiles=frozenset({blocked_rock.tile})),
        )

        self.assertEqual("EXPLORE_STONE", decision.objective)
        self.assertEqual(safe_rock, decision.target)

    def test_resolve_opportunity_anchor_returns_corridor_decision(self) -> None:
        resource = MineTarget(
            target_type="MINING_NODE",
            tile=Tile(10, 5),
            action="USE_PICKAXE",
            is_resource_mining_node=True,
            priority=12.0,
        )
        resolver = MiningTargetResolver(
            target_selector=FakeTargetSelector(),
            opportunity_selector=FakeOpportunitySelector([resource]),
            opportunity_policy=FakeOpportunityPolicy(),
        )

        decision = resolver.resolve_opportunity_anchor(
            state=self.state,
            allowed_target_types={"MINING_NODE"},
            ignored_tiles=set(),
            max_visible_resource_distance=10,
            target_path_builder=lambda target: [],
            corridor_finder=lambda target, max_breaks: (Tile(6, 5), 1, 5),
        )

        self.assertEqual("BREAK_CORRIDOR", decision.objective)
        self.assertEqual(resource, decision.target)
        self.assertEqual(Tile(6, 5), decision.corridor_stone_tile)


if __name__ == "__main__":
    unittest.main()
