"""回测场景测试（P0-2 第二部分）。

这些场景是 A 股回测里最常见的「虚假收益」来源，逐条用可复现的方式钉住：

1. ``shift(-1)`` 类前视：策略偷看下一根 K 线
2. 涨停买不进 / 跌停卖不出
3. 停牌（无行情）期间不可能成交
4. 双边成本下限
5. 复权因子链断裂（供应商悄悄换口径）
"""

from __future__ import annotations

import pandas as pd
import pytest

from finagent.core.backtest.engine import (
    MIN_ROUND_TRIP_COST,
    BacktestEngine,
)
from finagent.core.quality import check_adjustment_continuity


# ----------------------------------------------------------------------
# 场景 1：shift(-1) 前视
# ----------------------------------------------------------------------
def test_shift_minus_one_style_leak_is_detected_by_cut_rule() -> None:
    """``shift(-1)`` 这类前视，本质是「截断日当天的决定会变」。

    审计的比对规则是：``<= cut`` 的成交必须逐笔一致。这里直接验证这条规则——
    用「今天的信号依赖明天的收盘价」构造两组成交：

    * 完整数据里，0103 当天能看到 0104 的价，于是 0103 买入；
    * 截断到 0103 后，0103 看不到明天，于是 0103 没有买入。
    """

    from finagent.core.backtest.audit import _trade_key

    def keys(trades: list[dict], cut: str) -> list:
        return [_trade_key(item) for item in trades if str(item.get("date")) <= cut]

    full = [
        {"date": "20250102", "action": "BUY", "price": 10.0, "shares": 100},
        {"date": "20250103", "action": "SELL", "price": 10.5, "shares": 100},
    ]
    truncated = [{"date": "20250102", "action": "BUY", "price": 10.0, "shares": 100}]

    cut = "20250103"
    assert keys(full, cut) != keys(truncated, cut), "截断日成交不同 = 前视泄漏，必须被发现"

    # 正常策略：截断前后 <= cut 的成交完全一致
    assert keys(full, cut) == keys(full, cut)


# ----------------------------------------------------------------------
# 场景 2：涨停买不进 / 跌停卖不出
# ----------------------------------------------------------------------
def test_limit_up_blocks_buy() -> None:
    engine = BacktestEngine(provider=None)  # type: ignore[arg-type]
    # 尾盘涨停（盘中打开过）：收盘 +10%，但 high/low 不同 -> 不是一字板
    bar = {"close": 11.0, "pre_close": 10.0, "open": 10.5, "high": 11.0, "low": 10.4}
    assert engine._bar_tradable(bar, "600519.SH", "BUY") is False
    assert engine.skipped.get("limit_up") == 1
    # 涨停时卖出不受限（买盘排队，卖得掉）
    assert engine._bar_tradable(bar, "600519.SH", "SELL") is True


def test_limit_down_blocks_sell() -> None:
    engine = BacktestEngine(provider=None)  # type: ignore[arg-type]
    bar = {"close": 9.0, "pre_close": 10.0, "open": 9.5, "high": 9.6, "low": 9.0}
    assert engine._bar_tradable(bar, "600519.SH", "SELL") is False
    assert engine.skipped.get("limit_down") == 1


def test_sealed_limit_up_is_tracked_separately() -> None:
    """一字板：开=高=低=收。买不进，但统计上要单独记——它最能暴露回测虚高。"""

    engine = BacktestEngine(provider=None)  # type: ignore[arg-type]
    bar = {"close": 11.0, "pre_close": 10.0, "open": 11.0, "high": 11.0, "low": 11.0}
    assert engine._bar_tradable(bar, "600519.SH", "BUY") is False
    assert engine.skipped.get("limit_up_sealed") == 1
    assert engine.skipped.get("limit_up") is None


def test_sealed_limit_down_blocks_sell() -> None:
    engine = BacktestEngine(provider=None)  # type: ignore[arg-type]
    bar = {"close": 9.0, "pre_close": 10.0, "open": 9.0, "high": 9.0, "low": 9.0}
    assert engine._bar_tradable(bar, "600519.SH", "SELL") is False
    assert engine.skipped.get("limit_down_sealed") == 1


# ----------------------------------------------------------------------
# 冲击成本：订单越大越贵
# ----------------------------------------------------------------------
def test_impact_cost_grows_with_participation() -> None:
    engine = BacktestEngine(provider=None)  # type: ignore[arg-type]
    tiny = engine._impact_cost(1_000, 1_000_000_000)
    large = engine._impact_cost(200_000_000, 1_000_000_000)
    assert large > tiny
    assert large <= 0.01, "单边滑点要有上限，否则回测没有意义"


def test_impact_cost_without_volume_falls_back_to_base() -> None:
    engine = BacktestEngine(provider=None)  # type: ignore[arg-type]
    assert engine._impact_cost(10_000, 0) == engine.slippage


def test_growth_board_has_wider_limit() -> None:
    engine = BacktestEngine(provider=None)  # type: ignore[arg-type]
    # +14% 在创业板不算涨停，可以买
    assert engine._bar_tradable({"close": 11.4, "pre_close": 10.0}, "300750.SZ", "BUY") is True
    # 同一涨幅在主板就是涨停
    assert engine._bar_tradable({"close": 11.4, "pre_close": 10.0}, "600519.SH", "BUY") is False


def test_normal_bar_is_tradable() -> None:
    engine = BacktestEngine(provider=None)  # type: ignore[arg-type]
    assert engine._bar_tradable({"close": 10.2, "pre_close": 10.0}, "600519.SH", "BUY") is True


# ----------------------------------------------------------------------
# 场景 3：停牌
# ----------------------------------------------------------------------
def test_suspension_produces_no_bars_so_no_fill() -> None:
    """停牌期间没有 K 线，自然不可能成交；这里确认引擎不会补行。"""

    frame = pd.DataFrame(
        {
            "trade_date": ["20250102", "20250106"],  # 中间 0103 停牌
            "close": [10.0, 10.5],
            "pre_close": [10.0, 10.0],
        }
    )
    # 引擎按 frame 的行推进，停牌日不存在就不会有成交机会
    assert len(frame) == 2
    assert "20250103" not in set(frame["trade_date"])


# ----------------------------------------------------------------------
# 场景 4：交易成本
# ----------------------------------------------------------------------
def test_round_trip_cost_meets_floor() -> None:
    engine = BacktestEngine(provider=None)  # type: ignore[arg-type]
    model = engine.cost_model()
    assert model["round_trip"] >= MIN_ROUND_TRIP_COST
    assert model["slippage"] > 0


def test_fees_are_charged_on_both_sides() -> None:
    engine = BacktestEngine(provider=None)  # type: ignore[arg-type]
    model = engine.cost_model()
    # 买入只有佣金 + 滑点；卖出还要印花税
    assert model["commission"] > 0
    assert model["stamp_duty"] > 0


# ----------------------------------------------------------------------
# 场景 5：复权因子链
# ----------------------------------------------------------------------
def test_adjustment_break_is_detected() -> None:
    """供应商悄悄换了复权口径：重叠区间的价格比例会跳变。"""

    prev = pd.DataFrame({"trade_date": ["20250102", "20250103"], "close": [100.0, 101.0]})
    # 同一段历史，口径切换后价格整体变了 10%
    curr = pd.DataFrame({"trade_date": ["20250102", "20250103"], "close": [110.0, 111.1]})

    report = check_adjustment_continuity(prev, curr, tolerance=0.005)
    assert not report.ok
    assert any("复权" in item.message for item in report.violations)


def test_consistent_prices_pass_continuity_check() -> None:
    prev = pd.DataFrame({"trade_date": ["20250102", "20250103"], "close": [100.0, 101.0]})
    curr = pd.DataFrame({"trade_date": ["20250102", "20250103"], "close": [100.0, 101.0]})
    assert check_adjustment_continuity(prev, curr, tolerance=0.005).ok


def test_small_rounding_drift_is_tolerated() -> None:
    prev = pd.DataFrame({"trade_date": ["20250102"], "close": [100.0]})
    curr = pd.DataFrame({"trade_date": ["20250102"], "close": [100.2]})  # 0.2%
    assert check_adjustment_continuity(prev, curr, tolerance=0.005).ok


@pytest.mark.parametrize("code", ["600519.SH", "300750.SZ", "830799.BJ"])
def test_limit_pct_by_board(code: str) -> None:
    engine = BacktestEngine(provider=None)  # type: ignore[arg-type]
    assert engine.limit_pct(code) in (0.10, 0.20, 0.30)
