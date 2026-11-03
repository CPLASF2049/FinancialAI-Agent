"""P0-1 Provider 网关验收测试。

命题：数据源不稳定时，系统给出**明确状态**而不是抛一段异常文本；
接口失败会自动有限重试并按能力降级；持续失败进入熔断，不再反复打。
"""

from __future__ import annotations

import pytest

from finagent.errors import ProviderUnavailable
from finagent.providers.base import MarketDataProvider
from finagent.providers.gateway import (
    PROVIDER_CAPABILITIES,
    CircuitBreaker,
    Policy,
    ProviderGateway,
    Status,
    capability_of,
)


class _Flaky(MarketDataProvider):
    """前 N 次失败，之后成功。"""

    name = "flaky"
    CAPABILITIES = frozenset({"realtime_price", "daily_price"})

    def __init__(self, failures: int = 2) -> None:
        self.failures = failures
        self.calls = 0

    def _maybe_fail(self) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise OSError("Remote end closed connection without response")

    def realtime_price(self, ts_code: str):
        self._maybe_fail()
        return {"ts_code": ts_code, "price": 100.0}

    def daily_price(self, ts_code, start_date=None, end_date=None, adj=None):
        self._maybe_fail()
        return []

    # 其余抽象方法给最小实现
    def stock_basic(self, ts_code=None, name=None):
        return None

    def trade_calendar(self, start_date, end_date):
        return None

    def daily_basic(self, ts_code, start_date=None, end_date=None):
        return None

    def income_statement(self, ts_code, start_date=None, end_date=None):
        return None

    def index_daily(self, ts_code, start_date=None, end_date=None):
        return None


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """退避等待改成 0，测试才跑得快；真实逻辑不变。"""

    import finagent.providers.gateway as gw

    monkeypatch.setattr(gw.time, "sleep", lambda *_: None)


# ----------------------------------------------------------------------
# 三态返回
# ----------------------------------------------------------------------
def test_success_returns_data() -> None:
    gateway = ProviderGateway(_Flaky(failures=0))
    result = gateway.call("realtime_price", "600519.SH")
    assert result.status is Status.SUCCESS
    assert result.data["price"] == 100.0
    assert result.provider == "flaky"


def test_retry_eventually_succeeds() -> None:
    inner = _Flaky(failures=2)
    gateway = ProviderGateway(inner)
    result = gateway.call("realtime_price", "600519.SH")

    assert result.status is Status.SUCCESS
    assert result.attempts == 3
    assert inner.calls == 3


def test_all_attempts_fail_reports_unavailable() -> None:
    gateway = ProviderGateway(_Flaky(failures=99))
    result = gateway.call("realtime_price", "600519.SH")

    assert result.status is Status.PROVIDER_UNAVAILABLE
    assert result.error_code == "REMOTE_DISCONNECTED"
    assert result.retryable is True


def test_unsupported_capability_is_not_retried() -> None:
    """能力边界不该重试——重试只会浪费时间。"""

    inner = _Flaky(failures=0)
    gateway = ProviderGateway(inner, name="eastmoney")
    result = gateway.call("moneyflow", "600519.SH", "20250101", "20250201")

    assert result.status is Status.PROVIDER_UNAVAILABLE
    assert result.error_code == "CAPABILITY_NOT_SUPPORTED"
    assert result.retryable is False
    assert inner.calls == 0


def test_unsupported_is_visible_before_calling() -> None:
    gateway = ProviderGateway(_Flaky(), name="eastmoney")
    assert gateway.supports("daily_price") is True
    assert gateway.supports("moneyflow") is False


def test_capability_mapping_for_screener() -> None:
    assert capability_of("screening_universe") == "screener"
    assert capability_of("daily_price") == "daily_price"


# ----------------------------------------------------------------------
# 熔断
# ----------------------------------------------------------------------
def test_circuit_breaker_opens_after_threshold() -> None:
    breaker = CircuitBreaker(threshold=3, cooldown=60)
    key = "x"
    for _ in range(3):
        breaker.record(key, False)
    assert breaker.allow(key) is False


def test_circuit_breaker_recovers_after_cooldown() -> None:
    # cooldown=0 等价于「冷却已过」，不依赖 sleep（本文件把 sleep 打了桩）
    breaker = CircuitBreaker(threshold=2, cooldown=0.0)
    breaker.record("x", False)
    breaker.record("x", False)
    assert breaker.allow("x") is True


def test_circuit_breaker_stays_open_during_cooldown() -> None:
    breaker = CircuitBreaker(threshold=2, cooldown=3600.0)
    breaker.record("x", False)
    breaker.record("x", False)
    assert breaker.allow("x") is False


def test_success_resets_breaker() -> None:
    breaker = CircuitBreaker(threshold=3, cooldown=60)
    breaker.record("x", False)
    breaker.record("x", True)
    assert breaker.allow("x") is True


def test_gateway_reports_circuit_open() -> None:
    gateway = ProviderGateway(_Flaky(failures=99))
    gateway._breakers["flaky:realtime_price"] = CircuitBreaker(threshold=1, cooldown=60)
    gateway._breakers["flaky:realtime_price"].record("realtime_price", False)

    result = gateway.call("realtime_price", "600519.SH")
    assert result.error_code == "CIRCUIT_OPEN"
    assert result.retryable is True


# ----------------------------------------------------------------------
# 缓存与 singleflight
# ----------------------------------------------------------------------
def test_cache_prevents_repeat_calls() -> None:
    class _Cached(_Flaky):
        name = "cached"

    inner = _Cached(failures=0)
    gateway = ProviderGateway(inner)

    import finagent.providers.gateway as gw

    gw.METHOD_POLICY["realtime_price"] = Policy(attempts=1, cache_ttl=60.0)
    try:
        gateway.call("realtime_price", "600519.SH")
        gateway.call("realtime_price", "600519.SH")
        assert inner.calls == 1
        second = gateway.call("realtime_price", "600519.SH")
        assert second.from_cache is True
    finally:
        gw.METHOD_POLICY.pop("realtime_price", None)


# ----------------------------------------------------------------------
# 接口层：失败要抛领域异常，带 code
# ----------------------------------------------------------------------
def test_interface_raises_with_code() -> None:
    gateway = ProviderGateway(_Flaky(failures=99))
    with pytest.raises(ProviderUnavailable) as excinfo:
        gateway.realtime_price("600519.SH")

    payload = excinfo.value.to_payload()
    assert payload["code"] == "REMOTE_DISCONNECTED"
    assert payload["retryable"] is True


def test_interface_success_returns_raw_data() -> None:
    gateway = ProviderGateway(_Flaky(failures=0))
    assert gateway.realtime_price("600519.SH")["price"] == 100.0


# ----------------------------------------------------------------------
# 能力注册中心
# ----------------------------------------------------------------------
def test_registry_lists_known_providers() -> None:
    assert "eastmoney" in PROVIDER_CAPABILITIES
    assert "sina" in PROVIDER_CAPABILITIES
    assert "tushare" in PROVIDER_CAPABILITIES


def test_eastmoney_does_not_claim_moneyflow() -> None:
    """能力表是「事前就知道不支持」的依据，必须准。"""

    assert "moneyflow" not in PROVIDER_CAPABILITIES["eastmoney"]
    assert "moneyflow" in PROVIDER_CAPABILITIES["tushare"]
