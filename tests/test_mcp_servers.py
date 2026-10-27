"""MCP 服务器的契约测试，跑在内存 MCP 传输上。

这些是最要紧的测试：它们断言的是每个外部 MCP 客户端都依赖的**线上契约**——工具名、
JSON schema、``{"ok": ...}`` 信封、错误码与载荷上限。
"""

from __future__ import annotations

import json
from typing import Any

from mcp.shared.memory import create_connected_server_and_client_session

from finagent.mcp_servers.market import build_server as build_market
from finagent.mcp_servers.portfolio import build_server as build_portfolio
from finagent.mcp_servers.quant import build_server as build_quant


async def call(server: Any, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Invoke an MCP tool and return the parsed envelope."""

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        result = await session.call_tool(name, arguments or {})
        text = result.content[0].text if result.content else "{}"
        return json.loads(text)


async def tool_names(server: Any) -> list[str]:
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        listed = await session.list_tools()
        return sorted(tool.name for tool in listed.tools)


def assert_ok(payload: dict[str, Any]) -> Any:
    assert payload.get("ok") is True, payload
    return payload["data"]


def assert_error(payload: dict[str, Any], code: str | None = None) -> dict[str, Any]:
    assert payload.get("ok") is False, payload
    error = payload["error"]
    assert error["message"]
    if code:
        assert error["code"] == code
    return error


# ----------------------------------------------------------------------
# 目录清单
# ----------------------------------------------------------------------
async def test_market_tool_catalogue(offline_env) -> None:
    names = await tool_names(build_market())
    assert names == sorted(
        [
            "get_current_time",
            "resolve_symbol",
            "list_supported_symbols",
            "get_stock_basic",
            "get_trade_calendar",
            "get_realtime_price",
            "get_daily_price",
            "get_index_daily",
            "get_daily_basic",
            "get_income_statement",
            "get_forecast",
            "get_moneyflow",
            "get_limit_list",
            "get_top_list",
            "get_concept_detail",
            "get_macro",
            "get_global_index_comparison",
            "get_asset_basic",
            "get_asset_daily",
        ]
    )


async def test_quant_tool_catalogue(offline_env) -> None:
    names = await tool_names(build_quant())
    assert names == sorted(
        [
            "compute_indicators",
            "detect_patterns",
            "screen_stocks",
            "get_long_tail_stocks",
            "list_strategies",
            "run_backtest",
        ]
    )


async def test_portfolio_tool_catalogue(offline_env) -> None:
    names = await tool_names(build_portfolio())
    assert "add_position" in names
    assert "get_portfolio_status" in names
    assert "add_price_alert" in names
    assert "update_user_profile" in names
    assert len(names) == 16


async def test_tool_schemas_are_described(offline_env) -> None:
    async with create_connected_server_and_client_session(build_quant()._mcp_server) as session:
        listed = await session.list_tools()
        by_name = {tool.name: tool for tool in listed.tools}

    schema = by_name["run_backtest"].inputSchema
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"ts_code", "strategy"}
    assert "description" in schema["properties"]["strategy"]
    assert by_name["run_backtest"].description


# ----------------------------------------------------------------------
# 行情数据
# ----------------------------------------------------------------------
async def test_get_current_time_envelope(offline_env) -> None:
    data = assert_ok(await call(build_market(), "get_current_time"))
    assert data["provider"] == "offline"
    assert len(data["date"]) == 8


async def test_resolve_symbol(offline_env) -> None:
    data = assert_ok(await call(build_market(), "resolve_symbol", {"query": "茅台"}))
    assert data == {
        "ts_code": "600519.SH",
        "name": "贵州茅台",
        "industry": "白酒",
        "market": "主板",
        "in_reference_universe": True,
        "query": "茅台",
    }


async def test_resolve_symbol_unknown_is_an_error(offline_env) -> None:
    error = assert_error(
        await call(build_market(), "resolve_symbol", {"query": "不存在的公司"}),
        code="unknown_symbol",
    )
    assert "hint" in error


async def test_daily_price_payload(offline_env) -> None:
    data = assert_ok(
        await call(build_market(), "get_daily_price", {"ts_code": "600519.SH", "limit": 5})
    )
    assert data["row_count"] == 5
    assert data["truncated"] is True
    assert data["ts_code"] == "600519.SH"
    assert data["last_close"] > 0
    # 从新到旧
    assert data["rows"][0]["trade_date"] > data["rows"][-1]["trade_date"]


async def test_limit_is_respected(offline_env) -> None:
    data = assert_ok(
        await call(build_market(), "get_daily_price", {"ts_code": "600519.SH", "limit": 2})
    )
    assert len(data["rows"]) == 2
    assert data["total_rows"] > 2


async def test_daily_price_adj_variants(offline_env) -> None:
    raw = assert_ok(
        await call(build_market(), "get_daily_price", {"ts_code": "600519.SH", "limit": 1})
    )
    adjusted = assert_ok(
        await call(
            build_market(), "get_daily_price", {"ts_code": "600519.SH", "limit": 1, "adj": "qfq"}
        )
    )
    assert raw["adj"] == "none"
    assert adjusted["adj"] == "qfq"
    assert raw["rows"][0]["close"] != adjusted["rows"][0]["close"]


async def test_macro_indicator_validation(offline_env) -> None:
    data = assert_ok(await call(build_market(), "get_macro", {"indicator": "cpi", "limit": 2}))
    assert data["indicator"] == "cpi"
    assert len(data["rows"]) == 2


async def test_asset_tools(offline_env) -> None:
    catalogue = assert_ok(await call(build_market(), "get_asset_basic", {"asset": "hk"}))
    assert catalogue["row_count"] > 0
    code = catalogue["rows"][0]["ts_code"]
    prices = assert_ok(
        await call(build_market(), "get_asset_daily", {"asset": "hk", "ts_code": code, "limit": 3})
    )
    assert len(prices["rows"]) == 3


async def test_trade_calendar_defaults_to_a_range(offline_env) -> None:
    data = assert_ok(await call(build_market(), "get_trade_calendar", {"limit": 40}))
    assert data["row_count"] > 1


# ----------------------------------------------------------------------
# 量化
# ----------------------------------------------------------------------
async def test_list_strategies(offline_env) -> None:
    data = assert_ok(await call(build_quant(), "list_strategies"))
    assert data["count"] == 23
    names = {entry["name"] for entry in data["strategies"]}
    assert "ma_cross" in names and "turtle" in names


async def test_compute_indicators(offline_env) -> None:
    data = assert_ok(
        await call(
            build_quant(),
            "compute_indicators",
            {"ts_code": "600519.SH", "indicators": ["rsi", "macd"], "rows": 2},
        )
    )
    assert data["indicators"] == ["rsi", "macd"]
    assert len(data["rows"]) == 2
    assert "rsi" in data["rows"][0] and "dif" in data["rows"][0]


async def test_compute_indicators_rejects_unknown(offline_env) -> None:
    error = assert_error(
        await call(
            build_quant(), "compute_indicators", {"ts_code": "600519.SH", "indicators": ["xyz"]}
        ),
        code="invalid_argument",
    )
    assert "可用指标" in error["hint"]


async def test_detect_patterns(offline_env) -> None:
    data = assert_ok(await call(build_quant(), "detect_patterns", {"ts_code": "茅台"}))
    assert isinstance(data["patterns"], list)
    assert data["pattern_count"] == len(data["patterns"])
    assert set(data["signals"]) == {"macd", "kdj", "rsi", "close"}


async def test_screen_stocks(offline_env) -> None:
    data = assert_ok(
        await call(
            build_quant(), "screen_stocks", {"pe_max": 10, "industries": ["银行"], "limit": 3}
        )
    )
    assert data["returned"] <= 3
    assert data["matched"] >= data["returned"]
    assert all(row["industry"] == "银行" for row in data["results"])


async def test_run_backtest(offline_env) -> None:
    data = assert_ok(
        await call(
            build_quant(),
            "run_backtest",
            {
                "ts_code": "600519.SH",
                "strategy": "macd",
                "benchmark": "000001.SH",
                "curve_points": 10,
            },
        )
    )
    for key in (
        "total_return_pct",
        "annualized_return_pct",
        "max_drawdown_pct",
        "trades_count",
        "equity_curve",
        "benchmark",
    ):
        assert key in data
    assert len(data["equity_curve"]) <= 10


async def test_run_backtest_unknown_strategy_errors(offline_env) -> None:
    error = assert_error(
        await call(build_quant(), "run_backtest", {"ts_code": "600519.SH", "strategy": "nope"}),
        code="strategy_not_found",
    )
    assert "已注册策略" in error["hint"]


async def test_run_backtest_multi_asset_guard(offline_env) -> None:
    assert_error(
        await call(
            build_quant(),
            "run_backtest",
            {"ts_code": "600519.SH", "strategy": "cross_section_momentum"},
        ),
        code="invalid_argument",
    )


# ----------------------------------------------------------------------
# 持仓
# ----------------------------------------------------------------------
async def test_portfolio_roundtrip(offline_env) -> None:
    server = build_portfolio()
    created = assert_ok(
        await call(server, "add_position", {"ts_code": "平安银行", "amount": 1000, "price": 10.5})
    )
    assert created["ts_code"] == "000001.SZ"

    status = assert_ok(await call(server, "get_portfolio_status"))
    assert status["totals"]["count"] == 1
    assert status["positions"][0]["amount"] == 1000

    sold = assert_ok(
        await call(
            server, "remove_position", {"ts_code": "000001.SZ", "amount": 500, "price": 12.0}
        )
    )
    assert sold["sold"] == 500
    assert sold["remaining"] == 500


async def test_portfolio_sell_more_than_held_errors(offline_env) -> None:
    server = build_portfolio()
    await call(server, "add_position", {"ts_code": "600519.SH", "amount": 10, "price": 1000})
    assert_error(
        await call(server, "remove_position", {"ts_code": "600519.SH", "amount": 100}),
        code="invalid_argument",
    )


async def test_alert_tools(offline_env) -> None:
    server = build_portfolio()
    created = assert_ok(await call(server, "add_price_alert", {"ts_code": "茅台", "pct_change": 5}))
    assert created["pct_change"] == 5.0
    assert created["threshold"] > created["base_price"]

    listed = assert_ok(await call(server, "list_alerts"))
    assert listed["count"] == 1

    checked = assert_ok(await call(server, "check_alerts", {"trading_hours_only": False}))
    assert checked["evaluated"] == 1

    assert_ok(await call(server, "remove_alert", {"alert_id": created["id"]}))
    assert assert_ok(await call(server, "list_alerts"))["count"] == 0


async def test_alert_requires_a_trigger(offline_env) -> None:
    assert_error(
        await call(build_portfolio(), "add_price_alert", {"ts_code": "600519.SH"}),
        code="invalid_argument",
    )


async def test_profile_tools(offline_env) -> None:
    server = build_portfolio()
    updated = assert_ok(
        await call(
            server,
            "update_user_profile",
            {
                "risk_tolerance": "激进型",
                "favorite_sectors": ["半导体"],
                "investment_style": "高股息",
            },
        )
    )
    assert updated["profile"]["risk_tolerance"] == "Aggressive"
    fetched = assert_ok(await call(server, "get_user_profile"))
    assert "半导体" in fetched["profile"]["favorite_sectors"]
    assert "Aggressive" in fetched["summary"]


async def test_profile_update_with_nothing_is_an_error(offline_env) -> None:
    assert_error(await call(build_portfolio(), "update_user_profile", {}), code="invalid_argument")


async def test_default_portfolio_naming(offline_env) -> None:
    data = assert_ok(await call(build_portfolio(), "create_portfolio", {"name": "成长"}))
    listed = assert_ok(await call(build_portfolio(), "list_portfolios"))
    assert data["name"] == "成长"
    assert listed["count"] == 2
