import os
import time


class InventoryRecoveryDebugLogger:
    def __init__(self, log_path: str = "logs/inventory_recovery_debug.log") -> None:
        self.log_path = log_path
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

    def clear(self) -> None:
        with open(self.log_path, "w", encoding="utf-8") as file:
            file.write("")

    def log(self, message: str) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self.log_path, "a", encoding="utf-8") as file:
            file.write(f"[{timestamp}] {message}\n")
