"""图级测试：路由、专家执行、记忆、持久化。

整张图通过 in-process MCP 传输跑在离线数据源上，因此这些用例覆盖的是真实的
Supervisor → 专家 → 记忆路径，不需要网络、不需要 API Key、不需要 token。

每个用例都在自己的协程里打开 :class:`FinAgentApp`：MCP 的内存传输由 anyio task group
支撑，跨任务进入/退出（共享 async fixture 的做法）会触发 anyio 的 cancel scope 守卫。
"""

from __future__ import annotations

import pytest

from finagent.graph import open_app
from finagent.graph.state import EXPERT_AGENTS


# ----------------------------------------------------------------------
# 管道
# ----------------------------------------------------------------------
async def test_graph_loads_every_mcp_tool(offline_env) -> None:
    async with open_app() as app:
        assert len(app.toolkit.tools) == 41
        assert app.toolkit.transport == "inprocess"


async def test_expert_tool_subsets_are_distinct(offline_env) -> None:
    from finagent.graph.experts import EXPERT_TOOLS, tools_for

    async with open_app() as app:
        for agent in EXPERT_AGENTS:
            subset = app.toolkit.subset(tools_for(agent))
            assert subset, agent
            assert all(tool.name in app.toolkit.by_name for tool in subset)
    # 指标工具归分析师所有，预警专家不应该看到它们。
    assert "compute_indicators" in EXPERT_TOOLS["analyst"]
    assert "compute_indicators" not in EXPERT_TOOLS["alert"]


async def test_missing_tool_name_raises(offline_env) -> None:
    from finagent.errors import ConfigurationError

    async with open_app() as app:
        with pytest.raises(ConfigurationError):
            app.toolkit.subset(["definitely_not_a_tool"])


# ----------------------------------------------------------------------
# 路由
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("贵州茅台现在多少钱？", ["analyst"]),
        ("帮我筛选 PE 小于 10 的银行股", ["stock_picker"]),
        ("用 MACD 策略回测中信证券近一年", ["backtest"]),
        ("我以 10.5 元买入 1000 股平安银行", ["portfolio"]),
        ("茅台涨到 2000 元提醒我", ["alert"]),
    ],
)
async def test_single_expert_routing(offline_env, question: str, expected: list[str]) -> None:
    async with open_app() as app:
        result = await app.arun(question, user_id="route", thread_id=question[:8])
    assert result.agents == expected, result.agents
    assert result.errors == []
    assert result.answer.strip()


async def test_compound_request_runs_two_experts(offline_env) -> None:
    async with open_app() as app:
        result = await app.arun(
            "回测一下茅台的双均线策略，再看看我的持仓盈亏",
            user_id="compound",
            thread_id="c1",
        )
    assert set(result.agents) >= {"backtest", "portfolio"}
    assert "回测结果" in result.answer
    assert "持仓" in result.answer


async def test_hop_limit_is_enforced(offline_env, monkeypatch) -> None:
    monkeypatch.setenv("FINAGENT_MAX_AGENT_HOPS", "1")
    from finagent.settings import reset_settings_cache

    reset_settings_cache()

    async with open_app() as app:
        result = await app.arun(
            "回测茅台的双均线策略，再看看我的持仓盈亏",
            user_id="hoplimit",
            thread_id="h1",
        )
    assert len(result.agents) == 1


# ----------------------------------------------------------------------
# 专家输出
# ----------------------------------------------------------------------
async def test_analyst_answer_mentions_price_and_date(offline_env) -> None:
    async with open_app() as app:
        result = await app.arun("茅台最近一个月走势如何", user_id="a", thread_id="a1")
    assert "贵州茅台" in result.answer
    assert "最新价" in result.answer
    assert "PE" in result.answer
    assert any(character.isdigit() for character in result.answer)


async def test_screen_answer_is_a_markdown_table(offline_env) -> None:
    async with open_app() as app:
        result = await app.arun(
            "帮我筛选 PE 小于 10、市值大于 500 亿的银行股", user_id="b", thread_id="b1"
        )
    assert "| 代码 | 名称 |" in result.answer
    assert "工商银行" in result.answer


async def test_backtest_answer_has_metrics(offline_env) -> None:
    async with open_app() as app:
        result = await app.arun("用 MACD 策略回测中信证券近一年", user_id="c", thread_id="c1")
    for label in ("累计收益", "最大回撤", "夏普比率", "超额收益"):
        assert label in result.answer
    assert result.artifacts["backtest"]["ts_code"] == "600030.SH"


async def test_portfolio_buy_then_status(offline_env) -> None:
    async with open_app() as app:
        first = await app.arun("我以 10.5 元买入 1000 股平安银行", user_id="d", thread_id="d1")
        second = await app.arun("我的持仓盈亏怎么样", user_id="d", thread_id="d1")
    assert "已记录买入" in first.answer
    assert "平安银行" in second.answer
    assert "浮动盈亏" in second.answer


async def test_alert_creation_reports_absolute_threshold(offline_env) -> None:
    async with open_app() as app:
        result = await app.arun("茅台涨到 2000 元提醒我", user_id="e", thread_id="e1")
    assert "2000" in result.answer
    assert result.artifacts["alert"]["threshold"] == 2000.0


async def test_alert_percentage_computed_server_side(offline_env) -> None:
    async with open_app() as app:
        result = await app.arun("平安银行上涨 5% 就提醒我", user_id="f", thread_id="f1")
    alert = result.artifacts["alert"]
    assert alert["pct_change"] == 5.0
    assert alert["threshold"] > alert["base_price"]
    assert alert["operator"] == ">"


async def test_unknown_request_gets_guidance(offline_env) -> None:
    async with open_app() as app:
        result = await app.arun("随便聊聊", user_id="g", thread_id="g1")
    assert result.answer
    assert "标的" in result.answer or "试试" in result.answer


# ----------------------------------------------------------------------
# 记忆
# ----------------------------------------------------------------------
async def test_profile_is_remembered_and_hydrated(offline_env) -> None:
    async with open_app() as app:
        await app.arun("我是保守型投资者，偏好银行和白酒", user_id="mem", thread_id="m1")

        stored = app.store.get(("users", "mem"), "profile")
        assert stored is not None
        assert stored.value["risk_tolerance"] == "Conservative"
        assert "银行" in stored.value["favorite_sectors"]

        # 在*另一个*线程里的后续轮次仍然能看到这份画像。
        result = await app.arun("帮我看一下平安银行", user_id="mem", thread_id="m2")
        assert result.answer
        hydrated = app.store.get(("users", "mem"), "profile")
        assert "白酒" in hydrated.value["favorite_sectors"]


async def test_profile_is_injected_into_state(offline_env) -> None:
    async with open_app() as app:
        await app.arun("我是激进型投资者", user_id="mem5", thread_id="m6")
        await app.arun("茅台现在多少钱", user_id="mem5", thread_id="m6")
        snapshot = await app.graph.aget_state({"configurable": {"thread_id": "mem5:m6"}})
    assert snapshot.values["user_profile"]["risk_tolerance"] == "Aggressive"
    assert "Aggressive" in snapshot.values["profile_summary"]


async def test_preference_turn_acknowledges_without_expert(offline_env) -> None:
    async with open_app() as app:
        result = await app.arun("我是激进型投资者", user_id="mem2", thread_id="m3")
    assert result.agents == []
    assert "偏好档案" in result.answer
    assert result.profile_updates["risk_tolerance"] == "Aggressive"


async def test_memory_is_mirrored_into_the_mcp_profile_store(offline_env) -> None:
    from finagent.mcp_servers.portfolio import profile_store

    async with open_app() as app:
        await app.arun("我偏好长期持有高股息的股票", user_id="mem3", thread_id="m4")
    assert profile_store().get()["investment_style"] == "偏好高股息"


async def test_non_preference_turn_does_not_write(offline_env) -> None:
    async with open_app() as app:
        await app.arun("茅台现在多少钱", user_id="mem4", thread_id="m5")
        profile = app.store.get(("users", "mem4"), "profile")
    if profile is not None:
        assert profile.value.get("investment_style", "") == ""


# ----------------------------------------------------------------------
# 轮次隔离与持久化
# ----------------------------------------------------------------------
async def test_per_turn_channels_reset(offline_env) -> None:
    async with open_app() as app:
        await app.arun("用 MACD 回测中信证券近一年", user_id="iso", thread_id="i1")
        second = await app.arun("茅台现在多少钱", user_id="iso", thread_id="i1")
    # 新一轮对话里，回测专家不能被当成「已经跑过」。
    assert second.agents == ["analyst"]


async def test_conversation_history_accumulates(offline_env) -> None:
    async with open_app() as app:
        await app.arun("茅台现在多少钱", user_id="hist", thread_id="h1")
        await app.arun("平安银行呢", user_id="hist", thread_id="h1")
        snapshot = await app.graph.aget_state({"configurable": {"thread_id": "hist:h1"}})
    messages = snapshot.values.get("messages") or []
    roles = [message.type for message in messages]
    assert roles.count("human") == 2
    assert roles.count("ai") == 2


async def test_state_persists_across_reopen(offline_env) -> None:
    async with open_app() as first:
        await first.arun("茅台现在多少钱", user_id="persist", thread_id="p1")

    async with open_app() as second:
        snapshot = await second.graph.aget_state({"configurable": {"thread_id": "persist:p1"}})
    assert snapshot.values.get("messages"), "checkpoint should survive a restart"


# ----------------------------------------------------------------------
# 流式输出
# ----------------------------------------------------------------------
async def test_stream_emits_route_tool_and_final_events(offline_env) -> None:
    kinds: list[str] = []
    final = None
    async with open_app() as app:
        async for event in app.astream("茅台涨到 2000 元提醒我", user_id="s", thread_id="s1"):
            kinds.append(event["type"])
            if event["type"] == "final":
                final = event["result"]

    assert "start" in kinds
    assert "route" in kinds
    assert "agent" in kinds
    assert "final" in kinds
    assert final is not None
    assert final.answer
    assert final.agents == ["alert"]


async def test_stream_emits_tool_events(offline_env) -> None:
    tool_events: list[dict] = []
    async with open_app() as app:
        async for event in app.astream("茅台现在多少钱", user_id="s2", thread_id="s2"):
            if event["type"] == "event" and event.get("event") == "tool_call":
                tool_events.append(event)
    assert tool_events, "expected tool_call stream events"
    assert any(item["tool"] == "get_realtime_price" for item in tool_events)
