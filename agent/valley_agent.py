import os
import asyncio
from typing import cast

from langchain.messages import AIMessage, HumanMessage

from utils.logger import valley_logger, main_logger
from utils.screenshot import capture_specific_window

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.utils.uuid import uuid7
from datetime import datetime
from langchain_google_genai import ChatGoogleGenerativeAI


class ValleyAgent:
    def __init__(self):
        self.loop_logger = None

        self.complete_goal = False
        self.user_approved_ai_decision = True
        self.interrupt_flag = False

        self.session_state = self.set_session_state(
            session_thread_id=str(uuid7()),
        )

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

            self.agent = create_agent(
                model=self.chat_model,
                tools=self.tools,
                middleware=self.middleware,
                checkpointer=self.memory,
            )

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

    def set_interrupt(self, flag: bool, reason: str | None):
        self.interrupt_flag = flag

        if self.loop_logger:
            self.loop_logger.warning(f" AI 决策循环被中断: {reason}")

    def logger_write(self, message: str, level: str = "info"):
        if self.loop_logger:
            if level == "info":
                self.loop_logger.info(message)
            elif level == "warning":
                self.loop_logger.warning(message)
            elif level == "error":
                self.loop_logger.error(message)
            else:
                self.loop_logger.info(message)

    async def run_execute_loop(self):
        """
        核心入口函数
        """
        current_time_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        log_file_path = f"logs/agent_{self.session_thread_id}_{current_time_str}.log"

        self.loop_logger = valley_logger.create_logger(log_file_path)

        while not self.complete_goal:
            if not self.user_approved_ai_decision:
                self.logger_write(" 用户已经批准 AI 接管, 继续执行。。。", level="info")
                break

            if self.interrupt_flag:
                self.set_interrupt(flag=False, reason=None)

                await asyncio.sleep(1.0)
                continue

            await self.next_tick()

    async def resume_execute_loop(self):
        if self.user_approved_ai_decision:
            self.logger_write(" 用户已经批准 AI 接管, 继续执行。。。", level="info")

            while not self.complete_goal:
                if not self.user_approved_ai_decision:
                    self.logger_write(" 用户主动停止 AI 接管, 暂停执行。。。", level="warning")
                    break

                if self.interrupt_flag:
                    self.set_interrupt(flag=False, reason=None)

                    await asyncio.sleep(1.0)
                    continue

                await self.next_tick()

    async def next_tick(self):
        await self.update_overview()

    async def update_overview(self):
        try:
            TARGET_WINDOW = os.getenv("GAME_WINDOW_TITLE")
            screenshot = capture_specific_window(TARGET_WINDOW)

        except Exception as e:
            self.logger_write(f"update_overview 异常: {e}", level="error")

    def invoke(self, query: str):
        try:
            result = self.agent.invoke(
                {"messages": [HumanMessage(content=query)]},
                config={"configurable": {"thread_id": self.session_thread_id}},
            )

            ai_response = result["messages"][-1]

            if isinstance(ai_response, AIMessage):
                content = ai_response.content[0]
                if isinstance(content, dict):
                    if "text" in content:
                        return content["text"]
                    else:
                        raise ValueError(" 模型返回的消息内容中没有text字段。。。")
                elif isinstance(content, str):
                    return content
                else:
                    raise ValueError(" 模型返回的消息内容既不是字符串也不是字典。。。")

            else:
                raise ValueError(" 模型返回的最后一条消息不是AIMessage类型。。。")

        except Exception as e:
            self.logger_write(f" agent run 失败: {e}", level="error")
            self.logger_write(f" query: {query}", level="error")
            raise
