import pyautogui
import time

pyautogui.PAUSE = 0.1  # 每个动作间隔
pyautogui.FAILSAFE = True


def get_mouse_pos():
    try:
        while True:
            x, y = pyautogui.position()
            print(f"坐标 X:{x:4d}  Y:{y:4d}", end="\r")
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n坐标获取结束")


# ========== 2. 鼠标移动到指定坐标 ==========
def move_mouse(target_x: int, target_y: int, duration: float = 0.2):
    """
    :param target_x/target_y: 目标屏幕坐标
    :param duration: 移动耗时(秒)，越小越快
    """
    pyautogui.moveTo(target_x, target_y, duration=duration)


# ========== 3. 鼠标点击（左键/右键） ==========
def mouse_click(button: str = "left", clicks: int = 1):
    """button: left/right"""
    pyautogui.click(button=button, clicks=clicks)


# ========== 4. 键盘按键（单次按键） ==========
def press_key(key: str):
    """
    常用按键：w/a/s/d 方向键, enter, space, esc
    示例: press_key('w')
    """
    pyautogui.press(key)


# ========== 5. 持续按住按键（星露谷核心：持续走路） ==========
def hold_key(key: str, hold_sec: float):
    """按住按键指定时长，模拟持续行走"""
    pyautogui.keyDown(key)
    time.sleep(hold_sec)
    pyautogui.keyUp(key)


# ========== 示例：星露谷行走测试 ==========
if __name__ == "__main__":
    # 第一步：先运行 get_mouse_pos() 获取游戏内目标坐标
    # get_mouse_pos()

    # 示例1：按住 W 向前走 2 秒
    hold_key("w", 2)
    time.sleep(0.5)

    # 示例2：按住 D 向右走 1.5 秒
    hold_key("d", 1.5)
