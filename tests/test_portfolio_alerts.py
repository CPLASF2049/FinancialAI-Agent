"""持仓台账、价格预警与用户画像存储。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finagent.core.alerts import AlertStore, CompositeNotifier, JsonlNotifier
from finagent.core.portfolio import PortfolioStore
from finagent.core.profile import ProfileStore
from finagent.errors import InvalidArgument, NotFound


# ----------------------------------------------------------------------
# 持仓
# ----------------------------------------------------------------------
@pytest.fixture
def book(data_dir: Path) -> PortfolioStore:
    return PortfolioStore(data_dir / "portfolios.json")


def test_default_portfolio_exists(book: PortfolioStore) -> None:
    portfolios = book.list_portfolios()
    assert len(portfolios) == 1
    assert portfolios[0]["active"] is True


def test_add_position_creates_row(book: PortfolioStore) -> None:
    created = book.add_position("平安银行", 1000, 10.5)
    assert created["ts_code"] == "000001.SZ"
    assert created["action"] == "created"
    assert book.held_codes() == ["000001.SZ"]


def test_add_position_averages_cost(book: PortfolioStore) -> None:
    book.add_position("000001.SZ", 1000, 10.0)
    book.add_position("000001.SZ", 1000, 12.0)
    status = book.status(_FakeProvider())
    position = status["positions"][0]
    assert position["amount"] == pytest.approx(2000)
    assert position["cost"] == pytest.approx(11.0)


def test_invalid_amount_and_price_rejected(book: PortfolioStore) -> None:
    with pytest.raises(InvalidArgument):
        book.add_position("000001.SZ", 0, 10.0)
    with pytest.raises(InvalidArgument):
        book.add_position("000001.SZ", 100, 0)


def test_partial_sell_keeps_position(book: PortfolioStore) -> None:
    book.add_position("000001.SZ", 1000, 10.0)
    result = book.remove_position("000001.SZ", 400, price=12.0)
    assert result["action"] == "reduced"
    assert result["remaining"] == pytest.approx(600)
    assert result["realised_pnl"] == pytest.approx(800.0)
    assert book.held_codes() == ["000001.SZ"]


def test_full_sell_closes_position(book: PortfolioStore) -> None:
    book.add_position("000001.SZ", 1000, 10.0)
    result = book.remove_position("000001.SZ")
    assert result["action"] == "closed"
    assert book.held_codes() == []


def test_selling_more_than_held_is_rejected(book: PortfolioStore) -> None:
    book.add_position("000001.SZ", 100, 10.0)
    with pytest.raises(InvalidArgument):
        book.remove_position("000001.SZ", 200)


def test_selling_absent_symbol_raises(book: PortfolioStore) -> None:
    with pytest.raises(NotFound):
        book.remove_position("600519.SH", 100)


def test_status_totals_are_consistent(book: PortfolioStore) -> None:
    book.add_position("000001.SZ", 1000, 10.0)
    book.add_position("600519.SH", 100, 1000.0)
    status = book.status(_FakeProvider())
    totals = status["totals"]
    assert totals["count"] == 2
    assert totals["cost"] == pytest.approx(1000 * 10 + 100 * 1000)
    assert totals["pnl"] == pytest.approx(totals["market_value"] - totals["cost"])
    for position in status["positions"]:
        assert position["market_value"] == pytest.approx(position["amount"] * position["price"])


def test_multiple_portfolios_are_isolated(book: PortfolioStore) -> None:
    book.create_portfolio("成长组合")
    book.set_active("成长组合")
    book.add_position("600519.SH", 10, 1000.0)
    assert book.held_codes("成长组合") == ["600519.SH"]
    assert book.held_codes("default") == []


def test_cannot_delete_last_portfolio(book: PortfolioStore) -> None:
    with pytest.raises(InvalidArgument):
        book.delete_portfolio("default")


def test_clear_empties_portfolio(book: PortfolioStore) -> None:
    book.add_position("000001.SZ", 100, 10.0)
    book.clear()
    assert book.held_codes() == []


# ----------------------------------------------------------------------
# 预警
# ----------------------------------------------------------------------
@pytest.fixture
def alerts(data_dir: Path) -> AlertStore:
    return AlertStore(data_dir / "alerts.json", notifier=CompositeNotifier([]))


def test_absolute_alert_is_stored(alerts: AlertStore) -> None:
    alert = alerts.add("茅台", ">", threshold=2000.0)
    assert alert.ts_code == "600519.SH"
    assert alert.threshold == 2000.0
    assert alert.operator == ">"
    assert len(alerts.list()) == 1


def test_percentage_alert_computes_threshold(alerts: AlertStore, provider) -> None:
    alert = alerts.add("平安银行", pct_change=5, provider=provider)
    base = provider.realtime_price("000001.SZ")["price"]
    assert alert.base_price == pytest.approx(base, rel=1e-3)
    assert alert.threshold == pytest.approx(base * 1.05, rel=1e-3)
    assert alert.operator == ">"


def test_negative_percentage_alert_uses_lower_bound(alerts: AlertStore, provider) -> None:
    alert = alerts.add("平安银行", pct_change=-3, provider=provider)
    assert alert.operator == "<"
    assert alert.threshold < alert.base_price


def test_alert_requires_exactly_one_trigger(alerts: AlertStore) -> None:
    with pytest.raises(InvalidArgument):
        alerts.add("600519.SH", ">")
    with pytest.raises(InvalidArgument):
        alerts.add("600519.SH", ">", threshold=1.0, pct_change=5.0)


def test_alert_rejects_bad_operator(alerts: AlertStore) -> None:
    with pytest.raises(InvalidArgument):
        alerts.add("600519.SH", "≈", threshold=1.0)


def test_trigger_fires_and_disables(alerts: AlertStore, provider) -> None:
    price = provider.realtime_price("600519.SH")["price"]
    alerts.add("茅台", ">", threshold=price * 0.5)
    result = alerts.check(provider, trading_hours_only=False)
    assert result["evaluated"] == 1
    assert len(result["triggered"]) == 1
    stored = alerts.list()[0]
    assert stored["enabled"] is False
    assert stored["triggered_at"]


def test_non_triggering_alert_stays_enabled(alerts: AlertStore, provider) -> None:
    price = provider.realtime_price("600519.SH")["price"]
    alerts.add("茅台", ">", threshold=price * 10)
    result = alerts.check(provider, trading_hours_only=False)
    assert result["triggered"] == []
    assert alerts.list()[0]["enabled"] is True


def test_history_records_triggers(alerts: AlertStore, provider) -> None:
    price = provider.realtime_price("000001.SZ")["price"]
    alerts.add("平安银行", "<", threshold=price * 2)
    alerts.check(provider, trading_hours_only=False)
    assert len(alerts.history()) == 1


def test_update_rearms_triggered_alert(alerts: AlertStore, provider) -> None:
    price = provider.realtime_price("600519.SH")["price"]
    alert = alerts.add("茅台", ">", threshold=price * 0.5)
    alerts.check(provider, trading_hours_only=False)
    updated = alerts.update(alert.id, threshold=price * 10)
    assert updated.enabled is True
    assert updated.triggered_at is None


def test_remove_alert(alerts: AlertStore) -> None:
    alert = alerts.add("600519.SH", ">", threshold=1.0)
    alerts.remove(alert.id)
    assert alerts.list() == []
    with pytest.raises(NotFound):
        alerts.remove(alert.id)


def test_jsonl_notifier_writes_audit_line(data_dir: Path, provider) -> None:
    log_path = data_dir / "alerts_log.jsonl"
    store = AlertStore(data_dir / "a.json", notifier=JsonlNotifier(log_path))
    store.add("600519.SH", ">", threshold=1.0)
    store.check(provider, trading_hours_only=False)
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["alert"]["ts_code"] == "600519.SH"


def test_trading_hours_gate_skips(provider, data_dir: Path) -> None:
    store = AlertStore(data_dir / "b.json")
    store.add("600519.SH", ">", threshold=1.0)
    result = store.check(provider, trading_hours_only=True)
    if not __import__("finagent.core.calendar", fromlist=["x"]).is_trading_now():
        assert result["evaluated"] == 0
        assert result["skipped"]


# ----------------------------------------------------------------------
# 用户画像
# ----------------------------------------------------------------------
@pytest.fixture
def profile(data_dir: Path) -> ProfileStore:
    return ProfileStore(data_dir / "profile.json")


def test_profile_defaults(profile: ProfileStore) -> None:
    assert profile.is_empty()
    assert profile.get()["risk_tolerance"] == "Unknown"


def test_profile_canonicalises_chinese(profile: ProfileStore) -> None:
    profile.update(risk_tolerance="保守型", investment_horizon="长线")
    data = profile.get()
    assert data["risk_tolerance"] == "Conservative"
    assert data["investment_horizon"] == "Long-term"


def test_sector_lists_merge_and_deduplicate(profile: ProfileStore) -> None:
    profile.update(favorite_sectors=["银行", "白酒"])
    profile.update(favorite_sectors=["白酒", "半导体"])
    assert profile.get()["favorite_sectors"] == ["银行", "白酒", "半导体"]


def test_sectors_can_be_replaced(profile: ProfileStore) -> None:
    profile.update(favorite_sectors=["银行"])
    profile.update(favorite_sectors=["白酒"], merge_sectors=False)
    assert profile.get()["favorite_sectors"] == ["白酒"]


def test_custom_preferences_merge(profile: ProfileStore) -> None:
    profile.update(custom_preferences={"dividend": "high"})
    profile.update(custom_preferences={"max_pe": 20})
    prefs = profile.get()["custom_preferences"]
    assert prefs == {"dividend": "high", "max_pe": 20}


def test_summary_contains_values(profile: ProfileStore) -> None:
    profile.update(risk_tolerance="Aggressive", favorite_sectors=["半导体"])
    summary = profile.summary()
    assert "Aggressive" in summary
    assert "半导体" in summary


def test_profile_is_shared_through_the_file(profile: ProfileStore, data_dir: Path) -> None:
    profile.update(risk_tolerance="Balanced")
    reopened = ProfileStore(data_dir / "profile.json")
    assert reopened.get()["risk_tolerance"] == "Balanced"


class _FakeProvider:
    """Minimal provider stub with fixed prices, so valuation maths is exact."""

    name = "fake"
    CAPABILITIES = frozenset({"realtime_price", "daily_price"})

    def realtime_price(self, ts_code: str) -> dict[str, object]:
        prices = {"000001.SZ": 11.0, "600519.SH": 1100.0}
        return {"ts_code": ts_code, "price": prices.get(ts_code, 10.0), "trade_date": "20260101"}

    def daily_price(self, ts_code: str, *_args, **_kwargs):  # noqa: ANN001
        import pandas as pd

        return pd.DataFrame({"close": [10.0]})
