import time


from dotenv import load_dotenv
from scripts.clear_logs import clear_dir

load_dotenv(".env")


async def run_main_agent() -> None:
    from agent.valley_agent import ValleyAgent

    agent = ValleyAgent()

    await agent.initialize()

    # print("---------------3s 之后 agent 接管星露谷物语，请切回游戏界面！！！-----------------------")
    # time.sleep(3)

    await agent.invoke("前往皮埃尔商店购买20个防风草种子")

    # print(agent.run("请介绍一下你自己。"))
    # print("\n")
    # print(agent.run("我刚才的问题是什么。"))


def print_startup_banner():
    """打印启动横幅"""

    logo = """
    ╔═══════════════════════════════════════════════════════════╗
    ║                                                           ║
    ║   ██╗   ██╗ █████╗ ██╗     ██╗     ███████╗██╗   ██╗    ║
    ║   ██║   ██║██╔══██╗██║     ██║     ██╔════╝╚██╗ ██╔╝    ║
    ║   ██║   ██║███████║██║     ██║     █████╗   ╚████╔╝     ║
    ║   ╚██╗ ██╔╝██╔══██║██║     ██║     ██╔══╝    ╚██╔╝      ║
    ║    ╚████╔╝ ██║  ██║███████╗███████╗███████╗   ██║       ║
    ║     ╚═══╝  ╚═╝  ╚═╝╚══════╝╚══════╝╚══════╝   ╚═╝       ║
    ║                                                           ║
    ║           🌾 Stardew Valley Agent v1.0.0 🌾              ║
    ║                                                           ║
    ╚═══════════════════════════════════════════════════════════╝
    """
    print(logo)


if __name__ == "__main__":
    import asyncio

    print_startup_banner()
    clear_dir("logs")
    asyncio.run(run_main_agent())
