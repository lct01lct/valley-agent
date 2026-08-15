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


class DeferredLootRecord:
    def __init__(
        self,
        owner: str,
        source_tile: Tile,
        source_type: str,
        loot_tiles: list[Tile],
        created_at: float,
        priority: str = "normal",
    ) -> None:
        self.owner = owner
        self.source_tile = source_tile
        self.source_type = source_type
        self.loot_tiles = loot_tiles
        self.created_at = created_at
        self.priority = priority
        # 预计由后续主任务路径/站位顺路磁吸覆盖的地块。
        # 当玩家已经到达这些地块，且掉落物静止不再被磁吸时，应转为主动拾取。
        self.expected_cover_tiles: set[Tile] = set()
        # 记录掉落物在预计磁吸覆盖点附近的运动状态。
        # 如果掉落物仍在移动，说明可能已经被磁吸，不应立刻打断主任务。
        self.debris_last_positions: dict[tuple[int, int], tuple[float, float]] = {}
        self.debris_last_distances: dict[tuple[int, int], float] = {}
        self.debris_stationary_started_at: dict[tuple[int, int], float] = {}

    @property
    def key(self) -> tuple[str, int, int, str]:
        return (
            self.owner,
            self.source_tile.x,
            self.source_tile.y,
            self.source_type,
        )


class UnreceivableLootRecord:
    def __init__(
        self,
        owner: str | None,
        location_name: str,
        source_tile: Tile | None,
        source_type: str | None,
        item_key: str,
        inventory_signature: tuple[tuple[str, int], ...],
        expires_at: float,
        reason: str,
    ) -> None:
        self.owner = owner
        self.location_name = location_name
        self.source_tile = source_tile
        self.source_type = source_type
        self.item_key = item_key
        self.inventory_signature = inventory_signature
        self.expires_at = expires_at
        self.reason = reason

    @property
    def key(self) -> tuple[str | None, str, int | None, int | None, str | None, str]:
        return (
            self.owner,
            self.location_name,
            None if self.source_tile is None else self.source_tile.x,
            None if self.source_tile is None else self.source_tile.y,
            self.source_type,
            self.item_key,
        )


class ResidualLootRecord:
    def __init__(
        self,
        owner: str | None,
        location_name: str,
        source_tile: Tile | None,
        source_type: str | None,
        observed_item_keys: set[str],
        remaining_tiles: list[Tile],
        recovery_target_tile: Tile | None,
        created_at: float,
    ) -> None:
        self.owner = owner
        self.location_name = location_name
        self.source_tile = source_tile
        self.source_type = source_type
        self.observed_item_keys = observed_item_keys
        self.remaining_tiles = remaining_tiles
        self.recovery_target_tile = recovery_target_tile
        self.created_at = created_at
        # 背包恢复会临时离开掉落物现场。InventoryRecoveryNode 在去箱子的过程中
        # 记录经过的 tile，恢复后 CollectLootNode 优先沿反向路径回到现场，
        # 避免重新求宽泛返回区域时误触发不相关清障。
        self.inventory_recovery_departure_path: list[Tile] = []

    @property
    def key(self) -> tuple[str | None, str, int | None, int | None, str | None]:
        return (
            self.owner,
            self.location_name,
            None if self.source_tile is None else self.source_tile.x,
            None if self.source_tile is None else self.source_tile.y,
            self.source_type,
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
        # CollectLootNode 优先处理可达的近距离掉落物；必要时可通过黑板转交 ClearObstacleNode 清理局部阻塞。
        # 普通树掉落物可能弹散到不可达位置，背包恢复后会先回 source 附近再按物品身份重定位。
        self.require_collect_loot = False
        self.collect_loot_owner: str | None = None
        self.collect_loot_source_tile: Tile | None = None
        self.collect_loot_source_type: str | None = None
        self.pending_loot_tiles: list[Tile] = []
        self.skipped_loot_tiles: set[tuple[int, int]] = set()
        # 延迟拾取记录：工具动作产生掉落物后，如果后续主任务路径/站位能覆盖磁吸范围，先不抢占主任务。
        self.deferred_loot_records: list[DeferredLootRecord] = []
        # 当前背包状态下无法接收的掉落物短期跳过记录。
        # 该记录不是永久黑名单；背包变化或过期后会重新评估。
        # 当前 InventoryRecoveryNode 会在背包满时先尝试任务感知型存箱/丢弃恢复。
        self.unreceivable_loot_records: list[UnreceivableLootRecord] = []
        # 背包恢复会把角色临时带离掉落物现场；恢复成功后，本轮 CollectLoot
        # 需要允许回到原掉落物区域完成拾取，不应用普通树“低成本拾取”的路径长度限制提前放弃。
        self.collect_loot_resume_after_inventory_recovery = False
        # 背包满导致拾取中断时记录残留掉落物上下文。
        # 恢复后优先回到 source 附近，再按物品身份和最新 state 重定位残留掉落物。
        self.collect_loot_residual_record: ResidualLootRecord | None = None
        # 最近一次 InventoryRecovery 从掉落物现场前往箱子/丢弃点的实际经过路径。
        self.inventory_recovery_departure_path: list[Tile] = []

        # 背包风险与恢复信号
        # InventoryPolicy 只做事实判断；实际恢复由业务节点、Planner 或未来 InventoryNode 消费这些字段。
        self.inventory_check_failed = False
        self.inventory_risk_level: str | None = None
        self.inventory_failure_reason: str | None = None
        self.inventory_recovery_hint: str | None = None
        self.inventory_recovery_strategy: str | None = None
        self.inventory_recovery_context: dict[str, Any] = {}
        self.inventory_recovery_task: BaseTask | None = None
        self.inventory_discard_candidates: list[dict[str, str | int]] = []

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
