import base64
import os
import sys
from pathlib import Path
from langchain_core.messages import HumanMessage

ROOT_DIR = Path(__file__).resolve().parents[4]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from init_vlm import vlm_model
from agent.action.enviroment.enviroment import StardewEnvironmentInfo

IMAGE_PATH = "assets/images/farm.png"

if not os.path.exists(IMAGE_PATH):
    raise FileNotFoundError(f"未找到截图文件: {IMAGE_PATH}，请确保前一步的截图脚本运行成功！")

with open(IMAGE_PATH, "rb") as image_file:
    base64_image = base64.b64encode(image_file.read()).decode("utf-8")


query_message = HumanMessage(
    content=[
        {
            "type": "text",
            "text": "请仔细观察这张《星露谷物语》截图，忽略背景画面，专注于提取右上角时间盘、金币栏、右下角能量条以及快捷道具栏等用户界面（UI）中的关键属性和状态，并严格以上述压缩的 JSON 格式输出对应字段，不要包含任何多余的 Markdown 解释或首尾换行。",
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

structured_vlm = vlm_model.with_structured_output(StardewEnvironmentInfo)


try:
    response: StardewEnvironmentInfo = structured_vlm.invoke([query_message])  # type: ignore

    print("\n--- 🧠 Google VLM 云端分析与决策 ---")
    if response:
        print(response.season)

    print("-----------------------------------")
except Exception as e:
    print(f"❌ 调用 Google 云端失败: {e}")
