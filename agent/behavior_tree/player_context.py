import os
from typing import Literal, cast

from agent.action.valley_action.action_type import StardewCommand
from server.valley_server import StardewExecutorClient, StardewObserverClient, StardewState

type Player_Mode = Literal["Guard", "Route", "Farm", "Think"]


class PlayerContext:
    def __init__(self):
        self.state: None | StardewState = None
        self.observer_client = StardewObserverClient(
            cast(str, os.getenv("SMAPI_SEVER_HOST")),
            int(cast(str, os.getenv("SMAPI_OBSERVER_SERVER_PORT"))),
        )
        self.executor_client = StardewExecutorClient(
            cast(str, os.getenv("SMAPI_SEVER_HOST")),
            int(cast(str, os.getenv("SMAPI_EXECUTOR_SEVER_PORT"))),
        )

        self.observer_client.connect()
        print("🛠️ [System]：Stardew Observer Client 初始化。。。")
        self.executor_client.connect()
        print("🛠️ [System]：Stardew Observer Client 初始化。。。")

    def update(self):
        latest_state = self.observer_client.pop_game_state()
        if latest_state is not None:
            self.state = latest_state

    def run(self, command: StardewCommand):
        self.executor_client.send_command(command)
