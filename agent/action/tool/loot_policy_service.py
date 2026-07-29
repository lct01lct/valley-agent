import math
import time

from agent.behavior_tree.blackboard import AgentBlackboard, DeferredLootRecord
from server.valley_server import StardewState
from server.type import Tile

DEFAULT_MAGNETIC_RADIUS_RATIO = 0.5
MAGNETIC_RADIUS_TILE_BUFFER = 0.1
DEFERRED_LOOT_MAX_AGE_SECONDS = 3.0
MAGNETIC_DEBRIS_MOVE_EPSILON_PIXELS = 1.5
MAGNETIC_DEBRIS_STATIONARY_SECONDS = 0.35


class LootPolicyService:
    """
    工具动作后掉落物的拾取策略层。

    它只负责登记、刷新和判断“是否需要立即拾取”；真正移动拾取仍由 CollectLootNode 执行。
    """

    def register_deferred_loot(
        self,
        blackboard: AgentBlackboard,
        owner: str,
        source_tile: Tile | None,
        source_type: str,
        loot_tiles: list[Tile],
        priority: str = "normal",
    ) -> None:
        if source_tile is None or not loot_tiles:
            return

        existing_key_to_record = {record.key: record for record in blackboard.deferred_loot_records}
        record_key = (owner, source_tile.x, source_tile.y, source_type)
        existing_record = existing_key_to_record.get(record_key)
        if existing_record is None:
            blackboard.deferred_loot_records.append(
                DeferredLootRecord(
                    owner=owner,
                    source_tile=source_tile,
                    source_type=source_type,
                    loot_tiles=self._dedupe_tiles(loot_tiles),
                    created_at=time.time(),
                    priority=priority,
                )
            )
            return

        known_tiles = {(tile.x, tile.y) for tile in existing_record.loot_tiles}
        for loot_tile in loot_tiles:
            if (loot_tile.x, loot_tile.y) in known_tiles:
                continue
            existing_record.loot_tiles.append(loot_tile)
            known_tiles.add((loot_tile.x, loot_tile.y))
        existing_record.expected_cover_tiles = set()
        self._reset_magnetic_motion_observations(existing_record)

    def refresh_deferred_loot(self, blackboard: AgentBlackboard, state: StardewState) -> None:
        if not blackboard.deferred_loot_records:
            return

        refreshed_records: list[DeferredLootRecord] = []
        for record in blackboard.deferred_loot_records:
            existing_tiles = self._existing_collectible_tiles(state, record.loot_tiles)
            if not existing_tiles:
                # 延迟拾取记录不能在普通刷新阶段被直接丢弃。
                # 否则“理论上会顺路磁吸”的掉落物可能在超过延迟窗口前被吞掉，
                # 导致无法按策略转为主动拾取。最终是否仍存在由 CollectLootNode
                # 在主动拾取阶段根据最新 state 再验证。
                refreshed_records.append(record)
                continue
            record.loot_tiles = existing_tiles
            refreshed_records.append(record)

        blackboard.deferred_loot_records = refreshed_records

    def should_promote_deferred_loot(
        self,
        blackboard: AgentBlackboard,
        state: StardewState,
        owner: str,
        continuation_tiles: set[Tile] | list[Tile] | None = None,
        require_all_continuation_tiles: bool = False,
    ) -> bool:
        owner_records = [record for record in blackboard.deferred_loot_records if record.owner == owner]
        if not owner_records:
            return False

        now = time.time()
        for record in owner_records:
            if now - record.created_at >= DEFERRED_LOOT_MAX_AGE_SECONDS:
                return True
            if record.priority == "must_collect":
                return True

        self.refresh_deferred_loot(blackboard, state)
        owner_records = [record for record in blackboard.deferred_loot_records if record.owner == owner]
        if not owner_records:
            return False

        if self.has_missed_expected_cover(state, owner_records):
            return True

        if continuation_tiles:
            can_cover = (
                self.can_every_continuation_tile_cover_deferred_loot(state, owner_records, continuation_tiles)
                if require_all_continuation_tiles
                else self.can_continuation_cover_deferred_loot(state, owner_records, continuation_tiles)
            )
            if can_cover:
                return False

        return True

    def can_every_continuation_tile_cover_deferred_loot(
        self,
        state: StardewState,
        records: list[DeferredLootRecord],
        continuation_tiles: set[Tile] | list[Tile],
    ) -> bool:
        if not continuation_tiles:
            return False

        candidate_tiles = set(continuation_tiles)
        for record in records:
            record_cover_tiles: set[Tile] = set()
            for loot_tile in record.loot_tiles:
                debris = self._get_collectible_debris_for_tile(state, loot_tile)
                if debris is None:
                    continue
                if not all(
                    self.can_tile_cover_debris(state, candidate_tile, debris.position)
                    for candidate_tile in candidate_tiles
                ):
                    return False
                record_cover_tiles.update(candidate_tiles)
            self._set_expected_cover_tiles(record, record_cover_tiles)
        return True

    def has_expired_deferred_loot(self, blackboard: AgentBlackboard, owner: str) -> bool:
        now = time.time()
        return any(
            record.owner == owner and now - record.created_at >= DEFERRED_LOOT_MAX_AGE_SECONDS
            for record in blackboard.deferred_loot_records
        )

    def has_missed_expected_cover_for_owner(
        self,
        blackboard: AgentBlackboard,
        state: StardewState,
        owner: str,
    ) -> bool:
        owner_records = [record for record in blackboard.deferred_loot_records if record.owner == owner]
        return self.has_missed_expected_cover(state, owner_records)

    def can_continuation_cover_deferred_loot(
        self,
        state: StardewState,
        records: list[DeferredLootRecord],
        continuation_tiles: set[Tile] | list[Tile],
    ) -> bool:
        if not continuation_tiles:
            return False

        candidate_tiles = set(continuation_tiles)
        for record in records:
            record_cover_tiles: set[Tile] = set()
            for loot_tile in record.loot_tiles:
                debris = self._get_collectible_debris_for_tile(state, loot_tile)
                if debris is None:
                    continue
                cover_tiles = {
                    candidate_tile
                    for candidate_tile in candidate_tiles
                    if self.can_tile_cover_debris(state, candidate_tile, debris.position)
                }
                if not cover_tiles:
                    return False
                record_cover_tiles.update(cover_tiles)
            self._set_expected_cover_tiles(record, record_cover_tiles)
        return True

    def has_missed_expected_cover(self, state: StardewState, records: list[DeferredLootRecord]) -> bool:
        for record in records:
            if not record.expected_cover_tiles:
                continue
            if state.player_tile not in record.expected_cover_tiles:
                continue
            for loot_tile in record.loot_tiles:
                debris = self._get_collectible_debris_for_tile(state, loot_tile)
                if debris is None:
                    continue
                if self._is_debris_stationary_after_expected_cover(record, state, loot_tile, debris):
                    return True
        return False

    def _is_debris_stationary_after_expected_cover(
        self,
        record: DeferredLootRecord,
        state: StardewState,
        loot_tile: Tile,
        debris,
    ) -> bool:
        now = time.time()
        debris_key = (loot_tile.x, loot_tile.y)
        current_position = self._position_tuple(debris.position)
        current_distance = self._distance_between_positions(debris.position, state.position)
        last_position = record.debris_last_positions.get(debris_key)

        if last_position is None:
            record.debris_last_positions[debris_key] = current_position
            record.debris_last_distances[debris_key] = current_distance
            record.debris_stationary_started_at[debris_key] = now
            return False

        moved_pixels = math.hypot(
            current_position[0] - last_position[0],
            current_position[1] - last_position[1],
        )
        if moved_pixels >= MAGNETIC_DEBRIS_MOVE_EPSILON_PIXELS:
            record.debris_last_positions[debris_key] = current_position
            record.debris_last_distances[debris_key] = current_distance
            record.debris_stationary_started_at[debris_key] = now
            return False

        stationary_started_at = record.debris_stationary_started_at.get(debris_key, now)
        record.debris_last_distances[debris_key] = current_distance
        return now - stationary_started_at >= MAGNETIC_DEBRIS_STATIONARY_SECONDS

    def promote_deferred_loot(
        self,
        blackboard: AgentBlackboard,
        state: StardewState,
        owner: str,
    ) -> bool:
        self.refresh_deferred_loot(blackboard, state)
        owner_records = [record for record in blackboard.deferred_loot_records if record.owner == owner]
        if not owner_records:
            return False

        promoted_tiles: list[Tile] = []
        known_tiles: set[tuple[int, int]] = set()
        for record in owner_records:
            for loot_tile in record.loot_tiles:
                if (loot_tile.x, loot_tile.y) in known_tiles:
                    continue
                promoted_tiles.append(loot_tile)
                known_tiles.add((loot_tile.x, loot_tile.y))

        if not promoted_tiles:
            blackboard.deferred_loot_records = [
                record for record in blackboard.deferred_loot_records if record.owner != owner
            ]
            return False

        source_record = owner_records[0]
        blackboard.require_collect_loot = True
        blackboard.collect_loot_owner = owner
        blackboard.collect_loot_source_tile = source_record.source_tile
        blackboard.collect_loot_source_type = source_record.source_type
        blackboard.pending_loot_tiles = promoted_tiles
        blackboard.skipped_loot_tiles = set()
        blackboard.deferred_loot_records = [
            record for record in blackboard.deferred_loot_records if record.owner != owner
        ]
        return True

    def clear_owner_deferred_loot(self, blackboard: AgentBlackboard, owner: str) -> None:
        blackboard.deferred_loot_records = [
            record for record in blackboard.deferred_loot_records if record.owner != owner
        ]

    def can_tile_cover_debris(self, state: StardewState, stand_tile: Tile, debris_position) -> bool:
        tile_size = state.tile_size or 64
        player_width, player_height = state.player_size
        half_width = player_width / 2
        half_height = player_height / 2
        magnetic_radius = self.get_effective_magnetic_radius(state)
        if magnetic_radius <= 0:
            return False

        min_x = stand_tile.x * tile_size + half_width
        max_x = (stand_tile.x + 1) * tile_size - half_width
        min_y = stand_tile.y * tile_size + half_height
        max_y = (stand_tile.y + 1) * tile_size - half_height

        nearest_x = min(max(debris_position.x, min_x), max_x)
        nearest_y = min(max(debris_position.y, min_y), max_y)
        return (
            abs(nearest_x - debris_position.x) <= magnetic_radius
            and abs(nearest_y - debris_position.y) <= magnetic_radius
        )

    def get_effective_magnetic_radius(self, state: StardewState) -> float:
        tile_size = state.tile_size or 64
        raw_magnetic_radius = float(getattr(state, "applied_magnetic_radius", 0.0))
        if raw_magnetic_radius > 0:
            return max(0.0, raw_magnetic_radius - tile_size * MAGNETIC_RADIUS_TILE_BUFFER)
        return tile_size * DEFAULT_MAGNETIC_RADIUS_RATIO

    def _existing_collectible_tiles(self, state: StardewState, requested_tiles: list[Tile]) -> list[Tile]:
        requested_tile_set = set(requested_tiles)
        existing_tiles: list[Tile] = []
        seen_tiles: set[Tile] = set()
        for debris in getattr(state, "debris", []):
            if not bool(getattr(debris, "is_collectible", False)):
                continue
            if debris.tile not in requested_tile_set:
                continue
            if debris.tile in seen_tiles:
                continue
            existing_tiles.append(debris.tile)
            seen_tiles.add(debris.tile)
        return existing_tiles

    def _get_collectible_debris_for_tile(self, state: StardewState, target_tile: Tile):
        for debris in getattr(state, "debris", []):
            if not bool(getattr(debris, "is_collectible", False)):
                continue
            if debris.tile == target_tile:
                return debris
        return None

    def _set_expected_cover_tiles(self, record: DeferredLootRecord, expected_cover_tiles: set[Tile]) -> None:
        if record.expected_cover_tiles == expected_cover_tiles:
            return
        record.expected_cover_tiles = expected_cover_tiles
        self._reset_magnetic_motion_observations(record)

    def _reset_magnetic_motion_observations(self, record: DeferredLootRecord) -> None:
        record.debris_last_positions = {}
        record.debris_last_distances = {}
        record.debris_stationary_started_at = {}

    def _position_tuple(self, position) -> tuple[float, float]:
        return (float(position.x), float(position.y))

    def _distance_between_positions(self, start_position, end_position) -> float:
        return math.hypot(
            float(start_position.x) - float(end_position.x),
            float(start_position.y) - float(end_position.y),
        )

    def _dedupe_tiles(self, tiles: list[Tile]) -> list[Tile]:
        deduped_tiles: list[Tile] = []
        seen_tiles: set[tuple[int, int]] = set()
        for tile in tiles:
            if (tile.x, tile.y) in seen_tiles:
                continue
            deduped_tiles.append(tile)
            seen_tiles.add((tile.x, tile.y))
        return deduped_tiles
