from enum import Enum
from typing import List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

from agent.action.location.location import Location


class StardewAction(Enum):
    # =========================================================================
    # 🏃 1. 基础位移与状态相关 (Movement & Basic State)
    # =========================================================================
    IDLE = "IDLE"  # 原地待命，不进行任何操作
    MOVE_UP = "MOVE_UP"  # 朝上方移动一格 (触发 W 键)
    MOVE_DOWN = "MOVE_DOWN"  # 朝下方移动一格 (触发 S 键)
    MOVE_LEFT = "MOVE_LEFT"  # 朝左方移动一格 (触发 A 键)
    MOVE_RIGHT = "MOVE_RIGHT"  # 朝右方移动一格 (触发 D 键)
    MOVE_UP_RIGHT = "MOVE_UP_RIGHT"  # 朝右上角斜向移动 (同时触发 W + D 键)
    MOVE_UP_LEFT = "MOVE_UP_LEFT"  # 朝左上角斜向移动 (同时触发 W + A 键)
    MOVE_DOWN_RIGHT = "MOVE_DOWN_RIGHT"  # 朝右下角斜向移动 (同时触发 S + D 键)
    MOVE_DOWN_LEFT = "MOVE_DOWN_LEFT"  # 朝左下角斜向移动 (同时触发 S + A 键)
    FACE_DIRECTION = "FACE_DIRECTION"  # 原地调整朝向，不持续按住移动键

    CLOSE_DIALOG = "CLOSE_DIALOG"  # 关闭对话框：按下 X 键，关闭当前弹出的对话框或提示框

    # =========================================================================
    # 🛠️ 2. 农业与资源采集工具 (Tool Actions - 绝大多数消耗体力)
    # =========================================================================
    SWITCH_TOOL = "SWITCH_TOOL"  # 切换工具：按下数字键 1-9，切换当前手中处于激活状态的工具或物品
    USE_TOOL = "USE_TOOL"  # 使用工具：手持工具右键使用，触发对应的工具行为（如锄地、砍树、浇水等）
    USE_ITEM = "USE_ITEM"  # 使用当前手持物品：用于种子播种、放置物品等非工具动作
    # USE_AXE = "USE_AXE"  # 使用斧头：砍伐树木、清除大树桩、清理散落的木头
    # USE_PICKAXE = "USE_PICKAXE"  # 使用镐子：开采矿石、砸碎废石、清除错误刨出的耕地、回收摆放的家具
    # USE_HOE = "USE_HOE"  # 使用锄头：在可耕种土地上锄地、挖掘远古蚯蚓(远古种子/文物)
    # USE_WATERING_CAN = "USE_WATERING_CAN"  # 使用浇水壶：给耕地上的作物浇水、给宠物的水碗倒水
    # USE_SCYTHE = "USE_SCYTHE"  # 使用镰刀：收割成熟牧草(转化为干草)、清理杂草 (不消耗体力)
    # USE_FISHING_ROD = "USE_FISHING_ROD"  # 使用鱼竿：在水边抛竿、拉竿、触发并执行钓鱼小游戏
    # USE_MILK_PAIL = "USE_MILK_PAIL"  # 使用挤奶桶：靠近并采集奶牛、山羊等动物的奶
    # USE_SHEARS = "USE_SHEARS"  # 使用剪毛器/毛刷：靠近绵羊剪取羊毛，或抚摸动物提高好感度

    # =========================================================================
    # 📦 3. 世界交互与空手采集 (World Interaction - 通常不消耗体力)
    # =========================================================================
    HARVEST_CROP = "HARVEST_CROP"  # 空手采摘：采集已成熟的农作物（如防风草、土豆、蔓越莓等）
    PICKUP_FORAGE = "PICKUP_FORAGE"  # 野外觅食：捡起野外或海滩上散落的季节性觅食物（如松露、浆果、贝壳）
    INTERACT_OBJECT = "INTERACT_OBJECT"  # 激活设备：往熔炉塞矿石、往酿酒桶塞水果、打开/关闭宝箱提取物品
    OPEN_DOOR = "OPEN_DOOR"  # 场景开门：打开或关闭建筑物的正门、或者动物小屋让动物进出的闸门
    TALK_NPC = "TALK_NPC"  # NPC 对话：每日靠近村民进行交谈，用于刷好感度或推进任务
    GIFT_NPC = "GIFT_NPC"  # NPC 送礼：手持特定物品右键村民，触发每日/生日送礼行为

    # =========================================================================
    # 🩹 4. 自身状态管理 (Self-Management)
    # =========================================================================
    EAT_FOOD = "EAT_FOOD"  # 吃食物：手持可食用道具（如药水、奶酪、沙拉）吃下，补充体力和生命值
    SWITCH_TOOLBAR = "SWITCH_TOOLBAR"  # 切换快捷栏：通过数字键 1-9 切换当前手中处于激活状态的工具或物品
    GO_TO_SLEEP = "GO_TO_SLEEP"  # 上床睡觉：走到床边并确认弹出的“是否睡觉”对话框，强制结束这一天

    # =========================================================================
    # ⚔️ 5. 矿区战斗与危险应对 (Combat & Mining Exploration)
    # =========================================================================
    ATTACK_WEAPON = "ATTACK_WEAPON"  # 武器攻击：挥动剑、锤子、匕首等武器攻击史莱姆、蝙蝠等矿区怪物
    DEFEND_SWORD = "DEFEND_SWORD"  # 武器格挡：使用大剑类武器时的右键防御防守，抵挡怪物的突袭
    PLACE_BOMB = "PLACE_BOMB"  # 放置炸弹：手持并在地面放置樱桃炸弹/巨型炸弹，进行大面积岩石与矿产爆破

    # =========================================================================
    # 🧠 6. 低频地图知识查询 (Low-frequency Knowledge Queries)
    # =========================================================================
    QUERY_WATER_SOURCES = "QUERY_WATER_SOURCES"  # 查询当前或指定场景中的可补水水源坐标，用于地图知识缓存


type KeyType = Literal[
    "w",
    "a",
    "s",
    "d",
    "x",
    "c",
    "tab",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    "0",
    "-",
    "=",
]


class MouseType(BaseModel):
    event: Literal["right", "left"]
    position: Tuple[float, float]


class StardewCommand(BaseModel):
    action: StardewAction
    key: List[KeyType] | None = Field(default=None)
    mouse: MouseType | None = Field(default=None)
    location_name: Location | None = Field(default=None)
