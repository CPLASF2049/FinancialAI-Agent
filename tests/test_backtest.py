"""回测引擎与 23 个已注册策略。"""

from __future__ import annotations

import pytest

from finagent.core import calendar as cal
from finagent.core.backtest import (
    BacktestEngine,
    describe_strategies,
    get_strategy_class,
    list_registered_strategies,
    run_backtest,
)
from finagent.core.backtest.engine import LOT_SIZE
from finagent.errors import InsufficientData, InvalidArgument, StrategyNotFound

EXPECTED_STRATEGIES = {
    "ma_cross",
    "macd",
    "rsi",
    "kdj",
    "boll_reversion",
    "boll_breakout",
    "momentum_roc",
    "donchian_breakout",
    "turtle",
    "adx_macd",
    "triple_ma",
    "ema_sma_bias",
    "cci",
    "williams_r",
    "stochastic",
    "rsi_ma200",
    "volume_breakout",
    "obv_cross",
    "vwap_deviation",
    "ma_cross_atr_stop",
    "vol_target_ma_cross",
    "kelly_ma_cross",
    "cross_section_momentum",
}


def test_all_expected_strategies_registered() -> None:
    assert set(list_registered_strategies()) == EXPECTED_STRATEGIES


def test_describe_strategies_has_descriptions() -> None:
    described = describe_strategies()
    assert len(described) == len(EXPECTED_STRATEGIES)
    assert all(entry["description"] for entry in described)


def test_unknown_strategy_raises() -> None:
    with pytest.raises(StrategyNotFound) as excinfo:
        get_strategy_class("definitely_not_a_strategy")
    assert "已注册策略" in (excinfo.value.hint or "")


@pytest.mark.parametrize("name", sorted(EXPECTED_STRATEGIES - {"cross_section_momentum"}))
def test_every_strategy_runs_and_respects_invariants(provider, name: str) -> None:
    result = run_backtest(provider, "600519.SH", name, benchmark="000001.SH")

    assert result["strategy"] == name
    assert result["initial_capital"] == pytest.approx(100_000.0)
    assert result["max_drawdown_pct"] >= 0
    assert 0 <= result["exposure_pct"] <= 100
    assert result["trades_count"] >= 0
    assert result["final_value"] > 0

    # 只做多的引擎：每笔卖出之前必有买入，且股数都是整手。
    buys = [trade for trade in result["trades"] if trade["action"] == "BUY"]
    for trade in buys:
        assert trade["shares"] % LOT_SIZE == 0
        assert trade["shares"] > 0

    if result["win_rate_pct"] is not None:
        assert 0 <= result["win_rate_pct"] <= 100

    assert result["equity_curve"], "equity curve must not be empty"
    assert len(result["equity_curve"]) <= 180


def test_multi_asset_strategy_is_rejected(provider) -> None:
    with pytest.raises(InvalidArgument) as excinfo:
        run_backtest(provider, "600519.SH", "cross_section_momentum")
    assert "多标的" in str(excinfo.value)


def test_benchmark_comparison_is_consistent(provider) -> None:
    result = run_backtest(provider, "600519.SH", "ma_cross", benchmark="000001.SH")
    benchmark = result["benchmark"]
    assert "return_pct" in benchmark
    assert benchmark["excess_return_pct"] == pytest.approx(
        result["total_return_pct"] - benchmark["return_pct"], abs=0.02
    )


def test_result_is_deterministic(provider) -> None:
    first = run_backtest(provider, "000001.SZ", "macd")
    second = run_backtest(provider, "000001.SZ", "macd")
    assert first["total_return_pct"] == second["total_return_pct"]
    assert first["trades_count"] == second["trades_count"]


def test_invalid_date_range(provider) -> None:
    with pytest.raises(InvalidArgument):
        run_backtest(provider, "600519.SH", "ma_cross", start_date="20260601", end_date="20260101")


def test_symbol_without_data_raises(provider) -> None:
    engine = BacktestEngine(provider)
    with pytest.raises(InsufficientData):
        engine.run("600519.SH", "ma_cross", start_date="19900101", end_date="19900201")


def test_params_are_honoured(provider) -> None:
    fast = run_backtest(
        provider, "600519.SH", "ma_cross", params={"short_window": 3, "long_window": 8}
    )
    slow = run_backtest(
        provider, "600519.SH", "ma_cross", params={"short_window": 20, "long_window": 120}
    )
    assert fast["params"] == {"short_window": 3, "long_window": 8}
    # 慢得多的均线组合不应产出完全相同的成交列表。
    assert (
        fast["trades_count"] != slow["trades_count"]
        or fast["total_return_pct"] != slow["total_return_pct"]
    )


def test_equity_curve_is_downsampled(provider) -> None:
    engine = BacktestEngine(provider)
    start, end = cal.default_window(730)
    result = engine.run("600519.SH", "macd", start_date=start, end_date=end, curve_points=25)
    assert len(result.equity_curve) <= 25
    assert result.equity_curve[0]["trade_date"] >= start


def test_initial_capital_must_be_positive(provider) -> None:
    with pytest.raises(InvalidArgument):
        BacktestEngine(provider, initial_capital=0)


def test_position_sizing_halves_exposure(provider) -> None:
    full = run_backtest(provider, "600519.SH", "ma_cross", params={"position_fraction": 1.0})
    half = run_backtest(provider, "600519.SH", "ma_cross", params={"position_fraction": 0.5})
    full_buys = [t["shares"] for t in full["trades"] if t["action"] == "BUY"]
    half_buys = [t["shares"] for t in half["trades"] if t["action"] == "BUY"]
    if full_buys and half_buys:
        assert half_buys[0] <= full_buys[0]
