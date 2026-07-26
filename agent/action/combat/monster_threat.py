from dataclasses import dataclass
from typing import Literal

from server.type import Tile
from server.valley_server import MonsterState, StardewState


type ThreatLevel = Literal[
    "NONE",  # 无威胁：怪物未触发仇恨，且玩家不在基础感知范围内。
    "WATCH",  # 观察风险：玩家进入怪物基础感知范围，但暂不需要抢占当前任务。
    "AVOID",  # 避让风险：怪物已接近或已触发仇恨，优先让路径绕开。
    "FIGHT",  # 战斗风险：怪物贴脸或阻挡任务，需要 Defend 抢占并攻击。
    "BLOCK",  # 阻断风险：怪物威胁极高，应视为移动阻挡或高风险源。
]


@dataclass(frozen=True)
class MonsterThreat:
    monster: MonsterState
    distance_to_player: float
    threat_score: float
    threat_level: ThreatLevel

    @property
    def tile(self) -> Tile:
        return self.monster.tile


@dataclass(frozen=True)
class MonsterThreatSnapshot:
    threats: list[MonsterThreat]
    max_threat_score: float
    nearest_threat: MonsterThreat | None
    blocking_tiles: set[Tile]
    risk_tiles: dict[Tile, float]

    @property
    def has_defend_threat(self) -> bool:
        return any(threat.threat_level in ("FIGHT", "BLOCK") for threat in self.threats)


class MonsterThreatEvaluator:
    """
    怪物威胁评估层。

    这里只判断“风险是什么”，不负责攻击、躲避或任务推进。
    后续可以把玩家血量、体力、怪物速度和长期记忆逐步接进这一层。
    """

    FIGHT_DISTANCE = 1.0
    AVOID_DISTANCE = 3.0
    DEFAULT_SEARCH_RADIUS = 6

    def evaluate(self, state: StardewState) -> MonsterThreatSnapshot:
        threats = [
            self._evaluate_monster(state, monster)
            for monster in state.monsters
            if not monster.is_dead and not monster.is_invisible
        ]
        threats.sort(key=lambda threat: threat.threat_score, reverse=True)

        blocking_tiles: set[Tile] = set()
        risk_tiles: dict[Tile, float] = {}
        for threat in threats:
            self._add_risk_tiles(threat, risk_tiles, blocking_tiles)

        nearest_threat = min(threats, key=lambda threat: threat.distance_to_player) if threats else None
        max_threat_score = threats[0].threat_score if threats else 0.0
        return MonsterThreatSnapshot(
            threats=threats,
            max_threat_score=max_threat_score,
            nearest_threat=nearest_threat,
            blocking_tiles=blocking_tiles,
            risk_tiles=risk_tiles,
        )

    def _evaluate_monster(self, state: StardewState, monster: MonsterState) -> MonsterThreat:
        distance = self._tile_distance(state.player_tile, monster.tile)
        search_radius = max(monster.search_array_size, self.DEFAULT_SEARCH_RADIUS)
        in_search_range = distance <= search_radius

        if not monster.focused_on_farmer and not in_search_range:
            return MonsterThreat(monster=monster, distance_to_player=distance, threat_score=0.0, threat_level="NONE")

        distance_factor = max(0.0, 1.0 - distance / max(search_radius, 1))
        aggro_factor = 2.0 if monster.focused_on_farmer else 1.0
        damage_factor = 1.0 + max(monster.damage_to_farmer or 0, 0) / 10.0
        health_factor = 1.0 + max(monster.health, 0) / 100.0
        threat_score = distance_factor * aggro_factor * damage_factor * health_factor

        threat_level: ThreatLevel = "WATCH"
        if monster.focused_on_farmer:
            threat_level = "AVOID"
        if distance <= self.FIGHT_DISTANCE:
            threat_level = "FIGHT"
        elif threat_score >= 3.0:
            threat_level = "BLOCK"
        elif threat_score >= 1.5 or distance <= self.AVOID_DISTANCE:
            threat_level = "AVOID"

        return MonsterThreat(
            monster=monster,
            distance_to_player=distance,
            threat_score=threat_score,
            threat_level=threat_level,
        )

    def _add_risk_tiles(
        self,
        threat: MonsterThreat,
        risk_tiles: dict[Tile, float],
        blocking_tiles: set[Tile],
    ) -> None:
        monster_tile = threat.tile
        risk_tiles[monster_tile] = max(risk_tiles.get(monster_tile, 0.0), threat.threat_score + 10.0)
        blocking_tiles.add(monster_tile)

        if threat.monster.focused_on_farmer or threat.threat_level in ("FIGHT", "BLOCK"):
            for tile in self._neighbor_tiles(monster_tile):
                risk_tiles[tile] = max(risk_tiles.get(tile, 0.0), threat.threat_score + 3.0)
                if threat.threat_level in ("FIGHT", "BLOCK"):
                    blocking_tiles.add(tile)
            return

        if threat.threat_level in ("WATCH", "AVOID"):
            for tile in self._neighbor_tiles(monster_tile):
                risk_tiles[tile] = max(risk_tiles.get(tile, 0.0), threat.threat_score + 1.0)

    def _neighbor_tiles(self, tile: Tile) -> set[Tile]:
        return {
            Tile(tile.x + 1, tile.y),
            Tile(tile.x - 1, tile.y),
            Tile(tile.x, tile.y + 1),
            Tile(tile.x, tile.y - 1),
        }

    def _tile_distance(self, start_tile: Tile, end_tile: Tile) -> int:
        return abs(start_tile.x - end_tile.x) + abs(start_tile.y - end_tile.y)
