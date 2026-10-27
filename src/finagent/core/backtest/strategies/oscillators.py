"""震荡指标类策略。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from finagent.core.backtest.base import BacktestStrategy
from finagent.core.backtest.registry import register_strategy
from finagent.core.indicators import cci as cci_indicator
from finagent.core.indicators import kdj as kdj_indicator
from finagent.core.indicators import rsi as rsi_indicator
from finagent.core.indicators import stochastic as stochastic_indicator
from finagent.core.indicators import williams_r as williams_indicator


@register_strategy
class RsiStrategy(BacktestStrategy):
    """RSI 穿越阈值（跌破下限时买入）。"""

    name = "rsi"
    description = "RSI穿越超卖/超买阈值 (window, lower, upper)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        return rsi_indicator(frame, period=int(params.get("window", 14)))

    def signal(self, row, prev_row, params, context=None) -> int:
        if prev_row is None or self._na(row.get("rsi"), prev_row.get("rsi")):
            return 0
        lower = float(params.get("lower", 30))
        upper = float(params.get("upper", 70))
        if prev_row["rsi"] >= lower and row["rsi"] < lower:
            return 1
        if prev_row["rsi"] <= upper and row["rsi"] > upper:
            return -1
        return 0


@register_strategy
class KdjStrategy(BacktestStrategy):
    """K/D 交叉。"""

    name = "kdj"
    description = "K上穿D买入、K下穿D卖出 (k_period, d_period, j_period)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        return kdj_indicator(
            frame,
            k_period=int(params.get("k_period", 9)),
            d_period=int(params.get("d_period", 3)),
            j_period=int(params.get("j_period", 3)),
        )

    def signal(self, row, prev_row, params, context=None) -> int:
        if prev_row is None or self._na(
            row.get("k"), row.get("d"), prev_row.get("k"), prev_row.get("d")
        ):
            return 0
        if prev_row["k"] <= prev_row["d"] and row["k"] > row["d"]:
            return 1
        if prev_row["k"] >= prev_row["d"] and row["k"] < row["d"]:
            return -1
        return 0


@register_strategy
class CciStrategy(BacktestStrategy):
    """CCI 穿越 -100 / +100。"""

    name = "cci"
    description = "CCI穿越-100/+100 (period, oversold, overbought)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        return cci_indicator(frame, period=int(params.get("period", 20)))

    def signal(self, row, prev_row, params, context=None) -> int:
        if prev_row is None or self._na(row.get("cci"), prev_row.get("cci")):
            return 0
        low_th = float(params.get("oversold", -100.0))
        high_th = float(params.get("overbought", 100.0))
        if prev_row["cci"] <= low_th and row["cci"] > low_th:
            return 1
        if prev_row["cci"] >= high_th and row["cci"] < high_th:
            return -1
        return 0


@register_strategy
class WilliamsRStrategy(BacktestStrategy):
    """Williams %R 区段穿越。"""

    name = "williams_r"
    description = "Williams%R区段穿越 (period, oversold, overbought)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        return williams_indicator(frame, period=int(params.get("period", 14)))

    def signal(self, row, prev_row, params, context=None) -> int:
        if prev_row is None or self._na(row.get("willr"), prev_row.get("willr")):
            return 0
        oversold = float(params.get("oversold", -80.0))
        overbought = float(params.get("overbought", -20.0))
        if prev_row["willr"] <= oversold and row["willr"] > oversold:
            return 1
        if prev_row["willr"] >= overbought and row["willr"] < overbought:
            return -1
        return 0


@register_strategy
class StochasticStrategy(BacktestStrategy):
    """随机指标 %K/%D 在超卖/超买区交叉。"""

    name = "stochastic"
    description = (
        "随机指标%K/%D在超卖超买区交叉 (k_period, d_period, oversold_level, overbought_level)"
    )

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        return stochastic_indicator(
            frame,
            k_period=int(params.get("k_period", 14)),
            d_period=int(params.get("d_period", 3)),
        )

    def signal(self, row, prev_row, params, context=None) -> int:
        if prev_row is None or self._na(
            row.get("st_k"), row.get("st_d"), prev_row.get("st_k"), prev_row.get("st_d")
        ):
            return 0
        oversold = float(params.get("oversold_level", 30.0))
        overbought = float(params.get("overbought_level", 70.0))
        golden = (
            prev_row["st_k"] <= prev_row["st_d"]
            and row["st_k"] > row["st_d"]
            and row["st_k"] < oversold + 5
        )
        dead = (
            prev_row["st_k"] >= prev_row["st_d"]
            and row["st_k"] < row["st_d"]
            and row["st_k"] > overbought - 5
        )
        if golden:
            return 1
        if dead:
            return -1
        return 0
