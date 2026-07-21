from typing import Dict, List

from server.type import Tile

from agent.base_task import BaseTask
from agent.behavior_tree.farm_node import FarmTask
from agent.behavior_tree.route_node import RouteTask

TASK_MOCK_DATA: Dict[str, List[BaseTask]] = {
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
}
