from dataclasses import dataclass, field
from typing import Literal

from server.valley_server import StardewState


type InventoryRiskLevel = Literal[
    "OK",  # 背包空间充足，当前任务不受背包限制。
    "LOW_SPACE",  # 背包剩余空间较少，只作为风险提示，不直接抢占任务。
    "FULL_CAN_STACK",  # 背包已满，但当前目标物品可以堆叠进入。
    "FULL_BLOCKED",  # 背包已满，当前目标物品无法进入，需要恢复策略。
    "MISSING_REQUIRED",  # 缺少当前任务必需物品，例如工具或种子。
]

type InventoryRecoveryStrategy = Literal[
    "NONE",  # 不需要恢复。
    "DISCARD_LOW_VALUE",  # 可通过丢弃明确低价值且非 protected 物品腾出空间。
    "NEED_CHEST_STORAGE",  # 需要通过箱子整理背包。
    "STOP_FOR_PLANNING",  # 当前没有安全恢复动作，应停机交给 Planner/AI。
]


LOW_SPACE_FREE_SLOT_THRESHOLD = 2
DEFAULT_STACK_LIMIT = 999
LOW_VALUE_DISCARD_QUALIFIED_ITEM_IDS = {
    "(O)92",  # Sap
    "(O)771",  # Fiber
}
LOW_VALUE_DISCARD_NAMES = {
    "Sap",
    "Fiber",
}


@dataclass(frozen=True)
class InventoryDecision:
    can_accept: bool
    risk_level: InventoryRiskLevel
    reason: str
    requires_new_slot: bool = False
    can_stack: bool = False


@dataclass(frozen=True)
class DiscardCandidate:
    item_name: str
    qualified_item_id: str
    count: int
    index: int
    reason: str


@dataclass(frozen=True)
class InventoryRecoveryHint:
    strategy: InventoryRecoveryStrategy
    reason: str
    discard_candidates: list[DiscardCandidate] = field(default_factory=list)


@dataclass(frozen=True)
class InventorySummary:
    risk_level: InventoryRiskLevel
    max_items: int
    occupied_slots: int
    free_slots: int
    protected_items: list[str]
    discard_candidates: list[DiscardCandidate]


class InventoryPolicy:
    """
    背包事实与风险判断层。

    本类只做确定性判断，不发送命令、不修改 blackboard、不生成具体任务。
    """

    def build_summary(self, state: StardewState, required_item_names: set[str] | None = None) -> InventorySummary:
        required_item_names = required_item_names or set()
        free_slots = self.get_free_slots(state)
        risk_level: InventoryRiskLevel = "OK"
        if self.is_full(state):
            risk_level = "FULL_BLOCKED"
        elif free_slots <= LOW_SPACE_FREE_SLOT_THRESHOLD:
            risk_level = "LOW_SPACE"

        return InventorySummary(
            risk_level=risk_level,
            max_items=self.get_max_items(state),
            occupied_slots=self.get_occupied_slots(state),
            free_slots=free_slots,
            protected_items=self.get_protected_item_names(state, required_item_names),
            discard_candidates=self.find_low_value_discard_candidates(state, required_item_names),
        )

    def can_accept_debris(self, state: StardewState, debris) -> InventoryDecision:
        qualified_item_id = str(getattr(debris, "qualified_item_id", "") or "").strip()
        name = str(getattr(debris, "name", "") or "").strip()
        display_name = str(getattr(debris, "display_name", "") or "").strip()
        item_name = name or display_name
        stack = max(1, int(getattr(debris, "stack", 1) or 1))
        return self.can_accept_item(state, item_name, qualified_item_id, stack)

    def can_accept_item(
        self,
        state: StardewState,
        item_name: str = "",
        qualified_item_id: str = "",
        stack: int = 1,
    ) -> InventoryDecision:
        free_slots = self.get_free_slots(state)
        if free_slots > 0:
            risk_level: InventoryRiskLevel = "OK" if free_slots > LOW_SPACE_FREE_SLOT_THRESHOLD else "LOW_SPACE"
            return InventoryDecision(
                can_accept=True,
                risk_level=risk_level,
                reason=f"背包仍有空格: free_slots={free_slots}",
                requires_new_slot=True,
                can_stack=False,
            )

        if self._can_stack_existing_item(state, item_name, qualified_item_id, stack):
            return InventoryDecision(
                can_accept=True,
                risk_level="FULL_CAN_STACK",
                reason=f"背包已满，但目标物品可堆叠: item={self._format_item(item_name, qualified_item_id)}",
                requires_new_slot=False,
                can_stack=True,
            )

        return InventoryDecision(
            can_accept=False,
            risk_level="FULL_BLOCKED",
            reason=f"背包已满，目标物品无法进入: item={self._format_item(item_name, qualified_item_id)}",
            requires_new_slot=True,
            can_stack=False,
        )

    def build_recovery_hint(
        self,
        state: StardewState,
        decision: InventoryDecision,
        required_item_names: set[str] | None = None,
    ) -> InventoryRecoveryHint:
        if decision.can_accept:
            return InventoryRecoveryHint(strategy="NONE", reason=decision.reason)

        discard_candidates = self.find_low_value_discard_candidates(state, required_item_names)
        if discard_candidates:
            return InventoryRecoveryHint(
                strategy="DISCARD_LOW_VALUE",
                reason="背包已满，但存在明确低价值且非 protected 的可丢弃候选",
                discard_candidates=discard_candidates,
            )

        return InventoryRecoveryHint(
            strategy="NEED_CHEST_STORAGE",
            reason="背包已满且没有安全可丢弃候选，需要通过箱子整理或交给 Planner",
            discard_candidates=[],
        )

    def get_free_slots(self, state: StardewState) -> int:
        inventory = state.inventory
        raw_free_slots = getattr(inventory, "free_slots", None)
        if raw_free_slots is not None:
            return max(0, int(raw_free_slots))

        return max(0, self.get_max_items(state) - self.get_occupied_slots(state))

    def get_max_items(self, state: StardewState) -> int:
        inventory = state.inventory
        raw_max_items = getattr(inventory, "max_items", None)
        if raw_max_items is not None and int(raw_max_items) > 0:
            return int(raw_max_items)

        occupied_indexes = [int(getattr(item, "index", -1)) for item in inventory.items]
        if occupied_indexes:
            return max(12, max(occupied_indexes) + 1)
        return 12

    def get_occupied_slots(self, state: StardewState) -> int:
        inventory = state.inventory
        raw_occupied_slots = getattr(inventory, "occupied_slots", None)
        if raw_occupied_slots is not None:
            return max(0, int(raw_occupied_slots))
        return len(inventory.items)

    def is_full(self, state: StardewState) -> bool:
        return self.get_free_slots(state) <= 0

    def get_protected_item_names(self, state: StardewState, required_item_names: set[str] | None = None) -> list[str]:
        required_item_names = required_item_names or set()
        protected_names: list[str] = []
        for item in state.inventory.items:
            if self._is_protected_item(item, required_item_names):
                protected_names.append(item.name)
        return protected_names

    def find_low_value_discard_candidates(
        self,
        state: StardewState,
        required_item_names: set[str] | None = None,
    ) -> list[DiscardCandidate]:
        required_item_names = required_item_names or set()
        candidates: list[DiscardCandidate] = []
        for item in state.inventory.items:
            if self._is_protected_item(item, required_item_names):
                continue
            if not self._is_low_value_discard_item(item):
                continue
            candidates.append(
                DiscardCandidate(
                    item_name=item.name,
                    qualified_item_id=item.qualified_item_id,
                    count=1,
                    index=item.index,
                    reason="明确低价值物品且非工具、非武器、非当前任务必需物",
                )
            )
        return candidates

    def _can_stack_existing_item(
        self,
        state: StardewState,
        item_name: str,
        qualified_item_id: str,
        stack: int,
    ) -> bool:
        if not qualified_item_id and not item_name:
            return False

        for item in state.inventory.items:
            if not self._is_same_item(item, item_name, qualified_item_id):
                continue
            maximum_stack_size = self._get_maximum_stack_size(item)
            if maximum_stack_size <= 1:
                continue
            if item.stack + stack <= maximum_stack_size:
                return True
        return False

    def _is_same_item(self, item, item_name: str, qualified_item_id: str) -> bool:
        if qualified_item_id:
            return item.qualified_item_id == qualified_item_id
        return bool(item_name) and item.name == item_name

    def _get_maximum_stack_size(self, item) -> int:
        maximum_stack_size = int(getattr(item, "maximum_stack_size", 0) or 0)
        if maximum_stack_size > 0:
            return maximum_stack_size
        if bool(getattr(item, "is_tool", False)) or bool(getattr(item, "is_weapon", False)):
            return 1
        return DEFAULT_STACK_LIMIT

    def _is_protected_item(self, item, required_item_names: set[str]) -> bool:
        if bool(getattr(item, "is_tool", False)) or bool(getattr(item, "is_weapon", False)):
            return True
        return item.name in required_item_names or item.display_name in required_item_names

    def _is_low_value_discard_item(self, item) -> bool:
        if item.qualified_item_id in LOW_VALUE_DISCARD_QUALIFIED_ITEM_IDS:
            return True
        return item.name in LOW_VALUE_DISCARD_NAMES or item.display_name in LOW_VALUE_DISCARD_NAMES

    def _format_item(self, item_name: str, qualified_item_id: str) -> str:
        if qualified_item_id:
            return qualified_item_id
        return item_name or "unknown"
