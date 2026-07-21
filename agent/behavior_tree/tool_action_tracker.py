import time
from typing import Literal

from server.valley_server import StardewState


type ToolActionStatus = Literal[
    "IDLE",  # 没有正在跟踪的工具动作
    "WAITING_STARTED",  # 已发送 USE_TOOL，但还没观察到 UsingTool
    "WAITING_FINISHED",  # 已观察到 UsingTool，正在等待收招
    "FINISHED",  # 已观察到 UsingTool 从 true 变为 false，且 CanMove 恢复
    "TIMEOUT",  # 等待开始或收招超时
]


class ToolActionTracker:
    """
    跟踪 Stardew Valley 工具动作是否完成收招。

    这里区分“工具动作完成”和“业务结果成功”：
    - 工具动作完成：曾观察到 UsingTool=true，随后 UsingTool=false 且 CanMove=true。
    - 业务结果成功：由调用方根据 state 验证，例如障碍消失、HasHoeDirt、IsWatered。
    """

    def __init__(
        self,
        start_grace_seconds: float = 0.35,
        finish_timeout_seconds: float = 2.5,
    ) -> None:
        self.start_grace_seconds = start_grace_seconds
        self.finish_timeout_seconds = finish_timeout_seconds
        self._sent_at: float | None = None
        self._observed_using_tool = False
        self._finished_at: float | None = None
        self._last_status: ToolActionStatus = "IDLE"

    def start(self) -> None:
        self._sent_at = time.time()
        self._observed_using_tool = False
        self._finished_at = None
        self._last_status = "WAITING_STARTED"

    def reset(self) -> None:
        self._sent_at = None
        self._observed_using_tool = False
        self._finished_at = None
        self._last_status = "IDLE"

    def is_idle(self) -> bool:
        return self._sent_at is None

    def tick(self, game_state: StardewState) -> ToolActionStatus:
        if self._sent_at is None:
            self._last_status = "IDLE"
            return self._last_status

        now = time.time()
        elapsed = now - self._sent_at
        if elapsed > self.finish_timeout_seconds:
            self._last_status = "TIMEOUT"
            return self._last_status

        if game_state.using_tool:
            self._observed_using_tool = True
            self._last_status = "WAITING_FINISHED"
            return self._last_status

        if self._observed_using_tool and not game_state.using_tool and game_state.can_move:
            self._finished_at = now
            self._last_status = "FINISHED"
            return self._last_status

        # 某些工具动作很短，或旧版 C# state 尚未提供 UsingTool 时，避免永久等待开始信号。
        if not self._observed_using_tool and elapsed >= self.start_grace_seconds and game_state.can_move:
            self._finished_at = now
            self._last_status = "FINISHED"
            return self._last_status

        self._last_status = "WAITING_STARTED"
        return self._last_status

    def get_debug_snapshot(self) -> str:
        if self._sent_at is None:
            return "status=IDLE"

        return (
            f"status={self._last_status}, observed_using_tool={self._observed_using_tool}, "
            f"elapsed={time.time() - self._sent_at:.2f}s, finished_at={self._finished_at}"
        )
