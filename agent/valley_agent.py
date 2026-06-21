import os
import asyncio
from datetime import datetime
from typing import cast, overload
import time

from PIL import Image
from langchain.messages import HumanMessage


from agent.action.enviroment.enviroment import EnvironmentInfo
from agent.action.scene.scene import Scene, SCENES
from agent.action.valley_action.AStar import AStarParser, convert_path_to_keyboard_commands
from agent.action.valley_action.move import ValleyKeyCommand, player_move
from agent.prompt import (
    current_scene_prompt,
    GetSceneOutput,
    SceneChainItem,
    SceneChain,
    plan_path_finding_prompt,
    path_finding_prompt,
    PathFindingOutput,
    Obstacle,
)

from agent.prompt.path import draw_path_finding_mock_plot
from agent.prompt.plan import SceneChainItemWithSubStep, SceneChainSubStep
from server.valley_server import ValleyServer
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
    # 游戏固定参数
    tile_size: float
    scale_rate: float
    player_move_speed: float

    # 系统固定参数:
    screen_size: tuple[float, float]

    # 宏观任务
    mission_text: str  # 用户输入的终极目标, 如 "去皮埃尔杂货铺买种子"
    scene_chain: List[SceneChainItemWithSubStep]  # LLM 规划的跨地图拓扑链条
    current_chain_index: int  # 当前正处于拓扑链条的第几步

    # 实时环境状态
    current_scene: Scene  # 当前所在的场景
    current_position: str  # 当前所在位置的详细描述
    stuck_counter: int  # 卡墙计数器
    command_completed: bool  # 当前步是否正确闭环
    mission_completed: bool  # 任务是否完全闭环

    # 感知与计算中间量
    vlm_data: PathFindingOutput | None  # VLM 最新一次看屏幕的数据
    action_commands: List[ValleyKeyCommand]  # 尺子换算出来的、等待执行的物理按键队列

    # 异常判断
    replan_required: bool  # 是否需要重新规划路线/反思
    reperceive_required: bool  # 是否需要重新观察


class StaticEnviromentInfo(BaseModel):
    pass


class PlayerEnviromentInfo(BaseModel):
    pass


class PlanNodeState(BaseModel):
    scene_chain: List[SceneChainItemWithSubStep]
    current_chain_index: int
    current_scene: Scene
    replan_required: bool
    command_completed: Literal[False]


class PerceiveNodeState(BaseModel):
    vlm_data: PathFindingOutput | None
    replan_required: bool
    command_completed: Literal[False]


class CalculateNodeState(BaseModel):
    action_commands: List[ValleyKeyCommand]
    replan_required: bool
    reperceive_required: bool


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

        self.valley_server = ValleyServer(
            cast(str, os.getenv("SMAPI_SEVER_HOST")),
            int(cast(str, os.getenv("SMAPI_SEVER_PORT"))),
        )

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

            self.valley_server.start()
            main_logger.info(" valley server 初始化。。。")

            if self.mode == "dev":
                try:
                    while True:
                        state = self.valley_server.get_game_state()

                        if state is None:
                            print("⏳ 正在等待游戏内核发送第一帧完整数据包...", end="\r")
                            time.sleep(0.2)
                            continue

                        scene = state["scene_name"]
                        tile_x, tile_y = state["tile_x"], state["tile_y"]
                        obs_count = len(state["clean_obstacles"])

                        print(
                            f"🎬 实时场景: {scene:15} | 📍 玩家坐标: ({tile_x:2d}, {tile_y:2d}) | 🧱 障碍物数: {obs_count:4d}",
                            end="\r",
                        )
                        time.sleep(0.1)

                except KeyboardInterrupt:
                    self.valley_server.stop()

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

    async def update_overview(self):
        try:
            TARGET_WINDOW = os.getenv("GAME_WINDOW_TITLE")
            screenshot = capture_specific_window(TARGET_WINDOW)

            return screenshot

        except Exception as e:
            raise Exception(f"update_overview 异常: {e}")

    async def plan_node(self, state: AgentState) -> PlanNodeState:
        prompt = plan_path_finding_prompt.format(
            scene_name=state.current_scene,
            current_position=state.current_position,
            mission_text=state.mission_text,
            scenes=SCENES,
        )

        if self.is_mock_data:
            scene_chain: List[SceneChainItemWithSubStep] = [
                SceneChainItemWithSubStep(
                    scene_name="FarmHouse_Level0",
                    start_position="房间中央床铺左侧",
                    end_position="农舍南侧出口大门",
                    general_direction="向下（南）",
                    is_final=False,
                    sub_steps=[],
                ),
                SceneChainItemWithSubStep(
                    scene_name="Farm",
                    start_position="农舍正前方出口",
                    end_position="农场地图右侧通往小镇的栅栏门",
                    general_direction="向右（东）",
                    is_final=False,
                    sub_steps=[],
                ),
                SceneChainItemWithSubStep(
                    scene_name="Town",
                    start_position="小镇左侧入口",
                    end_position="皮埃尔杂货铺正门",
                    general_direction="向右（东）",
                    is_final=False,
                    sub_steps=[],
                ),
                SceneChainItemWithSubStep(
                    scene_name="SeedShop",
                    start_position="杂货铺入口处",
                    end_position="杂货铺内部柜台前的种子菜单",
                    general_direction="向上（北）",
                    is_final=True,
                    sub_steps=[],
                ),
            ]
        else:
            data: SceneChain = await self.chat_model.with_structured_output(SceneChain).ainvoke(prompt)  # type: ignore

            scene_chain = [
                SceneChainItemWithSubStep(
                    **scene_chain_item.model_dump(),
                    sub_steps=[],
                )
                for scene_chain_item in data.root
            ]

        self.logger_write("\n📋 llm 规划:")
        for scene_item in scene_chain:
            self.logger_write(
                f"     {scene_item.scene_name}: {scene_item.general_direction} | {scene_item.start_position}  -> {scene_item.end_position}"
            )

        return PlanNodeState(
            scene_chain=scene_chain,
            current_chain_index=0,
            current_scene=scene_chain[0].scene_name,
            replan_required=False,
            command_completed=False,
        )

    async def perceive_node(self, state: AgentState):
        current_step = state.scene_chain[state.current_chain_index]

        self.logger_write(
            f"\n👀 [观察周围环境]: 咔嚓！截取屏幕。当前所处场景: `{state.current_scene}`, 当前所在位置: `{state.current_position}`, 正在前往: `{current_step.end_position}`"
        )

        screen_shot = await self.get_screenshot(type="png")

        if self.is_mock_data:
            data = PathFindingOutput(
                player_normalized_coordinate=(416, 635),
                target_normalized_coordinate=(416, 780),
                is_target_in_sight=True,
                transition_point_position=None,
                obstacles=[
                    Obstacle(name="电视机", normalized_bounding_box=(340, 610, 400, 670)),
                    Obstacle(name="床铺", normalized_bounding_box=(435, 575, 500, 650)),
                    Obstacle(name="桌子", normalized_bounding_box=(465, 465, 500, 535)),
                    Obstacle(name="椅子", normalized_bounding_box=(435, 435, 495, 465)),
                    Obstacle(name="壁炉", normalized_bounding_box=(560, 560, 640, 630)),
                    Obstacle(name="北墙", normalized_bounding_box=(325, 325, 380, 675)),
                    Obstacle(name="西墙", normalized_bounding_box=(325, 380, 635, 435)),
                    Obstacle(name="东墙", normalized_bounding_box=(630, 380, 635, 675)),
                ],
            )
        else:
            data: PathFindingOutput = await self.vlm_model.with_structured_output(PathFindingOutput).ainvoke(
                [
                    HumanMessage(
                        content=[
                            {
                                "type": "text",
                                "text": path_finding_prompt.format(
                                    current_scene=current_step.scene_name,
                                    start_position=current_step.start_position,
                                    end_position=current_step.end_position,
                                    general_direction=current_step.general_direction,
                                    tile_size=f"{state.scale_rate * state.tile_size}px",
                                ),
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{image_to_base64(screen_shot)}"},
                            },
                        ]
                    )
                ]
            )  # type: ignore

        current_step.sub_steps.append(
            SceneChainSubStep(
                step_current_position=state.current_position,
                step_next_position=current_step.end_position if data.is_target_in_sight else current_step.end_position,
                fail_reason=None,
            )
        )

        to_pixel = lambda x: (x[0] / 1000 * screen_shot.size[0], x[1] / 1000 * screen_shot.size[1])
        self.logger_write(f"    玩家当前的位置是: {to_pixel(data.player_normalized_coordinate)}")
        self.logger_write(f"    目标当前的位置是: {to_pixel(data.target_normalized_coordinate)}")
        self.logger_write(f"    目标是否在视野内: {data.is_target_in_sight}")
        self.logger_write(f"    过渡点: {data.transition_point_position}")
        self.logger_write(f"    视野内的障碍物:")
        for obstacle in data.obstacles:
            self.logger_write(f"        {obstacle.name} ({obstacle.normalized_bounding_box})")

        if self.mode == "dev":
            file_name_prefix = f"chain-{state.current_chain_index}-step-{len(current_step.sub_steps) - 1}"

            screen_shot.save(
                os.path.join(
                    self.loop_img_dir,
                    f"{file_name_prefix}-screenshot.png",
                )
            )
            draw_path_finding_mock_plot(
                vlm_output=data,
                image_width=screen_shot.size[0],
                image_height=screen_shot.size[1],
                tile_size=128,
                output_filename=os.path.join(self.loop_img_dir, f"{file_name_prefix}-mock.png"),
            )

        return PerceiveNodeState(
            vlm_data=data,
            replan_required=False,
            command_completed=False,
        )

    def calculate_node(self, state: AgentState):
        vlm_res = state.vlm_data
        self.logger_write(f"\n📏 [数值转化与 A*]: 提取视觉比例进行降维换算...")

        if not vlm_res:
            self.logger_write("❌ perceive_node 未回传数据")
            raise Exception("❌perceive_node 未回传数据")

        player_px = self.coord_to_px(vlm_res.player_normalized_coordinate, state.screen_size)
        target_px = self.coord_to_px(vlm_res.target_normalized_coordinate, state.screen_size)
        real_obstacles = []

        for obs in vlm_res.obstacles:
            box = obs.normalized_bounding_box
            xmin, ymin = self.coord_to_px((box[0], box[1]), state.screen_size)
            xmax, ymax = self.coord_to_px((box[2], box[3]), state.screen_size)
            real_obstacles.append({"name": obs.name, "box": (xmin, ymin, xmax, ymax)})

        pixel_path = self.A_star_parser.plan_pixel_path(
            player_px=player_px,
            target_px=target_px,
            real_obstacles=real_obstacles,
        )
        keyboard_action_list = convert_path_to_keyboard_commands(
            pixel_path=pixel_path, walk_speed_px_per_sec=state.player_move_speed  # 游戏默认标准跑步速度
        )

        for action in keyboard_action_list:
            self.logger_write(f"     ({action.key}, {action.duration})")

        return CalculateNodeState(
            action_commands=keyboard_action_list,
            replan_required=False,
            reperceive_required=True,
        )

    def execute_node(self, state: AgentState):

        action_commands = state.action_commands

        for command in action_commands:
            player_move(command)

        current_step = state.scene_chain[state.current_chain_index]

        # TODO: 如果人物通过 command 未能到达到 current_step.sub_steps[-1].step_next_position
        # current_step.sub_steps[-1].step_next_position = vlm 判断的位置

        # return ExecuteNode(
        #     reperceive_required=True,
        #     current_chain_index=state.current_chain_index,
        #     command_completed=False,
        #     mission_completed=False,
        #     current_position=vlm 判断的位置,
        # )

        if state.vlm_data:
            if state.vlm_data.is_target_in_sight:
                self.logger_write(
                    f"   🚪 [转场加载成功]: 触碰传送点。地图更替：{state.current_scene} ➔ 。等待过图黑屏..."
                )
                time.sleep(1.0)

                return ExecuteNode(
                    reperceive_required=True,
                    current_chain_index=state.current_chain_index + 1,
                    command_completed=True,
                    mission_completed=True if state.current_chain_index + 1 == len(state.scene_chain) else False,
                    current_position=state.scene_chain[state.current_chain_index + 1].start_position,
                )
            else:

                self.logger_write(
                    f"   🌀 [成功到达过渡点]: 将继续在 `{state.current_scene}` 内的 `{state.vlm_data.transition_point_position}` 前往到 `{current_step.end_position}`"
                )

                return ExecuteNode(
                    reperceive_required=True,
                    current_chain_index=state.current_chain_index,
                    command_completed=True,
                    mission_completed=False,
                    current_position=str(state.vlm_data.transition_point_position),
                )

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
        workflow.add_node("perceiver", self.perceive_node)
        workflow.add_node("calculator", self.calculate_node)
        workflow.add_node("executor", self.execute_node)

        workflow.set_entry_point("planner")
        workflow.add_edge("planner", "perceiver")
        workflow.add_edge("perceiver", "calculator")
        workflow.add_edge("calculator", "executor")

        workflow.add_conditional_edges(
            "executor",
            self.should_continue,
            {
                "replan": "planner",  # 跨地图成功, 观察周围环境
                "reperceive": "perceiver",  # 异常或者寻路失败, 观察周围环境, 反思！
                "finish": END,  # 抵达终点
            },
        )
        return workflow

    async def invoke(self, query: str):

        self.logger_write(f"🧠 任务: {query}\n")

        envir_data, screen_size = await self.init_first_state()
        state = AgentState(
            mission_text=query,
            scale_rate=1.0,
            tile_size=128,
            player_move_speed=470,
            screen_size=screen_size,
            #
            scene_chain=[],
            current_chain_index=0,
            current_scene=envir_data.scene_name,
            current_position=envir_data.detail_desc,
            stuck_counter=0,
            replan_required=False,
            reperceive_required=True,
            mission_completed=False,
            command_completed=False,
            vlm_data=None,
            action_commands=[],
        )

        self.A_star_parser = AStarParser(tile_size=state.tile_size * state.scale_rate)

        async for event in self.agent_app.astream(state):
            for node_name, state_update in event.items():
                self.logger_write(f"--- 📍 [节点 {node_name} 执行完毕] ---\n")

    async def init_first_state(self):
        try:

            first_screenshot = await self.get_screenshot("png")

            state = self.valley_server.get_game_state()

            self.logger_write(f"玩家初始状态: {data.scene_name}({data.detail_desc})")
            self.logger_write(f"    是否在室内: {data.is_indoor}")
            self.logger_write(f"    判断规则: {data.visual_clues}")

            return data, first_screenshot.size

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
