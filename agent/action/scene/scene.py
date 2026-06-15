from typing import Literal

SCENES = (
    "农场",
    "农场房子",
    # "温室",
    #
    # --- 核心转场主干道 ---
    "巴士站",  # Bus Stop
    "深山",  # Mountain (罗宾家、矿洞所在的外围地图)
    "铁路",  # Railroad (温泉、火车站所在区域)
    "煤矿森林",  # Cindersap Forest (玛妮牧场、巫师塔、大葱岛所在的外围地图)
    "后山",  # Backwoods (农场上方的秘密通道)
    # 未解锁 # "秘密森林",  # Secret Woods
    #
    # --- 鹈鹕镇 (Pelican Town) 户外与室内建筑 ---
    "小镇",  # Pelican Town (小镇中心户外区域)
    "皮埃尔杂货铺",  # Pierre's General Store (包含皮埃尔家、种子店)
    "哈维的诊所",  # Harvey's Clinic
    "星落沙龙",  # Stardrop Saloon (酒吧)
    "镇长刘易斯的家",  # Mayor's Manor
    "乔治和艾芙琳的家",  # 1 River Road
    "海莉和艾米丽的家",  # 2 Willow Lane
    "乔迪、肯特和山姆的家",  # 3 Willow Lane
    "潘妮和潘姆的房车",  # Trailer (或后期的 社区升级房屋)
    "铁匠铺"  #
    "博物馆",  # Museum (图书馆、冈瑟所在处)
    "乔加超市",  # JojaMart (后期可能变为 电影院 Movie Theater)
    #
    # --- 煤矿森林区域内部建筑 ---
    "玛妮的牧场",  # Marnie's Ranch
    "莉亚的小屋",  # Leah's Cottage
    "巫师塔",  # Wizard's Tower
    #
    # --- 深山区域内部建筑 ---
    "罗宾的木匠铺",  # Carpenter's Shop (包含罗宾、德米特里厄斯、塞巴斯蒂安的家)
    "矿洞",  # The Mines (1-120层前台)
    "冒险家公会",  # Adventurer's Guild
    "采石场",  # Quarry
    "采石场矿洞",  # Quarry Mine
    "温泉",  # Spa
    "女巫的沼泽",  # Witch's Swamp (包含女巫的小屋)
)

type Scene = Literal[
    "农场",
    "农场房子",
    # "温室",
    #
    # --- 核心转场主干道 ---
    "巴士站",  # Bus Stop
    "深山",  # Mountain (罗宾家、矿洞所在的外围地图)
    "铁路",  # Railroad (温泉、火车站所在区域)
    "煤矿森林",  # Cindersap Forest (玛妮牧场、巫师塔、大葱岛所在的外围地图)
    "后山",  # Backwoods (农场上方的秘密通道)
    # 未解锁 # "秘密森林",  # Secret Woods
    #
    # --- 鹈鹕镇 (Pelican Town) 户外与室内建筑 ---
    "小镇",  # Pelican Town (小镇中心户外区域)
    "皮埃尔杂货铺",  # Pierre's General Store (包含皮埃尔家、种子店)
    "哈维的诊所",  # Harvey's Clinic
    "星落沙龙",  # Stardrop Saloon (酒吧)
    "镇长刘易斯的家",  # Mayor's Manor
    "乔治和艾芙琳的家",  # 1 River Road
    "海莉和艾米丽的家",  # 2 Willow Lane
    "乔迪、肯特和山姆的家",  # 3 Willow Lane
    "潘妮和潘姆的房车",  # Trailer (或后期的 社区升级房屋)
    "铁匠铺"  #
    "博物馆",  # Museum (图书馆、冈瑟所在处)
    "乔加超市",  # JojaMart (后期可能变为 电影院 Movie Theater)
    #
    # --- 煤矿森林区域内部建筑 ---
    "玛妮的牧场",  # Marnie's Ranch
    "莉亚的小屋",  # Leah's Cottage
    "巫师塔",  # Wizard's Tower
    #
    # --- 深山区域内部建筑 ---
    "罗宾的木匠铺",  # Carpenter's Shop (包含罗宾、德米特里厄斯、塞巴斯蒂安的家)
    "矿洞",  # The Mines (1-120层前台)
    "冒险家公会",  # Adventurer's Guild
    "采石场",  # Quarry
    "采石场矿洞",  # Quarry Mine
    "温泉",  # Spa
    "女巫的沼泽",  # Witch's Swamp (包含女巫的小屋)
]
