"""量价 / 资金流类策略。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from finagent.core.backtest.base import BacktestStrategy
from finagent.core.backtest.registry import register_strategy
from finagent.core.indicators import obv as obv_indicator
from finagent.core.indicators import vwap_ma


@register_strategy
class VolumeBreakoutStrategy(BacktestStrategy):
    """放量突破 N 日高点。"""

    name = "volume_breakout"
    description = "放量突破N日高 (breakout_period, vol_ma_period, volume_mult, exit_period)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        period = int(params.get("breakout_period", 20))
        vol_period = int(params.get("vol_ma_period", 20))
        exit_period = int(params.get("exit_period", period))
        frame["range_high"] = frame["high"].rolling(period).max().shift(1)
        frame["range_low"] = frame["low"].rolling(exit_period).min().shift(1)
        frame["vol_ma"] = frame["vol"].rolling(vol_period).mean()
        return frame

    def signal(self, row, prev_row, params, context=None) -> int:
        if prev_row is None or self._na(
            row.get("range_high"), row.get("range_low"), row.get("vol_ma")
        ):
            return 0
        mult = float(params.get("volume_mult", 1.5))
        if row["vol"] > mult * row["vol_ma"] and row["close"] > row["range_high"]:
            return 1
        if row["close"] < row["range_low"]:
            return -1
        return 0

    @classmethod
    def required_history(cls, params: Mapping[str, Any]) -> int:
        return (
            max(int(params.get("breakout_period", 20)), int(params.get("vol_ma_period", 20))) + 30
        )


@register_strategy
class ObvCrossStrategy(BacktestStrategy):
    """OBV 与其自身均线交叉。"""

    name = "obv_cross"
    description = "OBV与其均线交叉 (obv_ma_period)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        obv_indicator(frame)
        frame["obv_ma"] = frame["obv"].rolling(int(params.get("obv_ma_period", 30))).mean()
        return frame

    def signal(self, row, prev_row, params, context=None) -> int:
        if prev_row is None or self._na(
            row.get("obv"), row.get("obv_ma"), prev_row.get("obv"), prev_row.get("obv_ma")
        ):
            return 0
        if prev_row["obv"] <= prev_row["obv_ma"] and row["obv"] > row["obv_ma"]:
            return 1
        if prev_row["obv"] >= prev_row["obv_ma"] and row["obv"] < row["obv_ma"]:
            return -1
        return 0

    @classmethod
    def required_history(cls, params: Mapping[str, Any]) -> int:
        return int(params.get("obv_ma_period", 30)) + 30


@register_strategy
class VwapDeviationStrategy(BacktestStrategy):
    """围绕滚动 VWAP 线的均值回归。"""

    name = "vwap_deviation"
    description = "相对VWAP均线偏离反转 (period, deviation)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        return vwap_ma(frame, period=int(params.get("period", 20)))

    def signal(self, row, prev_row, params, context=None) -> int:
        if prev_row is None or self._na(row.get("vwap_ma"), row.get("close")):
            return 0
        deviation = float(params.get("deviation", 0.008))
        vwap = float(row["vwap_ma"])
        if row["close"] < vwap * (1.0 - deviation):
            return 1
        if row["close"] > vwap:
            return -1
        return 0
