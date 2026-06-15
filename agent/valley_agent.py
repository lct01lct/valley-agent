import os
import asyncio
from datetime import datetime
from typing import cast, overload
import time

from PIL import Image
from langchain.messages import HumanMessage


from agent.action.enviroment.enviroment import EnvironmentInfo
from agent.action.valley_action.path_finder import Pathfinder
from agent.action.scene.scene import Scene, SCENES
from agent.prompt import (
    current_scene_prompt,
    GetSceneOutput,
    SceneChainItem,
    SceneChain,
    plan_path_finding_prompt,
    path_finding_prompt,
    PathFindingOutput,
)

from agent.prompt.path import draw_path_finding_mock_plot
from utils.logger import valley_logger, main_logger
from utils.screenshot import capture_specific_window, image_to_base64

from langchain.chat_models import init_chat_model
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.utils.uuid import uuid7
from langgraph.graph import StateGraph, END
from langchain_core.output_parsers import PydanticOutputParser

from langchain_google_genai import ChatGoogleGenerativeAI

from typing import List, Tuple, Optional, Literal
from pydantic import BaseModel, Field, TypeAdapter


class UniversalSceneMap(BaseModel):
    reasoning: str = Field(description="当前场景的空间拓扑分析与下一步规划思考")
    player_pixel_ratio: Tuple[float, float] = Field(description="玩家双脚在当前屏幕的归一化比例坐标 (X, Y)")
    destination_pixel_ratio: Tuple[float, float] = Field(description="当前阶段目的地/大门/NPC 的归一化比例坐标 (X, Y)")
    obstacle_relative_tiles: List[Tuple[int, int]] = Field(
        description="以玩家为(0,0), 视野内所有阻挡物体的相对网格偏移量列表 (ΔX, ΔY)"
    )


pathfinder_tool = Pathfinder()


class AgentState(BaseModel):
    # 游戏固定信息
    tile_size: float
    scale_rate: float

    # 宏观任务
    mission_text: str  # 用户输入的终极目标, 如 "去皮埃尔杂货铺买种子"
    scene_chain: List[SceneChainItem]  # LLM 规划的跨地图拓扑链条
    current_step_index: int  # 当前正处于拓扑链条的第几步

    # 实时环境状态
    current_scene: Scene  # 当前所在的场景
    current_position: str  # 当前所在位置的详细描述
    stuck_counter: int  # 卡墙计数器
    replan_required: bool  # 是否需要重新规划路线/反思
    reperceive_required: bool  # 是否需要重新观察
    mission_completed: bool  # 任务是否完全闭环

    # 感知与计算中间量
    vlm_data: Optional[UniversalSceneMap]  # VLM 最新一次看屏幕的数据
    action_commands: List[dict]  # 尺子换算出来的、等待执行的物理按键队列


class StaticEnviromentInfo(BaseModel):
    pass


class PlayerEnviromentInfo(BaseModel):
    pass


class PlanNodeState(BaseModel):
    scene_chain: List[SceneChainItem]
    current_step_index: int
    current_scene: Scene
    replan_required: bool


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
        return ChatGoogleGenerativeAI(
            model="gemini-3.1-flash-lite",
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

            # self.agent = create_agent(
            #     model=self.chat_model,
            #     tools=self.tools,
            #     middleware=self.middleware,
            #     checkpointer=self.memory,
            # )

            current_time_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

            log_file_path = f"logs/{self.session_thread_id}/agent_{current_time_str}.log"
            mini_log_file_path = f"logs/{self.session_thread_id}/agent_{current_time_str}_mini.log"
            self.loop_img_dir = f"logs/{self.session_thread_id}/img"
            os.makedirs(self.loop_img_dir, exist_ok=True)

            self.loop_logger = valley_logger.create_logger(log_file_path)
            self.loop_mini_logger = valley_logger.create_logger(mini_log_file_path, mini=True)

            main_logger.info(" 日志初始化。。。")

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

    async def resume_execute_loop(self):
        pass

    async def next_tick(self):
        try:
            screenshot = await self.update_overview()

            enviroment_info = await asyncio.wait_for(
                self.global_enviroment.get_enviroment_info(self.vlm_model, screenshot), timeout=15.0
            )

            input_data = {}

        except asyncio.TimeoutError:
            self.logger_write("VLM概览分析超时（15秒）, 跳过本次视觉分析", level="error")
        except Exception as e:
            self.logger_write(str(e), level="error")

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

        # data: SceneChain = await self.chat_model.with_structured_output(SceneChain).ainvoke(prompt)  # type: ignore

        # scene_chain = data.root

        # MOCK:
        scene_chain: List[SceneChainItem] = [
            SceneChainItem(
                scene_name="农场房子",
                current_position="房间中央床铺左侧",
                next_position="农舍南侧出口大门",
                general_direction="向下（南）",
                is_final=False,
            ),
            SceneChainItem(
                scene_name="农场",
                current_position="农舍正前方出口",
                next_position="农场地图右侧通往小镇的栅栏门",
                general_direction="向右（东）",
                is_final=False,
            ),
            SceneChainItem(
                scene_name="小镇",
                current_position="小镇左侧入口",
                next_position="皮埃尔杂货铺正门",
                general_direction="向右（东）",
                is_final=False,
            ),
            SceneChainItem(
                scene_name="皮埃尔杂货铺",
                current_position="杂货铺入口处",
                next_position="杂货铺内部柜台前的种子菜单",
                general_direction="向上（北）",
                is_final=True,
            ),
        ]

        self.logger_write("\n📋 llm 规划:")
        for scene_item in scene_chain:
            self.logger_write(
                f"     {scene_item.scene_name}: {scene_item.general_direction} | {scene_item.current_position}  -> {scene_item.next_position}"
            )

        plan_node_state = PlanNodeState(
            scene_chain=scene_chain,
            current_step_index=0,
            current_scene=scene_chain[0].scene_name,
            replan_required=False,
        )

        return plan_node_state

    async def perceive_node(self, state: AgentState):
        current_step = state.scene_chain[state.current_step_index]
        self.logger_write(
            f"\n👀 [观察周围环境]: 咔嚓！截取屏幕。当前所处场景: `{state.current_scene}`, 当前所在位置: `{state.current_position}`, 正在前往: `{current_step.next_position}`"
        )

        screen_shot = await self.get_screenshot(type="png")

        data: PathFindingOutput = await self.vlm_model.with_structured_output(PathFindingOutput).ainvoke(
            [
                HumanMessage(
                    content=[
                        {
                            "type": "text",
                            "text": path_finding_prompt.format(
                                current_scene=current_step.scene_name,
                                current_position=current_step.current_position,
                                next_position=current_step.next_position,
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

        self.logger_write(f"{data}")

        if self.mode == "dev":
            (await self.get_screenshot(type="png")).save(os.path.join(self.loop_img_dir, f"step-{state.current_step_index}-screenshot.png"))  # type: ignore
            draw_path_finding_mock_plot(
                player_pos=data.player_pixel_coordinate,
                target_pos=data.target_pixel_coordinate,
                obstacles=data.obstacles,
                output_filename=os.path.join(self.loop_img_dir, f"step-{state.current_step_index}-mock.png"),
            )
        raise ValueError("-------------")

        # 模拟 VLM 调用（实际项目中替换为 model.with_structured_output(UniversalSceneMap).ainvoke(...)）
        # 这里模拟 VLM 认出了玩家、目标在屏幕上的比例, 以及沿途挡路的家具/障碍
        if state.stuck_counter > 0:
            self.logger_write("   ⚠️ 检测到上一步卡墙了, VLM 触发【反思修正】：更新避障路障矩阵...", level="error")
            mock_obstacles = [(21, 20), (21, 19), (20, 21)]  # 新增了动态阻挡
        else:
            mock_obstacles = [(21, 20), (21, 19)]  # 默认阻挡（如桌椅）

        mock_vlm_output = UniversalSceneMap(
            reasoning=f"玩家在屏幕中央偏左, {current_step.next_position}在右前方。中间有家具卡口, 需要走之字形路线。",
            player_pixel_ratio=(0.4, 0.5),
            destination_pixel_ratio=(0.64, 0.33),
            obstacle_relative_tiles=mock_obstacles,
        )

        self.logger_write(f"   ➔ VLM 推理思考: {mock_vlm_output.reasoning}")
        return {"vlm_data": mock_vlm_output, "replan_required": False}

    def calculate_node(self, state: AgentState):
        vlm_res = state.vlm_data
        self.logger_write(f"\n📏 [数值转化与 A*]: 提取视觉比例进行降维换算...")

        if not vlm_res:
            raise Exception("💥💥💥vlm 未回传数据")

        # 1. 尺子换算：比例差 -> 网格差
        grid_dx, grid_dy = pathfinder_tool.calculate_grid_delta(
            vlm_res.player_pixel_ratio, vlm_res.destination_pixel_ratio
        )

        # 2. 建立本地棋盘起终点
        start_tile = (20, 20)
        goal_tile = (start_tile[0] + grid_dx, start_tile[1] + grid_dy)
        self.logger_write(f"   ➔ 转换为网格坐标：起点(20,20) -> 终点{goal_tile}")

        # 3. 运行本地 A* 算法
        path = pathfinder_tool.find_path(start_tile, goal_tile, vlm_res.obstacle_relative_tiles)
        if not path:
            self.logger_write("   ❌ A* 求解失败：前方无路可行！触发重写规划标记。")
            return {"replan_required": True, "action_commands": []}
        # 4. 将路径解算为动作执行指令
        commands = []
        cx, cy = start_tile
        for nx, ny in path:
            direction = "right" if nx > cx else "left" if nx < cx else "down" if ny > cy else "up"
            commands.append({"action": "press_key", "key": direction, "duration": 0.22})
            cx, cy = nx, ny

        self.logger_write(f"   ➔ A* 解算成功, 生成 {len(commands)} 步低延迟物理动作脉冲序列。")
        return {"action_commands": commands}

    def execute_node(self, state: AgentState):
        commands = state.action_commands
        current_step = state.scene_chain[state.current_step_index]

        self.logger_write(f"\n⌨️ [模拟键鼠行为]: 正在顺序执行 {len(commands)} 步物理按键...")

        # 真实项目中这里会执行 pyautogui.keyDown(cmd['key']) -> sleep -> keyUp
        for cmd in commands:
            self.logger_write(f"  -> 按住 [{cmd['key']}] 持续 {cmd['duration']} 秒")
            pass

        self.logger_write("   ➔ 按键流执行完毕。进入【断言判定层】观察环境反馈...")

        # ---------------- ReAct 动态断言逻辑模拟 ----------------
        # 模拟场景1：遇到恶劣情况, 突发卡墙（比如被乱动的宠物狗卡死在农场）
        if state.stuck_counter == 0:
            self.logger_write(
                "   🚨 [监督者警告]: 物理移动序列已完结, 但 CV 判定画面像素未发生改变！角色卡墙了！", level="warning"
            )
            return {"stuck_counter": 1, "replan_required": True}

        # 模拟场景2：卡墙后, 第二轮走出了困境, 成功切图
        if state.stuck_counter == 1:
            self.logger_write("   ✨ [监督者恢复]: 成功绕开意外障碍物！")
            # 顺延进入下一步转场

        # 判定是否彻底完成了最终地图的任务
        if current_step.is_final:
            self.logger_write("   🎉 [任务完结断言]: 已贴紧皮埃尔柜台, 按 X 成功触发了商店交易菜单！")
            return {"mission_completed": True}

        # 途中阶段转场成功逻辑：更替地图状态
        next_index = state.current_step_index + 1
        next_map = state.scene_chain[next_index].scene_name
        self.logger_write(
            f"   🚪 [转场加载成功]: 触碰传送点。地图更替：{state.current_scene} ➔ {next_map}。等待过图黑屏..."
        )
        time.sleep(1.0)

        return {
            "current_step_index": next_index,
            "current_map": next_map,
            "stuck_counter": 0,  # 重置卡墙计数
            "replan_required": False,
        }

    async def should_continue(self, state: AgentState) -> str:
        if state.mission_completed:
            return "finish"

        if state.reperceive_required:
            self.logger_write(
                "   🔄 [ReAct 路由决策]: 检测到异常状态（异常/A*寻路失败),流转回【节点 perceiver】重新观察!"
            )
            return "reperceive"

        if state.replan_required:
            self.logger_write("   🔄 [ReAct 路由决策]: 异常, 流转回【节点 plan】重新审视并反思修正!")
            return "replan"

        self.logger_write(f"   💥💥💥 [ReAct 路由决策]: 严重告警, 流向状态不明, 强行结束, {state}")
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

        data = await self.init_first_state()
        initial_state = AgentState(
            mission_text=query,
            scale_rate=1.0,
            tile_size=128,
            scene_chain=[],
            current_step_index=0,
            current_scene=data.scene_name,
            current_position=data.detail_desc,
            stuck_counter=0,
            replan_required=False,
            reperceive_required=True,
            mission_completed=False,
            vlm_data=None,
            action_commands=[],
        )

        async for event in self.agent_app.astream(initial_state):
            for node_name, state_update in event.items():
                self.logger_write(f"--- 📍 [节点 {node_name} 执行完毕] ---\n")

    async def init_first_state(self):
        try:
            # MOCK:
            data = GetSceneOutput(
                scene_name="农场房子",
                is_indoor=True,
                visual_clues="画面展示了典型的玩家初始农舍内部，包含标志性的电视机、单人床、壁炉以及木质地板和墙纸。",
                detail_desc="玩家正站在房间中央，位于电视机右侧，紧邻着床铺的左侧边缘，正前方是木桌和椅子。",
            )

            # first_screenshot = await self.get_screenshot()

            # data: GetSceneOutput = await self.vlm_model.with_structured_output(GetSceneOutput).ainvoke(
            #     [
            #         HumanMessage(
            #             content=[
            #                 {"type": "text", "text": current_scene_prompt},
            #                 {
            #                     "type": "image_url",
            #                     "image_url": {"url": f"data:image/png;base64,{first_screenshot}"},
            #                 },
            #             ]
            #         )
            #     ]
            # )  # type: ignore
            self.logger_write(f"{data}")

            return data

        except Exception as e:
            self.logger_write(f"\n\n💥💥💥 {e}", level="error")

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
