import os
from typing import cast

from langchain.messages import AIMessage, HumanMessage

from utils.logger import get_logger

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.utils.uuid import uuid7


class ValleyAgent:
    def __init__(self):
        self.logger = get_logger("MaiAgent")
        self.session_state = self.set_session_state(
            session_thread_id=str(uuid7()),
        )

    def _create_chat_model(self):
        self.model_name = "google_genai:gemini-3.1-flash-lite"
        self.api_key = cast(str, os.getenv("GOOGLE_API_KEY"))

        return init_chat_model(model=self.model_name, api_key=self.api_key)

    async def initialize(self):
        try:
            self.logger.info(" 开始初始化。。。")

            self.chat_model = self._create_chat_model()
            self.logger.info(" 模型初始化成功。。。")

            self.tools = self._create_tools()
            self.logger.info(" 工具初始化成功。。。")

            self.middleware = self._get_middleware()

            self.memory = InMemorySaver()

            self.agent = create_agent(
                model=self.chat_model,
                tools=self.tools,
                middleware=self.middleware,
                checkpointer=self.memory,
            )

            self.logger.info(" 初始化完成。。。")

        except Exception as e:
            self.logger.error(f" 初始化失败: {e}")
            raise

    def _create_tools(self):
        return []

    def _get_middleware(self):
        return []

    def set_session_state(self, session_thread_id: str | None):
        if session_thread_id:
            self.session_thread_id = session_thread_id

    def run(self, query: str):
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
            self.logger.error(f" agent run 失败: {e}")
            self.logger.error(f" query: {query}")
            raise
