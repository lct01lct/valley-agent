from typing import Literal

type TaskType = Literal["Defend", "ROUTE"]


class BaseTask:
    def __init__(self, task_type: TaskType, desc: str):
        self.task_type = task_type
        self.desc = desc
