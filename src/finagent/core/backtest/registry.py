"""策略注册表。"""

from __future__ import annotations

from collections.abc import Iterable

from finagent.core.backtest.base import BacktestStrategy
from finagent.errors import StrategyNotFound

_REGISTRY: dict[str, type[BacktestStrategy]] = {}


def register_strategy(cls: type[BacktestStrategy]) -> type[BacktestStrategy]:
    """把一个策略登记进全局注册表的类装饰器。"""

    if not issubclass(cls, BacktestStrategy):
        raise TypeError(f"{cls.__name__} 必须继承 BacktestStrategy")
    key = getattr(cls, "name", "")
    if not key or not isinstance(key, str):
        raise ValueError(f"{cls.__name__} 必须定义非空字符串 'name'")
    _REGISTRY[key] = cls
    return cls


def get_strategy_class(strategy: str) -> type[BacktestStrategy]:
    cls = _REGISTRY.get(str(strategy).strip())
    if cls is None:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise StrategyNotFound(
            f"未知策略 '{strategy}'。",
            hint=f"已注册策略：{known}",
        )
    return cls


def list_registered_strategies() -> list[str]:
    return sorted(_REGISTRY)


def describe_strategies() -> list[dict[str, str]]:
    return [
        {
            "name": name,
            "description": _REGISTRY[name].description,
            "multi_asset": _REGISTRY[name].requires_multi_asset,
        }
        for name in sorted(_REGISTRY)
    ]


def iter_strategies() -> Iterable[type[BacktestStrategy]]:
    for name in sorted(_REGISTRY):
        yield _REGISTRY[name]
