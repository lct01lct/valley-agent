import time
from typing import Literal
import pyautogui
from pydantic import BaseModel

type KeyType = Literal["w", "a", "s", "d"]


class ValleyKeyCommand(BaseModel):
    key: KeyType
    duration: float


def player_move(command: ValleyKeyCommand):
    pyautogui.keyDown(command.key)
    time.sleep(command.duration)
    pyautogui.keyUp(command.key)
