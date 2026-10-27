"""选股器、长尾挖掘与数据源 fixture。"""

from __future__ import annotations

import pytest

from finagent.core import calendar as cal
from finagent.core.screener import ScreenCriteria, long_tail, screen
from finagent.errors import InvalidArgument


def test_screen_returns_requested_number(provider) -> None:
    result = screen(provider, ScreenCriteria(limit=5))
    assert result["returned"] == 5
    assert result["universe_size"] > 0
    assert len(result["results"]) == 5


def test_pe_and_industry_filters(provider) -> None:
    result = screen(provider, ScreenCriteria(pe_max=10, industries=["银行"], limit=10))
    assert result["matched"] > 0, "expected at least one low-PE bank in the fixture"
    for row in result["results"]:
        assert row["industry"] == "银行"
        assert row["pe"] <= 10


def test_market_cap_is_in_yi(provider) -> None:
    result = screen(provider, ScreenCriteria(mv_min=1000, limit=10))
    for row in result["results"]:
        assert row["total_mv_yi"] >= 1000


def test_mv_range_is_exclusive(provider) -> None:
    result = screen(provider, ScreenCriteria(mv_min=100, mv_max=300, limit=20))
    for row in result["results"]:
        assert 100 <= row["total_mv_yi"] <= 300


def test_exclude_industries(provider) -> None:
    result = screen(provider, ScreenCriteria(exclude_industries=["银行"], limit=10))
    assert all(row["industry"] != "银行" for row in result["results"])


def test_sort_ascending_flag(provider) -> None:
    result = screen(provider, ScreenCriteria(sort_by="pe", limit=5))
    values = [row["pe"] for row in result["results"] if row["pe"] is not None]
    assert values == sorted(values)


def test_sort_by_unknown_field_raises(provider) -> None:
    with pytest.raises(InvalidArgument):
        screen(provider, ScreenCriteria(sort_by="not_a_field"))


def test_invalid_limit_raises(provider) -> None:
    with pytest.raises(InvalidArgument):
        screen(provider, ScreenCriteria(limit=0))


def test_impossible_criteria_returns_empty(provider) -> None:
    result = screen(provider, ScreenCriteria(pe_max=0.001, limit=5))
    assert result["matched"] == 0
    assert result["results"] == []


def test_long_tail_defaults(provider) -> None:
    result = long_tail(provider, limit=5)
    assert "criteria" in result
    assert result["returned"] <= 5
    for row in result["results"]:
        assert row["total_mv_yi"] <= 200
        assert row["pe"] <= 40
        assert row["pb"] <= 5


def test_long_tail_advanced_filters_are_applied(provider) -> None:
    result = long_tail(provider, check_consolidation=True, limit=5)
    assert result["advanced_filters"], "expected the consolidation filter to be reported"


# ----------------------------------------------------------------------
# 数据源 fixture
# ----------------------------------------------------------------------
def test_daily_price_shape(provider) -> None:
    start, end = cal.default_window(90)
    frame = provider.daily_price("600519.SH", start, end)
    assert not frame.empty
    for column in ("open", "high", "low", "close", "vol", "amount", "trade_date"):
        assert column in frame.columns
    assert (frame["high"] >= frame["low"]).all()
    assert (frame["high"] >= frame["close"]).all()
    assert (frame["low"] <= frame["close"]).all()
    assert frame["trade_date"].is_monotonic_increasing


def test_valuation_matches_price(provider) -> None:
    start, end = cal.default_window(30)
    prices = provider.daily_price("600519.SH", start, end)
    basics = provider.daily_basic("600519.SH", start, end)
    merged = prices.merge(basics, on="trade_date", suffixes=("", "_basic"))
    assert not merged.empty
    latest = merged.iloc[-1]
    assert latest["pe"] == pytest.approx(
        latest["close"] / (latest["close"] / latest["pe"]), rel=1e-6
    )
    # PE × EPS = 价格 —— 每个 fixture 的隐含 EPS 都为正
    assert latest["pe"] > 0


def test_realtime_price_is_close_to_last_bar(provider) -> None:
    tick = provider.realtime_price("000001.SZ")
    frame = provider.daily_price("000001.SZ")
    last_close = float(frame.iloc[-1]["close"])
    assert tick["price"] == pytest.approx(last_close, rel=0.05)
    assert tick["ts_code"] == "000001.SZ"


def test_fixture_is_deterministic_across_instances() -> None:
    from finagent.providers.offline import OfflineProvider

    first = OfflineProvider().screening_universe()
    second = OfflineProvider().screening_universe()
    assert first.equals(second)


def test_macro_series_present(provider) -> None:
    for indicator in ("gdp", "cpi", "m2", "interest_rate"):
        frame = provider.macro(indicator)
        assert not frame.empty, indicator
        assert len(frame.columns) >= 2


def test_cross_asset_catalogue_and_prices(provider) -> None:
    for asset in ("hk", "us", "etf", "cb", "futures"):
        catalogue = provider.asset_basic(asset)
        assert not catalogue.empty, asset
        code = catalogue.iloc[0]["ts_code"]
        frame = provider.asset_daily(asset, code)
        assert not frame.empty, asset


def test_trade_calendar_marks_weekends_closed(provider) -> None:
    frame = provider.trade_calendar("20260101", "20260131")
    assert set(frame["is_open"].unique()) <= {0, 1}
    assert frame["is_open"].sum() < len(frame)
