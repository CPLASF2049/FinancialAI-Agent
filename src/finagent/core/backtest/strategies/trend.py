"""趋势跟踪类策略。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from finagent.core.backtest.base import BacktestStrategy
from finagent.core.backtest.registry import register_strategy
from finagent.core.indicators import macd as macd_indicator
from finagent.core.indicators import rsi as rsi_indicator


@register_strategy
class MaCrossStrategy(BacktestStrategy):
    """双均线金叉/死叉。"""

    name = "ma_cross"
    description = "双均线金叉买入、死叉卖出 (short_window, long_window)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        frame["short_ma"] = frame["close"].rolling(int(params.get("short_window", 5))).mean()
        frame["long_ma"] = frame["close"].rolling(int(params.get("long_window", 20))).mean()
        return frame

    def signal(self, row, prev_row, params, context=None) -> int:
        if prev_row is None or self._na(
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

    @classmethod
    def required_history(cls, params: Mapping[str, Any]) -> int:
        return int(params.get("long_window", 20)) + 30


@register_strategy
class TripleMaStrategy(BacktestStrategy):
    """短线上穿中线，且中线在长线之上。"""

    name = "triple_ma"
    description = "三均线排列过滤 (short_window, mid_window, long_window)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        frame["short_ma"] = frame["close"].rolling(int(params.get("short_window", 5))).mean()
        frame["mid_ma"] = frame["close"].rolling(int(params.get("mid_window", 20))).mean()
        frame["long_ma"] = frame["close"].rolling(int(params.get("long_window", 60))).mean()
        return frame

    def signal(self, row, prev_row, params, context=None) -> int:
        if prev_row is None or self._na(
            row.get("short_ma"),
            row.get("mid_ma"),
            row.get("long_ma"),
            prev_row.get("short_ma"),
            prev_row.get("mid_ma"),
        ):
            return 0
        golden = prev_row["short_ma"] <= prev_row["mid_ma"] and row["short_ma"] > row["mid_ma"]
        dead = prev_row["short_ma"] >= prev_row["mid_ma"] and row["short_ma"] < row["mid_ma"]
        if golden and row["mid_ma"] > row["long_ma"]:
            return 1
        if dead:
            return -1
        return 0

    @classmethod
    def required_history(cls, params: Mapping[str, Any]) -> int:
        return int(params.get("long_window", 60)) + 20


@register_strategy
class EmaSmaBiasStrategy(BacktestStrategy):
    """价格上穿 EMA、同时 EMA > SMA 且乖离足够大时买入。"""

    name = "ema_sma_bias"
    description = "价格上穿EMA且EMA>SMA且乖离达标 (ema_span, sma_window, bias_threshold)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        frame["ema_fast"] = (
            frame["close"].ewm(span=int(params.get("ema_span", 12)), adjust=False).mean()
        )
        frame["sma_slow"] = frame["close"].rolling(int(params.get("sma_window", 50))).mean()
        return frame

    def signal(self, row, prev_row, params, context=None) -> int:
        if prev_row is None or self._na(
            row.get("ema_fast"),
            row.get("sma_slow"),
            row.get("close"),
            prev_row.get("close"),
            prev_row.get("ema_fast"),
        ):
            return 0
        ema = float(row["ema_fast"])
        if ema <= float(row["sma_slow"]):
            return 0
        bias = float(params.get("bias_threshold", 0.0))
        bias_ok = (float(row["close"]) - ema) / (ema + 1e-12) >= bias
        cross_up = prev_row["close"] <= prev_row["ema_fast"] and row["close"] > ema
        cross_down = prev_row["close"] >= prev_row["ema_fast"] and row["close"] < ema
        if cross_up and bias_ok:
            return 1
        if cross_down:
            return -1
        return 0

    @classmethod
    def required_history(cls, params: Mapping[str, Any]) -> int:
        return int(params.get("sma_window", 50)) + 20


@register_strategy
class MomentumRocStrategy(BacktestStrategy):
    """N 日 ROC 过零。"""

    name = "momentum_roc"
    description = "N日ROC过零 (roc_window)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        window = int(params.get("roc_window", 10))
        frame["roc"] = (frame["close"] / frame["close"].shift(window) - 1.0) * 100.0
        return frame

    def signal(self, row, prev_row, params, context=None) -> int:
        if prev_row is None or self._na(row.get("roc"), prev_row.get("roc")):
            return 0
        if prev_row["roc"] < 0 <= row["roc"]:
            return 1
        if prev_row["roc"] > 0 >= row["roc"]:
            return -1
        return 0


@register_strategy
class MacdStrategy(BacktestStrategy):
    """DIF/DEA 金叉死叉。"""

    name = "macd"
    description = "DIF/DEA金叉死叉 (fast_period, slow_period, signal_period)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        return macd_indicator(
            frame,
            fast_period=int(params.get("fast_period", 12)),
            slow_period=int(params.get("slow_period", 26)),
            signal_period=int(params.get("signal_period", 9)),
        )

    def signal(self, row, prev_row, params, context=None) -> int:
        if prev_row is None or self._na(
            row.get("dif"), row.get("dea"), prev_row.get("dif"), prev_row.get("dea")
        ):
            return 0
        if prev_row["dif"] <= prev_row["dea"] and row["dif"] > row["dea"]:
            return 1
        if prev_row["dif"] >= prev_row["dea"] and row["dif"] < row["dea"]:
            return -1
        return 0

    @classmethod
    def required_history(cls, params: Mapping[str, Any]) -> int:
        return int(params.get("slow_period", 26)) * 3


@register_strategy
class RsiMa200Strategy(BacktestStrategy):
    """RSI 反转信号，叠加长期均线趋势过滤。"""

    name = "rsi_ma200"
    description = "RSI反转+长期均线趋势过滤 (ma_window, window, lower, upper)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        frame["trend_ma"] = frame["close"].rolling(int(params.get("ma_window", 200))).mean()
        return rsi_indicator(frame, period=int(params.get("window", 14)))

    def signal(self, row, prev_row, params, context=None) -> int:
        if prev_row is None or self._na(
            row.get("rsi"),
            prev_row.get("rsi"),
            row.get("trend_ma"),
            prev_row.get("trend_ma"),
            row.get("close"),
        ):
            return 0
        lower = float(params.get("lower", 30))
        upper = float(params.get("upper", 70))
        above = float(row["close"]) > float(row["trend_ma"])
        if above and prev_row["rsi"] < lower and row["rsi"] >= lower:
            return 1
        if prev_row["rsi"] > upper and row["rsi"] <= upper:
            return -1
        if prev_row["close"] >= prev_row["trend_ma"] and row["close"] < row["trend_ma"]:
            return -1
        return 0

    @classmethod
    def required_history(cls, params: Mapping[str, Any]) -> int:
        return int(params.get("ma_window", 200)) + 40
