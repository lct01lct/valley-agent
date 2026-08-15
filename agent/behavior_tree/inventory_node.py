import time
from typing import Literal

from agent.action.chest.chest_knowledge_service import ChestKnowledgeService
from agent.action.inventory.inventory_fill_policy import InventoryFillPolicy, InventoryGoal
from agent.action.location.location import Location
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.base_task import BaseTask, TaskType
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.chest_node import ChestNode, ChestTask
from agent.behavior_tree.inventory_debug_logger import InventoryDebugLogger
from agent.behavior_tree.player_context import PlayerContext
from server.valley_server import StardewState
from server.type import Tile


type InventoryAction = Literal[
    "FILL_INVENTORY",  # 从当前场景箱子取任务无关物品，直到背包 FreeSlots == 0。
    "EMPTY_CHEST_TO_INVENTORY",  # 尽量把指定或最近箱子里的物品转入背包，受背包容量限制。
    "REACH_INVENTORY_STATE",  # 按 InventoryGoal 达成背包目标状态；未来 Planner/LLM 的优先输出形式。
]


INVENTORY_ACTION_TIMEOUT_SECONDS = 18.0


class InventoryTask(BaseTask):
    def __init__(
        self,
        task_type: TaskType,
        desc: str,
        inventory_action: InventoryAction,
        target_loc: Location,
        chest_tile: Tile | None = None,
        goal: InventoryGoal | None = None,
    ):
        super().__init__(task_type=task_type, desc=desc)
        self.inventory_action = inventory_action
        self.target_loc: Location = target_loc
        self.chest_tile = chest_tile
        self.goal = goal or self._build_legacy_goal(inventory_action)

    def _build_legacy_goal(self, inventory_action: InventoryAction) -> InventoryGoal | None:
        if inventory_action == "FILL_INVENTORY":
            return InventoryGoal.fill_inventory()
        return None


class InventoryNode(BTNode):
    """
    背包高层目标状态节点。

    本节点不直接操作箱子协议，而是基于 state / MapKnowledgeCache 做策略选择，
    再临时复用 ChestNode 执行 SCAN / QUERY / TAKE。
    """

    def __init__(self) -> None:
        self.fill_policy = InventoryFillPolicy()
        self.chest_node = ChestNode()
        self.chest_knowledge_service = ChestKnowledgeService(self._log)
        self.debug_logger = InventoryDebugLogger()
        self._task_signature: tuple | None = None
        self._started_at: float | None = None
        self._active_chest_task: ChestTask | None = None
        self._observed_chest_tiles_for_goal: set[tuple[int, int]] = set()
        self._last_temporary_chest_task_completed = False

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        if not blackboard.macro_plan or blackboard.current_step_index >= len(blackboard.macro_plan):
            self._reset()
            return "FAILURE"

        current_task = blackboard.macro_plan[blackboard.current_step_index]
        if not isinstance(current_task, InventoryTask):
            self._reset()
            return "FAILURE"

        if current_task.task_type != "INVENTORY":
            self._reset()
            return "FAILURE"

        game_state = context.state
        if game_state is None:
            return "RUNNING"

        if self._is_new_task(blackboard, current_task):
            self._start(blackboard, game_state, current_task)

        if game_state.location_name != current_task.target_loc:
            self._fail(
                context,
                blackboard,
                current_task,
                f"当前场景不是 InventoryTask 目标场景: current={game_state.location_name}, target={current_task.target_loc}",
            )
            return "FAILURE"

        if self._started_at is not None and time.time() - self._started_at > INVENTORY_ACTION_TIMEOUT_SECONDS:
            self._fail(context, blackboard, current_task, "InventoryTask 执行超时")
            return "FAILURE"

        if current_task.inventory_action == "FILL_INVENTORY":
            return await self._run_fill_inventory(context, blackboard, game_state, current_task)

        if current_task.inventory_action == "REACH_INVENTORY_STATE":
            return await self._run_reach_inventory_state(context, blackboard, game_state, current_task)

        if current_task.inventory_action == "EMPTY_CHEST_TO_INVENTORY":
            return await self._run_empty_chest_to_inventory(context, blackboard, game_state, current_task)

        self._fail(context, blackboard, current_task, f"暂不支持 InventoryAction: {current_task.inventory_action}")
        return "FAILURE"

    def _start(
        self,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: InventoryTask,
    ) -> None:
        self._task_signature = self._build_task_signature(blackboard, current_task)
        self._started_at = time.time()
        self._active_chest_task = None
        self._observed_chest_tiles_for_goal = set()
        self._last_temporary_chest_task_completed = False
        print(f"\n🎒 [InventoryNode] 开始背包目标任务: action={current_task.inventory_action}")
        self._log(
            f"开始 InventoryTask: action={current_task.inventory_action}, target_loc={current_task.target_loc}, "
            f"chest={current_task.chest_tile}, free_slots={game_state.inventory.free_slots}, "
            f"occupied={game_state.inventory.occupied_slots}/{game_state.inventory.max_items}, "
            f"goal={self._format_goal(current_task.goal)}"
        )

    async def _run_reach_inventory_state(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: InventoryTask,
    ) -> NodeStatus:
        goal = current_task.goal
        if goal is None:
            self._fail(context, blackboard, current_task, "REACH_INVENTORY_STATE 缺少 InventoryGoal")
            return "FAILURE"

        if goal.target_free_slots is None:
            self._fail(context, blackboard, current_task, f"暂不支持的 InventoryGoal: {self._format_goal(goal)}")
            return "FAILURE"

        return await self._run_fill_inventory(context, blackboard, game_state, current_task)

    async def _run_fill_inventory(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: InventoryTask,
    ) -> NodeStatus:
        if self._active_chest_task is not None:
            return await self._run_active_chest_task(context, blackboard, current_task)

        goal = current_task.goal or InventoryGoal.fill_inventory()
        free_slots = int(game_state.inventory.free_slots or 0)
        target_free_slots = max(0, int(goal.target_free_slots or 0))
        if free_slots <= target_free_slots:
            print("\n🟢 [InventoryNode] 背包已填满。")
            self._log(
                f"背包目标状态已达成，InventoryTask 完成: free_slots={game_state.inventory.free_slots}, "
                f"target_free_slots={target_free_slots}, occupied={game_state.inventory.occupied_slots}/{game_state.inventory.max_items}"
            )
            self._complete(blackboard)
            return "SUCCESS"

        include_stale = bool(goal.allow_stale_chest_cache and "KNOWN_CHESTS" in goal.allowed_sources)
        chest_contents = context.map_knowledge_cache.get_chest_contents(current_task.target_loc, include_stale=include_stale)

        next_task = self._get_next_task(blackboard)
        take_plan = self.fill_policy.build_fill_inventory_take_plan(game_state, chest_contents, next_task, goal)
        if take_plan is None:
            observe_task = self._build_next_chest_observation_task(context, game_state, current_task, goal)
            if observe_task is not None:
                self._active_chest_task = observe_task
                return await self._run_active_chest_task(context, blackboard, current_task)

            self._fail(
                context,
                blackboard,
                current_task,
                f"没有可达成背包目标状态的新物品: free_slots={free_slots}, "
                f"target_free_slots={target_free_slots}, known_chests={len(chest_contents)}, goal={self._format_goal(goal)}",
            )
            return "FAILURE"

        self._active_chest_task = ChestTask(
            task_type="CHEST",
            desc="Inventory Goal：从已观察箱子取物达成背包目标状态",
            chest_action="TAKE",
            target_loc=current_task.target_loc,
            chest_tile=take_plan.chest_content.tile,
            items=take_plan.item_requests,
        )
        self._log(
            f"生成背包目标状态取物任务: chest={take_plan.chest_content.tile}, "
            f"items={self._format_item_requests(take_plan.item_requests)}, reason={take_plan.reason}, "
            f"goal={self._format_goal(goal)}"
        )
        return await self._run_active_chest_task(context, blackboard, current_task)

    async def _run_empty_chest_to_inventory(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: InventoryTask,
    ) -> NodeStatus:
        if self._active_chest_task is not None:
            return await self._run_active_chest_task(context, blackboard, current_task)

        chest_tile = self._resolve_chest_tile(context, game_state, current_task)
        if chest_tile is None:
            self._fail(context, blackboard, current_task, "无法解析要清空的箱子")
            return "FAILURE"

        chest_content = context.map_knowledge_cache.get_chest_content(current_task.target_loc, chest_tile)
        if chest_content is None:
            self._active_chest_task = ChestTask(
                task_type="CHEST",
                desc="Inventory EMPTY：打开目标箱子查看内容",
                chest_action="QUERY",
                target_loc=current_task.target_loc,
                chest_tile=chest_tile,
            )
            self._log(f"缺少目标箱子内容缓存，先打开查看: chest={chest_tile}")
            return await self._run_active_chest_task(context, blackboard, current_task)

        if not chest_content.items:
            print(f"\n🟢 [InventoryNode] 目标箱子已清空: chest={chest_tile}")
            self._complete(blackboard)
            return "SUCCESS"

        take_plan = self.fill_policy.build_empty_chest_take_plan(game_state, chest_content)
        if take_plan is None:
            self._fail(
                context,
                blackboard,
                current_task,
                f"背包容量不足，无法继续清空箱子: chest={chest_tile}, free_slots={game_state.inventory.free_slots}",
            )
            return "FAILURE"

        self._active_chest_task = ChestTask(
            task_type="CHEST",
            desc="Inventory EMPTY：尽量把箱子物品取入背包",
            chest_action="TAKE",
            target_loc=current_task.target_loc,
            chest_tile=chest_tile,
            items=take_plan.item_requests,
        )
        self._log(
            f"生成清空箱子取物任务: chest={chest_tile}, "
            f"items={self._format_item_requests(take_plan.item_requests)}, reason={take_plan.reason}"
        )
        return await self._run_active_chest_task(context, blackboard, current_task)

    async def _run_active_chest_task(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        current_task: InventoryTask,
    ) -> NodeStatus:
        if self._active_chest_task is None:
            return "FAILURE"

        status = await self._run_chest_node_with_temporary_plan(blackboard, context, self._active_chest_task)
        if status == "RUNNING":
            return "RUNNING"

        if self._last_temporary_chest_task_completed:
            self._log(f"临时 ChestTask 完成: desc={self._active_chest_task.desc}")
            if self._active_chest_task.chest_action in ("QUERY", "SCAN") and self._active_chest_task.chest_tile is not None:
                self._observed_chest_tiles_for_goal.add(
                    (self._active_chest_task.chest_tile.x, self._active_chest_task.chest_tile.y)
                )
            self._active_chest_task = None
            self.chest_node._reset()
            return "RUNNING"

        self._log(f"临时 ChestTask 未完成: status={status}, desc={self._active_chest_task.desc}")
        failed_chest_task_desc = self._active_chest_task.desc
        self._active_chest_task = None
        self.chest_node._reset()
        self._fail(context, blackboard, current_task, f"临时 ChestTask 未完成: {failed_chest_task_desc}")
        return "FAILURE"

    async def _run_chest_node_with_temporary_plan(
        self,
        blackboard: AgentBlackboard,
        context: PlayerContext,
        chest_task: ChestTask,
    ) -> NodeStatus:
        original_plan = blackboard.macro_plan
        original_step_index = blackboard.current_step_index
        original_prompt = blackboard.prompt
        self._last_temporary_chest_task_completed = False
        try:
            blackboard.macro_plan = [chest_task]
            blackboard.current_step_index = 0
            status = await self.chest_node.run(blackboard, context)
            self._last_temporary_chest_task_completed = blackboard.current_step_index >= 1
            if self._last_temporary_chest_task_completed:
                return "SUCCESS"
            if status == "RUNNING":
                return "RUNNING"
            return status
        finally:
            temporary_prompt = blackboard.prompt
            blackboard.macro_plan = original_plan
            blackboard.current_step_index = original_step_index
            if temporary_prompt != original_prompt and not self._last_temporary_chest_task_completed:
                blackboard.prompt = temporary_prompt
            else:
                blackboard.prompt = original_prompt

    def _resolve_chest_tile(
        self,
        context: PlayerContext,
        game_state: StardewState,
        current_task: InventoryTask,
    ) -> Tile | None:
        if current_task.chest_tile is not None:
            return current_task.chest_tile

        cached_chests = context.map_knowledge_cache.get_chest_locations(current_task.target_loc)
        if not cached_chests:
            cached_chests = self.chest_knowledge_service.query_chests(context, current_task.target_loc) or []
        if not cached_chests:
            return None

        return sorted(
            [chest.tile for chest in cached_chests],
            key=lambda tile: (
                abs(tile.x - game_state.player_tile.x) + abs(tile.y - game_state.player_tile.y),
                tile.x,
                tile.y,
            ),
        )[0]

    def _build_next_chest_observation_task(
        self,
        context: PlayerContext,
        game_state: StardewState,
        current_task: InventoryTask,
        goal: InventoryGoal,
    ) -> ChestTask | None:
        if "OBSERVED_CHESTS" not in goal.allowed_sources:
            self._log(f"InventoryGoal 不允许观察新箱子: goal={self._format_goal(goal)}")
            return None

        chest_locations = context.map_knowledge_cache.get_chest_locations(current_task.target_loc)
        if not chest_locations:
            chest_locations = self.chest_knowledge_service.query_chests(context, current_task.target_loc) or []
        if not chest_locations:
            self._log(f"当前场景没有可观察箱子: location={current_task.target_loc}")
            return None

        chest_contents = context.map_knowledge_cache.get_chest_contents(current_task.target_loc, include_stale=True)
        observe_plan = self.fill_policy.build_next_chest_observation_plan(
            game_state,
            chest_locations,
            chest_contents,
            goal,
            self._observed_chest_tiles_for_goal,
        )
        if observe_plan is None:
            self._log(
                f"没有剩余可观察箱子: location={current_task.target_loc}, "
                f"known_contents={len(chest_contents)}, observed={sorted(self._observed_chest_tiles_for_goal)}"
            )
            return None

        self._log(
            f"生成箱子观察任务: chest={observe_plan.chest_tile}, reason={observe_plan.reason}, "
            f"goal={self._format_goal(goal)}"
        )
        return ChestTask(
            task_type="CHEST",
            desc="Inventory Goal：观察一个候选箱子内容",
            chest_action="QUERY",
            target_loc=current_task.target_loc,
            chest_tile=observe_plan.chest_tile,
        )

    def _complete(self, blackboard: AgentBlackboard) -> None:
        blackboard.current_step_index += 1
        self._reset()

    def _fail(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        current_task: InventoryTask,
        reason: str,
    ) -> None:
        context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
        blackboard.prompt = f"InventoryTask {current_task.inventory_action} 失败，需要重新规划：{reason}"
        blackboard.macro_plan = []
        blackboard.current_step_index = 0
        print(f"\n🔴 [InventoryNode] {reason}")
        self._log(
            f"InventoryTask 失败: action={current_task.inventory_action}, "
            f"target_loc={current_task.target_loc}, chest={current_task.chest_tile}, reason={reason}"
        )
        self._reset()

    def _is_new_task(self, blackboard: AgentBlackboard, current_task: InventoryTask) -> bool:
        return self._task_signature != self._build_task_signature(blackboard, current_task)

    def _build_task_signature(self, blackboard: AgentBlackboard, current_task: InventoryTask) -> tuple:
        return (
            blackboard.current_step_index,
            current_task.task_type,
            current_task.inventory_action,
            current_task.target_loc,
            current_task.chest_tile,
            current_task.goal,
        )

    def _get_next_task(self, blackboard: AgentBlackboard) -> BaseTask | None:
        next_step_index = blackboard.current_step_index + 1
        if not blackboard.macro_plan or next_step_index >= len(blackboard.macro_plan):
            return None
        return blackboard.macro_plan[next_step_index]

    def _format_item_requests(self, item_requests: list) -> str:
        return ", ".join(f"{item.item_name}({item.qualified_item_id or 'name'}):{item.count}" for item in item_requests)

    def _format_goal(self, goal: InventoryGoal | None) -> str:
        if goal is None:
            return "None"
        return (
            f"target_free_slots={goal.target_free_slots}, "
            f"preserve_required_items={goal.preserve_required_items}, "
            f"item_policy={goal.item_policy}, allowed_sources={goal.allowed_sources}, "
            f"allow_stale_chest_cache={goal.allow_stale_chest_cache}"
        )

    def _log(self, message: str) -> None:
        self.debug_logger.log(f"[InventoryNode] {message}")

    def _reset(self) -> None:
        self._task_signature = None
        self._started_at = None
        self._active_chest_task = None
        self._observed_chest_tiles_for_goal = set()
        self._last_temporary_chest_task_completed = False
        self.chest_node._reset()
