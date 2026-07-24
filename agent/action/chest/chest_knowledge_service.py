import json
from typing import Callable

from agent.action.location.location import Location
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.behavior_tree.player_context import PlayerContext
from agent.memory.map_knowledge_cache import ChestContentItem, ChestContentKnowledge, ChestLocationKnowledge
from server.type import Tile


class ChestKnowledgeService:
    """
    Chest P2/P3 的场景内箱子知识服务。

    该类不是行为树节点，不推进 current_step_index；它只负责低频查询、解析结果和
    写入 MapKnowledgeCache。

    注意：游戏行为层面的 SCAN / 自动找箱必须由 ChestNode 逐个走到箱子旁、打开、
    查看、关闭来完成。这里的内容查询能力只应在箱子已经被行为节点打开后使用，
    不要把底层 QUERY_CHEST_CONTENT 当成“隔空遍历箱子”的游戏内行为。
    """

    def __init__(self, log: Callable[[str], None] | None = None) -> None:
        self._log = log or (lambda message: None)

    def query_chests(self, context: PlayerContext, location_name: Location) -> list[ChestLocationKnowledge] | None:
        response = context.executor_client.send_command(
            StardewCommand(
                action=StardewAction.QUERY_CHESTS,
                location_name=location_name,
            )
        )
        chests = self._parse_query_chests_response(response, location_name)
        if chests is None:
            self._log(f"QUERY_CHESTS 失败: location={location_name}, response={response}")
            return None

        context.map_knowledge_cache.remember_chest_locations(location_name, chests)
        self._log(
            f"写入箱子位置缓存: location={location_name}, "
            f"chests={self._format_chest_tiles([chest.tile for chest in chests])}"
        )
        return chests

    def query_chest_content(
        self,
        context: PlayerContext,
        location_name: Location,
        chest_tile: Tile,
    ) -> ChestContentKnowledge | None:
        response = context.executor_client.send_command(
            StardewCommand(
                action=StardewAction.QUERY_CHEST_CONTENT,
                location_name=location_name,
                tile=(chest_tile.x, chest_tile.y),
            )
        )
        chest_content = self._parse_query_chest_content_response(response, location_name, chest_tile)
        if chest_content is None:
            self._log(f"QUERY_CHEST_CONTENT 失败: location={location_name}, tile={chest_tile}, response={response}")
            return None

        context.map_knowledge_cache.remember_chest_content(chest_content)
        self._log(
            f"写入箱子内容缓存: location={location_name}, tile={chest_tile}, "
            f"items={self.format_chest_content_items(chest_content.items)}"
        )
        return chest_content

    def _parse_query_chests_response(
        self,
        response: str | None,
        fallback_location_name: Location,
    ) -> list[ChestLocationKnowledge] | None:
        if response is None:
            return None

        try:
            response_data = json.loads(response)
        except json.JSONDecodeError:
            return None

        if response_data.get("status") != "SUCCESS":
            return None

        location_name = response_data.get("location_name", fallback_location_name)
        chests: list[ChestLocationKnowledge] = []
        for raw_chest in response_data.get("chests", []):
            raw_tile = raw_chest.get("Tile") if isinstance(raw_chest, dict) else None
            if not isinstance(raw_tile, list) or len(raw_tile) < 2:
                continue
            chests.append(
                ChestLocationKnowledge.create(
                    location_name=location_name,
                    tile=Tile(int(raw_tile[0]), int(raw_tile[1])),
                    source="QUERY_CHESTS",
                )
            )
        return chests

    def _parse_query_chest_content_response(
        self,
        response: str | None,
        fallback_location_name: Location,
        fallback_chest_tile: Tile,
    ) -> ChestContentKnowledge | None:
        if response is None:
            return None

        try:
            response_data = json.loads(response)
        except json.JSONDecodeError:
            return None

        if response_data.get("status") != "SUCCESS":
            return None

        location_name = response_data.get("location_name", fallback_location_name)
        raw_tile = response_data.get("tile", [fallback_chest_tile.x, fallback_chest_tile.y])
        chest_tile = Tile(int(raw_tile[0]), int(raw_tile[1]))
        items: list[ChestContentItem] = []
        for raw_item in response_data.get("items", []):
            if not isinstance(raw_item, dict):
                continue
            items.append(
                ChestContentItem(
                    Name=str(raw_item.get("Name", "")),
                    DisplayName=str(raw_item.get("DisplayName", "")),
                    QualifiedItemId=str(raw_item.get("QualifiedItemId", "")),
                    Stack=int(raw_item.get("Stack", 0)),
                    Category=int(raw_item.get("Category", 0)),
                    IsTool=bool(raw_item.get("IsTool", False)),
                )
            )

        return ChestContentKnowledge.create(
            location_name=location_name,
            tile=chest_tile,
            items=items,
            source="QUERY_CHEST_CONTENT",
        )

    def format_chest_content_items(self, items: list[ChestContentItem]) -> str:
        if not items:
            return "[]"
        return "[" + ", ".join(f"{item.Name}({item.QualifiedItemId or 'id'}):{max(item.Stack, 1)}" for item in items) + "]"

    def _format_chest_tiles(self, tiles: list[Tile]) -> str:
        return str(sorted(tiles, key=lambda tile: (tile.x, tile.y)))
