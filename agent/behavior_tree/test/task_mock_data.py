from typing import Dict, List

from server.type import Tile

from agent.base_task import BaseTask
from agent.behavior_tree.chest_node import ChestItemRequest, ChestTask
from agent.behavior_tree.farm_node import FarmTask
from agent.behavior_tree.route_node import RouteTask

TASK_MOCK_DATA: Dict[str, List[BaseTask]] = {
    "CHEST_P0_1": [  # Chest P0 测试数据 1：前往农场，从指定箱子取出防风草种子。
        RouteTask(task_type="ROUTE", desc="前往农场", target_loc="Farm"),
        ChestTask(
            task_type="CHEST",
            desc="从指定箱子取防风草种子",
            chest_action="TAKE",
            target_loc="Farm",
            chest_tile=Tile(64, 15),
            item_name="Parsnip Seeds",
            count=49,
            qualified_item_id="(O)472",
        ),
    ],
    "CHEST_P0_2": [  # Chest P0 测试数据 2：确保背包里有基础农场工具和 49 个防风草种子。
        RouteTask(task_type="ROUTE", desc="前往农场", target_loc="Farm"),
        ChestTask(
            task_type="CHEST",
            desc="一次性从指定箱子取出基础农场工具和 49 个防风草种子",
            chest_action="TAKE",
            target_loc="Farm",
            chest_tile=Tile(64, 15),
            items=[
                ChestItemRequest(item_name="Pickaxe", count=1),
                ChestItemRequest(item_name="Axe", count=1),
                ChestItemRequest(item_name="Scythe", count=1),
                ChestItemRequest(item_name="Parsnip Seeds", count=49, qualified_item_id="(O)472"),
                ChestItemRequest(item_name="Watering Can", count=1),
                ChestItemRequest(item_name="Hoe", count=1),
            ],
        ),
    ],
    "ROUTE_1": [  # ROUTE 测试数据 1：寻路基础任务
        RouteTask(task_type="ROUTE", desc="前往皮埃尔商店", target_loc="SeedShop"),
    ],
    "ROUTE_BACKUP": [  # ROUTE：商店打烊后备选任务
        RouteTask(task_type="ROUTE", desc="前往后山", target_loc="Mountain"),
        RouteTask(task_type="ROUTE", desc="前往农场小屋", target_loc="FarmHouse"),
    ],
    "ROUTE_2": [  # ROUTE 测试数据 2：寻路过程中破障
        RouteTask(task_type="ROUTE", desc="前往后山", target_loc="Backwoods"),
    ],
    "ROUTE_3": [  # ROUTE 测试数据 3：寻路过程中破障
        RouteTask(task_type="ROUTE", desc="前往森林", target_loc="Forest"),
    ],
    "ROUTE_4": [  # ROUTE 测试数据 4：长地图寻路
        RouteTask(task_type="ROUTE", desc="前往铁匠铺", target_loc="Blacksmith"),
    ],
    "FARM_P0_1": [  # Farm P0 测试数据 1：自动选择最近的 1 个未浇水作物。
        RouteTask(task_type="ROUTE", desc="前往农场", target_loc="Farm"),
        FarmTask(
            task_type="FARM",
            desc="自动给最近的一个未浇水作物浇水",
            farm_action="WATER",
            target_loc="Farm",
            count=1,
        ),
    ],
    "FARM_P0_2": [  # Farm P0 测试数据 2：自动连续浇 3 个最近的未浇水作物。
        RouteTask(task_type="ROUTE", desc="前往农场", target_loc="Farm"),
        FarmTask(
            task_type="FARM",
            desc="自动给三个未浇水作物浇水",
            farm_action="WATER",
            target_loc="Farm",
            count=3,
        ),
    ],
    "FARM_P0_3": [  # Farm P0 测试数据 3：只浇指定地块；如果地块没有作物或已浇水，FarmNode 应跳过或失败并写日志。
        RouteTask(task_type="ROUTE", desc="前往农场", target_loc="Farm"),
        FarmTask(
            task_type="FARM",
            desc="给指定作物地块浇水",
            farm_action="WATER",
            target_loc="Farm",
            count=1,
            target_tiles=[Tile(66, 18)],
        ),
    ],
    "FARM_P0_4": [  # Farm P0 测试数据 4：浇完所有可见未浇水作物；count <= 0 表示不限制数量。
        RouteTask(task_type="ROUTE", desc="前往农场", target_loc="Farm"),
        FarmTask(
            task_type="FARM",
            desc="给所有未浇水作物浇水",
            farm_action="WATER",
            target_loc="Farm",
            count=0,
        ),
    ],
    "FARM_P1_1": [  # Farm P1 测试数据 1：指定单格，验证锄地 -> 播种 -> 浇水闭环。
        RouteTask(task_type="ROUTE", desc="前往农场", target_loc="Farm"),
        FarmTask(
            task_type="FARM",
            desc="在指定地块种植防风草并浇水",
            farm_action="PLANT_AND_WATER",
            target_loc="Farm",
            seed_name="Parsnip Seeds",
            count=1,
            target_tiles=[Tile(64, 25)],
        ),
    ],
    "FARM_P1_2": [  # Farm P1 测试数据 2：规划 7x7 区域种防风草；树/树桩跳过，其余可清障碍先清理。
        RouteTask(task_type="ROUTE", desc="前往农场", target_loc="Farm"),
        FarmTask(
            task_type="FARM",
            desc="规划 7x7 区域种植防风草并浇水",
            farm_action="PLANT_AND_WATER",
            target_loc="Farm",
            seed_name="Parsnip Seeds",
            count=49,
            area_origin=Tile(63, 25),
            area_width=7,
            area_height=7,
        ),
    ],
}
