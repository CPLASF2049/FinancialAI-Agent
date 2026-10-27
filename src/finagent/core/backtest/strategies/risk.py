"""双均线策略的风险管理变体。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from finagent.core.backtest.base import BacktestStrategy
from finagent.core.backtest.registry import register_strategy
from finagent.core.indicators import atr


def _ma_enrich(frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
    frame["short_ma"] = frame["close"].rolling(int(params.get("short_window", 5))).mean()
    frame["long_ma"] = frame["close"].rolling(int(params.get("long_window", 20))).mean()
    return frame


def _ma_signal(row, prev_row, params, context=None) -> int:
    if prev_row is None or BacktestStrategy._na(
        row.get("short_ma"),
        row.get("long_ma"),
        prev_row.get("short_ma"),
        prev_row.get("long_ma"),
    ):
        return 0
    if prev_row["short_ma"] <= prev_row["long_ma"] and row["short_ma"] > row["long_ma"]:
        return 1
    if prev_row["short_ma"] >= prev_row["long_ma"] and row["short_ma"] < row["long_ma"]:
        return -1
    return 0


@register_strategy
class MaCrossAtrStopStrategy(BacktestStrategy):
    """双均线 + ATR 倍数硬止损。"""

    name = "ma_cross_atr_stop"
    description = "双均线+ATR倍数硬止损 (atr_stop_mult, atr_period, short_window, long_window)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        _ma_enrich(frame, params)
        return atr(frame, period=int(params.get("atr_period", 14)))

    def signal(self, row, prev_row, params, context=None) -> int:
        return _ma_signal(row, prev_row, params, context)

    def risk_exit(self, row, prev_row, params, context) -> int:
        mult = float(params.get("atr_stop_mult", 2.0))
        entry = context.get("entry_price")
        if entry is None or self._na(row.get("atr")):
            return 0
        return -1 if row["close"] < float(entry) - mult * float(row["atr"]) else 0

    @classmethod
    def required_history(cls, params: Mapping[str, Any]) -> int:
        return int(params.get("long_window", 20)) + 30


@register_strategy
class VolTargetMaCrossStrategy(BacktestStrategy):
    """双均线，并按「ATR 占价的比例」缩仓以实现波动目标。"""

    name = "vol_target_ma_cross"
    description = "双均线+按ATR缩仓的波动目标 (risk_budget_pct, max_fraction, atr_period)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        _ma_enrich(frame, params)
        return atr(frame, period=int(params.get("atr_period", 14)))

    def signal(self, row, prev_row, params, context=None) -> int:
        return _ma_signal(row, prev_row, params, context)

    def position_fraction(self, params: Mapping[str, Any], context: Mapping[str, Any]) -> float:
        signal_row = context.get("signal_row")
        if signal_row is None or self._na(signal_row.get("atr"), signal_row.get("close")):
            return 1.0
        risk_pct = float(params.get("risk_budget_pct", 2.0)) / 100.0
        atr_fraction = max(1e-6, float(signal_row["atr"]) / float(signal_row["close"]))
        cap = float(params.get("max_fraction", 1.0))
        return min(cap, risk_pct / atr_fraction)

    @classmethod
    def required_history(cls, params: Mapping[str, Any]) -> int:
        return int(params.get("long_window", 20)) + 30


@register_strategy
class KellyMaCrossStrategy(BacktestStrategy):
    """双均线 + 固定仓位比例（简化版凯利上限）。"""

    name = "kelly_ma_cross"
    description = "双均线+固定仓位比例 (equity_fraction, short_window, long_window)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        return _ma_enrich(frame, params)

    def signal(self, row, prev_row, params, context=None) -> int:
        return _ma_signal(row, prev_row, params, context)

    def position_fraction(self, params: Mapping[str, Any], context: Mapping[str, Any]) -> float:
        return max(0.05, min(1.0, float(params.get("equity_fraction", 0.35))))

    @classmethod
    def required_history(cls, params: Mapping[str, Any]) -> int:
        return int(params.get("long_window", 20)) + 30
