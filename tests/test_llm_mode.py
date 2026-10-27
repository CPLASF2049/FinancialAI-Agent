"""LLM 模式下的代码路径，用脚本化聊天模型覆盖（不联网）。

覆盖 ReAct 循环、Supervisor 的 JSON 决策解析，以及注入模型后整张图的运行——
这些正是确定性模式跳过的代码路径。
"""

from __future__ import annotations

import json

from conftest import ScriptedChatModel, ai_text, ai_tool_call
from finagent.graph.agent_loop import run_tool_loop, stringify_tool_output
from finagent.graph.builder import build_graph
from finagent.graph.mcp_client import open_toolkit
from finagent.graph.nodes import GraphResources, _parse_supervisor
from finagent.graph.state import initial_state
from finagent.settings import get_settings


# ----------------------------------------------------------------------
# 单独测试 ReAct 循环
# ----------------------------------------------------------------------
async def test_tool_loop_executes_a_tool_then_answers(offline_env) -> None:
    async with open_toolkit(get_settings()) as toolkit:
        tools = toolkit.subset(["resolve_symbol", "get_realtime_price"])
        model = ScriptedChatModel(
            [
                ai_tool_call("resolve_symbol", {"query": "茅台"}, "c1"),
                ai_text("贵州茅台的 ts_code 是 600519.SH。"),
            ]
        )
        result = await run_tool_loop(
            agent="technical_analyst",
            model=model,
            tools=tools,
            system_prompt="You are a test agent.",
            user_messages=[{"role": "user", "content": "茅台代码是什么"}],
        )

    assert result.stopped_reason == "completed"
    assert result.tool_calls == 1
    assert result.records[0].tool == "resolve_symbol"
    assert result.records[0].ok is True
    assert "600519.SH" in result.records[0].preview
    assert "600519.SH" in result.text
    assert model.bound_tools == ["resolve_symbol", "get_realtime_price"]


async def test_tool_loop_records_error_envelopes(offline_env) -> None:
    async with open_toolkit(get_settings()) as toolkit:
        tools = toolkit.subset(["resolve_symbol"])
        model = ScriptedChatModel(
            [
                ai_tool_call("resolve_symbol", {"query": "不存在的公司"}, "c1"),
                ai_text("找不到这家公司。"),
            ]
        )
        result = await run_tool_loop(
            agent="technical_analyst",
            model=model,
            tools=tools,
            system_prompt="test",
            user_messages=[{"role": "user", "content": "?"}],
        )

    assert result.records[0].ok is False
    assert "resolve" in (result.records[0].error or "").lower() or result.records[0].error


async def test_tool_loop_stops_at_iteration_cap(offline_env) -> None:
    async with open_toolkit(get_settings()) as toolkit:
        tools = toolkit.subset(["get_current_time"])
        model = ScriptedChatModel(
            [ai_tool_call("get_current_time", {}, f"c{index}") for index in range(5)]
        )
        result = await run_tool_loop(
            agent="technical_analyst",
            model=model,
            tools=tools,
            system_prompt="test",
            user_messages=[{"role": "user", "content": "loop"}],
            max_iterations=3,
        )

    assert result.stopped_reason == "max_iterations"
    assert result.tool_calls == 3


async def test_tool_loop_handles_unknown_tool(offline_env) -> None:
    async with open_toolkit(get_settings()) as toolkit:
        tools = toolkit.subset(["get_current_time"])
        model = ScriptedChatModel([ai_tool_call("not_a_tool", {}, "c1"), ai_text("done")])
        result = await run_tool_loop(
            agent="technical_analyst",
            model=model,
            tools=tools,
            system_prompt="test",
            user_messages=[{"role": "user", "content": "x"}],
        )
    assert result.records[0].ok is False
    assert "未知工具" in (result.records[0].error or "")


# ----------------------------------------------------------------------
# Supervisor 决策解析
# ----------------------------------------------------------------------
def test_parse_supervisor_plain_json() -> None:
    decision = _parse_supervisor('{"agent": "backtest", "reason": "asked for a backtest"}')
    assert decision["agent"] == "backtest"


def test_parse_supervisor_tolerates_prose() -> None:
    text = 'Sure.\n```json\n{"agent": "portfolio", "done": false}\n```'
    decision = _parse_supervisor(text)
    assert decision["agent"] == "portfolio"


def test_parse_supervisor_garbage() -> None:
    assert _parse_supervisor("no json here") == {}


# ----------------------------------------------------------------------
# 使用 LLM 版 Supervisor + LLM 版专家的整图
# ----------------------------------------------------------------------
async def test_graph_with_llm_routing(offline_env) -> None:
    async with open_toolkit(get_settings()) as toolkit:
        model = ScriptedChatModel(
            [
                # Supervisor 的决策
                ai_text(json.dumps({"agent": "technical_analyst", "reason": "quote question"})),
                # 分析师的 ReAct：先调工具，再作答
                ai_tool_call("get_realtime_price", {"ts_code": "600519.SH"}, "t1"),
                ai_text("贵州茅台最新价已获取（见工具结果）。"),
                # Supervisor 收尾
                ai_text(json.dumps({"agent": "finish", "reason": "answered"})),
            ]
        )
        graph = build_graph(
            GraphResources(
                toolkit=toolkit,
                settings=get_settings(),
                store=None,
                model_getter=lambda: model,
            )
        )
        final = await graph.ainvoke(initial_state("茅台现在多少钱", user_id="u", thread_id="t"))

    assert "technical_analyst" in final["completed_agents"]
    assert "贵州茅台" in final["answer"]
    assert final["answer"].strip()


async def test_graph_falls_back_to_rules_on_bad_llm_decision(offline_env) -> None:
    async with open_toolkit(get_settings()) as toolkit:
        model = ScriptedChatModel(
            [
                ai_text("I am not going to answer with JSON."),
                ai_tool_call("get_current_time", {}, "t1"),
                ai_text("这里是回答。"),
                ai_text(json.dumps({"agent": "finish"})),
            ]
        )
        graph = build_graph(
            GraphResources(
                toolkit=toolkit,
                settings=get_settings(),
                store=None,
                model_getter=lambda: model,
            )
        )
        final = await graph.ainvoke(initial_state("茅台现在多少钱", user_id="u", thread_id="t2"))

    # 回退到关键词路由，它会为「查行情」这类问题选中分析师。
    assert "technical_analyst" in final["completed_agents"]


# ----------------------------------------------------------------------
# 输出归一化
# ----------------------------------------------------------------------
def test_stringify_handles_all_shapes() -> None:
    assert stringify_tool_output("plain") == "plain"
    assert stringify_tool_output({"type": "text", "text": "x"}) == "x"
    assert (
        stringify_tool_output([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
        == "ab"
    )
    assert stringify_tool_output([{"text": "only"}]) == "only"
    assert json.loads(stringify_tool_output({"ok": True})) == {"ok": True}
    assert stringify_tool_output(None) == ""
