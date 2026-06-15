from pydantic import BaseModel, Field

from agent.action.scene.scene import SCENES, Scene

total_scene_prompt = ""
for scene in SCENES:
    total_scene_prompt += f"- {scene}\n"

current_scene_prompt = f"""\
# Role
你是一个精通经典像素风模拟经营游戏《星露谷物语》（Stardew Valley）的资深游戏专家与视觉分析助手。

# Task
请仔细观察我提供的游戏截图，结合你对《星露谷物语》游戏地图、NPC、建筑风格和室内摆设的了解，告诉我当前玩家正身处游戏的哪一个具体“场景/地图。

# Scene Candidate References (常见场景参考)
为了帮你缩小范围，你可以参考以下常见场景（但不仅限于这些）：
{total_scene_prompt}


# CoT (思考路径引导)
在给出最终结论前，请在心中或输出中简单分析以下视觉线索：
1. 【环境与墙壁/地面】：是室内（木地板、地毯、壁纸、特定的家具柜台）还是室外（草地、泥地、柏油路、围栏）？
2. 【标志性特征/NPC】：是否看到了特定的 NPC（如皮埃尔、罗宾、路易斯）？是否有特定的功能性标志（如出货箱、售货柜台、巴士、床铺）？
3. 【界面/UI 提示】：右上方的小地图（如果有）、时间日期、或是当前触发的对话框是否有地点提及？
"""


class GetSceneOutput(BaseModel):
    scene_name: Scene = Field(description="场景，可选值：参考给定的范围")
    is_indoor: bool = Field(description="是否为室内场景")
    visual_clues: str = Field(
        description="你得出该结论的关键视觉依据（例如：看到了标志性的木质前台柜台，且柜台后站着红发的皮埃尔）"
    )
    detail_desc: str = Field(
        description=(
            "对玩家当前具体位置的极细致描述。请结合游戏画面的空间方位（如：正中央、左上角、右下角）"
            "以及紧邻的像素参照物（如：站在电视机左侧、紧贴着向下的出门大门、停在巴士左侧的柏油路上）。"
            "字数控制在 20-50 字内，必须精确到‘在什么物体的什么方位’。"
        )
    )
