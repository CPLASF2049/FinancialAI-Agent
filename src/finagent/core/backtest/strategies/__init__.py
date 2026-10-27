"""内置策略。import 本包即完成全部策略的注册。"""

from finagent.core.backtest.strategies import (  # noqa: F401  (registration side effects)
    channels,
    multi_asset,
    oscillators,
    risk,
    trend,
    volume,
)

__all__ = ["trend", "oscillators", "channels", "volume", "risk", "multi_asset"]
