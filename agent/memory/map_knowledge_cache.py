import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol

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
    "QUERY_CHESTS",  # C# 低频查询返回的箱子位置事实
    "QUERY_CHEST_CONTENT",  # C# 低频查询返回的箱子内容快照
    "PLACED_BY_AGENT",  # 未来 Agent 自己制作并放置箱子时写入的位置事实
    "PLANNER_INTENT",  # 未来 Planner 给箱子定义用途标签时写入的语义事实
    "PERSISTENT_MEMORY",  # 未来从长期记忆加载的地图事实
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


class ItemRequestLike(Protocol):
    item_name: str
    count: int
    qualified_item_id: str | None


@dataclass
class ChestLocationKnowledge:
    location_name: Location
    tile: Tile
    source: MapFactSource
    first_seen_at: float
    updated_at: float
    confidence: float = 1.0

    @classmethod
    def create(
        cls,
        location_name: Location,
        tile: Tile,
        source: MapFactSource,
        confidence: float = 1.0,
    ) -> "ChestLocationKnowledge":
        now = time.time()
        return cls(
            location_name=location_name,
            tile=tile,
            source=source,
            first_seen_at=now,
            updated_at=now,
            confidence=confidence,
        )


@dataclass(frozen=True)
class ChestContentItem:
    # 以下字段名来自 C# / SMAPI 协议，保持原始大小写，便于未来长期记忆无损迁移。
    Name: str
    DisplayName: str
    QualifiedItemId: str
    Stack: int
    Category: int
    IsTool: bool


@dataclass
class ChestContentKnowledge:
    location_name: Location
    tile: Tile
    items: list[ChestContentItem]
    source: MapFactSource
    updated_at: float
    is_stale: bool = False
    stale_reason: str | None = None

    @classmethod
    def create(
        cls,
        location_name: Location,
        tile: Tile,
        items: list[ChestContentItem],
        source: MapFactSource,
    ) -> "ChestContentKnowledge":
        return cls(
            location_name=location_name,
            tile=tile,
            items=items,
            source=source,
            updated_at=time.time(),
        )


@dataclass
class ChestSemanticMemory:
    """
    未来长期记忆预留：描述“这个箱子打算用来放什么”，不代表箱子真实内容。

    当前阶段只定义结构和缓存接口，不参与 ChestNode 决策。
    """

    location_name: Location
    tile: Tile
    labels: set[str]
    intended_items: set[str]
    source: MapFactSource
    updated_at: float
    confidence: float = 0.5


class MapKnowledgeCache:
    """
    当前运行期地图知识缓存。

    这层不是实时 state，也不是长期落盘记忆；它保存 Agent 在当前运行中见过或低频查询到的地图事实。
    当前实际使用水源事实；采集物、箱子和交互点先保留通用接口，等待后续模块接入。
    """

    def __init__(self) -> None:
        self._facts: dict[tuple[Location, MapFactType, int, int, str], MapFact] = {}
        self._chest_locations: dict[tuple[Location, int, int], ChestLocationKnowledge] = {}
        self._chest_contents: dict[tuple[Location, int, int], ChestContentKnowledge] = {}
        self._chest_semantics: dict[tuple[Location, int, int], ChestSemanticMemory] = {}

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

    def remember_chest_locations(
        self,
        location_name: Location,
        chests: list[ChestLocationKnowledge],
    ) -> None:
        for chest in chests:
            if chest.location_name != location_name:
                continue

            chest_key = self._build_chest_key(chest.location_name, chest.tile)
            previous_chest = self._chest_locations.get(chest_key)
            if previous_chest is None:
                self._chest_locations[chest_key] = chest
            else:
                previous_chest.updated_at = max(previous_chest.updated_at, chest.updated_at)
                previous_chest.source = chest.source
                previous_chest.confidence = chest.confidence

            self.remember_fact(
                MapFact.create(
                    location_name=chest.location_name,
                    tile=chest.tile,
                    fact_type="CHEST",
                    name="Chest",
                    source=chest.source,
                    status="VERIFIED",
                    confidence=chest.confidence,
                )
            )

    def get_chest_locations(self, location_name: Location) -> list[ChestLocationKnowledge]:
        return sorted(
            [
                chest
                for chest in self._chest_locations.values()
                if chest.location_name == location_name
            ],
            key=lambda chest: (chest.tile.x, chest.tile.y),
        )

    def remember_chest_content(self, chest_content: ChestContentKnowledge) -> None:
        chest_key = self._build_chest_key(chest_content.location_name, chest_content.tile)
        self._chest_contents[chest_key] = chest_content

        if chest_key not in self._chest_locations:
            self._chest_locations[chest_key] = ChestLocationKnowledge.create(
                location_name=chest_content.location_name,
                tile=chest_content.tile,
                source=chest_content.source,
            )

    def get_chest_content(
        self,
        location_name: Location,
        tile: Tile,
        include_stale: bool = False,
    ) -> ChestContentKnowledge | None:
        chest_content = self._chest_contents.get(self._build_chest_key(location_name, tile))
        if chest_content is None:
            return None
        if chest_content.is_stale and not include_stale:
            return None
        return chest_content

    def get_chest_contents(
        self,
        location_name: Location,
        include_stale: bool = False,
    ) -> list[ChestContentKnowledge]:
        contents = [
            chest_content
            for chest_content in self._chest_contents.values()
            if chest_content.location_name == location_name
        ]
        if not include_stale:
            contents = [chest_content for chest_content in contents if not chest_content.is_stale]
        return sorted(contents, key=lambda chest_content: (chest_content.tile.x, chest_content.tile.y))

    def mark_chest_content_stale(self, location_name: Location, tile: Tile, reason: str) -> None:
        chest_content = self._chest_contents.get(self._build_chest_key(location_name, tile))
        if chest_content is None:
            return

        chest_content.is_stale = True
        chest_content.stale_reason = reason
        chest_content.updated_at = time.time()

    def remember_chest_semantic(self, chest_semantic: ChestSemanticMemory) -> None:
        self._chest_semantics[self._build_chest_key(chest_semantic.location_name, chest_semantic.tile)] = chest_semantic

    def get_chest_semantic(self, location_name: Location, tile: Tile) -> ChestSemanticMemory | None:
        return self._chest_semantics.get(self._build_chest_key(location_name, tile))

    def find_chests_containing_items(
        self,
        location_name: Location,
        item_requests: list[ItemRequestLike],
        player_tile: Tile | None = None,
    ) -> list[ChestContentKnowledge]:
        matched_contents = [
            chest_content
            for chest_content in self.get_chest_contents(location_name)
            if self._does_chest_content_satisfy_requests(chest_content, item_requests)
        ]
        return sorted(
            matched_contents,
            key=lambda chest_content: (
                self._get_tile_distance(player_tile, chest_content.tile) if player_tile is not None else 0,
                chest_content.tile.x,
                chest_content.tile.y,
            ),
        )

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

    def _build_chest_key(self, location_name: Location, tile: Tile) -> tuple[Location, int, int]:
        return (location_name, tile.x, tile.y)

    def _does_chest_content_satisfy_requests(
        self,
        chest_content: ChestContentKnowledge,
        item_requests: list[ItemRequestLike],
    ) -> bool:
        for item_request in item_requests:
            if self._count_items_in_chest_content(chest_content, item_request) < item_request.count:
                return False
        return True

    def _count_items_in_chest_content(
        self,
        chest_content: ChestContentKnowledge,
        item_request: ItemRequestLike,
    ) -> int:
        total_count = 0
        for item in chest_content.items:
            if item_request.qualified_item_id is not None:
                if item.QualifiedItemId != item_request.qualified_item_id:
                    continue
            elif item.Name != item_request.item_name and item.DisplayName != item_request.item_name:
                continue
            total_count += max(item.Stack, 1)
        return total_count

    def _get_tile_distance(self, start_tile: Tile | None, end_tile: Tile) -> int:
        if start_tile is None:
            return 0
        return abs(start_tile.x - end_tile.x) + abs(start_tile.y - end_tile.y)
