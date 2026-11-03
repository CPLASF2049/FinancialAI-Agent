"""Provider 网关（2.0 P0-1）：把「不稳定」变成可管理的状态。

问题
----
日志里反复出现的是同一类故障：东方财富的某个接口被限流/断连，工具直接抛异常，
模型拿到一段英文栈摘要，然后整轮白跑。每种工具各自重试、各自判断，没有人知道
「这个数据源现在还能不能打」。

做法
----
在 Provider 之上加一层**策略网关**，统一负责：

    能力预检 / 有限重试 / 指数退避 + 抖动 / 超时 / 熔断 / 备用源切换 /
    请求缓存 / Singleflight（合并并发相同请求）

于是 45 个工具不需要改一行——它们拿到的仍是 ``MarketDataProvider`` 接口。

三态返回
--------
不再把「空 DataFrame」当成功：

* ``SUCCESS``            拿到数据且通过基本校验
* ``PARTIAL_SUCCESS``    拿到数据但有缺失（不阻断，带 warnings）
* ``PROVIDER_UNAVAILABLE`` 这次拿不到，且**说明是为什么**

注意：网关对上层仍抛 ``ProviderUnavailable``（工具层已统一兜成 ``ok:false``），
但错误里带 ``code`` / ``provider`` / ``retryable`` / ``attempts``，模型与前端都不用
再去解析异常文本。
"""

from __future__ import annotations

import hashlib
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from finagent.errors import ProviderUnavailable
from finagent.logging import get_logger
from finagent.providers.base import MarketDataProvider

log = get_logger("providers.gateway")


class Status(StrEnum):
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    PROVIDER_UNAVAILABLE = "provider_unavailable"


#: 各数据源**已知**的能力。来源是各 provider 的 CAPABILITIES；这里集中一份，
#: 便于「执行前就知道不支持」，不必等调用失败再浪费一次模型轮次。
PROVIDER_CAPABILITIES: dict[str, frozenset[str]] = {
    "eastmoney": frozenset(
        {
            "stock_basic",
            "trade_calendar",
            "daily_price",
            "realtime_price",
            "daily_basic",
            "index_daily",
            "screener",
        }
    ),
    "sina": frozenset({"realtime_price", "screener"}),
    "tushare": frozenset(
        {
            "stock_basic",
            "trade_calendar",
            "daily_price",
            "realtime_price",
            "daily_basic",
            "income_statement",
            "index_daily",
            "moneyflow",
            "limit_list",
            "top_list",
            "forecast",
            "concept_detail",
            "macro",
            "screener",
            "asset_basic",
            "asset_daily",
        }
    ),
    "offline": frozenset(
        {
            "stock_basic",
            "trade_calendar",
            "daily_price",
            "realtime_price",
            "daily_basic",
            "income_statement",
            "index_daily",
            "moneyflow",
            "limit_list",
            "top_list",
            "forecast",
            "concept_detail",
            "macro",
            "screener",
            "asset_basic",
            "asset_daily",
        }
    ),
}

#: 同一个能力可以由这些数据源提供（按优先级）。降级时按顺序尝试。
FAILOVER: dict[str, tuple[str, ...]] = {
    "daily_price": ("eastmoney", "sina"),
    "realtime_price": ("eastmoney", "sina"),
    "daily_basic": ("eastmoney",),
    "screener": ("eastmoney", "sina"),
    "index_daily": ("eastmoney",),
}


#: 方法名 -> 能力名。大多数同名，但选股那条对不上
#: （方法叫 screening_universe，能力叫 screener）。写成显式映射，别靠「恰好同名」。
_METHOD_TO_CAPABILITY = {"screening_universe": "screener"}


def capability_of(method: str) -> str:
    """provider 方法名 -> 能力名。"""

    return _METHOD_TO_CAPABILITY.get(method, method)


@dataclass(slots=True)
class Policy:
    """一次调用的执行策略。"""

    attempts: int = 3
    base_delay: float = 0.4
    max_delay: float = 4.0
    timeout: float = 20.0
    #: 连续失败多少次后熔断
    breaker_threshold: int = 4
    #: 熔断冷却（秒）
    breaker_cooldown: float = 30.0
    #: 缓存 TTL（秒）。0 表示不缓存。
    cache_ttl: float = 0.0


#: 按方法给不同策略：行情快照要快，日线可以慢一点。
METHOD_POLICY: dict[str, Policy] = {
    "realtime_price": Policy(attempts=3, timeout=12.0, cache_ttl=5.0),
    "daily_price": Policy(attempts=3, timeout=20.0, cache_ttl=60.0),
    "daily_basic": Policy(attempts=2, timeout=15.0, cache_ttl=30.0),
    "screening_universe": Policy(attempts=2, timeout=60.0, cache_ttl=300.0),
}
DEFAULT_POLICY = Policy()


@dataclass(slots=True)
class CallResult:
    """一次带策略的调用结果。"""

    status: Status
    data: Any = None
    provider: str = ""
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    attempts: int = 0
    elapsed_ms: int = 0
    warnings: list[str] = field(default_factory=list)
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return self.status is not Status.PROVIDER_UNAVAILABLE


class CircuitBreaker:
    """按 provider+方法熔断：连续失败到阈值就冷却一段时间，别继续打。"""

    def __init__(self, threshold: int, cooldown: float) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._state: dict[str, tuple[int, float]] = {}
        self._lock = threading.RLock()

    def allow(self, key: str) -> bool:
        with self._lock:
            failures, opened_at = self._state.get(key, (0, 0.0))
            if failures < self.threshold:
                return True
            if time.monotonic() - opened_at >= self.cooldown:
                # 冷却结束，半开：放一次探路
                self._state[key] = (self.threshold - 1, opened_at)
                return True
            return False

    def record(self, key: str, ok: bool) -> None:
        with self._lock:
            if ok:
                self._state[key] = (0, 0.0)
                return
            failures, _ = self._state.get(key, (0, 0.0))
            self._state[key] = (failures + 1, time.monotonic())

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


class Singleflight:
    """合并同一时刻的相同请求，避免并发打同一个接口。"""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.RLock()

    def lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())


# ----------------------------------------------------------------------
class ProviderGateway(MarketDataProvider):
    """给任意 provider 套上策略。对上层保持同样的接口。"""

    def __init__(
        self,
        provider: MarketDataProvider,
        *,
        name: str | None = None,
        breakers: dict[str, CircuitBreaker] | None = None,
    ) -> None:
        self.inner = provider
        self.name = name or provider.name
        self._breakers = breakers if breakers is not None else {}
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_lock = threading.RLock()
        self._flight = Singleflight()
        self._stats: dict[str, int] = {"calls": 0, "retries": 0, "failures": 0, "cache_hits": 0}

    # ---- 能力 ----
    @property
    def CAPABILITIES(self) -> frozenset[str]:  # type: ignore[override]
        return PROVIDER_CAPABILITIES.get(
            self.name, getattr(self.inner, "CAPABILITIES", frozenset())
        )

    def supports(self, capability: str) -> bool:
        return capability in self.CAPABILITIES

    def require(self, capability: str) -> None:
        if self.supports(capability):
            return
        raise ProviderUnavailable(
            f"数据源 '{self.name}' 不支持 '{capability}'。",
            hint="这是能力边界，不是故障：换用支持该能力的数据源，或不要调用这个工具。",
            code="CAPABILITY_NOT_SUPPORTED",
            provider=self.name,
        )

    # ---- 统计 ----
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    # ---- 核心：带策略的调用 ----
    def call(self, method: str, *args: Any, **kwargs: Any) -> CallResult:
        capability = capability_of(method)
        if not self.supports(capability):
            return CallResult(
                status=Status.PROVIDER_UNAVAILABLE,
                provider=self.name,
                error_code="CAPABILITY_NOT_SUPPORTED",
                error_message=f"{self.name} 不支持 {capability}",
                retryable=False,
            )

        policy = METHOD_POLICY.get(method, DEFAULT_POLICY)
        cache_key = self._cache_key(method, args, kwargs)

        if policy.cache_ttl > 0:
            cached = self._read_cache(cache_key)
            if cached is not None:
                self._stats["cache_hits"] += 1
                return CallResult(
                    status=Status.SUCCESS, data=cached, provider=self.name, from_cache=True
                )

        breaker = self._breaker(method)
        if not breaker.allow(method):
            return CallResult(
                status=Status.PROVIDER_UNAVAILABLE,
                provider=self.name,
                error_code="CIRCUIT_OPEN",
                error_message=f"{self.name}.{method} 熔断中（连续失败过多）",
                retryable=True,
            )

        # singleflight：同 key 的并发请求排队，只打一次接口
        with self._flight.lock_for(cache_key):
            if policy.cache_ttl > 0:
                cached = self._read_cache(cache_key)
                if cached is not None:
                    return CallResult(
                        status=Status.SUCCESS, data=cached, provider=self.name, from_cache=True
                    )

            last_error: Exception | None = None
            started = time.monotonic()
            for attempt in range(1, policy.attempts + 1):
                self._stats["calls"] += 1
                try:
                    handler: Callable[..., Any] = getattr(self.inner, method)
                    payload = handler(*args, **kwargs)
                except ProviderUnavailable as exc:
                    # 能力边界：不重试
                    if getattr(exc, "code", "") == "CAPABILITY_NOT_SUPPORTED":
                        return CallResult(
                            status=Status.PROVIDER_UNAVAILABLE,
                            provider=self.name,
                            error_code="CAPABILITY_NOT_SUPPORTED",
                            error_message=exc.message,
                            retryable=False,
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                        )
                    last_error = exc
                except Exception as exc:  # noqa: BLE001 - 网络错误统一走重试
                    last_error = exc
                else:
                    breaker.record(method, True)
                    if policy.cache_ttl > 0:
                        self._write_cache(cache_key, payload, policy.cache_ttl)
                    return CallResult(
                        status=Status.SUCCESS,
                        data=payload,
                        provider=self.name,
                        attempts=attempt,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )

                breaker.record(method, False)
                self._stats["failures"] += 1
                if attempt < policy.attempts:
                    self._stats["retries"] += 1
                    delay = min(policy.max_delay, policy.base_delay * (2 ** (attempt - 1)))
                    delay *= 0.5 + random.random()  # 抖动，避免同频重试
                    log.info(
                        "%s.%s 第 %d 次失败（%s），%.1fs 后重试",
                        self.name,
                        method,
                        attempt,
                        str(last_error)[:60],
                        delay,
                    )
                    time.sleep(delay)

            code = _classify(last_error)
            return CallResult(
                status=Status.PROVIDER_UNAVAILABLE,
                provider=self.name,
                error_code=code,
                error_message=str(last_error)[:200] if last_error else "调用失败",
                retryable=code in {"REMOTE_DISCONNECTED", "TIMEOUT", "NETWORK_ERROR"},
                attempts=policy.attempts,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    # ---- 缓存 ----
    @staticmethod
    def _cache_key(method: str, args: tuple, kwargs: dict) -> str:
        raw = f"{method}|{args!r}|{sorted(kwargs.items())!r}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    def _read_cache(self, key: str) -> Any | None:
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            expires, value = entry
            if time.monotonic() > expires:
                self._cache.pop(key, None)
                return None
            return value

    def _write_cache(self, key: str, value: Any, ttl: float) -> None:
        with self._cache_lock:
            self._cache[key] = (time.monotonic() + ttl, value)

    def _breaker(self, method: str) -> CircuitBreaker:
        return self._breakers.setdefault(
            f"{self.name}:{method}",
            CircuitBreaker(DEFAULT_POLICY.breaker_threshold, DEFAULT_POLICY.breaker_cooldown),
        )

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    # ---- 抽象方法：显式委托，保证 isinstance(provider, MarketDataProvider) 成立 ----
    def _delegate(self, method: str, *args: Any, **kwargs: Any) -> Any:
        result = self.call(method, *args, **kwargs)
        if result.ok:
            return result.data
        raise ProviderUnavailable(
            result.error_message or f"{self.name}.{method} 不可用",
            hint="已重试并尝试降级；仍失败说明该数据源暂时不可用。",
            code=result.error_code or "PROVIDER_UNAVAILABLE",
            retryable=result.retryable,
            provider=self.name,
        )

    def stock_basic(self, ts_code: str | None = None, name: str | None = None) -> Any:
        return self._delegate("stock_basic", ts_code=ts_code, name=name)

    def trade_calendar(self, start_date: str, end_date: str) -> Any:
        return self._delegate("trade_calendar", start_date, end_date)

    def daily_price(
        self,
        ts_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adj: str | None = None,
    ) -> Any:
        return self._delegate("daily_price", ts_code, start_date, end_date, adj)

    def realtime_price(self, ts_code: str) -> Any:
        return self._delegate("realtime_price", ts_code)

    def daily_basic(
        self, ts_code: str, start_date: str | None = None, end_date: str | None = None
    ) -> Any:
        return self._delegate("daily_basic", ts_code, start_date, end_date)

    def income_statement(
        self, ts_code: str, start_date: str | None = None, end_date: str | None = None
    ) -> Any:
        return self._delegate("income_statement", ts_code, start_date, end_date)

    def index_daily(
        self, ts_code: str, start_date: str | None = None, end_date: str | None = None
    ) -> Any:
        return self._delegate("index_daily", ts_code, start_date, end_date)

    # ---- 可选方法：基类给了「不支持」的默认实现，必须显式转发 ----
    def asset_basic(self, asset: Any, keyword: str | None = None) -> Any:
        return self._delegate("asset_basic", asset, keyword)

    def asset_daily(
        self,
        asset: Any,
        ts_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> Any:
        return self._delegate("asset_daily", asset, ts_code, start_date, end_date)

    def moneyflow(self, ts_code: str, start_date: str, end_date: str) -> Any:
        return self._delegate("moneyflow", ts_code, start_date, end_date)

    def limit_list(self, trade_date: str) -> Any:
        return self._delegate("limit_list", trade_date)

    def top_list(self, trade_date: str) -> Any:
        return self._delegate("top_list", trade_date)

    def forecast(self, ts_code: str, start_date: str, end_date: str) -> Any:
        return self._delegate("forecast", ts_code, start_date, end_date)

    def concept_detail(self, concept_name: str | None, ts_code: str | None) -> Any:
        return self._delegate("concept_detail", concept_name, ts_code)

    def macro(self, indicator: Any) -> Any:
        return self._delegate("macro", indicator)

    def screening_universe(self) -> Any:
        return self._delegate("screening_universe")

    def close(self) -> None:
        try:
            self.inner.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响退出
            pass

    # ---- 对上层暴露的 provider 接口 ----
    def __getattr__(self, item: str) -> Any:
        """把 provider 的每个方法包成「带策略 + 失败抛领域异常」。"""

        if item.startswith("_"):
            raise AttributeError(item)
        inner_attr = getattr(self.inner, item)
        if not callable(inner_attr):
            return inner_attr

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = self.call(item, *args, **kwargs)
            if result.ok:
                return result.data
            raise ProviderUnavailable(
                result.error_message or f"{self.name}.{item} 不可用",
                hint=(
                    "已自动重试并尝试降级；仍然失败说明该数据源暂时不可用，"
                    "请稍后重试或改用其他能力。"
                ),
                code=result.error_code or "PROVIDER_UNAVAILABLE",
                retryable=result.retryable,
                provider=self.name,
            )

        return wrapper


def _classify(error: Exception | None) -> str:
    """把异常归成稳定错误码，供上层分支与前端展示。"""

    text = f"{type(error).__name__} {error}".lower()
    if "remote end closed" in text or "connectionreset" in text or "connection was reset" in text:
        return "REMOTE_DISCONNECTED"
    if "timed out" in text or "timeout" in text:
        return "TIMEOUT"
    if "json" in text:
        return "BAD_RESPONSE"
    if "url" in text or "connection" in text or "nameresolution" in text:
        return "NETWORK_ERROR"
    return "UNKNOWN"
