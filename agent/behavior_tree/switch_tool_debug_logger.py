import os
import queue
import threading
from datetime import datetime


class SwitchToolDebugLogger:
    """
    非阻塞切换工具诊断日志。

    行为树 tick 只负责把日志放入内存队列；后台 daemon 线程负责实际写文件，
    避免 SwitchToolNode 在高频重试和状态验证过程中被磁盘 IO 卡住。
    """

    def __init__(self, log_path: str = "logs/switch_tool_debug.log"):
        self.log_path = log_path
        self._queue: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._clear_log_file()
        self._thread = threading.Thread(target=self._write_loop, daemon=True)
        self._thread.start()

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self._queue.put(f"[{timestamp}] {message}")

    def _write_loop(self) -> None:
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)

        with open(self.log_path, "a", encoding="utf-8", buffering=1) as log_file:
            while True:
                message = self._queue.get()
                log_file.write(message + "\n")

    def _clear_log_file(self) -> None:
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with open(self.log_path, "w", encoding="utf-8"):
            pass
