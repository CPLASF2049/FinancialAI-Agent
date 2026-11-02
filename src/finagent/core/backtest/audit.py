"""回测前视偏差（look-ahead bias）审计。

前视偏差是量化回测里最隐蔽、也最致命的一类错误：策略在 t 日「看到了」t 之后的数据。
它不会报错，只会让收益曲线漂亮得不真实。单看代码很难发现——``enrich()`` 里随手一个
``shift(-1)``、``rolling(center=True)``、或者用了整段数据算 z-score，都会造成泄漏。

这里用**截断检验**（truncation test）把它变成一个可自动化的断言：

1. 用完整区间跑一遍回测，记下截止日 ``cut`` 之前的所有成交与权益曲线；
2. 把数据截断到 ``cut`` 再跑一遍；
3. 两者在 ``cut`` 之前必须**逐笔一致**。

道理很简单：如果策略在 t 日只用 ≤t 的数据，那么「后面还有没有数据」不该影响它已经做出的
决定。一旦不一致，就说明某个信号偷看了未来。

这个检验只依赖 provider 接口，所以对离线 fixture 和真实行情同样有效——它是**方法**级别的
保证，不是数据级别的承诺。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from finagent.core import calendar as cal
from finagent.core.backtest.engine import BacktestEngine
from finagent.core.backtest.registry import get_strategy_class, list_registered_strategies
from finagent.logging import get_logger
from finagent.providers.base import MarketDataProvider

log = get_logger("backtest.audit")

#: 默认在窗口的 70% 处截断：太靠前会因为预热不足而没有成交，太靠后则样本太少。
DEFAULT_CUT_RATIO = 0.7


@dataclass(slots=True)
class LeakReport:
    """单个「标的 × 策略」的前视偏差检验结果。"""

    ts_code: str
    strategy: str
    cut_date: str
    passed: bool
    trades_before_cut: int = 0
    curve_points_compared: int = 0
    leaks: list[str] = field(default_factory=list)
    skipped_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts_code": self.ts_code,
            "strategy": self.strategy,
            "cut_date": self.cut_date,
            "passed": self.passed,
            "trades_before_cut": self.trades_before_cut,
            "curve_points_compared": self.curve_points_compared,
            "leaks": self.leaks,
            "skipped_reason": self.skipped_reason,
        }


def _trade_key(trade: dict[str, Any]) -> tuple[str, str, float, int]:
    return (
        str(trade.get("date")),
        str(trade.get("action")),
        round(float(trade.get("price") or 0.0), 4),
        int(trade.get("shares") or 0),
    )


def check_strategy(
    provider: MarketDataProvider,
    ts_code: str,
    strategy: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    params: dict[str, Any] | None = None,
    cut_ratio: float = DEFAULT_CUT_RATIO,
) -> LeakReport:
    """对单个策略做截断检验。"""

    strategy_cls = get_strategy_class(strategy)
    default_start, default_end = cal.default_window(365)
    start = cal.format_date(start_date) if start_date else default_start
    end = cal.format_date(end_date) if end_date else default_end

    if strategy_cls.requires_multi_asset:
        return LeakReport(
            ts_code=ts_code,
            strategy=strategy,
            cut_date="",
            passed=True,
            skipped_reason="多标的策略，单标的引擎不适用",
        )

    cut = _cut_date(provider, ts_code, start, end, cut_ratio)
    if cut is None:
        return LeakReport(
            ts_code=ts_code,
            strategy=strategy,
            cut_date="",
            passed=True,
            skipped_reason="样本不足以截断",
        )

    engine = BacktestEngine(provider, initial_capital=1_000_000.0)
    kwargs = {
        "start_date": start,
        "params": params or {},
        "curve_points": 0,  # 不降采样，逐点比对
        "include_trades": True,
    }

    try:
        full = engine.run(ts_code, strategy, end_date=end, **kwargs)
        truncated = engine.run(ts_code, strategy, end_date=cut, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 跑不起来单独报告，不要让审计整体失败
        return LeakReport(
            ts_code=ts_code,
            strategy=strategy,
            cut_date=cut,
            passed=False,
            leaks=[f"回测执行失败：{exc}"],
        )

    # 用 `<=` 而不是 `<`：偷看未来的策略恰恰会在**截断日当天**做出不同的决定
    # （完整数据里它有「明天」，截断数据里没有），把截断日排除掉就抓不到它了。
    full_trades = [_trade_key(t) for t in full.all_trades if str(t.get("date")) <= cut]
    cut_trades = [_trade_key(t) for t in truncated.all_trades if str(t.get("date")) <= cut]

    leaks: list[str] = []
    if full_trades != cut_trades:
        leaks.append(
            f"{cut} 及之前成交不一致：完整数据 {len(full_trades)} 笔，截断数据 {len(cut_trades)} 笔"
            "（策略看到了未来的 K 线）"
        )

    full_curve = {p["trade_date"]: round(float(p["value"]), 2) for p in full.equity_curve}
    cut_curve = {p["trade_date"]: round(float(p["value"]), 2) for p in truncated.equity_curve}
    shared = sorted(set(full_curve) & set(cut_curve))
    mismatched = [date for date in shared if full_curve[date] != cut_curve[date]]
    if mismatched:
        leaks.append(
            f"{len(mismatched)} 个交易日的权益值随数据延长而变化"
            f"（首个：{mismatched[0]}，{full_curve[mismatched[0]]} vs {cut_curve[mismatched[0]]}）"
        )

    return LeakReport(
        ts_code=ts_code,
        strategy=strategy,
        cut_date=cut,
        passed=not leaks,
        trades_before_cut=len(full_trades),
        curve_points_compared=len(shared),
        leaks=leaks,
    )


def _cut_date(
    provider: MarketDataProvider,
    ts_code: str,
    start: str,
    end: str,
    ratio: float,
) -> str | None:
    """在区间内取一个截断日，要求它之后至少还有若干根 K 线。"""

    try:
        frame = provider.daily_price(ts_code, start, end)
    except Exception:  # noqa: BLE001
        return None
    if frame is None or frame.empty or len(frame) < 10:
        return None

    dates = frame.sort_values("trade_date")["trade_date"].astype(str).tolist()
    index = int(len(dates) * max(0.05, min(0.95, ratio)))
    index = min(index, len(dates) - 2)  # 保证截断日之后还有数据可比
    return dates[index]


def audit_strategies(
    provider: MarketDataProvider,
    ts_code: str,
    *,
    strategies: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[LeakReport]:
    """对一批策略跑前视偏差审计（默认跑全部单标的策略）。"""

    candidates = strategies or [
        name
        for name in list_registered_strategies()
        if not get_strategy_class(name).requires_multi_asset
    ]
    reports: list[LeakReport] = []
    for name in candidates:
        report = check_strategy(provider, ts_code, name, start_date=start_date, end_date=end_date)
        reports.append(report)
        if not report.passed:
            log.warning("前视偏差 %s/%s：%s", ts_code, name, "；".join(report.leaks))
    return reports


def summarise(reports: list[LeakReport]) -> dict[str, Any]:
    checked = [r for r in reports if not r.skipped_reason]
    failed = [r for r in checked if not r.passed]
    return {
        "total": len(reports),
        "checked": len(checked),
        "skipped": len(reports) - len(checked),
        "failed": len(failed),
        "passed": len(checked) - len(failed),
        "ok": not failed,
    }
