"""回测子系统：在单个标的上跑可插拔策略。"""

from finagent.core.backtest import strategies  # noqa: E402,F401  (registration side effects)
from finagent.core.backtest.base import BacktestStrategy
from finagent.core.backtest.engine import BacktestEngine, BacktestResult
from finagent.core.backtest.registry import (
    describe_strategies,
    get_strategy_class,
    list_registered_strategies,
    register_strategy,
)


def run_backtest(
    provider,
    ts_code: str,
    strategy: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    params: dict | None = None,
    initial_capital: float = 100_000.0,
    benchmark: str | None = None,
    curve_points: int = 180,
) -> dict:
    """便捷包装：直接返回可 JSON 序列化的数据。"""

    engine = BacktestEngine(provider, initial_capital=initial_capital)
    result = engine.run(
        ts_code,
        strategy,
        start_date=start_date,
        end_date=end_date,
        params=params,
        benchmark=benchmark,
        curve_points=curve_points,
    )
    return result.to_dict()


__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "BacktestStrategy",
    "describe_strategies",
    "get_strategy_class",
    "list_registered_strategies",
    "register_strategy",
    "run_backtest",
]
