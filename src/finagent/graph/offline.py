"""确定性专家实现（无 LLM 路径）。

当 ``FINAGENT_LLM_PROVIDER=none`` 时，图仍然跑完整的 Supervisor → 专家 → 记忆拓扑；
只是把*推理*这一步换成手写流程，而它调的是同一套 MCP 工具。好处：

* 没有 API Key、没有 token、没有网络也能演示和测试整套系统；
* 工具层以与生产完全一致的方式被实际使用；
* 调提示词时，维护者可以对照「确定性路径做了什么」和「LLM 做了什么」。

这些流程刻意写得直白：用 :mod:`finagent.graph.intents` 从请求里抽实体，调对的工具，
渲染 Markdown。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from finagent.core import calendar as cal
from finagent.core.indicators import PATTERN_LABELS_ZH
from finagent.core.symbols import display_name, resolve
from finagent.graph.intents import (
    extract_alert_spec,
    extract_screen_criteria,
    extract_strategy,
    extract_symbols,
    extract_window,
)
from finagent.graph.state import AGENT_LABELS
from finagent.graph.tooling import ToolRunner


# ----------------------------------------------------------------------
# 格式化辅助
# ----------------------------------------------------------------------
def money(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def pct(value: Any) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def magnitude(value: Any) -> str:
    """不带符号的百分比，用于回撤这类「加号会误导」的数值。"""

    try:
        return f"{abs(float(value)):.2f}%"
    except (TypeError, ValueError):
        return "—"


def rate(value: Any) -> str:
    """不带符号的百分比，用于胜率、持仓占比这类比率。"""

    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "—"


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_没有匹配结果。_"
    head = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = "\n".join("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join([head, sep, body])


@dataclass(slots=True)
class ExpertResult:
    answer: str
    agent: str
    artifacts: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# 意图辅助
# ----------------------------------------------------------------------
_MACRO_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("gdp", "国内生产总值", "经济增长"), "gdp"),
    (("cpi", "通胀", "物价", "居民消费价格"), "cpi"),
    (("m2", "货币供应", "m1"), "m2"),
    (("利率", "shibor", "lpr", "降息", "加息", "资金价格"), "interest_rate"),
)

_INDEX_HINTS = ("指数", "大盘", "上证", "深证", "创业板指", "沪深300", "中证500", "沪指")


def _macro_indicator(question: str) -> str | None:
    lowered = question.lower()
    for needles, indicator in _MACRO_RULES:
        if any(needle in lowered for needle in needles):
            return indicator
    return None


def _wants_index(question: str) -> bool:
    return any(hint in question for hint in _INDEX_HINTS)


# ----------------------------------------------------------------------
# 1a. 基本面分析专家（估值 / 财报 / 宏观 / 指数）
# ----------------------------------------------------------------------
async def analyse_fundamental(
    question: str, runner: ToolRunner, *, critique: dict[str, Any] | None = None
) -> ExpertResult:
    """只看估值、财务、宏观与指数——刻意不碰 K 线。"""

    artifacts: dict[str, Any] = {}
    errors: list[str] = []

    time_out = await runner.call("get_current_time")
    meta = time_out.data if time_out.ok else {}
    as_of = meta.get("latest_trading_day") or cal.format_date(cal.latest_trading_day())
    sections: list[str] = []

    # 宏观 -------------------------------------------------------------
    indicator = _macro_indicator(question)
    if indicator:
        out = await runner.call("get_macro", indicator=indicator, limit=8)
        if out.ok:
            rows = out.data.get("rows", [])
            headers = {
                "gdp": ["季度", "GDP(亿元)", "同比"],
                "cpi": ["月份", "CPI指数", "同比"],
                "m2": ["月份", "M2(亿元)", "同比", "M1同比"],
                "interest_rate": ["月份", "Shibor隔夜", "Shibor1Y", "LPR1Y", "LPR5Y"],
            }[indicator]
            keys = {
                "gdp": ["quarter", "gdp", "gdp_yoy"],
                "cpi": ["month", "cpi", "cpi_yoy"],
                "m2": ["month", "m2", "m2_yoy", "m1_yoy"],
                "interest_rate": ["month", "shibor_on", "shibor_1y", "lpr_1y", "lpr_5y"],
            }[indicator]
            table = md_table(headers, [[str(row.get(key, "—")) for key in keys] for row in rows])
            sections.append(f"### 宏观数据（{indicator}）\n\n{table}")
            artifacts["macro"] = {"indicator": indicator, "rows": rows}
        else:
            errors.append(out.error or "macro unavailable")
        return _result(
            "\n\n".join(sections) or _failure_message(errors),
            "fundamental_analyst",
            artifacts,
            errors,
        )

    # 指数 -------------------------------------------------------------
    symbols = extract_symbols(question)
    if _wants_index(question) and not symbols:
        symbols = ["000001.SH"]

    if not symbols:
        return _result(
            "请告诉我要看基本面的具体标的（例如：贵州茅台、600519.SH）。",
            "fundamental_analyst",
            artifacts,
            errors,
        )

    for code in symbols[:3]:
        block, block_artifacts, block_errors = await _fundamental_one(code, runner)
        errors.extend(block_errors)
        if block:
            sections.append(block)
        artifacts.setdefault("symbols", {}).update(block_artifacts)

    label = AGENT_LABELS.get(runner.agent, runner.agent)
    header = f"_数据截至 {as_of}（{label}，确定性分析器）_\n"
    body = header + "\n\n".join(sections)
    return _result(body, "fundamental_analyst", artifacts, errors)


async def _fundamental_one(code: str, runner: ToolRunner) -> tuple[str, dict[str, Any], list[str]]:
    errors: list[str] = []
    facts: dict[str, Any] = {}
    symbol = resolve(code)
    head = f"### {symbol.name if symbol else display_name(code)}（{code}）基本面"

    valuation = await runner.call("get_daily_basic", ts_code=code, limit=1)
    valuation_line = None
    if valuation.ok:
        latest = (valuation.data.get("rows") or [{}])[0]
        facts["valuation"] = latest
        mv = latest.get("total_mv")
        mv_yi = f"{float(mv) / 10000.0:,.1f} 亿元" if isinstance(mv, (int, float)) else "—"
        valuation_line = (
            f"- 估值：PE **{money(latest.get('pe'))}** / PB **{money(latest.get('pb'))}**"
            f" / 总市值 {mv_yi}"
        )
    else:
        errors.append(f"{code}: {valuation.error}")

    income = await runner.call("get_income_statement", ts_code=code, limit=4)
    income_line = None
    if income.ok:
        rows = income.data.get("rows") or []
        if rows:
            latest = rows[0]
            facts["income"] = latest
            income_line = (
                f"- 财报：营收 {money(latest.get('revenue'))} 亿元"
                f" / 净利润 {money(latest.get('net_profit'))} 亿元"
                f"（报告期 {latest.get('end_date', '—')}）"
            )

    lines = [head, valuation_line, income_line]
    body = "\n".join(line for line in lines if line)
    return body, {code: facts}, errors


# ----------------------------------------------------------------------
# 1b. 技术面分析专家（量价 / 指标 / 形态 / 资金）
# ----------------------------------------------------------------------
async def analyse_technical(
    question: str, runner: ToolRunner, *, critique: dict[str, Any] | None = None
) -> ExpertResult:
    """只看量价、指标、形态与资金——刻意不碰财报与估值。"""

    artifacts: dict[str, Any] = {}
    errors: list[str] = []

    time_out = await runner.call("get_current_time")
    meta = time_out.data if time_out.ok else {}
    as_of = meta.get("latest_trading_day") or cal.format_date(cal.latest_trading_day())
    window = extract_window(question)
    sections: list[str] = []

    symbols = extract_symbols(question)
    if _wants_index(question) and not symbols:
        symbols = ["000001.SH"]

    if not symbols:
        return _result(
            "请告诉我要做技术分析的具体标的（例如：贵州茅台、600519.SH）。",
            "technical_analyst",
            artifacts,
            errors,
        )

    for code in symbols[:3]:
        block, block_artifacts, block_errors = await _technical_one(code, window, runner)
        errors.extend(block_errors)
        if block:
            sections.append(block)
        artifacts.setdefault("symbols", {}).update(block_artifacts)

    label = AGENT_LABELS.get(runner.agent, runner.agent)
    header = f"_数据截至 {as_of}（{label}，确定性分析器）_\n"
    body = header + "\n\n".join(sections)
    return _result(body, "technical_analyst", artifacts, errors)


async def _technical_one(
    code: str, window, runner: ToolRunner
) -> tuple[str, dict[str, Any], list[str]]:
    errors: list[str] = []
    facts: dict[str, Any] = {}
    symbol = resolve(code)
    head = f"### {symbol.name if symbol else display_name(code)}（{code}）技术面"

    quote = await runner.call("get_realtime_price", ts_code=code)
    price_line = None
    if quote.ok:
        data = quote.data
        facts["quote"] = data
        price_line = (
            f"- 最新价 **{money(data.get('price'), 3)}** 元"
            f"（较前收盘 {pct(data.get('pct_chg'))}，数据日 {data.get('trade_date')}）"
        )
    else:
        errors.append(f"{code}: {quote.error}")

    history = await runner.call(
        "get_daily_price", ts_code=code, start_date=window.start, end_date=window.end, limit=5
    )
    range_line = None
    if history.ok:
        data = history.data
        facts["window"] = {k: data.get(k) for k in ("start_date", "end_date", "change_pct")}
        range_line = (
            f"- {window.label}区间涨跌 **{pct(data.get('change_pct'))}**"
            f"（{data.get('start_date')} → {data.get('end_date')}）"
        )
    else:
        errors.append(f"{code}: {history.error}")

    indicators = await runner.call("compute_indicators", ts_code=code, rows=1)
    indicator_line = None
    if indicators.ok:
        rows = indicators.data.get("rows") or []
        if rows:
            latest = rows[0]
            facts["indicators"] = latest
            indicator_line = (
                f"- 技术：MACD DIF {money(latest.get('dif'), 3)} / DEA {money(latest.get('dea'), 3)}，"
                f"RSI {money(latest.get('rsi'))}，KDJ K {money(latest.get('k'))} / D {money(latest.get('d'))}"
            )

    patterns = await runner.call("detect_patterns", ts_code=code)
    pattern_line = None
    if patterns.ok:
        data = patterns.data
        facts["patterns"] = data
        detected = data.get("patterns") or []
        if detected:
            readable = [PATTERN_LABELS_ZH.get(item, item) for item in detected]
            pattern_line = "- 形态：" + "、".join(f"**{item}**" for item in readable)
        else:
            pattern_line = "- 形态：当前无明确的交叉或超买超卖形态"

    flow = await runner.call("get_moneyflow", ts_code=code, limit=3)
    flow_line = None
    if flow.ok:
        rows = flow.data.get("rows") or []
        if rows:
            latest = rows[0]
            facts["moneyflow"] = latest
            flow_line = f"- 资金：主力净流入 {money(latest.get('net_amount'))} 万元"

    lines = [head, price_line, range_line, indicator_line, pattern_line, flow_line]
    body = "\n".join(line for line in lines if line)

    if facts.get("patterns") and facts["patterns"].get("patterns"):
        names = [PATTERN_LABELS_ZH.get(p, p) for p in facts["patterns"]["patterns"]]
        body += f"\n\n**结论**：{code} 当前出现 {'、'.join(names)}，"
        body += "可作为关注信号，但需结合成交量和板块环境确认；技术信号不预测基本面。"
    return body, {code: facts}, errors


# ----------------------------------------------------------------------
# 2. 选股专家
# ----------------------------------------------------------------------
async def pick_stocks(question: str, runner: ToolRunner) -> ExpertResult:
    errors: list[str] = []
    artifacts: dict[str, Any] = {}
    meta = (await runner.call("get_current_time")).data or {}
    as_of = meta.get("latest_trading_day", "")

    if any(word in question for word in ("长尾", "冷门", "没人关注")):
        out = await runner.call(
            "get_long_tail_stocks",
            check_consolidation=("横盘" in question or "震荡" in question),
            check_volume_spike=("放量" in question or "异动" in question),
            limit=10,
        )
        mode = "长尾（被忽视的）标的"
    else:
        criteria = extract_screen_criteria(question)
        out = await runner.call("screen_stocks", **criteria)
        mode = "条件选股"

    if not out.ok:
        return _result(
            f"选股失败：{out.error}" + (f"\n\n建议：{out.hint}" if out.hint else ""),
            "stock_picker",
            artifacts,
            [out.error or "screen failed"],
        )

    data = out.data
    rows = data.get("results") or []
    artifacts["screen"] = {
        "criteria": data.get("criteria"),
        "matched": data.get("matched"),
        "results": rows,
    }

    if not rows:
        return _result(
            f"按当前条件（{data.get('criteria')}）在 {data.get('universe_size', '—')} 只标的中没有匹配结果。"
            "可以适当放宽 PE、市值或换手率条件。",
            "stock_picker",
            artifacts,
            errors,
        )

    table = md_table(
        ["代码", "名称", "行业", "最新价", "涨跌幅", "PE", "PB", "总市值(亿)"],
        [
            [
                row.get("ts_code", ""),
                row.get("name", ""),
                row.get("industry", ""),
                money(row.get("close"), 2),
                pct(row.get("pct_chg")),
                money(row.get("pe"), 2),
                money(row.get("pb"), 2),
                money(row.get("total_mv_yi"), 1),
            ]
            for row in rows
        ],
    )

    total = data.get("matched", len(rows))
    answer = (
        f"_数据截至 {as_of}，来源：{data.get('provider', 'offline')}_\n\n"
        f"**{mode}**：条件 `{data.get('criteria')}`，"
        f"在 {data.get('universe_size', '—')} 只标的中命中 **{total}** 只，展示前 {len(rows)} 只。\n\n"
        f"{table}\n\n"
        "**结论**：以上为条件筛选结果，不构成投资建议；请结合最新公告与流动性复核。"
    )
    return _result(answer, "stock_picker", artifacts, errors)


# ----------------------------------------------------------------------
# 3. 策略回测
# ----------------------------------------------------------------------
_STRATEGY_ZH = {
    "ma_cross": "双均线",
    "macd": "MACD",
    "rsi": "RSI",
    "kdj": "KDJ",
    "boll_breakout": "布林突破",
    "boll_reversion": "布林回归",
    "momentum_roc": "动量ROC",
    "donchian_breakout": "唐奇安通道",
    "turtle": "海龟",
    "adx_macd": "ADX+MACD",
    "triple_ma": "三均线",
    "ema_sma_bias": "EMA/SMA乖离",
    "cci": "CCI",
    "williams_r": "Williams%R",
    "stochastic": "随机指标",
    "rsi_ma200": "RSI+MA200",
    "volume_breakout": "放量突破",
    "obv_cross": "OBV交叉",
    "vwap_deviation": "VWAP偏离",
    "ma_cross_atr_stop": "双均线+ATR止损",
    "vol_target_ma_cross": "双均线+波动目标",
    "kelly_ma_cross": "双均线+固定仓位",
}


async def backtest(question: str, runner: ToolRunner) -> ExpertResult:
    errors: list[str] = []
    artifacts: dict[str, Any] = {}

    symbols = extract_symbols(question)
    if not symbols:
        return _result(
            "请告诉我要回测的标的（例如：中信证券、600030.SH）。",
            "backtest",
            artifacts,
            ["missing symbol"],
        )
    code = symbols[0]
    strategy, explicit = extract_strategy(question)
    window = extract_window(question)

    out = await runner.call(
        "run_backtest",
        ts_code=code,
        strategy=strategy,
        start_date=window.start,
        end_date=window.end,
        benchmark="000001.SH",
        curve_points=36,
    )
    if not out.ok:
        return _result(
            f"回测失败：{out.error}" + (f"\n\n建议：{out.hint}" if out.hint else ""),
            "backtest",
            artifacts,
            [out.error or "backtest failed"],
        )

    data = out.data
    artifacts["backtest"] = data
    symbol_name = resolve(code)
    benchmark = data.get("benchmark") or {}

    metrics = md_table(
        ["指标", "数值"],
        [
            ["策略", f"{_STRATEGY_ZH.get(strategy, strategy)}（`{strategy}`）"],
            ["区间", f"{data['period']['start']} → {data['period']['end']}"],
            ["累计收益", f"**{pct(data.get('total_return_pct'))}**"],
            ["年化收益", pct(data.get("annualized_return_pct"))],
            ["最大回撤", magnitude(data.get("max_drawdown_pct"))],
            ["夏普比率", money(data.get("sharpe"), 3)],
            [
                "胜率",
                rate(data.get("win_rate_pct")) if data.get("win_rate_pct") is not None else "—",
            ],
            ["交易次数", str(data.get("trades_count", 0))],
            ["持仓占比", rate(data.get("exposure_pct"))],
            ["基准收益", pct(benchmark.get("return_pct")) if benchmark else "—"],
            ["超额收益", pct(benchmark.get("excess_return_pct")) if benchmark else "—"],
        ],
    )

    trades = data.get("trades") or []
    trade_block = ""
    if trades:
        trade_block = "\n\n**最近成交**\n\n" + md_table(
            ["日期", "方向", "价格", "股数", "费用"],
            [
                [
                    trade.get("date", ""),
                    "买入" if trade.get("action") == "BUY" else "卖出",
                    money(trade.get("price"), 3),
                    str(trade.get("shares", "")),
                    money(trade.get("fee"), 2),
                ]
                for trade in trades[-5:]
            ],
        )

    notional = "（未识别到明确策略，默认使用双均线）" if not explicit else ""
    answer = (
        f"### {symbol_name.name if symbol_name else display_name(code)}（{code}）回测结果\n"
        f"{notional}\n\n{metrics}{trade_block}\n\n"
        "**说明**：日线收盘成交、100 股整手、双边佣金 + 卖出印花税；未建模滑点。"
        f"样本 {data.get('trades_count', 0)} 笔交易，"
        + (
            "统计意义有限，结论仅供参考。"
            if data.get("trades_count", 0) < 10
            else "样本量可用于初步比较。"
        )
    )
    return _result(answer, "backtest", artifacts, errors)


# ----------------------------------------------------------------------
# 4. 持仓管理
# ----------------------------------------------------------------------
_SHARES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:股|shares)", re.IGNORECASE)
_LOTS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*手")
_PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*元")
_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:万元|万)")


async def manage_portfolio(question: str, runner: ToolRunner) -> ExpertResult:
    errors: list[str] = []
    artifacts: dict[str, Any] = {}
    lowered = question.lower()

    symbols = extract_symbols(question)
    shares = _extract_shares(question)
    price = _extract_price(question)
    is_sell = any(word in lowered for word in ("卖出", "卖掉", "减仓", "清仓", "止盈", "止损卖出"))
    is_buy = any(word in lowered for word in ("买入", "买进", "加仓", "建仓", "买了", "补仓"))

    if symbols and is_buy:
        code = symbols[0]
        if shares is None and _AMOUNT_RE.search(question):
            budget = float(_AMOUNT_RE.search(question).group(1)) * 10_000.0
            quote = await runner.call("get_realtime_price", ts_code=code)
            if quote.ok:
                unit = float(quote.data.get("price") or 0)
                shares = int(budget / unit / 100) * 100 if unit > 0 else None
                price = price or unit
        if shares is None or price is None:
            return _result(
                "请补充买入的股数与价格，例如：我以 10.5 元买入 1000 股平安银行。",
                "portfolio",
                artifacts,
                ["incomplete trade description"],
            )
        out = await runner.call("add_position", ts_code=code, amount=shares, price=price)
        if not out.ok:
            return _result(f"记录买入失败：{out.error}", "portfolio", artifacts, [out.error or ""])
        artifacts["buy"] = out.data
        status = await runner.call("get_portfolio_status")
        detail = _status_markdown(status.data) if status.ok else ""
        answer = (
            f"已记录买入：{display_name(code)}（{code}）**{shares:g} 股** @ **{money(price, 3)} 元**，"
            f"金额约 {money(shares * price)} 元。\n\n{detail}"
        )
        artifacts["portfolio"] = status.data if status.ok else None
        return _result(answer, "portfolio", artifacts, errors)

    if symbols and is_sell:
        code = symbols[0]
        out = await runner.call(
            "remove_position",
            ts_code=code,
            amount=shares,
            price=price,
        )
        if not out.ok:
            return _result(f"卖出失败：{out.error}", "portfolio", artifacts, [out.error or ""])
        artifacts["sell"] = out.data
        status = await runner.call("get_portfolio_status")
        detail = _status_markdown(status.data) if status.ok else ""
        realised = out.data.get("realised_pnl")
        realised_line = (
            f"，实现盈亏 **{money(realised)} 元**" if isinstance(realised, (int, float)) else ""
        )
        answer = (
            f"已记录卖出：{display_name(code)}（{code}）**{out.data.get('sold', 0):g} 股**"
            f"{realised_line}。\n\n{detail}"
        )
        artifacts["portfolio"] = status.data if status.ok else None
        return _result(answer, "portfolio", artifacts, errors)

    status = await runner.call("get_portfolio_status")
    if not status.ok:
        return _result(
            f"读取持仓失败：{status.error}" + (f"\n\n建议：{status.hint}" if status.hint else ""),
            "portfolio",
            artifacts,
            [status.error or ""],
        )
    artifacts["portfolio"] = status.data
    return _result(_status_markdown(status.data), "portfolio", artifacts, errors)


def _extract_shares(question: str) -> float | None:
    lots = _LOTS_RE.search(question)
    if lots:
        return float(lots.group(1)) * 100.0
    match = _SHARES_RE.search(question)
    if match:
        return float(match.group(1))
    return None


def _extract_price(question: str) -> float | None:
    match = _PRICE_RE.search(question)
    if match:
        return float(match.group(1))
    return None


def _status_markdown(status: dict[str, Any] | None) -> str:
    if not status:
        return "_暂无持仓数据。_"
    positions = status.get("positions") or []
    totals = status.get("totals") or {}
    name = (status.get("portfolio") or {}).get("name", "组合")

    if not positions:
        return f"### {name}\n\n当前没有持仓记录。"

    table = md_table(
        ["代码", "名称", "数量", "成本", "现价", "市值", "浮动盈亏", "浮盈比例"],
        [
            [
                row.get("ts_code", ""),
                row.get("name", ""),
                f"{row.get('amount', 0):g}",
                money(row.get("cost"), 3),
                money(row.get("price"), 3),
                money(row.get("market_value")),
                money(row.get("pnl")),
                pct(row.get("pnl_pct")),
            ]
            for row in positions
        ],
    )
    return (
        f"### {name}\n\n{table}\n\n"
        f"- 总成本：**{money(totals.get('cost'))} 元**\n"
        f"- 总市值：**{money(totals.get('market_value'))} 元**\n"
        f"- 浮动盈亏：**{money(totals.get('pnl'))} 元（{pct(totals.get('pnl_pct'))}）**\n"
        f"- 持仓数量：{totals.get('count', len(positions))} 只"
    )


# ----------------------------------------------------------------------
# 5. 价格预警
# ----------------------------------------------------------------------
async def manage_alerts(question: str, runner: ToolRunner) -> ExpertResult:
    errors: list[str] = []
    artifacts: dict[str, Any] = {}
    lowered = question.lower()

    if any(word in lowered for word in ("列出", "查看", "所有", "哪些", "list")):
        out = await runner.call("list_alerts")
        if not out.ok:
            return _result(f"读取预警失败：{out.error}", "alert", artifacts, [out.error or ""])
        alerts = out.data.get("alerts") or []
        artifacts["alerts"] = alerts
        if not alerts:
            return _result("当前没有设置任何价格预警。", "alert", artifacts, errors)
        table = md_table(
            ["ID", "标的", "条件", "状态", "备注"],
            [
                [
                    alert.get("id", "")[-8:],
                    f"{alert.get('name', '')}（{alert.get('ts_code', '')}）",
                    alert.get("label", ""),
                    "已触发"
                    if alert.get("triggered_at")
                    else ("启用" if alert.get("enabled") else "暂停"),
                    alert.get("note", "") or "—",
                ]
                for alert in alerts
            ],
        )
        return _result(f"### 当前价格预警\n\n{table}", "alert", artifacts, errors)

    spec = extract_alert_spec(question)
    if spec.ts_code is None:
        return _result(
            "请告诉我要预警的标的，例如：贵州茅台涨到 2000 元提醒我。",
            "alert",
            artifacts,
            ["missing symbol"],
        )

    if spec.pct_change is not None:
        out = await runner.call(
            "add_price_alert", ts_code=spec.ts_code, pct_change=spec.pct_change, note=spec.note
        )
    elif spec.threshold is not None:
        out = await runner.call(
            "add_price_alert",
            ts_code=spec.ts_code,
            operator=spec.operator or ">",
            threshold=spec.threshold,
            note=spec.note,
        )
    else:
        return _result(
            "请给出触发条件：目标价（如「涨到 2000 元」）或涨跌幅（如「上涨 5%」）。",
            "alert",
            artifacts,
            ["missing trigger"],
        )

    if not out.ok:
        return _result(
            f"创建预警失败：{out.error}" + (f"\n\n建议：{out.hint}" if out.hint else ""),
            "alert",
            artifacts,
            [out.error or ""],
        )

    alert = out.data
    artifacts["alert"] = alert
    base_line = (
        f"\n- 基准价：{money(alert.get('base_price'), 3)} 元（触发幅度 {alert.get('pct_change'):+.2f}%）"
        if alert.get("base_price")
        else ""
    )
    answer = (
        f"已创建价格预警：**{alert.get('label')}**。\n"
        f"- 预警 ID：`{alert.get('id')}`"
        f"{base_line}\n"
        "- 触发后会写入 `alerts_log.jsonl`，并（若配置了 SMTP）发送邮件。\n\n"
        "需要立即检查是否已触发，可以说「检查一下我的预警」。"
    )
    return _result(answer, "alert", artifacts, errors)


# ----------------------------------------------------------------------
def _failure_message(errors: list[str]) -> str:
    if not errors:
        return "未能获取到数据，请补充更具体的标的或条件。"
    return "分析过程中出现以下问题：\n" + "\n".join(f"- {error}" for error in errors)


def _result(answer: str, agent: str, artifacts: dict[str, Any], errors: list[str]) -> ExpertResult:
    return ExpertResult(answer=answer, agent=agent, artifacts=artifacts, errors=errors)


#: 专家节点使用的分发表。
HANDLERS = {
    "fundamental_analyst": analyse_fundamental,
    "technical_analyst": analyse_technical,
    "stock_picker": pick_stocks,
    "backtest": backtest,
    "portfolio": manage_portfolio,
    "alert": manage_alerts,
}

#: 纯分析型专家（无副作用），返工时可以安全重跑。
_ANALYSIS_AGENTS = ("fundamental_analyst", "technical_analyst", "stock_picker", "backtest")


def _with_revision_note(answer: str, critique: dict[str, Any] | None) -> str:
    """返工时在结论前加一行「已按审查意见修订」，让用户看得见这版是重做的。

    确定性路径没有语言模型可以改写，所以它实际做的是**重新取数**（口径与上一版一致），
    这里只负责把「这是第几版」说清楚，避免和上一版混淆。
    """

    if not critique or not critique.get("issues"):
        return answer
    count = int(critique.get("revision") or 1)
    note = f"> 已按审查意见修订（第 {count + 1} 版），重新核对了下列问题涉及的取数口径。\n\n"
    return note + answer


async def run(
    agent: str,
    question: str,
    runner: ToolRunner,
    *,
    critique: dict[str, Any] | None = None,
) -> ExpertResult:
    handler = HANDLERS.get(agent)
    if handler is None:
        return ExpertResult(
            answer=f"未实现的分析器：{agent}", agent=agent, errors=[f"no handler for {agent}"]
        )

    result = await handler(question, runner)
    if agent in _ANALYSIS_AGENTS:
        result.answer = _with_revision_note(result.answer, critique)
    return result
