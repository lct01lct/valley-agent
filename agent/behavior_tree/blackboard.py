from typing import List

from agent.base_task import BaseTask


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
