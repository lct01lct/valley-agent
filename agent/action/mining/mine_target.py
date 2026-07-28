from dataclasses import dataclass
from typing import Literal

from server.valley_server import MineInteractTargetState, MineObjectTargetState, MiningNodeState, StardewState
from server.type import Tile


type MineTargetType = Literal[
    "LADDER",  # 矿井下一层入口，执行方式是站到相邻格并交互。
    "MINE_ENTRANCE",  # 矿洞大厅进入第一层的入口，执行方式是站到相邻格并交互。
    "STONE",  # 普通石头，执行方式是切 Pickaxe 后站到相邻格破坏。
    "MINING_NODE",  # 矿石、宝石等资源矿点，执行方式是切 Pickaxe 后站到相邻格破坏。
    "COLLECTIBLE",  # 地晶、石英、火水晶等徒手采集物，后续由 C# state 补齐。
    "BREAKABLE_CONTAINER",  # 矿井木箱/木桶等可破坏容器，后续使用武器攻击。
]

type MineTargetAction = Literal[
    "INTERACT",  # 对入口、梯子或徒手采集目标按交互键。
    "USE_PICKAXE",  # 对 Stone / MiningNode 使用镐子。
    "ATTACK_WEAPON",  # 对木箱、木桶或怪物阻挡目标使用武器攻击。
    "WALK_TO_COLLECT",  # 走到磁吸/拾取范围内，不需要额外工具动作。
]


@dataclass(frozen=True)
class MineTarget:
    """
    矿井内可执行目标的统一抽象。

    MineNode 负责执行；MineTarget 只描述“这个目标是什么、在哪、应该用什么方式处理”。
    后续接入 AI/战术层时，可以在目标选择器里根据 profile 对这些目标做评分。
    """

    target_type: MineTargetType
    tile: Tile
    action: MineTargetAction
    name: str = ""
    display_name: str = ""
    qualified_item_id: str = ""
    source: str = ""
    required_tool: str | None = None
    can_stand_on_target: bool = False
    require_tool_target: bool = True
    require_close_to_target: bool = False
    blocks_movement: bool = True
    priority: float = 0.0

    @property
    def candidate_stand_tiles(self) -> set[Tile]:
        if self.can_stand_on_target:
            return {self.tile, *build_cardinal_neighbor_tiles(self.tile)}
        return build_cardinal_neighbor_tiles(self.tile)

    @property
    def is_interact_target(self) -> bool:
        return self.action == "INTERACT"

    @property
    def is_breakable_rock(self) -> bool:
        return self.action == "USE_PICKAXE"


class MineTargetSelector:
    """
    从当前 SMAPI state 中构建矿井目标。

    当前接入 Ladder、MineEntrance、Stone、MiningNode、Collectible 和 BreakableContainer。
    """

    def build_ladder_targets(self, state: StardewState, excluded_tiles: set[Tile] | None = None) -> list[MineTarget]:
        excluded_tiles = excluded_tiles or set()
        return [
            self._from_ladder_state(ladder)
            for ladder in state.ladders
            if ladder.tile not in excluded_tiles
        ]

    def build_mine_entrance_targets(self, state: StardewState) -> list[MineTarget]:
        return [self._from_mine_entrance_state(entrance) for entrance in state.mine_entrances]

    def build_breakable_rock_targets(
        self,
        state: StardewState,
        excluded_tiles: set[Tile] | None = None,
    ) -> list[MineTarget]:
        excluded_tiles = excluded_tiles or set()
        targets_by_tile: dict[Tile, MineTarget] = {}

        for mining_node in state.mining_nodes:
            if mining_node.tile in excluded_tiles:
                continue
            targets_by_tile[mining_node.tile] = self._from_mining_node_state(mining_node)

        for stone_tile in state.layers.get("Stone", set()):
            if stone_tile in excluded_tiles or stone_tile in targets_by_tile:
                continue
            targets_by_tile[stone_tile] = self._from_stone_tile(stone_tile)

        return list(targets_by_tile.values())

    def build_collectible_targets(
        self,
        state: StardewState,
        excluded_tiles: set[Tile] | None = None,
    ) -> list[MineTarget]:
        excluded_tiles = excluded_tiles or set()
        return [
            self._from_collectible_state(collectible)
            for collectible in state.mine_collectibles
            if collectible.tile not in excluded_tiles
        ]

    def build_breakable_container_targets(
        self,
        state: StardewState,
        excluded_tiles: set[Tile] | None = None,
    ) -> list[MineTarget]:
        excluded_tiles = excluded_tiles or set()
        return [
            self._from_breakable_container_state(container)
            for container in state.mine_breakable_containers
            if container.tile not in excluded_tiles
        ]

    def build_all_targets(self, state: StardewState, excluded_tiles: set[Tile] | None = None) -> list[MineTarget]:
        excluded_tiles = excluded_tiles or set()
        return [
            *self.build_ladder_targets(state, excluded_tiles),
            *self.build_mine_entrance_targets(state),
            *self.build_collectible_targets(state, excluded_tiles),
            *self.build_breakable_container_targets(state, excluded_tiles),
            *self.build_breakable_rock_targets(state, excluded_tiles),
        ]

    def select_nearest_target(self, state: StardewState, targets: list[MineTarget]) -> MineTarget | None:
        if not targets:
            return None
        return min(
            targets,
            key=lambda target: (
                target.priority,
                self._tile_distance(state.player_tile, target.tile),
                target.tile.y,
                target.tile.x,
            ),
        )

    def _from_ladder_state(self, ladder: MineInteractTargetState) -> MineTarget:
        return MineTarget(
            target_type="LADDER",
            tile=ladder.tile,
            action="INTERACT",
            name=ladder.name,
            display_name=ladder.display_name,
            qualified_item_id=ladder.qualified_item_id,
            source=ladder.source,
            required_tool=None,
            can_stand_on_target=False,
            require_tool_target=True,
            require_close_to_target=True,
            blocks_movement=True,
            priority=0.0,
        )

    def _from_mine_entrance_state(self, entrance: MineInteractTargetState) -> MineTarget:
        return MineTarget(
            target_type="MINE_ENTRANCE",
            tile=entrance.tile,
            action="INTERACT",
            name=entrance.name,
            display_name=entrance.display_name,
            qualified_item_id=entrance.qualified_item_id,
            source=entrance.source,
            required_tool=None,
            can_stand_on_target=False,
            require_tool_target=True,
            require_close_to_target=True,
            blocks_movement=True,
            priority=0.0,
        )

    def _from_mining_node_state(self, mining_node: MiningNodeState) -> MineTarget:
        return MineTarget(
            target_type="MINING_NODE",
            tile=mining_node.tile,
            action="USE_PICKAXE",
            name=mining_node.name,
            display_name=mining_node.display_name,
            qualified_item_id=mining_node.qualified_item_id,
            source=mining_node.type,
            required_tool="Pickaxe",
            can_stand_on_target=False,
            require_tool_target=True,
            require_close_to_target=False,
            blocks_movement=True,
            priority=1.0,
        )

    def _from_stone_tile(self, stone_tile: Tile) -> MineTarget:
        return MineTarget(
            target_type="STONE",
            tile=stone_tile,
            action="USE_PICKAXE",
            name="Stone",
            display_name="Stone",
            source="StoneLayer",
            required_tool="Pickaxe",
            can_stand_on_target=False,
            require_tool_target=True,
            require_close_to_target=False,
            blocks_movement=True,
            priority=2.0,
        )

    def _from_collectible_state(self, collectible: MineObjectTargetState) -> MineTarget:
        return MineTarget(
            target_type="COLLECTIBLE",
            tile=collectible.tile,
            action="INTERACT",
            name=collectible.name,
            display_name=collectible.display_name,
            qualified_item_id=collectible.qualified_item_id,
            source=collectible.source,
            required_tool=None,
            can_stand_on_target=True,
            require_tool_target=True,
            require_close_to_target=True,
            blocks_movement=False,
            priority=1.5,
        )

    def _from_breakable_container_state(self, container: MineObjectTargetState) -> MineTarget:
        return MineTarget(
            target_type="BREAKABLE_CONTAINER",
            tile=container.tile,
            action="ATTACK_WEAPON",
            name=container.name,
            display_name=container.display_name,
            qualified_item_id=container.qualified_item_id,
            source=container.source,
            required_tool="Weapon",
            can_stand_on_target=False,
            require_tool_target=True,
            require_close_to_target=False,
            blocks_movement=True,
            priority=1.25,
        )

    def _tile_distance(self, start_tile: Tile, end_tile: Tile) -> int:
        return abs(start_tile.x - end_tile.x) + abs(start_tile.y - end_tile.y)


def build_cardinal_neighbor_tiles(target_tile: Tile) -> set[Tile]:
    return {
        Tile(target_tile.x + 1, target_tile.y),
        Tile(target_tile.x - 1, target_tile.y),
        Tile(target_tile.x, target_tile.y + 1),
        Tile(target_tile.x, target_tile.y - 1),
    }
