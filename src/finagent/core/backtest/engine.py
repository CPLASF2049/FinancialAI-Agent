"""单标的、日频回测引擎。

引擎的行为约定：

* provider 由外部注入，引擎本身不绑定任何数据源；
* 结果包含风险调整指标（夏普、年化收益、胜率）和可选的基准对比，便于讨论*相对*表现；
* 权益曲线有长度上限（``curve_points``），因为它会经 MCP 工具直接进模型上下文；
* 建模了 A 股摩擦：100 股整手、双边佣金、卖出 0.05% 印花税。

已知限制：按信号当日收盘成交，没有滑点模型，且无法同日回转（等效 T+1）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from finagent.core import calendar as cal
from finagent.core.backtest.registry import get_strategy_class
from finagent.errors import InsufficientData, InvalidArgument
from finagent.providers.base import MarketDataProvider

DEFAULT_COMMISSION = 0.0003
DEFAULT_STAMP_DUTY = 0.0005
#: 滑点：以对手价成交的代价。A 股小单通常 0.05% 量级。
DEFAULT_SLIPPAGE = 0.0005
#: 双边总成本下限。低于这个数的回测在真实市场里不成立。
MIN_ROUND_TRIP_COST = 0.002
#: 涨跌停幅度：主板 10%、创业板/科创板 20%、北交所 30%。
_LIMIT_MAIN = 0.10
_LIMIT_GROWTH = 0.20
_LIMIT_BJ = 0.30
LOT_SIZE = 100


@dataclass(slots=True)
class Trade:
    date: str
    action: str
    price: float
    shares: int
    fee: float
    cash_after: float


@dataclass(slots=True)
class BacktestResult:
    ts_code: str
    strategy: str
    params: dict[str, Any]
    start_date: str
    end_date: str
    initial_capital: float
    final_value: float
    total_return_pct: float
    annualized_return_pct: float
    max_drawdown_pct: float
    sharpe: float | None
    trades_count: int
    win_rate_pct: float | None
    exposure_pct: float
    benchmark: dict[str, Any] | None = None
    #: 成本模型（让「扣了多少」可复核，而不是埋在实现里）
    cost_model: dict[str, float] = field(default_factory=dict)
    #: 被拒绝的成交：涨跌停买不进/卖不出、停牌无行情
    skipped: dict[str, int] = field(default_factory=dict)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: **全部**成交，供审计使用；``trades`` 只是为了塞进模型上下文而截断的最后 12 笔，
    #: 不能拿来做「截止日之前是否一致」这类判断。
    all_trades: list[dict[str, Any]] = field(default_factory=list)
    #: 实际用到的最后一根 K 线日期。它就是「数据截止时间」——结论里所有数字都以它为准。
    data_asof: str = ""
    #: 实际加载的首根 K 线（含预热段），用于证明指标有足够的预热窗口。
    data_start: str = ""
    #: 数据源名（``tushare`` / ``offline``），让下游能把「演示」和「真实」区分开。
    provider: str = ""
    #: 加载的 K 线根数（含预热），排查「样本太少」时有用。
    bars: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ts_code": self.ts_code,
            "strategy": self.strategy,
            "params": self.params,
            "period": {"start": self.start_date, "end": self.end_date},
            "data": {
                "provider": self.provider,
                "asof": self.data_asof,
                "first_bar": self.data_start,
                "bars": self.bars,
            },
            "initial_capital": round(self.initial_capital, 2),
            "final_value": round(self.final_value, 2),
            "total_return_pct": self.total_return_pct,
            "annualized_return_pct": self.annualized_return_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "sharpe": self.sharpe,
            "trades_count": self.trades_count,
            "win_rate_pct": self.win_rate_pct,
            "exposure_pct": self.exposure_pct,
            "equity_curve": self.equity_curve,
            "trades": self.trades,
        }
        if self.benchmark is not None:
            payload["benchmark"] = self.benchmark
        if self.cost_model:
            payload["cost_model"] = self.cost_model
        if self.skipped:
            payload["skipped"] = self.skipped
        if self.warnings:
            payload["warnings"] = self.warnings
        return payload


class BacktestEngine:
    """在单个标的上跑一个策略。"""

    def __init__(
        self,
        provider: MarketDataProvider,
        *,
        initial_capital: float = 100_000.0,
        commission: float = DEFAULT_COMMISSION,
        stamp_duty: float = DEFAULT_STAMP_DUTY,
        slippage: float = DEFAULT_SLIPPAGE,
        enforce_price_limits: bool = True,
    ) -> None:
        if initial_capital <= 0:
            raise InvalidArgument("initial_capital 必须为正数。")
        self.provider = provider
        self.initial_capital = float(initial_capital)
        self.commission = float(commission)
        self.stamp_duty = float(stamp_duty)
        self.slippage = float(slippage)
        self.enforce_price_limits = bool(enforce_price_limits)
        self.skipped: dict[str, int] = {}

    # ------------------------------------------------------------------
    def cost_model(self) -> dict[str, float]:
        """成本模型摘要。双边合计低于 :data:`MIN_ROUND_TRIP_COST` 时给出警告。"""

        round_trip = self.commission * 2 + self.stamp_duty + self.slippage * 2
        return {
            "commission": self.commission,
            "stamp_duty": self.stamp_duty,
            "slippage": self.slippage,
            "round_trip": round(round_trip, 6),
        }

    @staticmethod
    def limit_pct(ts_code: str) -> float:
        """按板块给出涨跌停幅度。"""

        code = str(ts_code).partition(".")[0]
        if code.startswith(("30", "68")):
            return _LIMIT_GROWTH
        if code.startswith(("4", "8", "92")):
            return _LIMIT_BJ
        return _LIMIT_MAIN

    def _skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def _bar_tradable(self, row: dict[str, Any], ts_code: str, action: str) -> bool:
        """这根 K 线能不能成交？

        涨停买不进、跌停卖不出——这是 A 股回测里最常见的「虚假收益」来源：
        信号恰好在涨停板上，按收盘价买入在现实中根本成交不了。

        两种涨停要分开记，但**方向限制一样**：

        * 涨停（含一字板）：买不进，卖得掉——涨停时买盘排队，卖出没问题
        * 跌停（含一字板）：卖不出，买得到

        一字板单独记一笔统计，是因为它最能暴露回测虚高：全市场封死的时候你
        还「买到了」，说明成交假设根本不成立。
        """

        if not self.enforce_price_limits:
            return True

        close = float(row.get("close") or 0)
        pre_close = float(row.get("pre_close") or 0)
        if close <= 0 or pre_close <= 0:
            return True

        change = close / pre_close - 1.0
        limit = self.limit_pct(ts_code)
        high = float(row.get("high") or close)
        low = float(row.get("low") or close)
        open_ = float(row.get("open") or close)
        sealed = high == low and open_ == high  # 一字板：全天一个价

        # 留 0.2 个百分点容差：不同数据源的四舍五入会让 10.00% 变成 9.98%
        if change >= limit - 0.002:
            self._skip("limit_up_sealed" if sealed else "limit_up")
            return action != "BUY"
        if change <= -limit + 0.002:
            self._skip("limit_down_sealed" if sealed else "limit_down")
            return action != "SELL"
        return True

    def _impact_cost(self, order_value: float, bar_amount: float) -> float:
        """冲击成本：订单占当日成交额越大，滑点越高。

        固定滑点在高价股上低估、在小票上高估。平方根模型（Almgren 简化版）
        至少能反映「买得越多越贵」这个事实。
        """

        if bar_amount <= 0 or order_value <= 0:
            return self.slippage
        participation = min(order_value / bar_amount, 1.0)
        impact = self.slippage * (participation**0.5) * 2.0
        # 单边滑点上限 1%：再高就说明这个仓位本身不可执行
        return min(self.slippage + impact, 0.01)

    # ------------------------------------------------------------------
    def _load_frame(
        self, ts_code: str, start_date: str, end_date: str, warmup_days: int
    ) -> pd.DataFrame:
        warm_start = cal.warmup_start(start_date, warmup_days)
        frame = self.provider.daily_price(ts_code, warm_start, end_date)
        if frame is None or frame.empty:
            raise InsufficientData(
                f"{ts_code} 在 {warm_start} 至 {end_date} 之间没有日线数据。",
                hint="请检查 ts_code，或放宽日期区间。",
            )
        frame = frame.sort_values("trade_date", ascending=True).reset_index(drop=True)
        for column in ("open", "high", "low", "close", "vol"):
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if "vol" not in frame.columns:
            frame["vol"] = 0.0
        return frame

    def run(
        self,
        ts_code: str,
        strategy: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        params: Mapping[str, Any] | None = None,
        benchmark: str | None = None,
        curve_points: int = 180,
        include_trades: bool = True,
    ) -> BacktestResult:
        strategy_cls = get_strategy_class(strategy)
        if strategy_cls.requires_multi_asset:
            raise InvalidArgument(
                f"策略 '{strategy}' 需要多标的截面数据，单标的引擎无法运行。",
                hint="请从 list_strategies() 里选择一个单标的策略。",
            )

        effective_params = dict(params or {})
        default_start, default_end = cal.default_window(365)
        start = cal.format_date(start_date) if start_date else default_start
        end = cal.format_date(end_date) if end_date else default_end
        if start > end:
            raise InvalidArgument(f"起始日期 {start} 晚于结束日期 {end}。")

        warmup = max(strategy_cls.required_history(effective_params), 60)
        frame = self._load_frame(ts_code, start, end, warmup)

        instance = strategy_cls()
        enriched = instance.enrich(frame.copy(), effective_params)

        trade_mask = enriched["trade_date"] >= start
        if not trade_mask.any():
            raise InsufficientData(
                f"{ts_code} 在 {start}-{end} 区间内没有 K 线。",
                hint="请放宽请求的时间窗口。",
            )
        first_idx = int(trade_mask.idxmax())
        begin = max(first_idx - 1, 0)

        cash = self.initial_capital
        position = 0
        entry_price: float | None = None
        trades: list[Trade] = []
        curve: list[dict[str, Any]] = []
        bars_in_market = 0

        records = enriched.iloc[begin:].to_dict("records")
        prev_row: dict[str, Any] | None = None
        for series in records:
            trade_date = str(series["trade_date"])
            in_window = trade_date >= start
            price = float(series["close"])

            if prev_row is not None and in_window and position > 0:
                bars_in_market += 1

            if in_window:
                curve.append({"trade_date": trade_date, "value": round(cash + position * price, 2)})

            context = {"position": position, "entry_price": entry_price, "cash": cash}
            signal = 0
            if prev_row is not None:
                if position > 0:
                    signal = instance.risk_exit(series, prev_row, effective_params, context)
                if signal == 0:
                    signal = instance.signal(series, prev_row, effective_params, context)

            if in_window and prev_row is not None and signal != 0:
                if signal == 1 and position == 0 and cash > 0:
                    if not self._bar_tradable(series, ts_code, "BUY"):
                        signal = 0
                elif signal == -1 and position > 0:
                    if not self._bar_tradable(series, ts_code, "SELL"):
                        signal = 0

                if signal == 1 and position == 0 and cash > 0:
                    fraction = float(instance.position_fraction(effective_params, context))
                    fraction = max(0.0, min(1.0, fraction))
                    # 成交价带冲击成本：订单占当日成交额越大越贵
                    order_value = cash * fraction
                    slip = self._impact_cost(order_value, float(series.get("amount") or 0))
                    fill_price = price * (1.0 + slip)
                    affordable = (
                        int(cash / (fill_price * (1.0 + self.commission)) / LOT_SIZE) * LOT_SIZE
                    )
                    shares = int(affordable * fraction / LOT_SIZE) * LOT_SIZE
                    if shares > 0:
                        cost = shares * fill_price
                        fee = cost * self.commission
                        cash -= cost + fee
                        position = shares
                        entry_price = fill_price
                        trades.append(
                            Trade(
                                date=trade_date,
                                action="BUY",
                                price=round(fill_price, 3),
                                shares=shares,
                                fee=round(fee, 2),
                                cash_after=round(cash, 2),
                            )
                        )
                elif signal == -1 and position > 0:
                    # 卖单往下让价，同样按冲击成本
                    position_value = position * price
                    slip = self._impact_cost(position_value, float(series.get("amount") or 0))
                    fill_price = price * (1.0 - slip)
                    revenue = position * fill_price
                    fee = revenue * (self.commission + self.stamp_duty)
                    cash += revenue - fee
                    trades.append(
                        Trade(
                            date=trade_date,
                            action="SELL",
                            price=round(fill_price, 3),
                            shares=position,
                            fee=round(fee, 2),
                            cash_after=round(cash, 2),
                        )
                    )
                    position = 0
                    entry_price = None

            prev_row = series

        final_price = float(enriched.iloc[-1]["close"])
        final_value = cash + position * final_price
        total_return = final_value / self.initial_capital - 1.0

        metrics = _curve_metrics(curve, self.initial_capital)
        wins, round_trips = _win_rate(trades)

        result = BacktestResult(
            ts_code=ts_code,
            strategy=strategy_cls.name,
            params=effective_params,
            start_date=start,
            end_date=end,
            initial_capital=self.initial_capital,
            final_value=final_value,
            total_return_pct=round(total_return * 100.0, 2),
            annualized_return_pct=metrics["annualized_return_pct"],
            max_drawdown_pct=metrics["max_drawdown_pct"],
            sharpe=metrics["sharpe"],
            trades_count=len(trades),
            win_rate_pct=round(wins / round_trips * 100.0, 2) if round_trips else None,
            exposure_pct=round(bars_in_market / max(len(curve), 1) * 100.0, 2),
            equity_curve=_downsample(curve, curve_points),
            trades=[_trade_dict(t) for t in trades[-12:]] if include_trades else [],
            all_trades=[_trade_dict(t) for t in trades],
            data_asof=str(enriched.iloc[-1]["trade_date"]),
            data_start=str(enriched.iloc[0]["trade_date"]),
            provider=str(getattr(self.provider, "name", "") or ""),
            bars=int(len(enriched)),
            cost_model=self.cost_model(),
            skipped=dict(self.skipped),
        )

        if benchmark:
            result.benchmark = self._benchmark(benchmark, start, end, total_return)

        # 双边成本低于下限时明确告警：这样的回测在真实市场里不成立
        if self.cost_model()["round_trip"] < MIN_ROUND_TRIP_COST:
            result.warnings.append(
                f"双边交易成本仅 {self.cost_model()['round_trip']:.2%}，低于 "
                f"{MIN_ROUND_TRIP_COST:.2%} 的经验下限，收益可能被高估。"
            )
        if self.skipped:
            result.warnings.append(
                "有成交被拒绝："
                + "、".join(f"{key}={value}" for key, value in self.skipped.items())
            )
        return result

    # ------------------------------------------------------------------
    def _benchmark(
        self, ts_code: str, start: str, end: str, strategy_return: float
    ) -> dict[str, Any]:
        try:
            frame = self.provider.index_daily(ts_code, start, end)
        except Exception as exc:  # noqa: BLE001 - benchmark is best-effort
            return {"ts_code": ts_code, "error": str(exc)}
        if frame is None or frame.empty:
            return {"ts_code": ts_code, "error": "no benchmark data"}
        frame = frame.sort_values("trade_date", ascending=True)
        first = float(frame.iloc[0]["close"])
        last = float(frame.iloc[-1]["close"])
        benchmark_return = (last / first - 1.0) * 100.0
        return {
            "ts_code": ts_code,
            "return_pct": round(benchmark_return, 2),
            "excess_return_pct": round(strategy_return * 100.0 - benchmark_return, 2),
        }


# ----------------------------------------------------------------------
def _downsample(curve: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or len(curve) <= limit:
        return curve
    step = len(curve) / float(limit)
    sampled = [curve[min(int(index * step), len(curve) - 1)] for index in range(limit)]
    if sampled[-1] is not curve[-1]:
        sampled[-1] = curve[-1]
    return sampled


def _curve_metrics(curve: list[dict[str, Any]], initial: float) -> dict[str, Any]:
    if len(curve) < 2:
        return {"annualized_return_pct": 0.0, "max_drawdown_pct": 0.0, "sharpe": None}

    values = np.array([point["value"] for point in curve], dtype=float)
    running_max = np.maximum.accumulate(values)
    drawdowns = (running_max - values) / np.where(running_max == 0, 1.0, running_max)
    max_drawdown = float(drawdowns.max()) if drawdowns.size else 0.0

    returns = np.diff(values) / np.where(values[:-1] == 0, 1.0, values[:-1])
    trading_days = len(values)
    years = max(trading_days / 252.0, 1e-9)
    growth = values[-1] / initial if initial else 0.0
    annualized = (growth ** (1.0 / years) - 1.0) * 100.0 if growth > 0 else -100.0

    sharpe: float | None = None
    if returns.size > 1:
        std = float(returns.std(ddof=1))
        if std > 1e-12:
            sharpe = round(float(returns.mean()) / std * math.sqrt(252.0), 3)

    return {
        "annualized_return_pct": round(float(annualized), 2),
        "max_drawdown_pct": round(max_drawdown * 100.0, 2),
        "sharpe": sharpe,
    }


def _win_rate(trades: list[Trade]) -> tuple[int, int]:
    """用简单的 FIFO 买卖配对统计盈利的完整交易。"""

    wins = 0
    round_trips = 0
    buy_price: float | None = None
    for trade in trades:
        if trade.action == "BUY":
            buy_price = trade.price
        elif trade.action == "SELL" and buy_price is not None:
            round_trips += 1
            if trade.price > buy_price:
                wins += 1
            buy_price = None
    return wins, round_trips


def _trade_dict(trade: Trade) -> dict[str, Any]:
    return {
        "date": trade.date,
        "action": trade.action,
        "price": trade.price,
        "shares": trade.shares,
        "fee": trade.fee,
        "cash_after": trade.cash_after,
    }
