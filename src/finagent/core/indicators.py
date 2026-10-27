"""技术指标与形态识别。

所有函数都是纯粹的 ``DataFrame -> DataFrame`` 变换：不持有数据源、不读配置、不写文件。
因此回测策略可以直接复用，也能脱离网络做单元测试。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import numpy as np
import pandas as pd

from finagent.errors import InsufficientData

OHLC_REQUIRED = ("open", "high", "low", "close")


def _ensure_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    missing = [column for column in ("high", "low", "close") if column not in frame.columns]
    if missing:
        raise InsufficientData(f"行情数据缺少字段：{', '.join(missing)}")
    return frame


def macd(
    frame: pd.DataFrame,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> pd.DataFrame:
    """DIF / DEA / MACD 柱（柱值 =（DIF - DEA）× 2，A 股习惯）。"""

    frame = _ensure_ohlcv(frame)
    ema_fast = frame["close"].ewm(span=fast_period, adjust=False).mean()
    ema_slow = frame["close"].ewm(span=slow_period, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal_period, adjust=False).mean()
    frame["dif"] = dif
    frame["dea"] = dea
    frame["macd"] = (dif - dea) * 2
    return frame


def rsi(frame: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    frame = _ensure_ohlcv(frame)
    delta = frame["close"].diff()
    gain = delta.where(delta > 0, 0.0).ewm(alpha=1.0 / period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1.0 / period, adjust=False).mean()
    rs = gain / loss.replace(0.0, np.nan)
    frame["rsi"] = (100.0 - (100.0 / (1.0 + rs))).fillna(100.0)
    return frame


def kdj(
    frame: pd.DataFrame,
    k_period: int = 9,
    d_period: int = 3,
    j_period: int = 3,
) -> pd.DataFrame:
    """KDJ，采用 SMA(2/3, 1/3) 递推，K/D 以 50 为初始值。"""

    frame = _ensure_ohlcv(frame)
    low_min = frame["low"].rolling(window=k_period).min()
    high_max = frame["high"].rolling(window=k_period).max()
    span = (high_max - low_min).replace(0.0, np.nan)
    rsv = (frame["close"] - low_min) / span * 100.0

    k_values: list[float] = []
    d_values: list[float] = []
    k = 50.0
    d = 50.0
    for value in rsv:
        if pd.isna(value):
            k_values.append(np.nan)
            d_values.append(np.nan)
            continue
        k = (2.0 / 3.0) * k + (1.0 / 3.0) * float(value)
        d = (2.0 / 3.0) * d + (1.0 / 3.0) * k
        k_values.append(k)
        d_values.append(d)

    frame["k"] = k_values
    frame["d"] = d_values
    frame["j"] = 3.0 * frame["k"] - 2.0 * frame["d"]
    return frame


def boll(frame: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    frame = _ensure_ohlcv(frame)
    mid = frame["close"].rolling(window=period).mean()
    std = frame["close"].rolling(window=period).std()
    frame["boll_mid"] = mid
    frame["boll_upper"] = mid + std * std_dev
    frame["boll_lower"] = mid - std * std_dev
    return frame


def atr(frame: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """平均真实波幅（Wilder 平滑）。"""

    frame = _ensure_ohlcv(frame)
    prev_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev_close).abs(),
            (frame["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame["atr"] = true_range.ewm(alpha=1.0 / period, adjust=False).mean()
    return frame


def adx_di(frame: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """ADX 与 +DI / -DI（Wilder 方向运动）。"""

    frame = _ensure_ohlcv(frame)
    high, low = frame["high"], frame["low"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move

    prev_close = frame["close"].shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    smoothed_tr = true_range.ewm(alpha=1.0 / period, adjust=False).mean()

    plus_di = 100.0 * (plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / (smoothed_tr + 1e-12))
    minus_di = 100.0 * (
        minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / (smoothed_tr + 1e-12)
    )
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-12)

    frame["adx"] = dx.ewm(alpha=1.0 / period, adjust=False).mean()
    frame["plus_di"] = plus_di
    frame["minus_di"] = minus_di
    return frame


def cci(frame: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    frame = _ensure_ohlcv(frame)
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    sma = typical.rolling(period).mean()
    mean_dev = typical.rolling(period).apply(
        lambda window: np.mean(np.abs(window - np.mean(window))), raw=True
    )
    frame["cci"] = (typical - sma) / (0.015 * mean_dev + 1e-12)
    return frame


def williams_r(frame: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    frame = _ensure_ohlcv(frame)
    highest = frame["high"].rolling(period).max()
    lowest = frame["low"].rolling(period).min()
    frame["willr"] = -100.0 * (highest - frame["close"]) / (highest - lowest + 1e-12)
    return frame


def stochastic(frame: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> pd.DataFrame:
    frame = _ensure_ohlcv(frame)
    lowest = frame["low"].rolling(k_period).min()
    highest = frame["high"].rolling(k_period).max()
    frame["st_k"] = 100.0 * (frame["close"] - lowest) / (highest - lowest + 1e-12)
    frame["st_d"] = frame["st_k"].rolling(d_period).mean()
    return frame


def obv(frame: pd.DataFrame) -> pd.DataFrame:
    frame = _ensure_ohlcv(frame)
    direction = np.sign(frame["close"].diff().fillna(0.0))
    volume = frame["vol"].fillna(0.0) if "vol" in frame.columns else 0.0
    frame["obv"] = (direction * volume).cumsum()
    return frame


def vwap_ma(frame: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """日线上的滚动 VWAP 近似。"""

    frame = _ensure_ohlcv(frame)
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    volume = frame["vol"].fillna(0.0) if "vol" in frame.columns else 1.0
    numerator = (typical * volume).rolling(period).sum()
    denominator = volume.rolling(period).sum()
    frame["vwap_ma"] = numerator / (denominator + 1e-12)
    return frame


def moving_averages(frame: pd.DataFrame, windows: Iterable[int] = (5, 10, 20, 60)) -> pd.DataFrame:
    for window in windows:
        frame[f"ma{window}"] = frame["close"].rolling(int(window)).mean()
    return frame


#: 注册表，调用方可以按名请求其中一部分。
INDICATORS: dict[str, Any] = {
    "macd": macd,
    "rsi": rsi,
    "kdj": kdj,
    "boll": boll,
    "atr": atr,
    "adx": adx_di,
    "cci": cci,
    "williams_r": williams_r,
    "stochastic": stochastic,
    "obv": obv,
    "vwap": vwap_ma,
}

DEFAULT_INDICATORS: tuple[str, ...] = ("macd", "rsi", "kdj", "boll")

_DISPLAY_COLUMNS = (
    "trade_date",
    "close",
    "dif",
    "dea",
    "macd",
    "rsi",
    "k",
    "d",
    "j",
    "boll_upper",
    "boll_mid",
    "boll_lower",
    "atr",
    "adx",
    "plus_di",
    "minus_di",
    "cci",
    "willr",
    "st_k",
    "st_d",
    "obv",
    "vwap_ma",
)


def compute(
    frame: pd.DataFrame,
    names: Iterable[str] = DEFAULT_INDICATORS,
    *,
    params: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """按顺序应用请求的指标族。"""

    working = frame.copy()
    overrides = params or {}
    for name in names:
        key = str(name).lower()
        function = INDICATORS.get(key)
        if function is None:
            from finagent.errors import InvalidArgument

            raise InvalidArgument(
                f"未知指标 '{name}'。",
                hint=f"可用指标：{', '.join(sorted(INDICATORS))}",
            )
        function(working, **overrides.get(key, {}))
    return working


def snapshot(frame: pd.DataFrame, rows: int = 1) -> list[dict[str, Any]]:
    """取最近若干根 K 线（从新到旧），只保留展示用列并做四舍五入。"""

    if frame.empty:
        return []
    columns = [column for column in _DISPLAY_COLUMNS if column in frame.columns]
    tail = frame.tail(max(rows, 1))[columns].iloc[::-1]
    records = tail.to_dict("records")
    cleaned: list[dict[str, Any]] = []
    for record in records:
        item: dict[str, Any] = {}
        for key, value in record.items():
            if key == "trade_date":
                item[key] = str(value)
            elif value is None or (isinstance(value, float) and pd.isna(value)):
                item[key] = None
            elif isinstance(value, (np.floating, float)):
                item[key] = round(float(value), 4)
            elif isinstance(value, (np.integer, int)):
                item[key] = int(value)
            else:
                item[key] = value
        cleaned.append(item)
    return cleaned


def detect_patterns(frame: pd.DataFrame) -> dict[str, Any]:
    """针对最新一根 K 线的经典交叉与超买超卖形态。"""

    if frame.empty or len(frame) < 2:
        return {"trade_date": None, "patterns": [], "signals": {}, "note": "insufficient data"}

    enriched = compute(frame, ("macd", "rsi", "kdj", "boll"))
    current = enriched.iloc[-1]
    previous = enriched.iloc[-2]

    def value(row: pd.Series, key: str) -> float | None:
        if key not in row.index:
            return None
        raw = row[key]
        if raw is None or pd.isna(raw):
            return None
        return float(raw)

    patterns: list[str] = []

    def crossed(key_a: str, key_b: str, *, up: bool) -> bool:
        a_prev, b_prev = value(previous, key_a), value(previous, key_b)
        a_now, b_now = value(current, key_a), value(current, key_b)
        if None in (a_prev, b_prev, a_now, b_now):
            return False
        return (a_prev <= b_prev and a_now > b_now) if up else (a_prev >= b_prev and a_now < b_now)

    if crossed("dif", "dea", up=True):
        patterns.append("MACD_Golden_Cross")
    if crossed("dif", "dea", up=False):
        patterns.append("MACD_Dead_Cross")
    if crossed("k", "d", up=True):
        patterns.append("KDJ_Golden_Cross")
    if crossed("k", "d", up=False):
        patterns.append("KDJ_Dead_Cross")

    k_now, d_now = value(current, "k"), value(current, "d")
    if (k_now is not None and k_now > 80) or (d_now is not None and d_now > 80):
        patterns.append("KDJ_Overbought")
    if (k_now is not None and k_now < 20) or (d_now is not None and d_now < 20):
        patterns.append("KDJ_Oversold")

    rsi_now = value(current, "rsi")
    if rsi_now is not None:
        if rsi_now > 70:
            patterns.append("RSI_Overbought")
        elif rsi_now < 30:
            patterns.append("RSI_Oversold")

    close_now = value(current, "close")
    upper, lower = value(current, "boll_upper"), value(current, "boll_lower")
    if close_now is not None and upper is not None and close_now > upper:
        patterns.append("BOLL_Upper_Break")
    if close_now is not None and lower is not None and close_now < lower:
        patterns.append("BOLL_Lower_Break")

    dif_now, dea_now = value(current, "dif"), value(current, "dea")
    signals = {
        "macd": None
        if dif_now is None or dea_now is None
        else ("bullish" if dif_now > dea_now else "bearish"),
        "kdj": None
        if k_now is None or d_now is None
        else ("bullish" if k_now > d_now else "bearish"),
        "rsi": None if rsi_now is None else round(rsi_now, 2),
        "close": close_now,
    }
    return {
        "trade_date": str(current.get("trade_date", "")),
        "patterns": patterns,
        "signals": signals,
    }


PATTERN_LABELS_ZH = {
    "MACD_Golden_Cross": "MACD金叉",
    "MACD_Dead_Cross": "MACD死叉",
    "KDJ_Golden_Cross": "KDJ金叉",
    "KDJ_Dead_Cross": "KDJ死叉",
    "KDJ_Overbought": "KDJ超买",
    "KDJ_Oversold": "KDJ超卖",
    "RSI_Overbought": "RSI超买",
    "RSI_Oversold": "RSI超卖",
    "BOLL_Upper_Break": "突破布林上轨",
    "BOLL_Lower_Break": "跌破布林下轨",
}
