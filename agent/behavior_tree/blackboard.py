from typing import List

from agent.base_task import BaseTask
from server.type import Tile


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
        self.clear_obstacle_tile: Tile | None = None
        self.clear_obstacle_type: str | None = None
        self.failed_clear_obstacles: set[tuple[int, int]] = set()
