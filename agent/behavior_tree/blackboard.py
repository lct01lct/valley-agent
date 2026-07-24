from typing import List

from agent.action.location.location import Location
from agent.base_task import BaseTask
from server.type import Tile


class BorrowedChestItem:
    def __init__(
        self,
        location_name: Location,
        chest_tile: Tile,
        item_name: str,
        count: int,
        qualified_item_id: str | None = None,
    ) -> None:
        self.location_name = location_name
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

        # 清理可破坏障碍物
        self.require_clear_obstacle = False
        self.clear_obstacle_owner: str | None = None
        self.clear_obstacle_tile: Tile | None = None
        self.clear_obstacle_type: str | None = None
        self.failed_clear_obstacles: set[tuple[int, int]] = set()

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
