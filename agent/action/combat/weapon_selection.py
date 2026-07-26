from server.valley_server import InventoryItem, StardewState


SWORD_KEYWORDS: tuple[str, ...] = (
    "sword",
    "blade",
    "saber",
    "cutlass",
    "katana",
    "claymore",
    "rapier",
    "slasher",
)
NON_SWORD_KEYWORDS: tuple[str, ...] = ("dagger", "club", "hammer", "mallet")


class WeaponSelector:
    """
    武器选择策略层。

    当前 P1 只做确定性规则：优先剑类武器，再按伤害高低选择。
    后续可接入攻速、击退、特殊效果、怪物类型和 Agent 策略。
    """

    def select_best_weapon(self, state: StardewState) -> InventoryItem | None:
        weapons = [item for item in state.inventory.items if self._is_weapon(item)]
        if not weapons:
            return None
        return max(weapons, key=self._weapon_score)

    def _is_weapon(self, item: InventoryItem) -> bool:
        if item.is_weapon:
            return True
        if item.qualified_item_id.startswith("(W)"):
            return True
        return self._has_weapon_name(item)

    def _weapon_score(self, item: InventoryItem) -> tuple[int, int, int, int]:
        return (
            1 if self._is_sword(item) else 0,
            item.max_damage or 0,
            item.min_damage or 0,
            -item.index,
        )

    def _is_sword(self, item: InventoryItem) -> bool:
        text = self._weapon_text(item)
        if any(keyword in text for keyword in NON_SWORD_KEYWORDS):
            return False
        return any(keyword in text for keyword in SWORD_KEYWORDS)

    def _has_weapon_name(self, item: InventoryItem) -> bool:
        text = self._weapon_text(item)
        return any(keyword in text for keyword in SWORD_KEYWORDS + NON_SWORD_KEYWORDS)

    def _weapon_text(self, item: InventoryItem) -> str:
        return " ".join(
            [
                item.name,
                item.display_name,
                item.qualified_item_id,
                item.type_definition_id,
            ]
        ).lower()
