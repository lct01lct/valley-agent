from typing import Literal

type TaskType = Literal[
    "DEFEND",
    "ROUTE",
    "CHEST",  # 箱子资源取放：用于从指定箱子取出资源，或未来把背包资源放回箱子。
    "INVENTORY",  # 背包目标状态管理：由基础 Route/Chest 能力组合实现填满背包、清空箱子等高层需求。
    "FARM",  # Levels are gained by harvesting crops and caring for animals. Each level grants +1 hoe and watering can proficiency (see tools).
    "FISH",  # Fishing is associated with successfully completing the fishing mini-game or catching fish in a Crab Pot, increasing the fishing skill. Each level grants +1 fishing rod proficiency.
    "MINE",  # Mining skill is increased by breaking rocks (normally done with a Pickaxe). Each level grants +1 pickaxe proficiency.
    "COMBAT",  # Combat is a skill tied to the player's ability to fight against monsters.
    "FORAGE",  # Foraging skill includes both gathered foraged goods, and wood from trees chopped with an axe tool. Each level grants +1 axe proficiency.
]


class BaseTask:
    def __init__(self, task_type: TaskType, desc: str):
        self.task_type = task_type
        self.desc = desc
