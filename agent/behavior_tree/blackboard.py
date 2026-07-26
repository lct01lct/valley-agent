from typing import Any, List, Literal

from agent.action.location.location import Location
from agent.base_task import BaseTask
from server.type import Tile

type InteractionOwner = Literal[
    "Route",  # 路线/传送/建筑入口等移动交互
    "Chest",  # 箱子打开、取物、存物等箱子菜单交互
    "Farm",  # 农业模块主动触发的工具/物品/确认菜单交互
    "Mining",  # 采矿模块主动触发的矿洞入口、梯子、工具动作反馈
    "Npc",  # 未来 NPC 对话、送礼和任务对话
    "Shop",  # 未来商店购买/出售菜单
    "Guard",  # Guard 自身发起的保护性 UI 处理
]

type FeedbackEventType = Literal[
    "LOCATION_CLOSED",  # 建筑或地点打烊，当前 Route/交互任务需要重新规划
    "LOCKED_DOOR",  # 门、建筑或入口上锁，当前交互无法继续
    "TOOL_REWARD_NOTICE",  # 使用工具后出现的奖励/提示，例如挖到晶球
    "BLOCKING_DIALOG",  # 普通阻塞对话或提示框，关闭后通常可继续原任务
    "UNKNOWN_BLOCKING_UI",  # 未分类阻塞 UI，保守关闭并记录上下文
]


class InteractionSession:
    def __init__(
        self,
        owner: InteractionOwner,
        intent: str,
        target_name: str | None = None,
        target_tile: Tile | None = None,
        expected_menu_types: set[str] | None = None,
    ) -> None:
        self.owner = owner
        self.intent = intent
        self.target_name = target_name
        self.target_tile = target_tile
        self.expected_menu_types = expected_menu_types or set()

    def matches_menu_type(self, menu_type: str | None) -> bool:
        return bool(menu_type) and menu_type in self.expected_menu_types


class ActionFeedbackEvent:
    def __init__(
        self,
        event_type: FeedbackEventType,
        source_owner: InteractionOwner | None = None,
        text: str = "",
        target_name: str | None = None,
        target_tile: Tile | None = None,
        should_replan: bool = False,
    ) -> None:
        self.event_type = event_type
        self.source_owner = source_owner
        self.text = text
        self.target_name = target_name
        self.target_tile = target_tile
        self.should_replan = should_replan


class BorrowedChestItem:
    def __init__(
        self,
        location_name: Location,
        chest_tile: Tile,
        item_name: str,
        count: int,
        qualified_item_id: str | None = None,
    ) -> None:
        self.location_name: Location = location_name
        self.chest_tile = chest_tile
        self.item_name = item_name
        self.count = count
        self.qualified_item_id = qualified_item_id

    @property
    def key(self) -> tuple[Location, int, int, str, str | None]:
        return (
            self.location_name,
            self.chest_tile.x,
            self.chest_tile.y,
            self.item_name,
            self.qualified_item_id,
        )


class AgentBlackboard:
    def __init__(self):
        self.macro_plan: List[BaseTask] = []
        self.current_step_index = 0

        # 🚦 异步 llm 标志位
        self.is_llm_thinking = False  # 大模型是否正在高维思考中
        self.new_plan_received = False  # 是否收到了刚出炉的新计划
        self.prompt = ""

        # 开门
        self.require_open_door = False
        self.is_opening_door = False
        self.should_reset_route = False

        # 全局 UI / 交互反馈
        # InteractionSession 用于标记“当前菜单属于哪个业务节点”，避免 Guard 误关 NPC/商店/箱子等预期 UI。
        self.pending_interaction: InteractionSession | None = None
        # ActionFeedbackEvent 用于把打烊、上锁、晶球提示等 UI 文本转换为结构化事件。
        self.action_feedback_event: ActionFeedbackEvent | None = None

        # 清理可破坏障碍物
        self.require_clear_obstacle = False
        self.clear_obstacle_owner: str | None = None
        self.clear_obstacle_tile: Tile | None = None
        self.clear_obstacle_type: str | None = None
        self.failed_clear_obstacles: set[tuple[int, int]] = set()

        # 工具动作后自动拾取
        # CollectLootNode 只处理可达的近距离掉落物，不为拾取触发清障。
        # 普通树掉落物可能弹散到不可达位置，因此允许部分拾取并跳过不可达掉落物。
        self.require_collect_loot = False
        self.collect_loot_owner: str | None = None
        self.collect_loot_source_tile: Tile | None = None
        self.collect_loot_source_type: str | None = None
        self.pending_loot_tiles: list[Tile] = []
        self.skipped_loot_tiles: set[tuple[int, int]] = set()

        # 切换工具
        self.require_switch_tool = False
        self.required_tool_owner: str | None = None
        self.is_switching_tool = False
        self.required_tool: str | None = None

        # 水壶补水
        self.require_refill_watering_can = False
        self.refill_watering_can_owner: str | None = None
        self.refill_water_source_tile: Tile | None = None

        # Farm 资源检查
        self.farm_resource_check_failed = False
        self.farm_missing_resources: list[str] = []
        self.farm_missing_chest_items: list[dict[str, str | int | None]] = []
        self.farm_resource_recovery_hint: str | None = None
        self.farm_recovery_task: BaseTask | None = None

        # Chest 任务级借用记录
        # 记录本轮计划中“从哪个箱子取出了哪些工具”，用于 Farm 结束后把工具放回原箱子。
        # 这是临时调度事实，不等同于长期 ChestSemanticMemory。
        self.borrowed_chest_items: list[BorrowedChestItem] = []

        # 战术决策
        # 由 Mining 等业务节点写入，Guard/DefendNode 消费。
        # 这里用 Any 避免 blackboard 与 combat action 层形成强耦合。
        self.combat_tactical_decision: Any | None = None
