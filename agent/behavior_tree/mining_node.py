import json
import time
from typing import Literal

from agent.action.location.location import Location
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.action.valley_action.positioning_controller import PositioningController, PositioningGoal, PositioningResult
from agent.base_task import BaseTask, TaskType
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.mining_debug_logger import MiningDebugLogger
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.tool_action_tracker import ToolActionTracker
from agent.behavior_tree.tool_selection import is_current_tool
from server.valley_server import MineInteractTargetState, StardewState
from server.type import Tile


type MiningAction = Literal[
    "FIND_NEXT_LEVEL",  # 找到并进入下一层；若当前层没有梯子，则挖石头直到梯子出现
]
type MiningPhase = Literal[
    "ENTER_MINE",  # 在矿洞大厅寻找入口并进入第一层
    "FIND_LADDER",  # 在矿层中寻找可交互的下层梯子
    "BREAK_STONE",  # 没有梯子时，选择 Stone / MiningNode 并用镐子破坏
    "DONE",  # 已进入目标矿层，任务完成
]


PICKAXE_TOOL_NAME = "Pickaxe"
MINE_NODE_TIMEOUT_SECONDS = 90.0
MINE_INTERACT_RETRY_INTERVAL_SECONDS = 0.45
MINE_TOOL_START_GRACE_SECONDS = 0.35
MINE_TOOL_FINISH_TIMEOUT_SECONDS = 3.0
MAX_STONE_ATTEMPTS = 8


class MiningTask(BaseTask):
    def __init__(
        self,
        task_type: TaskType,
        desc: str,
        mine_action: MiningAction,
        target_loc: Location = "Mine",
        target_mine_level: int = 2,
        max_stones_to_break: int = 60,
    ) -> None:
        super().__init__(task_type=task_type, desc=desc)
        self.mine_action = mine_action
        self.target_loc = target_loc
        self.target_mine_level = target_mine_level
        self.max_stones_to_break = max_stones_to_break


class MineNode(BTNode):
    """
    Mining P0：进入矿洞第一层，并找到/制造通往第二层的入口。
    """

    def __init__(self) -> None:
        self.positioning_controller = PositioningController()
        self.tool_action_tracker = ToolActionTracker(
            start_grace_seconds=MINE_TOOL_START_GRACE_SECONDS,
            finish_timeout_seconds=MINE_TOOL_FINISH_TIMEOUT_SECONDS,
        )
        self.mining_debug_logger = MiningDebugLogger()
        self._phase: MiningPhase | None = None
        self._task_signature: tuple[int, int, str] | None = None
        self._started_at: float | None = None
        self._target_tile: Tile | None = None
        self._detected_ladder_tile: Tile | None = None
        self._active_mine_level: int | None = None
        self._return_prompt_tiles: set[Tile] = set()
        self._stone_attempt_count = 0
        self._broken_stone_count = 0
        self._failed_stone_tiles: set[Tile] = set()
        self._last_interact_at = 0.0
        self._has_logged_task = False
        self._last_debug_heartbeat_at = 0.0

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        if not blackboard.macro_plan or blackboard.current_step_index >= len(blackboard.macro_plan):
            self._reset()
            return "FAILURE"

        current_task = blackboard.macro_plan[blackboard.current_step_index]
        if not isinstance(current_task, MiningTask):
            self._reset()
            return "FAILURE"

        if current_task.task_type != "MINE":
            self._reset()
            return "FAILURE"

        game_state = context.state
        if game_state is None:
            return "RUNNING"

        task_signature = (
            blackboard.current_step_index,
            current_task.target_mine_level,
            current_task.mine_action,
        )
        if self._task_signature != task_signature:
            self._reset()
            self._task_signature = task_signature
            self._phase = "ENTER_MINE"
            self._started_at = time.time()

        self._log_debug_heartbeat(game_state, current_task)
        if not self._has_logged_task:
            self._has_logged_task = True
            print(
                "\n⛏️ [MineNode] 收到采矿任务: "
                f"action={current_task.mine_action}, target_loc={current_task.target_loc}, "
                f"target_mine_level={current_task.target_mine_level}"
            )
            self._log(
                "收到采矿任务: "
                f"action={current_task.mine_action}, target_loc={current_task.target_loc}, "
                f"target_mine_level={current_task.target_mine_level}, "
                f"location={game_state.location_name}, mine_level={game_state.mine_level}, "
                f"player={game_state.player_tile}"
            )

        if self._started_at is not None and time.time() - self._started_at > MINE_NODE_TIMEOUT_SECONDS:
            return self._fail(context, blackboard, current_task, "Mining P0 超时")

        if current_task.mine_action != "FIND_NEXT_LEVEL":
            return self._fail(context, blackboard, current_task, f"暂不支持的采矿动作: {current_task.mine_action}")

        if self._has_reached_target_level(game_state, current_task):
            return self._finish(context, blackboard, current_task)

        if game_state.mine_level is None:
            return self._run_enter_mine_phase(context, blackboard, game_state, current_task)

        if self._active_mine_level != game_state.mine_level:
            self._active_mine_level = game_state.mine_level
            self._record_return_prompt_tiles(game_state)
            self.positioning_controller.reset()
            self._target_tile = None
            self._detected_ladder_tile = None
            self._last_interact_at = 0.0

        if self._phase == "ENTER_MINE":
            self._phase = "FIND_LADDER"
            print(f"\n⛏️ [MineNode] 已进入矿层: MineLevel={game_state.mine_level}，开始寻找下一层。")
            self._log(
                f"已进入矿层: MineLevel={game_state.mine_level}, player={game_state.player_tile}, "
                f"return_prompt_tiles={self._format_tiles(self._return_prompt_tiles)}"
            )

        if self._detected_ladder_tile is not None:
            self._phase = "FIND_LADDER"
            return self._run_interact_target(
                context=context,
                blackboard=blackboard,
                game_state=game_state,
                current_task=current_task,
                target_tile=self._detected_ladder_tile,
                target_name="破石后出现的梯子",
                require_tool_target=True,
                require_close_to_target=True,
            )

        ladder = self._select_next_level_ladder(game_state)
        if ladder is not None:
            self._phase = "FIND_LADDER"
            return self._run_interact_target(
                context=context,
                blackboard=blackboard,
                game_state=game_state,
                current_task=current_task,
                target_tile=ladder.tile,
                target_name="梯子",
                require_tool_target=True,
                require_close_to_target=True,
            )

        self._phase = "BREAK_STONE"
        return self._run_break_stone_phase(context, blackboard, game_state, current_task)

    def _run_enter_mine_phase(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: MiningTask,
    ) -> NodeStatus:
        if game_state.location_name != current_task.target_loc:
            return self._fail(
                context,
                blackboard,
                current_task,
                f"当前场景不是矿洞入口场景: current={game_state.location_name}, target={current_task.target_loc}",
            )

        entrance = self._select_mine_level_entrance(game_state)
        if entrance is None:
            return self._fail(
                context,
                blackboard,
                current_task,
                f"当前矿洞大厅没有找到可进入第一层的矿洞入口: entrances={self._format_targets(game_state.mine_entrances)}",
            )

        self._phase = "ENTER_MINE"
        return self._run_interact_target(
            context=context,
            blackboard=blackboard,
            game_state=game_state,
            current_task=current_task,
            target_tile=entrance.tile,
            target_name="矿洞入口",
            require_tool_target=True,
            forced_stand_tiles=self._build_mine_entrance_stand_tiles(entrance.tile),
            require_close_to_target=True,
        )

    def _run_interact_target(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: MiningTask,
        target_tile: Tile,
        target_name: str,
        allow_standing_on_target: bool = False,
        require_tool_target: bool = True,
        stand_on_target_only: bool = False,
        forced_stand_tiles: set[Tile] | None = None,
        require_close_to_target: bool = False,
    ) -> NodeStatus:
        if self._target_tile != target_tile:
            self._target_tile = target_tile
            self.positioning_controller.reset()
            self._last_interact_at = 0.0
            print(f"\n⛏️ [MineNode] 准备交互{target_name}: target={target_tile}")
            stand_tiles_text = (
                f", forced_stand_tiles={self._format_tiles(forced_stand_tiles)}"
                if forced_stand_tiles is not None
                else ""
            )
            self._log(f"准备交互{target_name}: target={target_tile}, player={game_state.player_tile}{stand_tiles_text}")

        positioning_result = self._tick_positioning(
            game_state,
            context,
            target_tile,
            allow_standing_on_target=allow_standing_on_target,
            require_tool_target=require_tool_target,
            block_target=not allow_standing_on_target,
            stand_on_target_only=stand_on_target_only,
            forced_stand_tiles=forced_stand_tiles,
            require_close_to_target=require_close_to_target,
        )
        if positioning_result.status == "FAILED":
            return self._fail(
                context,
                blackboard,
                current_task,
                f"无法移动到{target_name}旁: target={target_tile}, reason={positioning_result.reason}",
            )

        if positioning_result.status in ("MOVING", "FACING"):
            return "RUNNING"

        now = time.time()
        if now - self._last_interact_at < MINE_INTERACT_RETRY_INTERVAL_SECONDS:
            return "RUNNING"

        self._last_interact_at = now
        response = context.executor_client.send_command(
            StardewCommand(
                action=StardewAction.INTERACT_TILE,
                key=["x"],
                tile=(target_tile.x, target_tile.y),
            )
        )
        self._log(
            f"发送 INTERACT_TILE: target_name={target_name}, target={target_tile}, "
            f"response={response}, mine_level={game_state.mine_level}, player={game_state.player_tile}"
        )
        return "RUNNING"

    def _run_break_stone_phase(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        game_state: StardewState,
        current_task: MiningTask,
    ) -> NodeStatus:
        if self._broken_stone_count >= current_task.max_stones_to_break:
            return self._fail(
                context,
                blackboard,
                current_task,
                f"已达到最大破石数量仍未发现梯子: broken={self._broken_stone_count}",
            )

        if self._target_tile is None:
            self._target_tile = self._select_nearest_stone_tile(game_state)
            self._stone_attempt_count = 0
            self.positioning_controller.reset()
            self.tool_action_tracker.reset()
            if self._target_tile is None:
                return self._fail(context, blackboard, current_task, "当前矿层没有发现可挖 Stone / MiningNode")
            print(f"\n⛏️ [MineNode] 没有发现梯子，准备破坏石头: target={self._target_tile}")
            self._log(f"选择破坏石头: target={self._target_tile}, player={game_state.player_tile}")

        if not is_current_tool(game_state, PICKAXE_TOOL_NAME):
            blackboard.require_switch_tool = True
            blackboard.is_switching_tool = True
            blackboard.required_tool_owner = "Mining"
            blackboard.required_tool = PICKAXE_TOOL_NAME
            print(f"\n🟡 [MineNode] 当前工具不是 {PICKAXE_TOOL_NAME}，等待切换工具后再挖矿。")
            return "SUCCESS"

        if not self.tool_action_tracker.is_idle():
            tool_status = self.tool_action_tracker.tick(game_state)
            self._log(
                f"等待挥镐收招: target={self._target_tile}, status={tool_status}, "
                f"tracker={self.tool_action_tracker.get_debug_snapshot()}"
            )
            if tool_status == "FINISHED":
                self.tool_action_tracker.reset()
                if self._is_stone_gone_or_ladder_found(game_state, self._target_tile):
                    self._mark_current_stone_done_after_tile_query(context, game_state)
                elif self._stone_attempt_count >= MAX_STONE_ATTEMPTS:
                    self._failed_stone_tiles.add(self._target_tile)
                    self._log(f"石头重试耗尽，加入失败集合: target={self._target_tile}")
                    self._target_tile = None
                    self.positioning_controller.reset()
                return "RUNNING"
            if tool_status == "TIMEOUT":
                self.tool_action_tracker.reset()
                if self._target_tile is not None:
                    self._failed_stone_tiles.add(self._target_tile)
                self._log(f"挥镐等待超时，换下一个石头: target={self._target_tile}")
                self._target_tile = None
                self.positioning_controller.reset()
                return "RUNNING"
            return "RUNNING"

        if self._target_tile is not None and self._is_stone_gone_or_ladder_found(game_state, self._target_tile):
            self._mark_current_stone_done_after_tile_query(context, game_state)
            return "RUNNING"

        positioning_result = self._tick_positioning(game_state, context, self._target_tile)
        if positioning_result.status == "FAILED":
            self._failed_stone_tiles.add(self._target_tile)
            self._log(f"石头站位失败，换下一个: target={self._target_tile}, reason={positioning_result.reason}")
            self._target_tile = None
            self.positioning_controller.reset()
            return "RUNNING"

        if positioning_result.status in ("MOVING", "FACING"):
            return "RUNNING"

        response = context.executor_client.send_command(
            StardewCommand(action=StardewAction.USE_TOOL, key=["c"])
        )
        if response == "BUSY":
            self._log(f"挥镐被 C# 判定 BUSY，等待下一帧: target={self._target_tile}")
            return "RUNNING"
        if response == "TIMEOUT" or response is None:
            self._log(f"挥镐命令异常，等待下一帧重试: target={self._target_tile}, response={response}")
            return "RUNNING"

        self._stone_attempt_count += 1
        self.tool_action_tracker.start()
        print(f"\n⛏️ [MineNode] 使用镐子破坏石头: target={self._target_tile}, attempt={self._stone_attempt_count}")
        self._log(
            f"发送 USE_TOOL 挥镐: target={self._target_tile}, response={response}, "
            f"attempt={self._stone_attempt_count}/{MAX_STONE_ATTEMPTS}, player={game_state.player_tile}"
        )
        return "RUNNING"

    def _tick_positioning(
        self,
        game_state: StardewState,
        context: PlayerContext,
        target_tile: Tile,
        allow_standing_on_target: bool = False,
        require_tool_target: bool = True,
        block_target: bool = True,
        stand_on_target_only: bool = False,
        forced_stand_tiles: set[Tile] | None = None,
        require_close_to_target: bool = False,
    ) -> PositioningResult:
        if forced_stand_tiles is not None:
            candidate_stand_tiles = forced_stand_tiles
        elif stand_on_target_only:
            candidate_stand_tiles = {target_tile}
        else:
            candidate_stand_tiles = self._build_cardinal_neighbor_tiles(target_tile)
            if allow_standing_on_target:
                candidate_stand_tiles.add(target_tile)
        positioning_result = self.positioning_controller.tick(
            game_state,
            PositioningGoal(
                candidate_stand_tiles=candidate_stand_tiles,
                tool_target_tile=target_tile if require_tool_target else None,
                extra_blocked_tiles={target_tile} if block_target else set(),
                allowed_blocked_tiles={target_tile} if allow_standing_on_target else set(),
                require_close_to_target=require_close_to_target,
            ),
        )

        if positioning_result.command is not None:
            context.executor_client.send_command(positioning_result.command)

        self._log(
            f"站位结果: target={target_tile}, status={positioning_result.status}, "
            f"stand={positioning_result.stand_tile}, reason={positioning_result.reason}, "
            f"player={game_state.player_tile}, tool_target={game_state.tool_target.tile}, "
            f"positioning={self.positioning_controller.get_debug_snapshot()}"
        )
        return positioning_result

    def _build_cardinal_neighbor_tiles(self, target_tile: Tile) -> set[Tile]:
        return {
            Tile(target_tile.x + 1, target_tile.y),
            Tile(target_tile.x - 1, target_tile.y),
            Tile(target_tile.x, target_tile.y + 1),
            Tile(target_tile.x, target_tile.y - 1),
        }

    def _build_mine_entrance_stand_tiles(self, entrance_tile: Tile) -> set[Tile]:
        return self._build_cardinal_neighbor_tiles(entrance_tile)

    def _select_nearest_interact_target(
        self,
        game_state: StardewState,
        targets: list[MineInteractTargetState],
    ) -> MineInteractTargetState | None:
        if not targets:
            return None
        return min(targets, key=lambda target: self._tile_distance(game_state.player_tile, target.tile))

    def _select_mine_level_entrance(self, game_state: StardewState) -> MineInteractTargetState | None:
        exact_mine_entrances = [
            target
            for target in game_state.mine_entrances
            if self._normalize_action(target.action) == "mine"
        ]
        if exact_mine_entrances:
            return self._select_nearest_interact_target(game_state, exact_mine_entrances)

        fallback_entrances = [
            target
            for target in game_state.mine_entrances
            if self._is_possible_mine_level_entrance(target)
        ]
        return self._select_nearest_interact_target(game_state, fallback_entrances)

    def _select_next_level_ladder(self, game_state: StardewState) -> MineInteractTargetState | None:
        next_level_ladders = [
            ladder
            for ladder in game_state.ladders
            if ladder.tile not in self._return_prompt_tiles
        ]
        if len(next_level_ladders) != len(game_state.ladders):
            self._log(
                "过滤矿层返回入口梯子: "
                f"return_prompt_tiles={self._format_tiles(self._return_prompt_tiles)}, "
                f"raw_ladders={self._format_targets(game_state.ladders)}, "
                f"next_level_ladders={self._format_targets(next_level_ladders)}"
            )
        return self._select_nearest_interact_target(game_state, next_level_ladders)

    def _record_return_prompt_tiles(self, game_state: StardewState) -> None:
        player_tile = game_state.player_tile
        self._return_prompt_tiles = {
            player_tile,
            Tile(player_tile.x, player_tile.y - 1),
        }
        self._log(
            f"记录矿层返回入口提示区: mine_level={game_state.mine_level}, "
            f"return_prompt_tiles={self._format_tiles(self._return_prompt_tiles)}"
        )

    def _select_nearest_stone_tile(self, game_state: StardewState) -> Tile | None:
        stone_tiles: set[Tile] = {node.tile for node in game_state.mining_nodes if node.tile not in self._failed_stone_tiles}
        stone_tiles.update(tile for tile in game_state.layers.get("Stone", set()) if tile not in self._failed_stone_tiles)
        if not stone_tiles:
            return None
        return min(stone_tiles, key=lambda tile: self._tile_distance(game_state.player_tile, tile))

    def _is_stone_gone_or_ladder_found(self, game_state: StardewState, target_tile: Tile) -> bool:
        if self._has_next_level_ladder(game_state):
            return True
        if target_tile not in game_state.mining_nodes_by_tile and target_tile not in game_state.layers.get("Stone", set()):
            return True
        return False

    def _has_next_level_ladder(self, game_state: StardewState) -> bool:
        return any(ladder.tile not in self._return_prompt_tiles for ladder in game_state.ladders)

    def _mark_current_stone_done(self, game_state: StardewState) -> None:
        if self._target_tile is None:
            return
        finished_tile = self._target_tile
        self._broken_stone_count += 1
        print(f"\n⛏️ [MineNode] 石头已消失或梯子已出现: target={finished_tile}, broken={self._broken_stone_count}")
        self._log(
            f"石头处理完成: target={finished_tile}, broken={self._broken_stone_count}, "
            f"ladders={self._format_targets(game_state.ladders)}"
        )
        self._target_tile = None
        self._stone_attempt_count = 0
        self.positioning_controller.reset()
        self.tool_action_tracker.reset()

    def _mark_current_stone_done_after_tile_query(self, context: PlayerContext, game_state: StardewState) -> None:
        if self._target_tile is None:
            return

        finished_tile = self._target_tile
        has_ladder, ladder_tile, reason = self._query_ladder_at_tile(context, game_state, finished_tile)
        if has_ladder and ladder_tile is not None:
            self._detected_ladder_tile = ladder_tile
            self._broken_stone_count += 1
            print(f"\n⛏️ [MineNode] 破石后发现梯子: target={finished_tile}, ladder={ladder_tile}")
            self._log(
                f"破石后单 tile 查询发现梯子: target={finished_tile}, ladder={ladder_tile}, "
                f"reason={reason}, broken={self._broken_stone_count}"
            )
            self._target_tile = None
            self._stone_attempt_count = 0
            self.positioning_controller.reset()
            self.tool_action_tracker.reset()
            return

        if has_ladder is None:
            self._log(f"破石后单 tile 查询失败，按石头已处理继续: target={finished_tile}, reason={reason}")
        else:
            self._log(f"破石后单 tile 查询未发现梯子: target={finished_tile}, reason={reason}")
        self._mark_current_stone_done(game_state)

    def _query_ladder_at_tile(
        self,
        context: PlayerContext,
        game_state: StardewState,
        tile: Tile,
    ) -> tuple[bool | None, Tile | None, str]:
        response = context.executor_client.send_command(
            StardewCommand(
                action=StardewAction.QUERY_LADDER_AT_TILE,
                tile=(tile.x, tile.y),
            )
        )
        if response in (None, "TIMEOUT"):
            return None, None, str(response)

        try:
            payload = json.loads(response)
        except json.JSONDecodeError as exc:
            return None, None, f"INVALID_JSON:{exc}:{response}"

        status = payload.get("status")
        if status != "SUCCESS":
            return None, None, str(payload.get("reason") or status)

        has_ladder = bool(payload.get("has_ladder"))
        reason = str(payload.get("reason") or "")
        if not has_ladder:
            return False, None, reason

        ladder_obj = payload.get("ladder") if isinstance(payload.get("ladder"), dict) else {}
        raw_tile = ladder_obj.get("Tile") if isinstance(ladder_obj, dict) else None
        if not isinstance(raw_tile, list) or len(raw_tile) < 2:
            raw_tile = payload.get("tile")
        if not isinstance(raw_tile, list) or len(raw_tile) < 2:
            return True, tile, reason

        return True, Tile(int(raw_tile[0]), int(raw_tile[1])), reason

    def _has_reached_target_level(self, game_state: StardewState, current_task: MiningTask) -> bool:
        return game_state.mine_level is not None and game_state.mine_level >= current_task.target_mine_level

    def _finish(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        current_task: MiningTask,
    ) -> NodeStatus:
        context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
        blackboard.current_step_index += 1
        print(f"\n🏆 [MineNode] 已进入目标矿层: MineLevel={current_task.target_mine_level}，Mining P0 完成！")
        self._log(f"任务完成: target_mine_level={current_task.target_mine_level}")
        self._reset()
        return "SUCCESS"

    def _fail(
        self,
        context: PlayerContext,
        blackboard: AgentBlackboard,
        current_task: MiningTask,
        reason: str,
    ) -> NodeStatus:
        context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
        blackboard.prompt = (
            f"MineNode 执行失败: {reason}; "
            f"target_loc={current_task.target_loc}, target_mine_level={current_task.target_mine_level}"
        )
        print(f"\n🔴 [MineNode] {reason}")
        self._log(f"任务失败: reason={reason}")
        self._reset()
        return "FAILURE"

    def _reset(self) -> None:
        self.positioning_controller.reset()
        self.tool_action_tracker.reset()
        self._phase = None
        self._task_signature = None
        self._started_at = None
        self._target_tile = None
        self._detected_ladder_tile = None
        self._active_mine_level = None
        self._return_prompt_tiles = set()
        self._stone_attempt_count = 0
        self._broken_stone_count = 0
        self._failed_stone_tiles = set()
        self._last_interact_at = 0.0
        self._has_logged_task = False
        self._last_debug_heartbeat_at = 0.0

    def _tile_distance(self, start_tile: Tile, end_tile: Tile) -> int:
        return abs(start_tile.x - end_tile.x) + abs(start_tile.y - end_tile.y)

    def _normalize_action(self, action: str | None) -> str:
        if action is None:
            return ""
        return " ".join(action.strip().split()).lower()

    def _is_possible_mine_level_entrance(self, target: MineInteractTargetState) -> bool:
        action = self._normalize_action(target.action)
        if not action.startswith("mine"):
            return False
        if "minecart" in action or "elevator" in action:
            return False
        return True

    def _log_debug_heartbeat(self, game_state: StardewState, current_task: MiningTask) -> None:
        now = time.time()
        if now - self._last_debug_heartbeat_at < 0.25:
            return

        self._last_debug_heartbeat_at = now
        self._log(
            f"心跳: phase={self._phase}, task={current_task.mine_action}, "
            f"loc={game_state.location_name}, mine_level={game_state.mine_level}, "
            f"player={game_state.player_tile}, target={self._target_tile}, "
            f"detected_ladder={self._detected_ladder_tile}, "
            f"stone_attempt={self._stone_attempt_count}, broken={self._broken_stone_count}, "
            f"ladders={self._format_targets(game_state.ladders)}, "
            f"entrances={self._format_targets(game_state.mine_entrances)}, "
            f"mining_nodes={len(game_state.mining_nodes)}, stone_layer={len(game_state.layers.get('Stone', set()))}, "
            f"using_tool={game_state.using_tool}, can_move={game_state.can_move}, "
            f"tracker={self.tool_action_tracker.get_debug_snapshot()}"
        )

    def _format_targets(self, targets: list[MineInteractTargetState]) -> str:
        preview = [
            (
                f"{target.type}@{target.tile}"
                f"/source={target.source or '-'}"
                f"/qid={target.qualified_item_id or '-'}"
                f"/action={target.action or '-'}"
            )
            for target in targets[:8]
        ]
        return "[" + ", ".join(preview) + "]"

    def _format_tiles(self, tiles: set[Tile]) -> str:
        ordered_tiles = sorted(tiles, key=lambda tile: (tile.x, tile.y))
        return "[" + ", ".join(str(tile) for tile in ordered_tiles) + "]"

    def _log(self, message: str) -> None:
        self.mining_debug_logger.log(f"[MineNode] {message}")
