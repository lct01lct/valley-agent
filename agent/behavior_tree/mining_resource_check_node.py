from agent.action.inventory.inventory_policy import InventoryPolicy, InventoryRecoveryHint
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.mining_node import MiningTask
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.tool_selection import find_tool_item


PICKAXE_TOOL_NAME = "Pickaxe"
MINING_REQUIRED_ITEMS = {PICKAXE_TOOL_NAME}


class MiningResourceCheckNode(BTNode):
    """
    Mining 任务的轻量资源前置检查。

    P0 只要求背包里有 Pickaxe；后续体力、血量、背包容量、炸弹等资源管理
    应在 Mining P3 扩展，不在当前节点里提前塞复杂恢复。
    """

    def __init__(self) -> None:
        self.inventory_policy = InventoryPolicy()

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        if not blackboard.macro_plan or blackboard.current_step_index >= len(blackboard.macro_plan):
            return "FAILURE"

        current_task = blackboard.macro_plan[blackboard.current_step_index]
        if not isinstance(current_task, MiningTask):
            return "FAILURE"

        if current_task.task_type != "MINE":
            return "FAILURE"

        game_state = context.state
        if game_state is None:
            return "RUNNING"

        if find_tool_item(game_state, PICKAXE_TOOL_NAME) is None:
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
            self._register_inventory_recovery(
                blackboard,
                game_state,
                current_task,
                "MISSING_REQUIRED",
                "背包中没有 Pickaxe，无法开始 Mining。",
                InventoryRecoveryHint(
                    strategy="NEED_CHEST_STORAGE",
                    reason="缺少 Mining 必需工具，需要通过箱子或 Planner 找回 Pickaxe",
                    discard_candidates=[],
                ),
            )
            blackboard.prompt = (
                "MiningResourceCheckNode 检查失败：背包中没有 Pickaxe。"
                "需要 Planner 先安排 ChestTask 取回镐子，或人工确认工具位置。"
            )
            print("\n🔴 [MiningResourceCheckNode] 背包中没有 Pickaxe，无法开始 Mining P0。")
            return "FAILURE"

        summary = self.inventory_policy.build_summary(game_state, MINING_REQUIRED_ITEMS)
        blackboard.inventory_risk_level = summary.risk_level
        if summary.risk_level == "LOW_SPACE":
            print(
                f"\n🟡 [MiningResourceCheckNode] 背包空间较低，但不打断 Mining: "
                f"free_slots={summary.free_slots}, occupied={summary.occupied_slots}/{summary.max_items}"
            )
            return "SUCCESS"

        if summary.risk_level == "FULL_BLOCKED" and current_task.collect_opportunity_resources:
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
            recovery_hint = InventoryRecoveryHint(
                strategy="DISCARD_LOW_VALUE" if summary.discard_candidates else "NEED_CHEST_STORAGE",
                reason=(
                    "背包已满且当前 MiningTask 启用了机会资源采集，需要先腾出空间。"
                    if summary.discard_candidates
                    else "背包已满且没有安全可丢弃候选，需要通过箱子整理或 Planner 恢复。"
                ),
                discard_candidates=summary.discard_candidates,
            )
            self._register_inventory_recovery(
                blackboard,
                game_state,
                current_task,
                "FULL_BLOCKED",
                "背包已满，当前机会资源采集任务不适合继续开始。",
                recovery_hint,
            )
            print(
                f"\n🔴 [MiningResourceCheckNode] 背包已满，暂停机会资源采集 Mining: "
                f"strategy={recovery_hint.strategy}, free_slots={summary.free_slots}, "
                f"discard_candidates={blackboard.inventory_discard_candidates}"
            )
            return "FAILURE"

        if summary.risk_level == "FULL_BLOCKED":
            print(
                f"\n🟡 [MiningResourceCheckNode] 背包已满，但当前 Mining 主目标不是主动采集资源，"
                f"先允许继续冲层: occupied={summary.occupied_slots}/{summary.max_items}"
            )

        return "SUCCESS"

    def _register_inventory_recovery(
        self,
        blackboard: AgentBlackboard,
        game_state,
        current_task: MiningTask,
        risk_level: str,
        failure_reason: str,
        recovery_hint: InventoryRecoveryHint,
    ) -> None:
        summary = self.inventory_policy.build_summary(game_state, MINING_REQUIRED_ITEMS)
        blackboard.inventory_check_failed = True
        blackboard.inventory_risk_level = risk_level
        blackboard.inventory_failure_reason = failure_reason
        blackboard.inventory_recovery_hint = recovery_hint.reason
        blackboard.inventory_recovery_strategy = recovery_hint.strategy
        blackboard.inventory_recovery_task = current_task
        blackboard.inventory_discard_candidates = [
            {
                "item_name": candidate.item_name,
                "qualified_item_id": candidate.qualified_item_id,
                "count": candidate.count,
                "index": candidate.index,
                "reason": candidate.reason,
            }
            for candidate in recovery_hint.discard_candidates
        ]
        blackboard.inventory_recovery_context = {
            "owner": "Mining",
            "task_desc": current_task.desc,
            "mine_action": current_task.mine_action,
            "collect_opportunity_resources": current_task.collect_opportunity_resources,
            "location_name": game_state.location_name,
            "mine_level": game_state.mine_level,
            "risk_level": summary.risk_level,
            "free_slots": summary.free_slots,
            "occupied_slots": summary.occupied_slots,
            "max_items": summary.max_items,
            "protected_items": summary.protected_items,
        }
