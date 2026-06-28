import os
from datetime import datetime
from typing import cast, overload
import time

from PIL import Image


from agent.action.enviroment.enviroment import EnvironmentInfo
from agent.action.location.location import Location, LOCATIONS
from agent.action.valley_action.AStar import astar_solver
from agent.action.valley_action.action_type import StardewCommand
from agent.action.valley_action.move import get_next_direction_command
from agent.prompt import (
    LocationMoveChainItem,
    LocationMoveChain,
    plan_path_finding_prompt,
)

from agent.prompt.plan import LocationMoveChainItemWithMovementHistory
from server.valley_server import StardewObserverClient, render_live_map
from utils.logger import valley_logger, main_logger
from utils.screenshot import capture_specific_window, image_to_base64

from langchain.chat_models import init_chat_model
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.utils.uuid import uuid7
from langgraph.graph import StateGraph, END

from langchain_google_genai import ChatGoogleGenerativeAI

from typing import List, Tuple, Literal
from pydantic import BaseModel, Field


class AgentState(BaseModel):
    # 宏观任务
    mission_text: str  # 用户输入的终极目标, 如 "去皮埃尔杂货铺买种子"

    # 游戏固定参数
    tile_size: float
    scale_rate: float

    # 系统固定参数:
    screen_size: tuple[float, float]

    # 寻路
    location_move_chain_with_movement_history: List[
        LocationMoveChainItemWithMovementHistory
    ]  # LLM 规划的跨地图拓扑链条
    current_location_move_index: int  # 当前正处于拓扑链条的第几步

    # 实时环境状态
    current_location: Location  # 当前所在的场景
    current_coord: Tuple[int, int]  # 当前坐标
    command_completed: bool  # 当前步是否正确闭环
    mission_completed: bool  # 任务是否完全闭环

    command: StardewCommand | None  # 等待执行的物理行为

    # 异常判断
    replan_required: bool  # 是否需要重新规划路线/反思


class StaticEnviromentInfo(BaseModel):
    pass


class PlayerEnviromentInfo(BaseModel):
    pass


class PlanNodeState(BaseModel):
    location_move_chain_with_movement_history: List[LocationMoveChainItemWithMovementHistory]
    current_location_move_index: int
    current_location: Location
    replan_required: bool
    command_completed: Literal[False]


class CalculateNodeState(BaseModel):
    command: StardewCommand | None
    replan_required: bool


class ExecuteNode(BaseModel):
    reperceive_required: bool
    current_chain_index: int
    command_completed: bool
    mission_completed: bool
    current_position: str


class ValleyAgent:
    def __init__(self):
        self.loop_logger = None

        self.global_enviroment = EnvironmentInfo()

        self.set_session_state(
            session_thread_id=str(uuid7()),
        )

        self.workflow = self.build_workflow()
        self.agent_app = self.workflow.compile()
        self.mode = os.getenv("MODE", "dev")
        self.is_mock_data = False

        self.stardew_valley_state = None
        self.stardew_observer_client = StardewObserverClient(
            cast(str, os.getenv("SMAPI_SEVER_HOST")),
            int(cast(str, os.getenv("SMAPI_SEVER_PORT"))),
        )
        self.update_stardew_valley_state_frequency = 0.1

        # 绘制 langgraph 架构图
        png_data = self.agent_app.get_graph(xray=True).draw_mermaid_png()
        with open("docs/stage1-graph.png", "wb") as f:
            f.write(png_data)

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

    async def initialize(self):
        try:
            main_logger.info(" 开始初始化。。。")

            self.chat_model = self._create_chat_model()
            main_logger.info(" chat 模型初始化成功。。。")

            self.vlm_model = self._create_vlm_model()
            main_logger.info(" vlm 模型初始化成功。。。")

            self.tools = self._create_tools()
            main_logger.info(" 工具初始化成功。。。")

            self.middleware = self._get_middleware()

            self.memory = InMemorySaver()

            current_time_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

            log_file_path = f"logs/{self.session_thread_id}/agent_{current_time_str}.log"
            mini_log_file_path = f"logs/{self.session_thread_id}/agent_{current_time_str}_mini.log"
            self.loop_img_dir = f"logs/{self.session_thread_id}/img"
            os.makedirs(self.loop_img_dir, exist_ok=True)

            self.loop_logger = valley_logger.create_logger(log_file_path)
            self.loop_mini_logger = valley_logger.create_logger(mini_log_file_path, mini=True)

            main_logger.info(" 日志初始化。。。")

            self.stardew_observer_client.start()
            main_logger.info(" stardew perception server 初始化。。。")

            try:
                while True:
                    stardew_valley_state = self.stardew_observer_client.pop_game_state()
                    self.stardew_valley_state = stardew_valley_state

                    if stardew_valley_state is None:
                        time.sleep(self.update_stardew_valley_state_frequency)
                        continue
                    else:
                        if self.mode == "dev":
                            render_live_map(
                                stardew_valley_state,
                                "server/img/stardew_live_map.png",
                                grid_pixel=40,
                            )

                    time.sleep(self.update_stardew_valley_state_frequency)

            except KeyboardInterrupt:
                self.logger_write("\n🏁 服务端已安全退出。")

            main_logger.info(" 初始化完成。。。")

        except Exception as e:
            main_logger.error(f" 初始化失败: {e}")
            raise

    def _create_tools(self):
        return []

    def _get_middleware(self):
        return []

    def set_session_state(self, session_thread_id: str | None):
        if session_thread_id:
            self.session_thread_id = session_thread_id

    def logger_write(self, message: str, level: Literal["info", "error", "warning"] = "info", decorate=None):
        if self.loop_logger:
            if level == "info":
                self.loop_logger.info(message)
            elif level == "warning":
                self.loop_logger.warning(message)
            elif level == "error":
                self.loop_logger.error(message)
            else:
                self.loop_logger.info(message)

        if self.loop_mini_logger:
            if level == "info":
                self.loop_mini_logger.info(message)
            elif level == "warning":
                self.loop_mini_logger.warning(message)
            elif level == "error":
                self.loop_mini_logger.error(message)
            else:
                self.loop_mini_logger.info(message)

    async def plan_node(self, state: AgentState) -> PlanNodeState:
        prompt = plan_path_finding_prompt.format(
            current_location=state.current_location,
            mission_text=state.mission_text,
            location=LOCATIONS,
        )

        if self.is_mock_data:
            location_move_chain: List[LocationMoveChainItem] = [
                LocationMoveChainItem(
                    current_location="FarmHouse_Level0",
                    next_location="Farm",
                    is_final=False,
                ),
                LocationMoveChainItem(
                    current_location="Farm",
                    next_location="BusStop",
                    is_final=False,
                ),
                LocationMoveChainItem(
                    current_location="BusStop",
                    next_location="Town",
                    is_final=False,
                ),
                LocationMoveChainItem(
                    current_location="Town",
                    next_location="SeedShop",
                    is_final=False,
                ),
            ]
        else:
            data: LocationMoveChain = await self.chat_model.with_structured_output(LocationMoveChain).ainvoke(prompt)  # type: ignore

        location_move_chain_with_movement_history = [
            LocationMoveChainItemWithMovementHistory(
                **scene_chain_item.model_dump(),
                movement_history=[],
            )
            for scene_chain_item in data.root
        ]

        self.logger_write("\n📋 llm 规划:")
        for location_move_chain_item in location_move_chain_with_movement_history:
            self.logger_write(
                f"     {location_move_chain_item.current_location}  -> {location_move_chain_item.next_location}"
            )

        return PlanNodeState(
            location_move_chain_with_movement_history=location_move_chain_with_movement_history,
            current_location_move_index=0,
            current_location=location_move_chain[0].current_location,
            replan_required=False,
            command_completed=False,
        )

    def calculate_node(self, state: AgentState):
        self.logger_write(f"\n📏 [数值转化与 A*]: 网格建模进行求解路径...")

        if self.stardew_valley_state:
            if (
                state.current_location
                != state.location_move_chain_with_movement_history[state.current_location_move_index].current_location
            ):
                self.logger_write(
                    f"     ❌[calculate_node]: 当前的 location 为 {state.current_location} 与 规划路径的 {state.location_move_chain_with_movement_history[state.current_location_move_index].current_location} 不一致",
                    level="error",
                )

                return CalculateNodeState(
                    command=None,
                    replan_required=True,
                )
            else:
                route_list = astar_solver.find_path_to_warp_zone(
                    self.stardew_valley_state,
                    (self.stardew_valley_state.player_tile_x, self.stardew_valley_state.player_tile_y),
                    state.location_move_chain_with_movement_history[state.current_location_move_index].next_location,
                )

                movement_history = state.location_move_chain_with_movement_history[
                    state.current_location_move_index
                ].movement_history

                if len(movement_history) == 0:
                    movement_history.append(route_list[0])

                movement_history.append(route_list[1])

                if self.mode == "dev":
                    render_live_map(
                        self.stardew_valley_state,
                        "server/img/stardew_live_map.png",
                        grid_pixel=40,
                        route_list=route_list,
                    )
        current_position = route_list[0]
        next_position = route_list[1]
        move_command = get_next_direction_command(current=current_position, next_step=next_position)

        return CalculateNodeState(
            command=move_command,
            replan_required=False,
        )

    def execute_node(self, state: AgentState):
        command = state.command

        # self.logger_write(f"   🚪 [转场加载成功]: 触碰传送点。地图更替：{state.current_scene} ➔ 。等待过图黑屏...")

        # self.logger_write(
        #     f"   🌀 [成功到达过渡点]: 将继续在 `{state.current_scene}` 内的 `{state.vlm_data.transition_point_position}` 前往到 `{current_step.end_position}`"
        # )

        # self.logger_write(f"   🚪 [转场加载成功]: 触碰传送点。地图更替：{state.current_scene} ➔ 。等待过图黑屏...")

        # self.logger_write(
        #     f"   🌀 [成功到达过渡点]: 将继续在 `{state.current_scene}` 内的 `{state.vlm_data.transition_point_position}` 前往到 `{current_step.end_position}`"
        # )

    async def should_continue(self, state: AgentState) -> str:
        mission_completed = state.mission_completed
        replan_required = state.replan_required
        reperceive_required = state.reperceive_required
        command_completed = state.command_completed
        current_chain_index = state.current_chain_index
        scene_chain_len = len(state.scene_chain)

        if mission_completed and current_chain_index == scene_chain_len:
            self.logger_write("   🟢 [ReAct 路由决策]: 完全闭环整个任务")
            return "finish"

        if reperceive_required:
            if command_completed:
                if current_chain_index < scene_chain_len:
                    self.logger_write(
                        "   ✅ [ReAct 路由决策]: 模拟键鼠命令正常执行, 流转回【节点 perceiver】生成下一个命令!"
                    )
                    return "reperceive"
                elif current_chain_index > scene_chain_len:
                    self.logger_write(
                        f"   ❌ [ReAct 路由决策]: 严重告警, current_chain_index({current_chain_index}) > scene_chain_len({scene_chain_len}), 强行结束!",
                        level="error",
                    )
                    return "finish"
            else:
                self.logger_write(
                    "   ⚠️ [ReAct 路由决策]: 检测到异常状态（异常/A*寻路失败), 流转回【节点 perceiver】重新观察!"
                )
                return "reperceive"

        if replan_required:
            self.logger_write("   🟠 [ReAct 路由决策]: 异常, 流转回【节点 plan】重新审视并反思修正!")
            return "replan"

        self.logger_write(f"   ❌ [ReAct 路由决策]: 严重告警, 流向状态不明, 强行结束! {state}", level="error")
        return "finish"

    def build_workflow(self):
        workflow = StateGraph(AgentState)

        workflow.add_node("planner", self.plan_node)
        workflow.add_node("calculator", self.calculate_node)
        workflow.add_node("executor", self.execute_node)

        workflow.set_entry_point("planner")
        workflow.add_edge("planner", "calculator")
        workflow.add_edge("calculator", "executor")

        workflow.add_conditional_edges(
            "executor",
            self.should_continue,
            {
                "replan": "planner",  # 跨地图成功, 观察周围环境
                "finish": END,  # 抵达终点
            },
        )
        return workflow

    async def invoke(self, query: str):

        self.logger_write(f"🧠 任务: {query}\n")

        screen_size = await self.init_first_state()
        stardew_valley_state = self.stardew_valley_state

        if stardew_valley_state:

            agent_state = AgentState(
                #
                mission_text=query,
                #
                tile_size=stardew_valley_state.tile_size,
                scale_rate=1.0,
                #
                screen_size=screen_size,
                # 寻路
                location_move_chain_with_movement_history=[],
                current_location_move_index=-1,
                #
                current_location=stardew_valley_state.location_name,
                current_coord=(stardew_valley_state.player_tile_x, stardew_valley_state.player_tile_y),
                command_completed=False,
                mission_completed=False,
                #
                command=None,
                #
                replan_required=False,
            )

            async for event in self.agent_app.astream(agent_state):
                for node_name, state_update in event.items():
                    self.logger_write(f"--- 📍 [节点 {node_name} 执行完毕] ---\n")
        else:
            self.logger_write(f"\n\n❌ agent 无法连接 stardew valley!", level="error")
            raise ValueError("agent 无法连接 stardew valley!")

    async def init_first_state(self):
        state = self.stardew_valley_state
        try:
            if state:
                self.logger_write(
                    f"玩家初始状态: 🎬 场景 : {state.location_name} | 📍 坐标: ({state.player_tile_x}, {state.player_tile_y})"
                )

            first_screenshot = await self.get_screenshot("png")

            return first_screenshot.size

        except Exception as e:
            self.logger_write(f"\n\n❌ {e}", level="error")

            raise Exception(e)

    @overload
    async def get_screenshot(self, type: Literal["base64"]) -> str: ...

    @overload
    async def get_screenshot(self, type: Literal["png"]) -> Image.Image: ...

    async def get_screenshot(self, type: Literal["base64", "png"] = "base64"):
        TARGET_WINDOW = os.getenv("GAME_WINDOW_TITLE")
        image = capture_specific_window(TARGET_WINDOW)

        if type == "base64":
            return image_to_base64(image)
        return image

    def coord_to_px(self, coordinate: tuple[int, int], screen_size):
        return (int((coordinate[0] / 1000.0) * screen_size[0]), int((coordinate[1] / 1000.0) * screen_size[1]))
