from typing import Literal

type TaskType = Literal[
    "DEFEND",
    "ROUTE",
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
