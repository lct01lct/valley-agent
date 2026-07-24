import asyncio
import os
import time
from typing import List, cast

from langchain.chat_models import init_chat_model
from langchain_google_genai import ChatGoogleGenerativeAI

from agent.base_task import BaseTask, TaskType
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.farm_node import FarmTask
from agent.behavior_tree.route_node import RouteTask
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.test.task_mock_data import TASK_MOCK_DATA


class Agent_Model:
    def __init__(self) -> None:
        self.is_mock_data = True

    def initialize(self):
        self.chat_model = self._create_chat_model()
        print("🛠️ [System]：chat 模型初始化成功")

        self.vlm_model = self._create_vlm_model()
        print("🛠️ [System]：vlm 模型初始化成功")

    def _create_chat_model(self):
        self.model_name = "google_genai:gemini-3.1-flash-lite"
        self.api_key = cast(str, os.getenv("GOOGLE_API_KEY"))

        return init_chat_model(model=self.model_name, api_key=self.api_key)

    def _create_vlm_model(self):
        self.vlm_model_name = "gemini-3.1-flash-lite"
        # self.vlm_model_name = "gemini-3.5-flash"
        return ChatGoogleGenerativeAI(
            model=self.vlm_model_name,
            temperature=0.0,
            google_api_key=self.api_key,
        )

    async def run(self, prompt: str, ctx: PlayerContext) -> List[BaseTask]:
        TEST_MODE: TaskType = "ROUTE"
        TEST_MODE: TaskType = "FARM"
        TEST_MODE: TaskType = "CHEST"

        if self.is_mock_data:
            await asyncio.sleep(2.0)
            if TEST_MODE == "ROUTE":
                if not "打烊" in prompt:
                    return TASK_MOCK_DATA["ROUTE_1"]
                return TASK_MOCK_DATA["ROUTE_BACKUP"]
                # return TASK_MOCK_DATA["ROUTE_3"]
                # return TASK_MOCK_DATA["ROUTE_4"]
            elif TEST_MODE == "CHEST":
                return TASK_MOCK_DATA["CHEST_P0_1"]
                # return TASK_MOCK_DATA["CHEST_P0_2"]
                # return TASK_MOCK_DATA["CHEST_P1_1"]
            elif TEST_MODE == "FARM":
                # return TASK_MOCK_DATA["FARM_P0_1"]
                # return TASK_MOCK_DATA["FARM_P0_2"]
                # return TASK_MOCK_DATA["FARM_P0_3"]
                # return TASK_MOCK_DATA["FARM_P0_4"]
                # return TASK_MOCK_DATA["FARM_P1_1"]
                return TASK_MOCK_DATA["FARM_P1_2"]
            else:
                return []
        else:
            return []


class LLM_Node(BTNode):
    def __init__(self, agent_instance):
        self.agent = agent_instance

    async def run(self, blackboard: AgentBlackboard, ctx: PlayerContext) -> NodeStatus:

        # 1：就在这一帧，后台的异步大模型终于把结果送到了！
        if blackboard.is_llm_thinking and blackboard.new_plan_received:
            print("🟢 [LLM_Node] 收到后台注入的新战略，解除思考锁，让开通道！")
            blackboard.is_llm_thinking = False
            blackboard.new_plan_received = False

            # 返回 FAILURE 是为了让 Selector 重新从最左侧（高优先级）开始扫描
            # 从而点亮赶路、种田等新灌进来的节点
            return "FAILURE"

        # 2. 如果大模型已经在后台卡网络请求，身体在这一帧继续保持警戒
        if blackboard.is_llm_thinking:
            return "RUNNING"

        # 3. 如果没在思考，且黑板里的计划全部被消费完了（或者破产了）
        if len(blackboard.macro_plan) == 0:
            blackboard.is_llm_thinking = True
            blackboard.new_plan_received = False

            async def async_worker():
                tasks = await self.agent.models.run(blackboard.prompt, ctx)
                blackboard.macro_plan = tasks
                blackboard.current_step_index = 0
                blackboard.new_plan_received = True

                blackboard.prompt = ""

            # 【绝对不加 await】，让它自己去后台跑
            asyncio.create_task(async_worker())

            return "RUNNING"

        return "FAILURE"  # 还有计划在干，放行控制流
