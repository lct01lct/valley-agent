from dataclasses import dataclass
from typing import Literal

from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.action.valley_action.clearance_policy import decide_clear_obstacle, get_obstacle_type_at_tile
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.farm_node import FarmTask
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.tool_selection import count_inventory_items, find_tool_item, select_required_tool_for_obstacle
from server.valley_server import StardewState
from server.type import Tile


type FarmResourceIssueCode = Literal[
    "MISSING_TOOL_IN_INVENTORY",  # 背包/工具栏里缺少必要工具；未来可由 Chest 节点去箱子取回。
    "MISSING_SEED_IN_INVENTORY",  # 背包里没有目标种子。
    "INSUFFICIENT_SEED_COUNT",  # 背包里目标种子数量不足以完成当前规划数量。
    "MISSING_WATERING_CAN_STATE",  # 水壶存在，但 SMAPI state 没有同步 WaterLeft/WaterCapacity。
]


FARM_TOOL_NAME = {
    "Hoe": "Hoe",
    "Watering Can": "Watering Can",
}


@dataclass(frozen=True)
class FarmResourceIssue:
    code: FarmResourceIssueCode
    resource_name: str
    required_count: int = 1
    available_count: int = 0
    detail: str = ""


class FarmResourceCheckNode(BTNode):
    """
    Farm 任务执行前的资源检查节点。

    当前只检查玩家背包/工具栏 state 中已经可见的资源；如果工具或种子在箱子里，本节点不会直接开箱，
    而是把缺口写入 blackboard，留给未来 Chest/取物节点或 LLM 规划器恢复。
    """

    def __init__(self) -> None:
        self._passed_task_signature: tuple | None = None

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        if not blackboard.macro_plan or blackboard.current_step_index >= len(blackboard.macro_plan):
            self._reset()
            return "FAILURE"

        current_task = blackboard.macro_plan[blackboard.current_step_index]
        if not isinstance(current_task, FarmTask):
            self._reset()
            return "FAILURE"

        if current_task.task_type != "FARM":
            self._reset()
            return "FAILURE"

        game_state = context.state
        if game_state is None:
            return "RUNNING"

        task_signature = self._build_task_signature(blackboard, current_task)
        if self._passed_task_signature == task_signature:
            return "SUCCESS"

        issues = self._collect_resource_issues(game_state, current_task)
        if issues:
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
            self._fail(blackboard, current_task, issues)
            return "FAILURE"

        self._passed_task_signature = task_signature
        blackboard.farm_resource_check_failed = False
        blackboard.farm_missing_resources = []
        blackboard.farm_resource_recovery_hint = None
        print(f"\n🟢 [FarmResourceCheckNode] Farm 资源检查通过: action={current_task.farm_action}")
        return "SUCCESS"

    def _collect_resource_issues(self, game_state: StardewState, current_task: FarmTask) -> list[FarmResourceIssue]:
        issues: list[FarmResourceIssue] = []

        required_tools = self._collect_required_tools(game_state, current_task)
        for tool_name in sorted(required_tools):
            if find_tool_item(game_state, tool_name) is not None:
                continue
            issues.append(
                FarmResourceIssue(
                    code="MISSING_TOOL_IN_INVENTORY",
                    resource_name=tool_name,
                    detail="当前 state 只能确认背包/工具栏缺少该工具；如果工具在箱子里，需要后续 Chest 节点取回。",
                )
            )

        if current_task.farm_action in ("PLANT", "PLANT_AND_WATER"):
            issues.extend(self._collect_seed_issues(game_state, current_task))

        if "Watering Can" in required_tools:
            watering_can = find_tool_item(game_state, "Watering Can")
            if watering_can is not None and (watering_can.water_left is None or watering_can.water_capacity is None):
                issues.append(
                    FarmResourceIssue(
                        code="MISSING_WATERING_CAN_STATE",
                        resource_name="Watering Can",
                        detail="水壶存在，但 state 缺少 WaterLeft/WaterCapacity，无法可靠判断补水需求。",
                    )
                )

        return issues

    def _collect_required_tools(self, game_state: StardewState, current_task: FarmTask) -> set[str]:
        required_tools: set[str] = set()

        if current_task.farm_action in ("PLANT", "PLANT_AND_WATER"):
            required_tools.add(FARM_TOOL_NAME["Hoe"])
            required_tools.update(self._collect_clear_obstacle_tools(game_state, current_task))

        if current_task.farm_action in ("WATER", "PLANT_AND_WATER"):
            required_tools.add(FARM_TOOL_NAME["Watering Can"])

        return required_tools

    def _collect_clear_obstacle_tools(self, game_state: StardewState, current_task: FarmTask) -> set[str]:
        required_tools: set[str] = set()
        for target_tile in self._get_plant_candidate_tiles(current_task):
            obstacle_type = get_obstacle_type_at_tile(game_state, target_tile)
            clear_decision = decide_clear_obstacle(game_state, target_tile, obstacle_type, "Farm")
            if not clear_decision.can_clear or clear_decision.obstacle_type is None:
                continue

            required_tool = select_required_tool_for_obstacle(
                game_state,
                clear_decision.obstacle_type,
                target_tile,
                "Farm",
            )
            if required_tool is not None:
                required_tools.add(required_tool)
        return required_tools

    def _collect_seed_issues(self, game_state: StardewState, current_task: FarmTask) -> list[FarmResourceIssue]:
        if current_task.seed_name is None:
            return [
                FarmResourceIssue(
                    code="MISSING_SEED_IN_INVENTORY",
                    resource_name="<missing seed_name>",
                    detail="种植任务缺少 seed_name，无法选择种子。",
                )
            ]

        required_seed_count = self._estimate_required_seed_count(game_state, current_task)
        available_seed_count = count_inventory_items(game_state, current_task.seed_name)
        if available_seed_count <= 0:
            return [
                FarmResourceIssue(
                    code="MISSING_SEED_IN_INVENTORY",
                    resource_name=current_task.seed_name,
                    required_count=required_seed_count,
                    available_count=available_seed_count,
                    detail="背包里没有目标种子；如果种子在箱子里，需要后续 Chest 节点取回。",
                )
            ]

        if required_seed_count > 0 and available_seed_count < required_seed_count:
            return [
                FarmResourceIssue(
                    code="INSUFFICIENT_SEED_COUNT",
                    resource_name=current_task.seed_name,
                    required_count=required_seed_count,
                    available_count=available_seed_count,
                    detail="目标种子数量不足；可恢复方案是减少种植数量、去商店购买或从箱子取种子。",
                )
            ]

        return []

    def _estimate_required_seed_count(self, game_state: StardewState, current_task: FarmTask) -> int:
        candidate_tiles = [
            target_tile
            for target_tile in self._get_plant_candidate_tiles(current_task)
            if not self._should_skip_tile_for_seed_estimation(game_state, target_tile)
        ]
        if current_task.count <= 0:
            return len(candidate_tiles)
        return min(current_task.count, len(candidate_tiles))

    def _should_skip_tile_for_seed_estimation(self, game_state: StardewState, target_tile: Tile) -> bool:
        obstacle_type = get_obstacle_type_at_tile(game_state, target_tile)
        clear_decision = decide_clear_obstacle(game_state, target_tile, obstacle_type, "Farm")
        if clear_decision.should_skip_tile:
            return True

        farm_tile_state = game_state.farm_tiles_by_tile.get(target_tile)
        if farm_tile_state is not None and farm_tile_state.has_crop:
            return True

        return False

    def _get_plant_candidate_tiles(self, current_task: FarmTask) -> list[Tile]:
        if current_task.target_tiles:
            return current_task.target_tiles

        if current_task.area_origin is None or current_task.area_width <= 0 or current_task.area_height <= 0:
            return []

        return [
            Tile(current_task.area_origin.x + dx, current_task.area_origin.y + dy)
            for dy in range(current_task.area_height)
            for dx in range(current_task.area_width)
        ]

    def _fail(
        self,
        blackboard: AgentBlackboard,
        current_task: FarmTask,
        issues: list[FarmResourceIssue],
    ) -> None:
        missing_resources = [self._format_issue(issue) for issue in issues]
        blackboard.farm_resource_check_failed = True
        blackboard.farm_missing_resources = missing_resources
        blackboard.farm_resource_recovery_hint = (
            "Farm 任务资源不足。当前只能确认背包/工具栏缺口；若资源在箱子里，需要后续 Chest/取物节点补计划。"
        )
        blackboard.prompt = (
            f"Farm 任务资源检查失败，需要恢复计划。task={current_task.desc}; "
            f"missing_resources={missing_resources}; hint={blackboard.farm_resource_recovery_hint}"
        )
        blackboard.macro_plan = []
        blackboard.current_step_index = 0
        self._passed_task_signature = None
        print("\n🔴 [FarmResourceCheckNode] Farm 资源检查失败，已停止并请求恢复计划。")
        for issue in issues:
            print(f"   - {self._format_issue(issue)}")

    def _format_issue(self, issue: FarmResourceIssue) -> str:
        return (
            f"code={issue.code}, resource={issue.resource_name}, "
            f"required={issue.required_count}, available={issue.available_count}, detail={issue.detail}"
        )

    def _build_task_signature(self, blackboard: AgentBlackboard, current_task: FarmTask) -> tuple:
        return (
            blackboard.current_step_index,
            current_task.task_type,
            current_task.farm_action,
            current_task.target_loc,
            current_task.seed_name,
            current_task.count,
            tuple(current_task.target_tiles),
            current_task.area_origin,
            current_task.area_width,
            current_task.area_height,
        )

    def _reset(self) -> None:
        self._passed_task_signature = None
