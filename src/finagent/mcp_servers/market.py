"""MCP 服务器：行情数据。

把 provider 层（Tushare 或离线 fixture）包装成可复用的 MCP 工具。
用 ``finagent-mcp-market`` 或 ``python -m finagent.mcp_servers market`` 启动。

所有工具都返回 ``_common`` 里描述的 ``{"ok": ...}`` 信封。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field

from finagent.core import calendar as cal
from finagent.core.symbols import (
    INDICES,
    UNIVERSE,
    all_industries,
    resolve,
    resolve_strict,
)
from finagent.errors import InvalidArgument
from finagent.mcp_servers._common import (
    DEFAULT_MAX_ROWS,
    error_boundary,
    frame_payload,
    ok,
    provider,
    serve,
    settings,
)

try:  # FastMCP 由 mcp>=1.9 提供
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("MCP 服务器需要 'mcp' 包（>=1.9,<2）。") from exc


INSTRUCTIONS = """\
Fin-Agent 行情数据服务器。

提供 A 股基础资料、行情、估值、财务、指数、宏观序列，以及跨市场（港股/美股/ETF/
可转债/期货）报价。

约定：
* 日期一律是 `YYYYMMDD` 字符串；省略时使用合理的默认区间。
* 金额沿用 Tushare 口径：`total_mv`/`circ_mv` 单位万元，`amount` 单位千元，
  `vol` 单位手（100 股）。选股类工具另有以「亿元」为单位的 `total_mv_yi`。
* `resolve_symbol` 同时接受中文名、6 位裸代码和完整 ts_code；其余工具请传它返回的
  `ts_code`。
* 每个结果都是 `{"ok": true, "data": ...}` 或 `{"ok": false, "error": {...}}`。
"""


def build_server() -> FastMCP:
    mcp = FastMCP("finagent-market", instructions=INSTRUCTIONS, log_level="WARNING")

    # ------------------------------------------------------------------
    # 元信息
    # ------------------------------------------------------------------
    @mcp.tool()
    @error_boundary
    def get_current_time() -> str:
        """获取当前日期时间与交易时段状态。

        任何涉及「今天」「最新」「本周」等时间敏感的问题，都必须先调用它，
        以便正确锚定日期区间。
        """
        now = datetime.now()
        moment = now
        latest = cal.latest_trading_day()
        return ok(
            {
                "now": moment.strftime("%Y-%m-%d %H:%M:%S"),
                "date": moment.strftime("%Y%m%d"),
                "weekday": moment.strftime("%A"),
                "is_trading_day": cal.is_trading_day(moment.date()),
                "is_trading_now": cal.is_trading_now(moment),
                "latest_trading_day": cal.format_date(latest),
                "previous_trading_day": cal.format_date(cal.previous_trading_day(latest)),
                "provider": settings().provider,
            }
        )

    @mcp.tool()
    @error_boundary
    def resolve_symbol(
        query: Annotated[
            str,
            Field(
                description="可以是中文名（'贵州茅台'、'茅台'、'招行'）、6 位裸代码（'600519'）或完整 ts_code（'600519.SH'）。"
            ),
        ],
    ) -> str:
        """把用户输入解析成规范的 ts_code。

        用户提到公司名而不是代码时就调用它，然后把返回的 `ts_code` 传给其他工具。
        """
        symbol = resolve(query)
        if symbol is None:
            # 语法合法但不在参考库里的代码允许透传，交给 provider 直接查询。
            code, name = resolve_strict(query)
            return ok(
                {
                    "ts_code": code,
                    "name": name,
                    "industry": None,
                    "in_reference_universe": False,
                    "note": "不在内置参考库中，将直接向数据源查询。",
                }
            )
        return ok(
            {
                "ts_code": symbol.ts_code,
                "name": symbol.name,
                "industry": symbol.industry,
                "market": symbol.market,
                "in_reference_universe": True,
                "query": query,
            }
        )

    @mcp.tool()
    @error_boundary
    def list_supported_symbols(
        industry: Annotated[str | None, Field(description="可选的行业过滤，例如 '银行'。")] = None,
    ) -> str:
        """列出内置参考标的库（离线/演示的覆盖范围）。

        没有 Tushare token 时，用它来发现可用的示例标的。
        """
        rows = [
            {
                "ts_code": symbol.ts_code,
                "name": symbol.name,
                "industry": symbol.industry,
                "market": symbol.market,
                "kind": "stock",
            }
            for symbol in UNIVERSE
        ] + [
            {
                "ts_code": symbol.ts_code,
                "name": symbol.name,
                "industry": symbol.industry,
                "market": symbol.market,
                "kind": "index",
            }
            for symbol in INDICES
        ]
        if industry:
            needle = str(industry)
            rows = [row for row in rows if needle in str(row["industry"])]
        return ok(
            {
                "count": len(rows),
                "industries": all_industries(),
                "symbols": rows,
                "provider": settings().provider,
            }
        )

    # ------------------------------------------------------------------
    # 基础资料
    # ------------------------------------------------------------------
    @mcp.tool()
    @error_boundary
    def get_stock_basic(
        ts_code: Annotated[str | None, Field(description="ts_code，例如 '600519.SH'。")] = None,
        name: Annotated[str | None, Field(description="名称包含的子串，例如 '银行'。")] = None,
        limit: Annotated[int, Field(description="最多返回多少行。")] = 20,
    ) -> str:
        """个股基础资料：名称、行业、板块、交易所、上市日期。"""
        code = resolve_strict(ts_code)[0] if ts_code else None
        frame = provider().stock_basic(ts_code=code, name=name)
        return _table(frame, limit)

    @mcp.tool()
    @error_boundary
    def get_trade_calendar(
        start_date: Annotated[str | None, Field(description="起始日期 'YYYYMMDD'。")] = None,
        end_date: Annotated[str | None, Field(description="结束日期 'YYYYMMDD'。")] = None,
        limit: Annotated[int, Field(description="最多返回多少行。")] = 60,
    ) -> str:
        """交易所交易日历：哪些日期开市，以及它们各自的上一个交易日。"""
        anchor = cal.latest_trading_day()
        start = (
            cal.format_date(start_date)
            if start_date
            else cal.format_date(anchor - timedelta(days=30))
        )
        end = cal.format_date(end_date) if end_date else cal.format_date(anchor)
        if start > end:
            raise InvalidArgument(f"起始日期 {start} 晚于结束日期 {end}。")
        frame = provider().trade_calendar(start, end)
        return _table(frame, limit)

    # ------------------------------------------------------------------
    # 行情
    # ------------------------------------------------------------------
    @mcp.tool()
    @error_boundary
    def get_realtime_price(
        ts_code: Annotated[str, Field(description="标的或名称，例如 '600519.SH' 或 '茅台'。")],
    ) -> str:
        """最新报价快照（价格、涨跌额、涨跌幅、最高/最低、成交量、交易日）。

        在 `tushare` 模式下取最新日线；在 `offline` 模式下由最后一根日线派生出
        确定性的一个 tick。
        """
        code, name = resolve_strict(ts_code)
        tick = provider().realtime_price(code)
        tick.setdefault("name", name)
        return ok(tick)

    @mcp.tool()
    @error_boundary
    def get_daily_price(
        ts_code: Annotated[str, Field(description="标的或名称。")],
        start_date: Annotated[
            str | None, Field(description="起始日期 'YYYYMMDD'，默认 90 天前。")
        ] = None,
        end_date: Annotated[
            str | None, Field(description="结束日期 'YYYYMMDD'，默认最新交易日。")
        ] = None,
        adj: Annotated[
            str | None,
            Field(description="复权方式：null/不复权、'qfq'/前复权、'hfq'/后复权。"),
        ] = None,
        limit: Annotated[int, Field(description="最多返回多少行（从新到旧）。")] = 60,
    ) -> str:
        """日线 OHLCV 历史，按时间从新到旧返回。

        走势、波动率，以及用户提出的任何区间统计都用它（如「最近一周」「过去一个月」）。
        画图或算收益通常用 `adj='qfq'`（前复权）。
        """
        code, name = resolve_strict(ts_code)
        window_start = (
            cal.format_date(start_date)
            if start_date
            else cal.format_date(cal.latest_trading_day() - timedelta(days=90))
        )
        window_end = (
            cal.format_date(end_date) if end_date else cal.format_date(cal.latest_trading_day())
        )
        frame = provider().daily_price(code, window_start, window_end, adj)
        payload = frame_payload(frame, limit=limit, newest_first=True)
        payload.update(
            {
                "ts_code": code,
                "name": name,
                "start_date": window_start,
                "end_date": window_end,
                "adj": adj or "none",
            }
        )
        if payload["rows"]:
            payload["first_close"] = payload["rows"][-1].get("close")
            payload["last_close"] = payload["rows"][0].get("close")
            payload["change_pct"] = _pct_change(payload["first_close"], payload["last_close"])
        return ok(payload)

    @mcp.tool()
    @error_boundary
    def get_index_daily(
        ts_code: Annotated[
            str, Field(description="指数代码：'000001.SH'、'399001.SZ'、'000300.SH' 等。")
        ],
        start_date: Annotated[str | None, Field(description="起始日期 'YYYYMMDD'。")] = None,
        end_date: Annotated[str | None, Field(description="结束日期 'YYYYMMDD'。")] = None,
        limit: Annotated[int, Field(description="最多返回多少行（从新到旧）。")] = 60,
    ) -> str:
        """指数 OHLCV（上证指数、沪深300、创业板指等）。"""
        code, name = resolve_strict(ts_code)
        start = (
            cal.format_date(start_date)
            if start_date
            else cal.format_date(
                cal.previous_trading_day(cal.latest_trading_day()) - timedelta(days=180)
            )
        )
        end = cal.format_date(end_date) if end_date else cal.format_date(cal.latest_trading_day())
        frame = provider().index_daily(code, start, end)
        payload = frame_payload(frame, limit=limit, newest_first=True)
        payload.update({"ts_code": code, "name": name, "start_date": start, "end_date": end})
        return ok(payload)

    # ------------------------------------------------------------------
    # 基本面
    # ------------------------------------------------------------------
    @mcp.tool()
    @error_boundary
    def get_daily_basic(
        ts_code: Annotated[str, Field(description="标的或名称。")],
        start_date: Annotated[str | None, Field(description="起始日期 'YYYYMMDD'。")] = None,
        end_date: Annotated[str | None, Field(description="结束日期 'YYYYMMDD'。")] = None,
        limit: Annotated[int, Field(description="最多返回多少行（从新到旧）。")] = 20,
    ) -> str:
        """估值指标：PE、PE(TTM)、PB、PS、总市值/流通市值、换手率。

        回答「贵不贵 / 便宜吗」这类问题用它。`total_mv` 单位是万元，
        同时给出的 `summary.total_mv_yi` 已换算成亿元。
        """
        code, name = resolve_strict(ts_code)
        start = (
            cal.format_date(start_date)
            if start_date
            else cal.format_date(
                cal.previous_trading_day(cal.latest_trading_day()) - timedelta(days=180)
            )
        )
        end = cal.format_date(end_date) if end_date else cal.format_date(cal.latest_trading_day())
        frame = provider().daily_basic(code, start, end)
        payload = frame_payload(frame, limit=limit, newest_first=True)
        payload.update({"ts_code": code, "name": name})
        if payload["rows"]:
            latest = payload["rows"][0]
            payload["summary"] = {
                "trade_date": latest.get("trade_date"),
                "pe": latest.get("pe"),
                "pb": latest.get("pb"),
                "total_mv_yi": _to_yi(latest.get("total_mv")),
            }
        return ok(payload)

    @mcp.tool()
    @error_boundary
    def get_income_statement(
        ts_code: Annotated[str, Field(description="标的或名称。")],
        start_date: Annotated[str | None, Field(description="报告期起始 'YYYYMMDD'。")] = None,
        end_date: Annotated[str | None, Field(description="报告期结束 'YYYYMMDD'。")] = None,
        limit: Annotated[int, Field(description="最多返回几个报告期（从新到旧）。")] = 8,
    ) -> str:
        """季度利润表：营业收入、营业利润、净利润、每股收益。

        金额单位见返回里的 `unit_note`：离线模式下是亿元，Tushare 模式下沿用原始单位。
        引用数字前先确认单位。
        """
        code, name = resolve_strict(ts_code)
        frame = provider().income_statement(code, start_date, end_date)
        payload = frame_payload(frame, limit=limit, newest_first=True)
        payload.update(
            {
                "ts_code": code,
                "name": name,
                "unit_note": "离线模式下 revenue / n_income 单位为亿元；Tushare 模式为原始单位。",
            }
        )
        return ok(payload)

    @mcp.tool()
    @error_boundary
    def get_forecast(
        ts_code: Annotated[str, Field(description="标的或名称。")],
        start_date: Annotated[str | None, Field(description="公告起始日期 'YYYYMMDD'。")] = None,
        end_date: Annotated[str | None, Field(description="公告结束日期 'YYYYMMDD'。")] = None,
        limit: Annotated[int, Field(description="最多返回多少行。")] = 10,
    ) -> str:
        """业绩预告：类型、预计同比变动区间、摘要。"""
        code, name = resolve_strict(ts_code)
        start = (
            cal.format_date(start_date)
            if start_date
            else cal.format_date(cal.today() - timedelta(days=365))
        )
        end = cal.format_date(end_date) if end_date else cal.format_date(cal.today())
        frame = provider().forecast(code, start, end)
        payload = frame_payload(frame, limit=limit)
        payload.update({"ts_code": code, "name": name})
        return ok(payload)

    # ------------------------------------------------------------------
    # 资金与事件
    # ------------------------------------------------------------------
    @mcp.tool()
    @error_boundary
    def get_moneyflow(
        ts_code: Annotated[str, Field(description="标的或名称。")],
        start_date: Annotated[
            str | None, Field(description="起始日期 'YYYYMMDD'，默认 30 天前。")
        ] = None,
        end_date: Annotated[str | None, Field(description="结束日期 'YYYYMMDD'。")] = None,
        limit: Annotated[int, Field(description="最多返回多少行（从新到旧）。")] = 20,
    ) -> str:
        """按单笔规模划分的资金流向：大单/特大单买卖金额、净流入。"""
        code, name = resolve_strict(ts_code)
        start = (
            cal.format_date(start_date)
            if start_date
            else cal.format_date(cal.today() - timedelta(days=30))
        )
        end = cal.format_date(end_date) if end_date else cal.format_date(cal.latest_trading_day())
        frame = provider().moneyflow(code, start, end)
        payload = frame_payload(frame, limit=limit)
        payload.update({"ts_code": code, "name": name})
        return ok(payload)

    @mcp.tool()
    @error_boundary
    def get_limit_list(
        trade_date: Annotated[str | None, Field(description="'YYYYMMDD'，默认最新交易日。")] = None,
        limit: Annotated[int, Field(description="最多返回多少行。")] = 40,
    ) -> str:
        """某个交易日的涨跌停板清单。"""
        target = (
            cal.format_date(trade_date) if trade_date else cal.format_date(cal.latest_trading_day())
        )
        frame = provider().limit_list(target)
        payload = frame_payload(frame, limit=limit)
        payload["trade_date"] = target
        return ok(payload)

    @mcp.tool()
    @error_boundary
    def get_top_list(
        trade_date: Annotated[str | None, Field(description="'YYYYMMDD'，默认最新交易日。")] = None,
        limit: Annotated[int, Field(description="最多返回多少行。")] = 20,
    ) -> str:
        """某个交易日的龙虎榜排行。"""
        target = (
            cal.format_date(trade_date) if trade_date else cal.format_date(cal.latest_trading_day())
        )
        frame = provider().top_list(target)
        payload = frame_payload(frame, limit=limit)
        payload["trade_date"] = target
        return ok(payload)

    @mcp.tool()
    @error_boundary
    def get_concept_detail(
        concept_name: Annotated[
            str | None, Field(description="概念名，例如 '白酒'、'人工智能'。")
        ] = None,
        ts_code: Annotated[
            str | None, Field(description="另一种用法：查某只股票属于哪些概念。")
        ] = None,
        limit: Annotated[int, Field(description="最多返回多少行。")] = 30,
    ) -> str:
        """概念/题材成分：某个概念包含哪些股票。"""
        code = resolve_strict(ts_code)[0] if ts_code else None
        if not concept_name and not code:
            raise InvalidArgument("请提供 concept_name 或 ts_code 之一。")
        frame = provider().concept_detail(concept_name, code)
        payload = frame_payload(frame, limit=limit)
        payload.update({"concept_name": concept_name, "ts_code": code})
        return ok(payload)

    # ------------------------------------------------------------------
    # 宏观
    # ------------------------------------------------------------------
    @mcp.tool()
    @error_boundary
    def get_macro(
        indicator: Annotated[
            Literal["gdp", "cpi", "m2", "interest_rate"],
            Field(description="要取哪条宏观序列。"),
        ],
        limit: Annotated[int, Field(description="最多返回多少个观测值（从新到旧）。")] = 12,
    ) -> str:
        """宏观序列：GDP、CPI、M2 货币供应量，或利率（Shibor/LPR）。"""
        frame = provider().macro(indicator)
        payload = frame_payload(frame, limit=limit)
        payload["indicator"] = indicator
        return ok(payload)

    @mcp.tool()
    @error_boundary
    def get_global_index_comparison() -> str:
        """跨市场指数表现快照（近 1 日 / 5 日 / 20 日 / 年初至今）。"""
        provider_instance = provider()
        if hasattr(provider_instance, "global_index_comparison"):
            frame = provider_instance.global_index_comparison()  # type: ignore[attr-defined]
            return _table(frame, DEFAULT_MAX_ROWS)
        return _table(provider_instance.index_daily("000001.SH"), DEFAULT_MAX_ROWS)

    # ------------------------------------------------------------------
    # 跨市场
    # ------------------------------------------------------------------
    @mcp.tool()
    @error_boundary
    def get_asset_basic(
        asset: Annotated[
            Literal["hk", "us", "etf", "cb", "futures"],
            Field(description="hk=港股，us=美股，etf=基金，cb=可转债，futures=商品/股指期货。"),
        ],
        keyword: Annotated[str | None, Field(description="可选的代码/名称过滤。")] = None,
        limit: Annotated[int, Field(description="最多返回多少行。")] = 20,
    ) -> str:
        """某一类非 A 股资产的标的目录。"""
        frame = provider().asset_basic(asset, keyword)
        payload = frame_payload(frame, limit=limit)
        payload["asset"] = asset
        return ok(payload)

    @mcp.tool()
    @error_boundary
    def get_asset_daily(
        asset: Annotated[
            Literal["hk", "us", "etf", "cb", "futures"],
            Field(description="与 ts_code 对应的资产类别。"),
        ],
        ts_code: Annotated[
            str, Field(description="标的代码，例如 '00700.HK'、'AAPL.O'、'510300.SH'。")
        ],
        start_date: Annotated[str | None, Field(description="起始日期 'YYYYMMDD'。")] = None,
        end_date: Annotated[str | None, Field(description="结束日期 'YYYYMMDD'。")] = None,
        limit: Annotated[int, Field(description="最多返回多少行（从新到旧）。")] = 30,
    ) -> str:
        """港股/美股/ETF/可转债/期货的日线行情。"""
        end = cal.format_date(end_date) if end_date else cal.format_date(cal.latest_trading_day())
        start = (
            cal.format_date(start_date)
            if start_date
            else cal.format_date(cal.latest_trading_day() - timedelta(days=180))
        )
        frame = provider().asset_daily(asset, ts_code, start, end)
        payload = frame_payload(frame, limit=limit, newest_first=True)
        payload.update({"asset": asset, "ts_code": ts_code})
        return ok(payload)

    return mcp


# ----------------------------------------------------------------------
def _table(frame: Any, limit: int) -> str:
    payload = frame_payload(frame, limit=limit)
    return ok(payload)


def _pct_change(first: Any, last: Any) -> float | None:
    try:
        first_value = float(first)
        last_value = float(last)
    except (TypeError, ValueError):
        return None
    if first_value == 0:
        return None
    return round((last_value / first_value - 1.0) * 100.0, 2)


def _to_yi(value: Any) -> float | None:
    try:
        return round(float(value) / 10_000.0, 2)
    except (TypeError, ValueError):
        return None


def main() -> None:
    serve(build_server, prog="finagent-mcp-market")
