import os
import queue
import threading
from datetime import datetime


class DefendDebugLogger:
    """
    非阻塞防御模块诊断日志。
    """

    def __init__(self, log_path: str = "logs/defend_node_debug.log"):
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
