"""数据源选择。

这一层之上的模块都不知道当前用的是哪个后端，这正是 MCP 服务器可以在真实 Tushare
数据流与确定性离线 fixture 之间自由切换的原因。
"""

from __future__ import annotations

from functools import lru_cache

from finagent.errors import ConfigurationError
from finagent.providers.base import MarketDataProvider
from finagent.settings import Settings, get_settings


def build_provider(settings: Settings | None = None) -> MarketDataProvider:
    """按配置实例化对应的数据源。"""

    resolved = settings or get_settings()
    if resolved.provider == "tushare":
        from finagent.providers.tushare import TushareProvider

        return TushareProvider(resolved.tushare_token, requests_per_second=resolved.tushare_rps)
    if resolved.provider == "offline":
        from finagent.providers.offline import OfflineProvider

        return OfflineProvider()
    raise ConfigurationError(
        f"未知数据源 '{resolved.provider}'。",
        hint="把 FINAGENT_PROVIDER 设为 'offline' 或 'tushare'。",
    )


@lru_cache(maxsize=4)
def _cached(provider_name: str, token: str | None) -> MarketDataProvider:
    settings = get_settings()
    if settings.provider != provider_name:
        settings = settings.model_copy(update={"provider": provider_name, "tushare_token": token})
    return build_provider(settings)


def get_provider(*, refresh: bool = False) -> MarketDataProvider:
    """进程级的数据源单例。"""

    settings = get_settings()
    if refresh:
        _cached.cache_clear()
    return _cached(settings.provider, settings.tushare_token)


def provider_name() -> str:
    return get_settings().provider
