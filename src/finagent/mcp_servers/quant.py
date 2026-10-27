"""MCP 服务器：量化分析。

把本项目最有价值的三块能力——技术指标、选股器、回测引擎——作为独立 MCP 工具暴露，
任何 MCP 客户端都能直接复用，不必 import Python。

用 ``finagent-mcp-quant`` 或 ``python -m finagent.mcp_servers quant`` 启动。
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from finagent.core import calendar as cal
from finagent.core.backtest import BacktestEngine, describe_strategies
from finagent.core.backtest.registry import get_strategy_class
from finagent.core.indicators import (
    DEFAULT_INDICATORS,
    compute,
    snapshot,
)
from finagent.core.indicators import (
    detect_patterns as detect_patterns_core,
)
from finagent.core.screener import ScreenCriteria, long_tail, screen
from finagent.core.symbols import resolve_strict
from finagent.errors import InvalidArgument
from finagent.mcp_servers._common import (
    error_boundary,
    ok,
    provider,
    serve,
    settings,
)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("MCP 服务器需要 'mcp' 包（>=1.9,<2）。") from exc


INSTRUCTIONS = """\
Fin-Agent 量化服务器。

三组能力：
* `compute_indicators` / `detect_patterns` —— 在日线上计算 MACD、RSI、KDJ、BOLL、
  ATR、ADX、CCI、Williams %R、随机指标、OBV 与 VWAP。
* `screen_stocks` / `get_long_tail_stocks` —— 基本面 + 技术面选股。
  市值阈值的单位是**亿元**。
* `list_strategies` / `run_backtest` —— 23 个内置策略，输出收益、回撤、夏普、胜率，
  并可选与基准指数对比。

每个结果都是 `{"ok": true, "data": ...}` 或 `{"ok": false, "error": {...}}`。
"""

#: 允许按名字请求的指标。
INDICATOR_NAMES = (
    "macd",
    "rsi",
    "kdj",
    "boll",
    "atr",
    "adx",
    "cci",
    "williams_r",
    "stochastic",
    "obv",
    "vwap",
)


def build_server() -> FastMCP:
    mcp = FastMCP("finagent-quant", instructions=INSTRUCTIONS, log_level="WARNING")

    # ------------------------------------------------------------------
    # 技术指标
    # ------------------------------------------------------------------
    @mcp.tool()
    @error_boundary
    def compute_indicators(
        ts_code: Annotated[str, Field(description="标的或名称，例如 '600519.SH' 或 '茅台'。")],
        indicators: Annotated[
            list[str] | None,
            Field(description="要计算哪些指标；省略则默认 MACD/RSI/KDJ/BOLL。"),
        ] = None,
        end_date: Annotated[
            str | None,
            Field(description="锚定日期 'YYYYMMDD'，默认最新交易日。"),
        ] = None,
        rows: Annotated[int, Field(description="返回最近多少根 K 线（最多 60）。")] = 5,
        params: Annotated[
            dict[str, Any] | None,
            Field(description='按指标覆盖参数，例如 {"macd": {"fast_period": 8}}。'),
        ] = None,
    ) -> str:
        """在日线上计算技术指标。

        可用指标：macd、rsi、kdj、boll、atr、adx、cci、williams_r、stochastic、
        obv、vwap。返回最近的若干根 K 线，按时间从新到旧。
        """
        code, name = resolve_strict(ts_code)
        requested = [str(item).lower() for item in (indicators or DEFAULT_INDICATORS)]
        unknown = [item for item in requested if item not in INDICATOR_NAMES]
        if unknown:
            raise InvalidArgument(
                f"未知指标：{', '.join(unknown)}。",
                hint=f"可用指标：{', '.join(INDICATOR_NAMES)}",
            )

        anchor = (
            cal.format_date(end_date) if end_date else cal.format_date(cal.latest_trading_day())
        )
        warm_start = cal.warmup_start(anchor, 400)
        frame = provider().daily_price(code, warm_start, anchor)
        if frame is None or frame.empty:
            raise InvalidArgument(f"{code} 在 {anchor} 之前没有行情数据。")

        enriched = compute(frame, requested, params=params)
        latest_rows = snapshot(enriched, rows=min(int(rows), 60))
        return ok(
            {
                "ts_code": code,
                "name": name,
                "as_of": latest_rows[0]["trade_date"] if latest_rows else None,
                "indicators": requested,
                "rows": latest_rows,
                "count": len(latest_rows),
            }
        )

    @mcp.tool()
    @error_boundary
    def detect_patterns(
        ts_code: Annotated[str, Field(description="标的或名称。")],
        end_date: Annotated[str | None, Field(description="锚定日期 'YYYYMMDD'。")] = None,
    ) -> str:
        """识别最新一根 K 线上的技术形态。

        报告 MACD/KDJ 金叉死叉、RSI/KDJ 超买超卖、布林带上下轨突破，
        并给出每个指标的多空倾向。
        """
        code, name = resolve_strict(ts_code)
        anchor = (
            cal.format_date(end_date) if end_date else cal.format_date(cal.latest_trading_day())
        )
        frame = provider().daily_price(code, cal.warmup_start(anchor, 400), anchor)
        if frame is None or frame.empty:
            raise InvalidArgument(f"{code} 在 {anchor} 之前没有行情数据。")
        result = detect_patterns_core(frame)
        return ok(
            {
                "ts_code": code,
                "name": name,
                **result,
                "pattern_count": len(result.get("patterns", [])),
            }
        )

    # ------------------------------------------------------------------
    # 选股
    # ------------------------------------------------------------------
    @mcp.tool()
    @error_boundary
    def screen_stocks(
        pe_min: Annotated[float | None, Field(description="市盈率（TTM）下限。")] = None,
        pe_max: Annotated[float | None, Field(description="市盈率（TTM）上限。")] = None,
        pb_min: Annotated[float | None, Field(description="市净率下限。")] = None,
        pb_max: Annotated[float | None, Field(description="市净率上限。")] = None,
        mv_min: Annotated[float | None, Field(description="总市值下限，单位亿元。")] = None,
        mv_max: Annotated[float | None, Field(description="总市值上限，单位亿元。")] = None,
        turnover_min: Annotated[float | None, Field(description="换手率下限（%）。")] = None,
        turnover_max: Annotated[float | None, Field(description="换手率上限（%）。")] = None,
        pct_chg_min: Annotated[float | None, Field(description="当日涨跌幅下限（%）。")] = None,
        pct_chg_max: Annotated[float | None, Field(description="当日涨跌幅上限（%）。")] = None,
        industries: Annotated[
            list[str] | None, Field(description="包含的行业，例如 ['银行','白酒']。")
        ] = None,
        exclude_industries: Annotated[list[str] | None, Field(description="要排除的行业。")] = None,
        require_profit: Annotated[bool, Field(description="只保留盈利标的（PE > 0）。")] = True,
        sort_by: Annotated[
            str,
            Field(
                description="排序字段：total_mv、pe、pb、pct_chg、turnover_rate、amount、close，或 none。"
            ),
        ] = "total_mv",
        ascending: Annotated[
            bool | None, Field(description="排序方向；省略则用该字段的默认方向。")
        ] = None,
        limit: Annotated[int, Field(description="最多返回多少行（上限 100）。")] = 20,
    ) -> str:
        """按基本面与价格表现筛选股票。

        把自然语言条件翻译成过滤参数：「PE 小于 15、市值大于 500 亿的银行股」
        对应 pe_max=15、mv_min=500、industries=['银行']。
        """
        criteria = ScreenCriteria(
            pe_min=pe_min,
            pe_max=pe_max,
            pb_min=pb_min,
            pb_max=pb_max,
            mv_min=mv_min,
            mv_max=mv_max,
            turnover_min=turnover_min,
            turnover_max=turnover_max,
            pct_chg_min=pct_chg_min,
            pct_chg_max=pct_chg_max,
            industries=list(industries or []),
            exclude_industries=list(exclude_industries or []),
            require_profit=require_profit,
            sort_by=sort_by,
            ascending=ascending,
            limit=min(int(limit), 100),
        )
        result = screen(provider(), criteria)
        result["provider"] = settings().provider
        return ok(result)

    @mcp.tool()
    @error_boundary
    def get_long_tail_stocks(
        mv_min: Annotated[float, Field(description="总市值下限，单位亿元。")] = 10.0,
        mv_max: Annotated[float, Field(description="总市值上限，单位亿元。")] = 200.0,
        pe_max: Annotated[float, Field(description="市盈率上限。")] = 40.0,
        pb_max: Annotated[float, Field(description="市净率上限。")] = 5.0,
        turnover_max: Annotated[
            float, Field(description="换手率上限（%）——「被忽视」意味着换手低。")
        ] = 3.0,
        check_consolidation: Annotated[
            bool, Field(description="要求近 60 日价格变异系数 < 15%（长期横盘）。")
        ] = False,
        check_volume_spike: Annotated[
            bool, Field(description="要求最新成交量 ≥ 20 日均量的 2 倍（异动放量）。")
        ] = False,
        limit: Annotated[int, Field(description="最多返回多少行。")] = 20,
    ) -> str:
        """挖掘被忽视的中小市值标的：便宜、市值不大、换手低，可选「横盘」或「开始放量」。

        这是本项目招牌的「长尾」挖掘：找市场关注度低但估值合理的品种，
        可叠加横盘整理或成交量异动过滤。
        """
        result = long_tail(
            provider(),
            mv_min=mv_min,
            mv_max=mv_max,
            pe_max=pe_max,
            pb_max=pb_max,
            turnover_max=turnover_max,
            check_consolidation=check_consolidation,
            check_volume_spike=check_volume_spike,
            limit=min(int(limit), 60),
        )
        result["provider"] = settings().provider
        return ok(result)

    # ------------------------------------------------------------------
    # 回测
    # ------------------------------------------------------------------
    @mcp.tool()
    @error_boundary
    def list_strategies() -> str:
        """列出全部已注册的回测策略及一句话说明。"""
        strategies = describe_strategies()
        return ok({"count": len(strategies), "strategies": strategies})

    @mcp.tool()
    @error_boundary
    def run_backtest(
        ts_code: Annotated[str, Field(description="待回测的标的或名称。")],
        strategy: Annotated[
            str, Field(description="策略名，取自 list_strategies，例如 'ma_cross'。")
        ],
        start_date: Annotated[
            str | None, Field(description="起始日期 'YYYYMMDD'，默认一年前。")
        ] = None,
        end_date: Annotated[
            str | None, Field(description="结束日期 'YYYYMMDD'，默认今天。")
        ] = None,
        params: Annotated[
            dict[str, Any] | None,
            Field(description='策略参数，例如 {"short_window": 5, "long_window": 20}。'),
        ] = None,
        initial_capital: Annotated[float, Field(description="初始资金，单位元。")] = 100_000.0,
        benchmark: Annotated[
            str | None,
            Field(description="用于计算超额收益的基准指数代码，例如 '000001.SH'。"),
        ] = None,
        curve_points: Annotated[int, Field(description="权益曲线最多返回多少个点。")] = 120,
    ) -> str:
        """在指定区间内对单个标的回测一个策略。

        返回累计/年化收益、最大回撤、夏普、胜率、交易次数、降采样后的权益曲线，
        以及（可选）相对基准的超额收益。

        撮合假设：日线、按信号当日收盘成交、100 股整手、双边佣金 + 卖出 0.05%
        印花税，不建模滑点。
        """
        code, name = resolve_strict(ts_code)
        strategy_cls = get_strategy_class(strategy)
        if strategy_cls.requires_multi_asset:
            raise InvalidArgument(
                f"策略 '{strategy}' 需要多标的截面数据，单标的引擎无法运行。",
                hint="请从 list_strategies 里选择一个单标的策略。",
            )

        engine = BacktestEngine(provider(), initial_capital=float(initial_capital))
        result = engine.run(
            code,
            strategy,
            start_date=start_date,
            end_date=end_date,
            params=params,
            benchmark=benchmark,
            curve_points=max(0, min(int(curve_points), 400)),
        )
        payload = result.to_dict()
        payload["name"] = name
        payload["strategy_description"] = strategy_cls.description
        payload["provider"] = settings().provider
        return ok(payload)

    return mcp


def main() -> None:
    serve(build_server, prog="finagent-mcp-quant")
