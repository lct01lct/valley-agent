import time
from dataclasses import dataclass
from typing import Literal

from agent.action.combat.monster_threat import MonsterThreat, MonsterThreatSnapshot
from server.type import Tile
from server.valley_server import StardewState


type MiningObjectiveType = Literal[
    "MINE_ENTRANCE",  # 矿洞入口或楼层入口，怪物阻挡时通常应主动处理。
    "LADDER",  # 下层梯子，优先绕路，绕不开时杀怪。
    "STONE",  # 采矿目标石头，怪物附近时可暂缓并换其他石头。
]

type TacticalDecisionType = Literal[
    "IGNORE",  # 当前怪物风险不影响目标，继续原任务。
    "AVOID",  # 暂时避让，仅用于未来明确需要拉开距离的场景；Mining 默认不使用。
    "ENGAGE",  # 主动接近并杀死目标怪物，适合怪物堵入口/梯子/必经路。
    "REROUTE",  # 绕开风险继续目标。
    "DEFER_OBJECTIVE",  # 暂缓当前目标，例如换一块石头。
]


@dataclass(frozen=True)
class MiningObjectiveContext:
    objective_type: MiningObjectiveType
    target_tile: Tile
    candidate_stand_tiles: set[Tile]


@dataclass(frozen=True)
class TacticalDecision:
    decision_type: TacticalDecisionType
    target_threat: MonsterThreat | None
    reason: str
    expires_at: float

    @property
    def is_active(self) -> bool:
        return time.time() < self.expires_at


class CombatTacticalResolver:
    """
    战术决策层。

    MonsterThreatEvaluator 只判断怪物风险；这里结合当前目标判断怪物是“危险”还是“路障”。
    """

    ENGAGE_COMMIT_SECONDS = 1.5
    AVOID_COMMIT_SECONDS = 0.8
    REROUTE_COMMIT_SECONDS = 0.6
    EMERGENCY_ENGAGE_DISTANCE = 2.0

    def resolve_for_mining(
        self,
        state: StardewState,
        threat_snapshot: MonsterThreatSnapshot,
        objective: MiningObjectiveContext,
    ) -> TacticalDecision:
        emergency_threat = self._find_emergency_engage_threat(threat_snapshot)
        if emergency_threat is not None:
            return self._decision(
                "ENGAGE",
                emergency_threat,
                f"怪物已经贴近玩家，主动战斗避免反复避让: distance={emergency_threat.distance_to_player}",
                self.ENGAGE_COMMIT_SECONDS,
            )

        blocking_threat = self._find_objective_blocking_threat(threat_snapshot, objective)
        if blocking_threat is not None:
            if objective.objective_type == "STONE":
                return self._decision(
                    "DEFER_OBJECTIVE",
                    blocking_threat,
                    f"怪物靠近采矿目标，暂缓当前石头: target={objective.target_tile}",
                    self.REROUTE_COMMIT_SECONDS,
                )

            return self._decision(
                "ENGAGE",
                blocking_threat,
                f"怪物阻挡关键目标，主动清除路障: objective={objective.objective_type}, target={objective.target_tile}",
                self.ENGAGE_COMMIT_SECONDS,
            )

        if self._has_risky_but_not_blocking_threat(threat_snapshot, objective):
            if objective.objective_type == "STONE":
                return self._decision(
                    "DEFER_OBJECTIVE",
                    threat_snapshot.nearest_threat,
                    f"怪物风险覆盖当前采矿站位，先换一块石头: target={objective.target_tile}",
                    self.REROUTE_COMMIT_SECONDS,
                )

            return self._decision(
                "REROUTE",
                threat_snapshot.nearest_threat,
                f"怪物靠近目标但未硬阻挡，优先绕路: objective={objective.objective_type}, target={objective.target_tile}",
                self.REROUTE_COMMIT_SECONDS,
            )

        return self._decision("IGNORE", None, "没有与当前目标冲突的怪物风险", 0.1)

    def _find_emergency_engage_threat(self, threat_snapshot: MonsterThreatSnapshot) -> MonsterThreat | None:
        for threat in threat_snapshot.threats:
            if threat.threat_level == "NONE":
                continue
            if threat.distance_to_player <= self.EMERGENCY_ENGAGE_DISTANCE:
                return threat
            if threat.monster.focused_on_farmer and threat.distance_to_player <= self.EMERGENCY_ENGAGE_DISTANCE + 1:
                return threat
        return None

    def _find_objective_blocking_threat(
        self,
        threat_snapshot: MonsterThreatSnapshot,
        objective: MiningObjectiveContext,
    ) -> MonsterThreat | None:
        objective_tiles = objective.candidate_stand_tiles | {objective.target_tile}
        for threat in threat_snapshot.threats:
            if threat.threat_level == "NONE":
                continue

            if threat.tile in objective_tiles:
                return threat

            if threat_snapshot.blocking_tiles.intersection(objective_tiles) and self._tile_distance(
                threat.tile,
                objective.target_tile,
            ) <= 3:
                return threat

            if threat.monster.focused_on_farmer and self._tile_distance(threat.tile, objective.target_tile) <= 3:
                return threat

            if threat.distance_to_player <= 2 and self._tile_distance(threat.tile, objective.target_tile) <= 4:
                return threat

        return None

    def _has_risky_but_not_blocking_threat(
        self,
        threat_snapshot: MonsterThreatSnapshot,
        objective: MiningObjectiveContext,
    ) -> bool:
        objective_tiles = objective.candidate_stand_tiles | {objective.target_tile}
        return any(tile in threat_snapshot.risk_tiles for tile in objective_tiles)

    def _decision(
        self,
        decision_type: TacticalDecisionType,
        target_threat: MonsterThreat | None,
        reason: str,
        commit_seconds: float,
    ) -> TacticalDecision:
        return TacticalDecision(
            decision_type=decision_type,
            target_threat=target_threat,
            reason=reason,
            expires_at=time.time() + commit_seconds,
        )

    def _tile_distance(self, start_tile: Tile, end_tile: Tile) -> int:
        return abs(start_tile.x - end_tile.x) + abs(start_tile.y - end_tile.y)
