from agent.action.mining.mine_target import MineTarget
from server.valley_server import StardewState
from server.type import Tile


class MiningRiskEvaluator:
    """
    Mining 机会资源风险评估入口。

    当前阶段暂不把怪物纳入机会资源评分，统一返回 0。
    未来接入怪物威胁时，可以在这里根据怪物仇恨范围、focusedOnFarmer、
    玩家血量/武器和路径风险计算 risk_cost，而不需要改 MineNode 主流程。
    """

    def calculate_path_risk(
        self,
        state: StardewState,
        target: MineTarget,
        path_tiles: list[Tile],
    ) -> float:
        return 0.0
