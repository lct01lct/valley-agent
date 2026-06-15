from pydantic import BaseModel, Field, RootModel
from agent.action.scene.scene import Scene

plan_path_finding_prompt = """
# Role
你是一个精通经典模拟经营游戏《星露谷物语》（Stardew Valley）世界地图拓扑结构与关卡设计的游戏数据专家。

# Task
请根据我提供的当前游戏状态，基于你对《星露谷物语》地图连接关系的了解，为玩家规划离开当前场景、切入下一个转场场景的宏观节点信息。
注意：你不需要提供微观的按键移动路径（如“向左走几格”），只需判断要前往最终目的地，当前场景的“出口大门/路口”是什么，以及大致的行进方向。

# Input Context (当前游戏状态变量)
- 玩家当前所在场景：{scene_name}
- 玩家所在具体位置: {current_position}
- 玩家最终任务终点：{mission_text}  # 例如：前往铁匠铺与克林特对话
- 候选场景范围：{scenes}

# CoT (思维链 - 请根据你在《星露谷物语》中的游戏知识进行以下逻辑推导)
1. 【判定当前初始落脚点】：
   当玩家刚切入或身处【{scene_name}】这个场景时，他通常是从哪个标志性位置出发的？（这对应 current_position，例如：“农舍内部的床铺左侧”或“小镇最左侧的土路入口”）

2. 【推导本场景的物理出口（转场闸门）】：
   在《星露谷物语》的拓扑地图中，要从【{scene_name}】一步步接近目标，玩家在当前这张地图中必须去穿过的“下一个场景切换大门”、“地图分界线路口”或“最终交互目标”是什么？（这对应 next_gate，例如：“农舍正下方的出门大门”或“通往巴士站的右手边东侧路口”）

3. 【确认键盘驱动的大致方位】：
   在游戏地图中，从刚才推导的初始落脚点移动到这个目标出口，玩家在屏幕上整体应该朝哪个大方向走？（这对应 general_direction，例如：“向下（南）”或 “向右（东）”）

4. 【终点状态判定】：
   本场景的 `next_position` 是否就是我们本次任务的最终目的地？
   - 如果是的（例如当前在铁匠铺，目标就是前台克林特），is_final 为 true。
   - 如果这只是一个中间过路的转场大门（例如当前在农舍，要穿过大门去农场），is_final 为 false。

5. 【补充】：
   - 目标所在的场景可能就是初始场景，所以只需要的从 current_position 移动到 next_position
   - 别忘了进入最后一个场景后，任可能有一段前往目标的路

# Output Format (请严格按以下 JSON 数组格式回复，最外层是方括号 []，不要包含任何多余的解释)
```json
[
  {{
    "scene_name": "当前所在的场景",
    "current_position": "初始物理落脚点描述",
    "next_gate": "本阶段的转场大门",
    "general_direction": "行进的大致方位",
    "is_final": false
  }},
  {{
    "scene_name": "下一个转场场景",
    ...
  }}
]
"""


class SceneChainItem(BaseModel):
    scene_name: Scene = Field(description="当前所在的场景名称")
    current_position: str = Field(description="对初始物理落脚点的清晰描述（例如: '巴士站地图最左侧的柏油路口'")
    next_position: str = Field(
        description="本阶段需要去锁定的转场大门或交互目标（例如: '通往小镇区域的右手边东侧分界线'）"
    )
    general_direction: str = Field(description="大致行进方向")
    is_final: bool = Field(description="next_position 是否就是最终目的地")


SceneChain = RootModel[list[SceneChainItem]]
