import time

from agent.action.combat.combat_tactical_resolver import TacticalDecision
from agent.action.combat.monster_threat import MonsterThreat, MonsterThreatEvaluator
from agent.action.combat.weapon_selection import WeaponSelector
from agent.action.valley_action.AStar import astar_solver
from agent.action.valley_action.action_type import StardewAction, StardewCommand
from agent.action.valley_action.positioning_controller import PositioningController, PositioningGoal
from agent.action.valley_action.tool_targeting import build_tool_target_face_command, is_tool_targeting
from agent.behavior_tree.behavior_tree import BTNode, NodeStatus
from agent.base_task import BaseTask, TaskType
from agent.behavior_tree.blackboard import AgentBlackboard
from agent.behavior_tree.defend_debug_logger import DefendDebugLogger
from agent.behavior_tree.player_context import PlayerContext
from agent.behavior_tree.tool_selection import is_current_tool
from server.type import Tile


DEFEND_ATTACK_INTERVAL_SECONDS = 0.45
DEFEND_MELEE_CHEBYSHEV_DISTANCE = 1


class Defend_Node(BTNode):
    def __init__(self) -> None:
        self.threat_evaluator = MonsterThreatEvaluator()
        self.weapon_selector = WeaponSelector()
        self.positioning_controller = PositioningController()
        self.defend_debug_logger = DefendDebugLogger()
        self._active = False
        self._has_sent_idle = False
        self._last_attack_at = 0.0
        self._last_debug_heartbeat_at = 0.0

    async def run(self, blackboard: AgentBlackboard, context: PlayerContext) -> NodeStatus:
        game_state = context.state
        if game_state is None:
            return "FAILURE"

        snapshot = self.threat_evaluator.evaluate(game_state)
        self._log_debug_heartbeat(snapshot)
        tactical_decision = self._get_active_tactical_decision(blackboard, snapshot)
        primary_threat = self._select_primary_threat(snapshot, tactical_decision)

        if primary_threat is None:
            if self._active:
                self._log("威胁解除，Guard 本 tick 让出控制权")
                self._reset()
                return "SUCCESS"
            return "FAILURE"

        self._active = True
        if not self._has_sent_idle:
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
            self._has_sent_idle = True
            self.positioning_controller.reset()
            self._log(
                f"发现威胁并停止移动: threat={self._format_threat(primary_threat)}, "
                f"tactical={self._format_tactical_decision(tactical_decision)}"
            )
            return "RUNNING"

        if tactical_decision is not None:
            self._log(
                f"执行战术决策: decision={tactical_decision.decision_type}, "
                f"reason={tactical_decision.reason}, threat={self._format_threat(primary_threat)}"
            )
            if tactical_decision.decision_type == "ENGAGE":
                return self._run_fight(blackboard, context, primary_threat)
            if tactical_decision.decision_type == "AVOID":
                return self._run_avoid(context, snapshot.threats)

            blackboard.combat_tactical_decision = None
            self._reset()
            return "FAILURE"

        if primary_threat.threat_level in ("FIGHT", "BLOCK"):
            return self._run_fight(blackboard, context, primary_threat)

        return self._run_avoid(context, snapshot.threats)

    def _run_fight(
        self,
        blackboard: AgentBlackboard,
        context: PlayerContext,
        threat: MonsterThreat,
    ) -> NodeStatus:
        game_state = context.state
        if game_state is None:
            return "RUNNING"

        weapon = self.weapon_selector.select_best_weapon(game_state)
        if weapon is None:
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
            self._log("需要战斗但背包中没有武器，保持停止并交给后续规划")
            return "SUCCESS"

        if not is_current_tool(game_state, weapon.name):
            blackboard.require_switch_tool = True
            blackboard.is_switching_tool = True
            blackboard.required_tool_owner = "Guard"
            blackboard.required_tool = weapon.name
            self._log(f"请求切换武器: weapon={self._format_weapon(weapon)}, threat={self._format_threat(threat)}")
            return "RUNNING"

        if self._is_same_tile(game_state.player_tile, threat.tile):
            return self._attack_threat(context, weapon, threat, "怪物与玩家位于同一地块，跳过站位和转向直接攻击")

        if self._chebyshev_distance(game_state.player_tile, threat.tile) <= DEFEND_MELEE_CHEBYSHEV_DISTANCE:
            if not is_tool_targeting(game_state, threat.tile):
                command = build_tool_target_face_command(game_state.player_tile, threat.tile)
                context.executor_client.send_command(command)
                self._log(f"贴身面向怪物: threat={self._format_threat(threat)}, command={command.action}")
                return "RUNNING"
            return self._attack_threat(context, weapon, threat, "怪物已在贴身范围内，直接攻击")

        if self._tile_distance(game_state.player_tile, threat.tile) > 1:
            return self._run_approach_monster(context, threat)

        if not is_tool_targeting(game_state, threat.tile):
            command = build_tool_target_face_command(game_state.player_tile, threat.tile)
            context.executor_client.send_command(command)
            self._log(f"面向怪物: threat={self._format_threat(threat)}, command={command.action}")
            return "RUNNING"

        return self._attack_threat(context, weapon, threat, "怪物位于正交相邻格，攻击")

    def _attack_threat(
        self,
        context: PlayerContext,
        weapon,
        threat: MonsterThreat,
        reason: str,
    ) -> NodeStatus:
        now = time.time()
        if now - self._last_attack_at < DEFEND_ATTACK_INTERVAL_SECONDS:
            return "RUNNING"

        response = context.executor_client.send_command(StardewCommand(action=StardewAction.ATTACK_WEAPON, key=["c"]))
        if response == "BUSY":
            self._log(f"攻击命令 BUSY，等待下一帧: reason={reason}, threat={self._format_threat(threat)}")
            return "RUNNING"

        self._last_attack_at = now
        self._log(
            f"发送攻击命令: reason={reason}, response={response}, "
            f"weapon={self._format_weapon(weapon)}, threat={self._format_threat(threat)}"
        )
        return "RUNNING"

    def _run_approach_monster(self, context: PlayerContext, threat: MonsterThreat) -> NodeStatus:
        game_state = context.state
        if game_state is None:
            return "RUNNING"

        result = self.positioning_controller.tick(
            game_state,
            PositioningGoal(
                candidate_stand_tiles=self._build_cardinal_neighbor_tiles(threat.tile),
                tool_target_tile=threat.tile,
                extra_blocked_tiles={threat.tile},
                require_close_to_target=True,
            ),
        )
        if result.command is not None:
            context.executor_client.send_command(result.command)

        self._log(
            f"接近怪物: threat={self._format_threat(threat)}, status={result.status}, "
            f"stand={result.stand_tile}, reason={result.reason}, "
            f"positioning={self.positioning_controller.get_debug_snapshot()}"
        )
        if result.status == "FAILED":
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
        return "RUNNING"

    def _run_avoid(self, context: PlayerContext, threats: list[MonsterThreat]) -> NodeStatus:
        game_state = context.state
        if game_state is None:
            return "RUNNING"

        safe_tile = self._select_safe_neighbor_tile(game_state, threats)
        if safe_tile is None:
            self._log("没有找到可用避让邻格，保持停止")
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
            return "RUNNING"

        result = self.positioning_controller.tick(
            game_state,
            PositioningGoal(
                candidate_stand_tiles={safe_tile},
                tool_target_tile=None,
                extra_blocked_tiles=self.threat_evaluator.evaluate(game_state).blocking_tiles,
            ),
        )
        if result.command is not None:
            context.executor_client.send_command(result.command)

        self._log(f"执行避让: safe_tile={safe_tile}, result={result.status}, reason={result.reason}")
        if result.status == "READY":
            return "SUCCESS"
        if result.status == "FAILED":
            context.executor_client.send_command(StardewCommand(action=StardewAction.IDLE))
            return "RUNNING"
        return "RUNNING"

    def _select_safe_neighbor_tile(self, game_state, threats: list[MonsterThreat]) -> Tile | None:
        blocked_tiles = astar_solver._get_blocked_tiles(game_state)
        snapshot = self.threat_evaluator.evaluate(game_state)
        candidates: list[Tile] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                tile = Tile(game_state.player_tile.x + dx, game_state.player_tile.y + dy)
                if tile.x < 0 or tile.y < 0:
                    continue
                if tile in blocked_tiles or tile in snapshot.blocking_tiles:
                    continue
                candidates.append(tile)

        if not candidates:
            return None

        def score(tile: Tile) -> tuple[float, float]:
            nearest_distance = min(self._tile_distance(tile, threat.tile) for threat in threats)
            risk_penalty = snapshot.risk_tiles.get(tile, 0.0)
            return nearest_distance - risk_penalty, -self._tile_distance(game_state.player_tile, tile)

        return max(candidates, key=score)

    def _get_active_tactical_decision(
        self,
        blackboard: AgentBlackboard,
        snapshot,
    ) -> TacticalDecision | None:
        tactical_decision = blackboard.combat_tactical_decision
        if not isinstance(tactical_decision, TacticalDecision):
            return None

        if not tactical_decision.is_active:
            blackboard.combat_tactical_decision = None
            return None

        if tactical_decision.target_threat is None:
            return tactical_decision

        if not self._find_matching_threat(snapshot.threats, tactical_decision.target_threat):
            blackboard.combat_tactical_decision = None
            return None

        return tactical_decision

    def _select_primary_threat(
        self,
        snapshot,
        tactical_decision: TacticalDecision | None,
    ) -> MonsterThreat | None:
        if tactical_decision is not None and tactical_decision.target_threat is not None:
            matching_threat = self._find_matching_threat(snapshot.threats, tactical_decision.target_threat)
            return matching_threat or tactical_decision.target_threat

        for threat in snapshot.threats:
            if threat.threat_level in ("FIGHT", "BLOCK"):
                return threat
        return None

    def _find_matching_threat(
        self,
        threats: list[MonsterThreat],
        target_threat: MonsterThreat,
    ) -> MonsterThreat | None:
        for threat in threats:
            if threat.monster.name == target_threat.monster.name and threat.tile == target_threat.tile:
                return threat
        return None

    def _build_cardinal_neighbor_tiles(self, target_tile: Tile) -> set[Tile]:
        return {
            Tile(target_tile.x + 1, target_tile.y),
            Tile(target_tile.x - 1, target_tile.y),
            Tile(target_tile.x, target_tile.y + 1),
            Tile(target_tile.x, target_tile.y - 1),
        }

    def _reset(self) -> None:
        self.positioning_controller.reset()
        self._active = False
        self._has_sent_idle = False
        self._last_attack_at = 0.0
        self._last_debug_heartbeat_at = 0.0

    def _tile_distance(self, start_tile: Tile, end_tile: Tile) -> int:
        return abs(start_tile.x - end_tile.x) + abs(start_tile.y - end_tile.y)

    def _chebyshev_distance(self, start_tile: Tile, end_tile: Tile) -> int:
        return max(abs(start_tile.x - end_tile.x), abs(start_tile.y - end_tile.y))

    def _is_same_tile(self, start_tile: Tile, end_tile: Tile) -> bool:
        return start_tile.x == end_tile.x and start_tile.y == end_tile.y

    def _format_threat(self, threat: MonsterThreat | None) -> str:
        if threat is None:
            return "None"
        monster = threat.monster
        return (
            f"name={monster.name}, tile={monster.tile}, focused={monster.focused_on_farmer}, "
            f"search={monster.search_array_size}, health={monster.health}, damage={monster.damage_to_farmer}, "
            f"distance={threat.distance_to_player}, score={threat.threat_score:.2f}, level={threat.threat_level}"
        )

    def _format_weapon(self, weapon) -> str:
        if weapon is None:
            return "None"
        return (
            f"index={weapon.index}, name={weapon.name}, qid={weapon.qualified_item_id}, "
            f"min_damage={weapon.min_damage}, max_damage={weapon.max_damage}, weapon_type={weapon.weapon_type}"
        )

    def _format_tactical_decision(self, tactical_decision: TacticalDecision | None) -> str:
        if tactical_decision is None:
            return "None"
        return (
            f"type={tactical_decision.decision_type}, reason={tactical_decision.reason}, "
            f"active={tactical_decision.is_active}"
        )

    def _log_debug_heartbeat(self, snapshot) -> None:
        now = time.time()
        if now - self._last_debug_heartbeat_at < 0.25:
            return

        self._last_debug_heartbeat_at = now
        preview = "; ".join(self._format_threat(threat) for threat in snapshot.threats[:5])
        self._log(
            f"心跳: active={self._active}, has_sent_idle={self._has_sent_idle}, "
            f"max_score={snapshot.max_threat_score:.2f}, nearest={self._format_threat(snapshot.nearest_threat)}, "
            f"blocking_tiles={sorted(snapshot.blocking_tiles, key=lambda tile: (tile.x, tile.y))[:12]}, threats=[{preview}]"
        )

    def _log(self, message: str) -> None:
        self.defend_debug_logger.log(f"[DefendNode] {message}")


class DefendTask(BaseTask):
    def __init__(self, task_type: TaskType, desc: str):
        super().__init__(task_type=task_type, desc=desc)
