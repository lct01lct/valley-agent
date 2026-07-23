import time
from dataclasses import dataclass
from typing import Any, Literal

from agent.action.location.location import Location
from server.type import Tile


type MapFactType = Literal[
    "WATER_SOURCE",  # 可补水水源，例如农场池塘、河流、湖泊
    "FORAGE_ITEM",  # 路上观察到但未立即采集的觅食物
    "CHEST",  # 场景内已知箱子位置
    "RESOURCE_NODE",  # 可采集或可破坏资源点，例如石头、树枝、杂草
    "INTERACTION_POINT",  # 商店柜台、设备、床等固定交互点
]

type MapFactSource = Literal[
    "OBSERVER_STATE",  # 高频/低频 state 中观察到的事实
    "QUERY_WATER_SOURCES",  # C# 低频查询返回的水源事实
    "MANUAL",  # 开发者或任务显式注入的事实
]

type MapFactStatus = Literal[
    "SEEN",  # 曾经看见过，尚未处理
    "RESERVED",  # 已被当前或未来计划占用
    "VERIFIED",  # 最近重新确认仍然存在
    "COLLECTED",  # 已采集、已使用或已处理
    "INVALID",  # 回到现场验证时发现不存在
    "BLOCKED",  # 暂时不可达，未来可重新尝试
]


@dataclass
class MapFact:
    location_name: Location
    tile: Tile
    fact_type: MapFactType
    name: str
    source: MapFactSource
    first_seen_at: float
    last_seen_at: float
    last_verified_at: float | None = None
    status: MapFactStatus = "SEEN"
    confidence: float = 1.0

    @classmethod
    def create(
        cls,
        location_name: Location,
        tile: Tile,
        fact_type: MapFactType,
        name: str,
        source: MapFactSource,
        status: MapFactStatus = "SEEN",
        confidence: float = 1.0,
    ) -> "MapFact":
        now = time.time()
        return cls(
            location_name=location_name,
            tile=tile,
            fact_type=fact_type,
            name=name,
            source=source,
            first_seen_at=now,
            last_seen_at=now,
            last_verified_at=now if status == "VERIFIED" else None,
            status=status,
            confidence=confidence,
        )


class MapKnowledgeCache:
    """
    当前运行期地图知识缓存。

    这层不是实时 state，也不是长期落盘记忆；它保存 Agent 在当前运行中见过或低频查询到的地图事实。
    当前实际使用水源事实；采集物、箱子和交互点先保留通用接口，等待后续模块接入。
    """

    def __init__(self) -> None:
        self._facts: dict[tuple[Location, MapFactType, int, int, str], MapFact] = {}

    def remember_from_state(self, state: Any) -> None:
        """
        从当前 state 中提取值得记忆的地图事实。

        当前阶段暂不接入调用。未来可在 PlayerContext.update() 中启用，用于记住路过的采集物、
        箱子、资源点等“机会记忆”。启用前应先明确对应 state 字段和失效策略。
        """
        return None

    def remember_query_result(
        self,
        location_name: Location,
        fact_type: MapFactType,
        facts: list[MapFact],
    ) -> None:
        for fact in facts:
            if fact.location_name != location_name or fact.fact_type != fact_type:
                continue
            self.remember_fact(fact)

    def remember_fact(self, fact: MapFact) -> None:
        fact_key = self._build_fact_key(fact.location_name, fact.fact_type, fact.tile, fact.name)
        previous_fact = self._facts.get(fact_key)
        if previous_fact is None:
            self._facts[fact_key] = fact
            return

        previous_fact.last_seen_at = max(previous_fact.last_seen_at, fact.last_seen_at)
        previous_fact.source = fact.source
        previous_fact.status = fact.status
        previous_fact.confidence = fact.confidence
        if fact.last_verified_at is not None:
            previous_fact.last_verified_at = fact.last_verified_at

    def get_facts(
        self,
        location_name: Location,
        fact_type: MapFactType,
        include_invalid: bool = False,
    ) -> list[MapFact]:
        facts = [
            fact
            for fact in self._facts.values()
            if fact.location_name == location_name and fact.fact_type == fact_type
        ]
        if not include_invalid:
            facts = [fact for fact in facts if fact.status != "INVALID"]
        return sorted(facts, key=lambda fact: (fact.tile.x, fact.tile.y, fact.name))

    def get_water_sources(self, location_name: Location) -> set[Tile]:
        return {fact.tile for fact in self.get_facts(location_name, "WATER_SOURCE")}

    def mark_fact_status(
        self,
        location_name: Location,
        tile: Tile,
        fact_type: MapFactType,
        status: MapFactStatus,
        name: str = "",
    ) -> None:
        fact_key = self._build_fact_key(location_name, fact_type, tile, name)
        fact = self._facts.get(fact_key)
        if fact is None:
            return

        fact.status = status
        if status == "VERIFIED":
            fact.last_verified_at = time.time()

    def forget_dynamic_facts_for_new_day(self) -> None:
        """
        新的一天清理动态机会记忆。

        水源、箱子、交互点通常是稳定地图知识，不在这里清理；采集物和资源点会随日期变化，
        后续接入新一天检测后可调用该方法。
        """
        dynamic_fact_types: set[MapFactType] = {"FORAGE_ITEM", "RESOURCE_NODE"}
        self._facts = {
            fact_key: fact for fact_key, fact in self._facts.items() if fact.fact_type not in dynamic_fact_types
        }

    def _build_fact_key(
        self,
        location_name: Location,
        fact_type: MapFactType,
        tile: Tile,
        name: str,
    ) -> tuple[Location, MapFactType, int, int, str]:
        return (location_name, fact_type, tile.x, tile.y, name)
