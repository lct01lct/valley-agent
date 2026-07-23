from agent.action.location.location import Location
from agent.memory.map_knowledge_cache import MapFact


class PersistentMemoryStore:
    """
    长期记忆预留接口。

    当前阶段不实现、不调用。未来可用于把部分 MapKnowledgeCache 写入磁盘或数据库，例如：
    - 稳定水源位置
    - 常用箱子位置和用途
    - 商店柜台位置
    - 某季节常见采集物区域

    注意：长期记忆只能作为线索，不能直接当作当前游戏事实。
    使用前必须通过 state 或低频查询动作重新验证。
    """

    def load_scene_facts(self, location_name: Location) -> list[MapFact]:
        raise NotImplementedError("长期记忆尚未实现，当前阶段不要调用。")

    def save_scene_facts(self, location_name: Location, facts: list[MapFact]) -> None:
        raise NotImplementedError("长期记忆尚未实现，当前阶段不要调用。")
