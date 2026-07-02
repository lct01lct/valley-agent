from typing import List, Tuple

from pydantic import BaseModel, Field, RootModel
from agent.action.location.location import Location

plan_path_finding_prompt = """
# Role
你是一个高精度的星露谷物语（Stardew Valley）自动化 AI 寻路决策系统。你通过分析地图网格数据和 A* 算法生成的原生路径，操控玩家 Agent 在游戏内进行最高效的移动和障碍清除。

# Task
请根据我提供的当前游戏状态，基于你对《星露谷物语》地图连接关系的了解，为玩家规划离开当前场景、切入下一个转场场景的宏观节点信息。
注意：你不需要提供微观的按键移动路径（如“向左走几格”），只需判断要前往最终目的地，当前场景的“出口大门/路口”是什么。

# Input Context (当前游戏状态变量)
- 玩家当前所在场景：{current_location}
- 玩家最终任务：{mission_text}  # 例如：前往铁匠铺与克林特对话
- 候选场景范围：{locations}

# CoT (思维链 - 请根据你在《星露谷物语》中的游戏知识进行以下逻辑推导)
1. 【推导本场景的物理出口（转场闸门）】：
   在《星露谷物语》的拓扑地图中，要一步步接近目标，玩家在当前这张地图中必须去穿过的“下一个场景切换大门”、“地图分界线路口”或“最终交互目标”是什么？（这对应 next_location）

2. 【终点状态判定】：
   本场景的 `next_location` 是否就是我们本次任务的最终目的地？
   - 如果是的（例如当前在铁匠铺，目标就是前台克林特），is_final 为 true。
   - 如果这只是一个中间过路的转场大门（例如当前在农舍，要穿过大门去农场），is_final 为 false。

3. 【补充】：
   - 目标所在的场景可能就是初始场景
"""


class LocationMoveChainItem(BaseModel):
    current_location: Location = Field(description="当前所在的场景名称")
    next_location: Location = Field(description="本阶段需要去场景名称")
    is_final: bool = Field(description="是否就是最终目的地")


# class LocationMoveChainItemWithMovementHistory(BaseModel):
#     current_location: Location = Field(description="当前所在的场景名称")
#     next_location: Location = Field(description="本阶段需要去场景名称或者交互目标")
#     is_final: bool = Field(description="是否就是最终目的地")
#     movement_history: List[Tuple[int, int]] = Field(description="移动历史记录")


LocationMoveChain = RootModel[list[LocationMoveChainItem]]
