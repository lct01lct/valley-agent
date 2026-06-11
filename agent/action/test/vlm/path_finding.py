import base64
import os
import sys
from pathlib import Path
from langchain_core.messages import HumanMessage

ROOT_DIR = Path(__file__).resolve().parents[4]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from init_vlm import vlm_model
from agent.action.valley_action.path_finder import PathFinding

IMAGE_PATH = "assets/images/farm_home_indoor.png"

if not os.path.exists(IMAGE_PATH):
    raise FileNotFoundError(f"未找到截图文件: {IMAGE_PATH}，请确保前一步的截图脚本运行成功！")

with open(IMAGE_PATH, "rb") as image_file:
    base64_image = base64.b64encode(image_file.read()).decode("utf-8")


query_message = HumanMessage(
    content=[
        {
            "type": "text",
            "text": "你是星露谷游戏助手 ,请在画面中找到电视机（不论款式和位置），并结合周围的障碍物（如桌椅、墙壁），规划一条从人物位置走到电视机正前方的最佳路线。请严格按照格式输出一条键盘操作序列，确保角色最终能贴紧电视机。",
        },
        {
            "type": "image_url",
            "image_url": {
                # 传入封装好的 base64 图像流
                "url": f"data:image/png;base64,{base64_image}"
            },
        },
    ]
)

structured_vlm = vlm_model.with_structured_output(PathFinding)


try:
    response: PathFinding = structured_vlm.invoke([query_message])  # type: ignore

    print("\n--- 🧠 Google VLM 云端分析与决策 ---")
    if response:
        print(response)
        """
reasoning='电视机位于画面左下角。角色当前位于房间右侧的床边。为了到达电视机，角色需要先向下移动避开床铺，然后向左穿过走廊，最后向下移动至电视机正前方。路径规划为：先向下走以离开床位区域，再向左穿过中间的空地，最后向下移动至电视机前。' actions=[MoveAction(key='s', distance=150.0), MoveAction(key='a', distance=300.0), MoveAction(key='s', distance=100.0)] final_direction='s'
        """

    print("-----------------------------------")
except Exception as e:
    print(f"❌ 调用 Google 云端失败: {e}")
