import asyncio
import os
from datetime import datetime
import time

from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.route_node import RouteNode
from agent.behavior_tree.behavior_tree import Selector, Sequence
from agent.behavior_tree.defend_node import Defend_Node
from agent.behavior_tree.open_door_node import OpenDoorNode
from agent.behavior_tree.llm_node import Agent_Model, LLM_Node
from agent.behavior_tree.player_context import PlayerContext
from utils.logger import valley_logger

from langchain_core.utils.uuid import uuid7


class ValleyAgent:
    def __init__(self):
        self.loop_logger = None

        self.set_session_state(
            session_thread_id=str(uuid7()),
        )

        self.models = Agent_Model()
        self.behavior_tree = Selector(
            [
                Sequence(node_name="Guard", children=[Defend_Node()]),
                Sequence(node_name="Route", children=[OpenDoorNode(), RouteNode()]),
                LLM_Node(self),
            ]
        )

    async def invoke(self, task: str):
        self.task_original_str = task
        self.ctx = PlayerContext()
        self.blackboard = AgentBlackboard()

        frame_interval = 1 / 120
        try:
            while True:
                # 同步游戏的数据
                self.ctx.update()

                if self.ctx is None:
                    await asyncio.sleep(0.01)
                    continue

                start_time = time.time()
                await self.behavior_tree.run(self.blackboard, self.ctx)

                if self.blackboard.macro_plan and self.blackboard.current_step_index >= len(self.blackboard.macro_plan):
                    print("\n🏆🏆🏆 [ValleyAgent] 任务圆满成功！")
                    self.blackboard.macro_plan = []
                    break

                # 补帧
                elapsed_time = time.time() - start_time
                sleep_time = max(0.001, frame_interval - elapsed_time)
                await asyncio.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\n🏁 [ValleyAgent] 已安全退出。")

    async def initialize(self):
        try:
            print("🛠️ [System]：开始初始化。。。")

            current_time_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

            # log_file_path = f"logs/{self.session_thread_id}/agent_{current_time_str}.log"
            mini_log_file_path = f"logs/{self.session_thread_id}/agent_{current_time_str}_mini.log"
            self.loop_img_dir = f"logs/{self.session_thread_id}/img"
            os.makedirs(self.loop_img_dir, exist_ok=True)

            # self.loop_logger = valley_logger.create_logger(log_file_path)
            self.loop_mini_logger = valley_logger.create_logger(mini_log_file_path, mini=True)

            self.behavior_tree.initialize()
            self.models.initialize()

            print("🛠️ [System]：初始化完成！！！")

        except Exception as e:
            print(f"🛠️ [System]：初始化失败: {e}")
            raise

    def set_session_state(self, session_thread_id: str | None):
        if session_thread_id:
            self.session_thread_id = session_thread_id
