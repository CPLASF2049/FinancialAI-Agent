"""回测策略契约。

策略只需要给行情数据补上它要用的列，并对每根 K 线给出方向信号（``1`` 开仓、
``-1`` 平仓、``0`` 持有）。仓位分配与止损退出是可选钩子，所以 23 个内置策略都
很短，同时仍能支持 ATR 止损和按波动率缩仓。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

import pandas as pd


class BacktestStrategy(ABC):
    """单标的、单根 K 线的策略接口。"""

    #: 注册表键，同时也是 ``run_backtest`` 的 ``strategy`` 参数。
    name: str = ""
    #: 一行人类可读的说明，由 ``list_strategies`` 展示。
    description: str = ""
    #: 供需要多标的截面的策略标记（单标的引擎不支持；
    #: 工具会返回明确错误而不是硬算）。
    requires_multi_asset: bool = False

    @abstractmethod
    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        """返回补好 :meth:`signal` 需要的新列的 ``frame``。"""

    @abstractmethod
    def signal(
        self,
        row: pd.Series,
        prev_row: pd.Series | None,
        params: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
    ) -> int:
        """返回 ``1``（买）、``-1``（卖）或 ``0``（持有）。第 0 根 K 线时 ``prev_row`` 为 ``None``。"""

    def risk_exit(
        self,
        row: pd.Series,
        prev_row: pd.Series | None,
        params: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> int:
        """持仓时优先于 :meth:`signal` 评估的强制退出钩子。"""

        return 0

    def position_fraction(self, params: Mapping[str, Any], context: Mapping[str, Any]) -> float:
        """开仓时实际投入的资金比例；默认取参数 ``position_fraction``。"""

        return float(params.get("position_fraction", 1.0))

    # ------------------------------------------------------------------
    @staticmethod
    def _na(*values: Any) -> bool:
        """任一值为空或 NaN 时返回 True。"""

        for value in values:
            if value is None or pd.isna(value):
                return True
        return False

    @classmethod
    def required_history(cls, params: Mapping[str, Any]) -> int:
        """产生第一个可用信号前需要的预热 K 线根数。"""

        _ = params
        return 60
