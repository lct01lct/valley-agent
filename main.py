from utils.logger import main_logger

from dotenv import load_dotenv

load_dotenv(".env")


async def run_main_agent() -> None:
    main_logger.info("正在初始化 ValleyAgent...")

    from agent.valley_agent import ValleyAgent

    agent = ValleyAgent()

    await agent.initialize()

    # print(agent.run("请介绍一下你自己。"))
    # print("\n")
    # print(agent.run("我刚才的问题是什么。"))


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_main_agent())
