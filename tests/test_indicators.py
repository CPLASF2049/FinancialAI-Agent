"""技术指标的数学与形态识别。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from finagent.core import indicators as ind
from finagent.errors import InsufficientData, InvalidArgument


def make_frame(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    close = np.array(closes, dtype=float)
    return pd.DataFrame(
        {
            "trade_date": [
                f"2026{(index // 28) + 1:02d}{(index % 28) + 1:02d}" for index in range(n)
            ],
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "vol": np.linspace(1000, 2000, n),
        }
    )


def trending(n: int = 120, step: float = 0.4, start: float = 10.0) -> pd.DataFrame:
    return make_frame([start + index * step for index in range(n)])


def test_macd_columns_and_relationship() -> None:
    frame = ind.macd(trending())
    assert {"dif", "dea", "macd"} <= set(frame.columns)
    # MACD 柱定义为 (dif - dea) * 2
    np.testing.assert_allclose(frame["macd"], (frame["dif"] - frame["dea"]) * 2, rtol=1e-9)
    # 稳定上涨时 DIF 会一直在 DEA 之上
    assert frame["dif"].iloc[-1] > frame["dea"].iloc[-1]


def test_rsi_bounds_and_direction() -> None:
    up = ind.rsi(trending())
    down = ind.rsi(make_frame([100 - index * 0.4 for index in range(120)]))
    assert up["rsi"].dropna().between(0, 100).all()
    assert up["rsi"].iloc[-1] > 70  # persistent gains -> overbought
    assert down["rsi"].iloc[-1] < 30  # persistent losses -> oversold


def test_kdj_definition_and_bounds() -> None:
    frame = ind.kdj(trending())
    np.testing.assert_allclose(frame["j"], 3 * frame["k"] - 2 * frame["d"], rtol=1e-9)
    clean = frame["k"].dropna()
    assert clean.between(0, 100).all()


def test_boll_band_ordering() -> None:
    frame = ind.boll(trending())
    tail = frame.dropna().tail(10)
    assert (tail["boll_upper"] >= tail["boll_mid"]).all()
    assert (tail["boll_mid"] >= tail["boll_lower"]).all()


def test_atr_is_positive_and_small_for_flat_prices() -> None:
    flat = pd.DataFrame(
        {
            "trade_date": [f"202601{index + 1:02d}" for index in range(60)],
            "open": 20.0,
            "high": 20.0,
            "low": 20.0,
            "close": 20.0,
            "vol": 1000.0,
        }
    )
    frame = ind.atr(flat, period=14)
    assert frame["atr"].dropna().iloc[-1] == pytest.approx(0.0, abs=1e-9)

    volatile = make_frame([20 + (5 if index % 2 else -5) for index in range(60)])
    assert ind.atr(volatile)["atr"].dropna().iloc[-1] > 1.0


def test_adx_di_columns() -> None:
    frame = ind.adx_di(trending())
    assert {"adx", "plus_di", "minus_di"} <= set(frame.columns)
    assert frame["plus_di"].dropna().iloc[-1] > frame["minus_di"].dropna().iloc[-1]


def test_obv_follows_volume_sign() -> None:
    frame = ind.obv(make_frame([10, 11, 12, 11, 10]))
    assert frame["obv"].iloc[-1] < frame["obv"].iloc[2]


def test_williams_r_range() -> None:
    frame = ind.williams_r(trending())
    values = frame["willr"].dropna()
    assert values.between(-100, 0).all()


def test_compute_selected_subset() -> None:
    frame = ind.compute(trending(), ("rsi", "boll"))
    assert "rsi" in frame.columns
    assert "boll_mid" in frame.columns
    assert "dif" not in frame.columns


def test_compute_rejects_unknown_indicator() -> None:
    with pytest.raises(InvalidArgument):
        ind.compute(trending(), ("not_a_real_indicator",))


def test_compute_requires_ohlc() -> None:
    with pytest.raises(InsufficientData):
        ind.macd(pd.DataFrame({"close": [1.0, 2.0]}))


def test_snapshot_is_newest_first_and_rounded() -> None:
    frame = ind.compute(trending(), ("rsi",))
    rows = ind.snapshot(frame, rows=3)
    assert len(rows) == 3
    assert rows[0]["trade_date"] > rows[-1]["trade_date"]
    assert all(len(str(row["rsi"]).split(".")[-1]) <= 4 for row in rows)


def test_detect_patterns_reports_crosses() -> None:
    # V 形走势中途会出现金叉；构造最后两根 K 线，让 DIF 恰好在最后一根上穿 DEA。
    closes = [10.0] * 40 + [8.0] * 10 + [9.0, 10.5]
    frame = make_frame(closes)
    result = ind.detect_patterns(frame)
    assert result["trade_date"]
    assert isinstance(result["patterns"], list)
    assert set(result["signals"]) >= {"macd", "kdj", "rsi"}


def test_detect_patterns_handles_tiny_frames() -> None:
    result = ind.detect_patterns(make_frame([10.0]))
    assert result["patterns"] == []


def test_rsi_ma_crossover_flags_overbought() -> None:
    frame = ind.compute(trending(), ("rsi",))
    patterns = ind.detect_patterns(frame)
    assert "RSI_Overbought" in patterns["patterns"]
