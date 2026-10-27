"""通道、突破与波动率过滤类策略。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from finagent.core.backtest.base import BacktestStrategy
from finagent.core.backtest.registry import register_strategy
from finagent.core.indicators import adx_di, atr, boll, macd


def _boll_enrich(frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
    return boll(
        frame,
        period=int(params.get("period", 20)),
        std_dev=float(params.get("std_dev", 2)),
    )


@register_strategy
class BollReversionStrategy(BacktestStrategy):
    """价格收回布林下轨之上买入，上穿中轨卖出。"""

    name = "boll_reversion"
    description = "布林下轨外收回买入、上穿中轨卖出 (period, std_dev)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        return _boll_enrich(frame, params)

    def signal(self, row, prev_row, params, context=None) -> int:
        if prev_row is None or self._na(
            row.get("close"),
            row.get("boll_lower"),
            row.get("boll_mid"),
            prev_row.get("close"),
            prev_row.get("boll_lower"),
            prev_row.get("boll_mid"),
        ):
            return 0
        if prev_row["close"] < prev_row["boll_lower"] and row["close"] >= row["boll_lower"]:
            return 1
        if prev_row["close"] < prev_row["boll_mid"] and row["close"] >= row["boll_mid"]:
            return -1
        return 0


@register_strategy
class BollBreakoutStrategy(BacktestStrategy):
    """突破布林上轨买入，跌回中轨下方卖出。"""

    name = "boll_breakout"
    description = "突破布林上轨买入、跌回中轨下方卖出 (period, std_dev)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        return _boll_enrich(frame, params)

    def signal(self, row, prev_row, params, context=None) -> int:
        if prev_row is None or self._na(
            row.get("close"),
            row.get("boll_upper"),
            row.get("boll_mid"),
            prev_row.get("close"),
            prev_row.get("boll_upper"),
            prev_row.get("boll_mid"),
        ):
            return 0
        if prev_row["close"] <= prev_row["boll_upper"] and row["close"] > row["boll_upper"]:
            return 1
        if prev_row["close"] >= prev_row["boll_mid"] and row["close"] < row["boll_mid"]:
            return -1
        return 0


@register_strategy
class DonchianBreakoutStrategy(BacktestStrategy):
    """单一窗口的唐奇安通道突破。"""

    name = "donchian_breakout"
    description = "突破N日高买入、跌破N日低卖出 (channel_period)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        period = int(params.get("channel_period", 20))
        frame["dc_upper"] = frame["high"].rolling(period).max().shift(1)
        frame["dc_lower"] = frame["low"].rolling(period).min().shift(1)
        return frame

    def signal(self, row, prev_row, params, context=None) -> int:
        if prev_row is None or self._na(row.get("dc_upper"), row.get("dc_lower")):
            return 0
        if row["close"] > row["dc_upper"]:
            return 1
        if row["close"] < row["dc_lower"]:
            return -1
        return 0

    @classmethod
    def required_history(cls, params: Mapping[str, Any]) -> int:
        return int(params.get("channel_period", 20)) + 30


@register_strategy
class TurtleStrategy(BacktestStrategy):
    """海龟系统：突破入场、反向突破出场，可选 ATR 止损。"""

    name = "turtle"
    description = (
        "海龟通道入场/出场，可选ATR止损 (entry_period, exit_period, atr_stop_mult, atr_period)"
    )

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        entry_n = int(params.get("entry_period", 20))
        exit_n = int(params.get("exit_period", 10))
        frame["dc_high"] = frame["high"].rolling(entry_n).max().shift(1)
        frame["dc_exit"] = frame["low"].rolling(exit_n).min().shift(1)
        if float(params.get("atr_stop_mult", 0) or 0) > 0:
            atr(frame, period=int(params.get("atr_period", 14)))
        return frame

    def signal(self, row, prev_row, params, context=None) -> int:
        context = context or {}
        if prev_row is None or self._na(row.get("dc_high"), row.get("dc_exit")):
            return 0
        position = int(context.get("position") or 0)
        if position > 0 and row["close"] < row["dc_exit"]:
            return -1
        if position == 0 and row["close"] > row["dc_high"]:
            return 1
        return 0

    def risk_exit(self, row, prev_row, params, context) -> int:
        mult = float(params.get("atr_stop_mult", 0) or 0)
        if mult <= 0:
            return 0
        entry = context.get("entry_price")
        if entry is None or self._na(row.get("atr")):
            return 0
        return -1 if row["close"] < float(entry) - mult * float(row["atr"]) else 0

    @classmethod
    def required_history(cls, params: Mapping[str, Any]) -> int:
        return max(int(params.get("entry_period", 20)), 30)


@register_strategy
class AdxMacdStrategy(BacktestStrategy):
    """用 ADX/DI 过滤趋势后，再做 MACD 金叉死叉。"""

    name = "adx_macd"
    description = "ADX+DI过滤趋势后用MACD金叉死叉 (adx_period, min_adx, fast_period, slow_period, signal_period)"

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        adx_di(frame, period=int(params.get("adx_period", 14)))
        return macd(
            frame,
            fast_period=int(params.get("fast_period", 12)),
            slow_period=int(params.get("slow_period", 26)),
            signal_period=int(params.get("signal_period", 9)),
        )

    def signal(self, row, prev_row, params, context=None) -> int:
        context = context or {}
        if prev_row is None or self._na(
            row.get("adx"),
            row.get("plus_di"),
            row.get("minus_di"),
            row.get("dif"),
            row.get("dea"),
            prev_row.get("dif"),
            prev_row.get("dea"),
        ):
            return 0
        min_adx = float(params.get("min_adx", 22.0))
        position = int(context.get("position") or 0)
        trend_ok = row["adx"] >= min_adx and row["plus_di"] > row["minus_di"]
        trend_bad = row["adx"] < min_adx or row["plus_di"] <= row["minus_di"]
        macd_buy = prev_row["dif"] <= prev_row["dea"] and row["dif"] > row["dea"]
        macd_sell = prev_row["dif"] >= prev_row["dea"] and row["dif"] < row["dea"]
        if position > 0 and (macd_sell or trend_bad):
            return -1
        if position == 0 and trend_ok and macd_buy:
            return 1
        return 0

    @classmethod
    def required_history(cls, params: Mapping[str, Any]) -> int:
        return int(params.get("slow_period", 26)) * 3
