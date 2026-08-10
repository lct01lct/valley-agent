import asyncio
import os
import time
from typing import List, cast

from langchain.chat_models import init_chat_model
from langchain_google_genai import ChatGoogleGenerativeAI

from agent.base_task import BaseTask, TaskType
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.chest_node import ChestItemRequest, ChestTask
from agent.behavior_tree.farm_node import FarmTask
from agent.behavior_tree.route_node import RouteTask
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.test.task_mock_data import TASK_MOCK_DATA

FARM_BORROWABLE_TOOL_NAMES: tuple[str, ...] = ("Axe", "Hoe", "Pickaxe", "Scythe", "Watering Can")


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
        TEST_MODE: TaskType = "MINE"

        if self.is_mock_data:
            await asyncio.sleep(2.0)
            if TEST_MODE == "ROUTE":
                # if not self._is_route_unavailable_prompt(prompt):
                #     return TASK_MOCK_DATA["ROUTE_1"]
                # return TASK_MOCK_DATA["ROUTE_BACKUP"]
                return TASK_MOCK_DATA["ROUTE_3"]
                # return TASK_MOCK_DATA["ROUTE_4"]
            elif TEST_MODE == "CHEST":
                return TASK_MOCK_DATA["CHEST_P2_P3_1"]
                # return TASK_MOCK_DATA["CHEST_P0_1"]
                # return TASK_MOCK_DATA["CHEST_P0_2"]
                # return TASK_MOCK_DATA["CHEST_P1_1"]
            elif TEST_MODE == "FARM":
                # return TASK_MOCK_DATA["FARM_P0_1"]
                # return TASK_MOCK_DATA["FARM_P0_2"]
                # return TASK_MOCK_DATA["FARM_P0_3"]
                # return TASK_MOCK_DATA["FARM_P0_4"]
                # return TASK_MOCK_DATA["FARM_P1_1"]
                # return TASK_MOCK_DATA["FARM_P1_2"]
                return TASK_MOCK_DATA["FARM_P1_3"]
            elif TEST_MODE == "MINE":
                # return TASK_MOCK_DATA["MINING_P0_1"]
                return TASK_MOCK_DATA["MINING_P0_2"]
            else:
                return []
        else:
            return []

    def _is_route_unavailable_prompt(self, prompt: str) -> bool:
        return any(keyword in prompt for keyword in ("打烊", "关门", "营业时间", "上锁", "锁住"))


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
                tasks = self._build_mock_route_recovery_plan(blackboard)
                if tasks is None:
                    tasks = self._build_mock_farm_resource_recovery_plan(blackboard)
                if tasks is None:
                    tasks = await self.agent.models.run(blackboard.prompt, ctx)
                blackboard.macro_plan = tasks
                blackboard.current_step_index = 0
                blackboard.new_plan_received = True

                blackboard.prompt = ""

            # 【绝对不加 await】，让它自己去后台跑
            asyncio.create_task(async_worker())

            return "RUNNING"

        return "FAILURE"  # 还有计划在干，放行控制流

    def _build_mock_route_recovery_plan(self, blackboard: AgentBlackboard) -> list[BaseTask] | None:
        if not self.agent.models.is_mock_data:
            return None

        feedback_event = blackboard.action_feedback_event
        if feedback_event is None:
            return None
        if not feedback_event.should_replan:
            return None
        if feedback_event.event_type not in ("LOCATION_CLOSED", "LOCKED_DOOR"):
            return None

        print(
            "\n🟢 [LLM_Node] Route 入口交互失败，生成 ROUTE_BACKUP mock 恢复计划："
            f"type={feedback_event.event_type}, text={feedback_event.text}"
        )
        blackboard.action_feedback_event = None
        return TASK_MOCK_DATA["ROUTE_BACKUP"]

    def _build_mock_farm_resource_recovery_plan(self, blackboard: AgentBlackboard) -> list[BaseTask] | None:
        if not self.agent.models.is_mock_data:
            return None
        if not blackboard.farm_resource_check_failed:
            return None
        if blackboard.farm_recovery_task is None:
            return None
        if not isinstance(blackboard.farm_recovery_task, FarmTask):
            return None

        missing_chest_items = [
            ChestItemRequest(
                item_name=str(raw_item["item_name"]),
                count=int(raw_item["count"] or 0),
                qualified_item_id=str(raw_item["qualified_item_id"]) if raw_item.get("qualified_item_id") else None,
            )
            for raw_item in blackboard.farm_missing_chest_items
            if raw_item.get("item_name") and int(raw_item.get("count") or 0) > 0
        ]
        if not missing_chest_items:
            return None

        recovery_farm_task = blackboard.farm_recovery_task
        chest_tasks = self._build_mock_farm_resource_chest_tasks(recovery_farm_task, missing_chest_items)
        return_tool_tasks = self._build_mock_return_borrowed_tool_tasks(recovery_farm_task, missing_chest_items)
        print(
            "\n🟢 [LLM_Node] Farm 资源缺失，生成 Chest P4 mock 恢复计划："
            f"items={[f'{item.item_name}:{item.count}' for item in missing_chest_items]}, "
            f"chest_tasks={len(chest_tasks)}, return_tool_tasks={len(return_tool_tasks)}"
        )
        blackboard.farm_resource_check_failed = False
        blackboard.farm_missing_resources = []
        blackboard.farm_missing_chest_items = []
        blackboard.farm_resource_recovery_hint = None
        blackboard.farm_recovery_task = None

        return [
            RouteTask(task_type="ROUTE", desc="前往农场箱子所在场景", target_loc=recovery_farm_task.target_loc),
            *chest_tasks,
            recovery_farm_task,
            *return_tool_tasks,
        ]

    def _build_mock_farm_resource_chest_tasks(
        self,
        recovery_farm_task: FarmTask,
        missing_chest_items: list[ChestItemRequest],
    ) -> list[ChestTask]:
        tool_items = [item for item in missing_chest_items if item.qualified_item_id is None]
        seed_or_stack_items = [item for item in missing_chest_items if item.qualified_item_id is not None]

        chest_tasks: list[ChestTask] = []
        if tool_items:
            chest_tasks.append(
                ChestTask(
                    task_type="CHEST",
                    desc="根据 Farm 缺失资源自动从当前场景箱子取工具",
                    chest_action="TAKE",
                    target_loc=recovery_farm_task.target_loc,
                    chest_tile=None,
                    items=tool_items,
                )
            )

        for item in seed_or_stack_items:
            chest_tasks.append(
                ChestTask(
                    task_type="CHEST",
                    desc=f"根据 Farm 缺失资源自动从当前场景箱子取 {item.item_name}",
                    chest_action="TAKE",
                    target_loc=recovery_farm_task.target_loc,
                    chest_tile=None,
                    items=[item],
                )
            )

        return chest_tasks

    def _build_mock_return_borrowed_tool_tasks(
        self,
        recovery_farm_task: FarmTask,
        missing_chest_items: list[ChestItemRequest],
    ) -> list[ChestTask]:
        borrowed_tool_items = [
            item
            for item in missing_chest_items
            if self._is_borrowable_tool_name(item.item_name) and item.qualified_item_id is None
        ]
        if not borrowed_tool_items:
            return []

        return [
            ChestTask(
                task_type="CHEST",
                desc="Farm 任务完成后把借来的工具放回原箱子",
                chest_action="PUT",
                target_loc=recovery_farm_task.target_loc,
                chest_tile=None,
                items=borrowed_tool_items,
            )
        ]

    def _is_borrowable_tool_name(self, item_name: str) -> bool:
        normalized_item_name = self._normalize_tool_text(item_name)
        for tool_name in FARM_BORROWABLE_TOOL_NAMES:
            normalized_tool_name = self._normalize_tool_text(tool_name)
            if normalized_item_name == normalized_tool_name or normalized_item_name.endswith(
                f" {normalized_tool_name}"
            ):
                return True
        return False

    def _normalize_tool_text(self, value: str) -> str:
        return " ".join(value.strip().lower().split())
