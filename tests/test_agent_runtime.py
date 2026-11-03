"""P1-4 验收测试：统一执行循环 + 预算 + 三层上下文。"""

from __future__ import annotations

import asyncio

import pytest

from finagent.runtime.agent_context import AgentContext, Layer, estimate_tokens
from finagent.runtime.agent_runtime import AgentRuntime
from finagent.runtime.execution_policy import Budget


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Reply:
    def __init__(self, text: str = "", tool_calls: list[dict] | None = None) -> None:
        self.content = text
        self.tool_calls = tool_calls or []
        self.usage_metadata = {"input_tokens": 10, "output_tokens": 5}


class _Model:
    def __init__(self, replies: list[_Reply]) -> None:
        self.replies = list(replies)
        self.calls = 0

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        return self.replies.pop(0) if self.replies else _Reply("完成")


class _Tool:
    def __init__(self, name: str = "t") -> None:
        self.name = name
        self.calls = 0

    async def ainvoke(self, args):
        self.calls += 1
        return '{"ok": true, "data": {"v": 1}}'


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 三层上下文
# ----------------------------------------------------------------------
def test_layers_are_tracked() -> None:
    context = AgentContext()
    context.add(_Msg("system"), Layer.CORE)
    context.add(_Msg("summary"), Layer.SUMMARY)
    context.add(_Msg("detail" * 100), Layer.DETAIL, ref="tool:c1")
    summary = context.summary()
    assert summary["by_layer"]["core"] == 1
    assert summary["by_layer"]["detail"] == 1


def test_compaction_keeps_core() -> None:
    context = AgentContext()
    core = _Msg("用户目标：分析中国太保")
    context.add(core, Layer.CORE)
    for index in range(6):
        context.add(_Msg("x" * 500), Layer.DETAIL, ref=f"tool:c{index}")

    context.compact(budget=200, keep_recent_details=1)
    assert core.content == "用户目标：分析中国太保", "核心上下文不能被压缩"
    assert context.references, "详情层被压缩后要留下引用"


def test_compaction_shrinks_tokens() -> None:
    context = AgentContext()
    for index in range(8):
        context.add(_Msg("y" * 800), Layer.DETAIL, ref=f"tool:d{index}")
    before = context.tokens()
    context.compact(budget=300, keep_recent_details=2)
    assert context.tokens() < before


def test_recent_details_are_kept() -> None:
    context = AgentContext()
    for index in range(5):
        context.add(_Msg("z" * 500), Layer.DETAIL, ref=f"tool:e{index}")
    context.compact(budget=100, keep_recent_details=2)
    assert len(context.items) >= 2


def test_summaries_fold_when_still_over_budget() -> None:
    context = AgentContext()
    context.add(_Msg("核心"), Layer.CORE)
    for index in range(4):
        context.add(_Msg(f"摘要{index}" + "x" * 200), Layer.SUMMARY)
    context.compact(budget=10)
    summaries = [item for item in context.items if item.layer is Layer.SUMMARY]
    assert len(summaries) <= 2


def test_budget_zero_is_noop() -> None:
    context = AgentContext()
    context.add(_Msg("a" * 1000), Layer.DETAIL, ref="tool:x")
    result = context.compact(budget=10_000)
    assert result["before_tokens"] == result["after_tokens"]


# ----------------------------------------------------------------------
# 执行循环与预算
# ----------------------------------------------------------------------
def test_runtime_completes() -> None:
    runtime = AgentRuntime(
        agent="researcher",
        model=_Model([_Reply("结论")]),
        tools=[],
        system_prompt="sys",
    )
    outcome = _run(runtime.run([_Msg("问题")], emit=lambda _e: None))
    assert outcome.stopped_reason == "completed"
    assert outcome.text == "结论"


def test_runtime_respects_llm_call_budget() -> None:
    """预算耗尽要停，而不是无限循环。"""

    model = _Model(
        [_Reply(tool_calls=[{"name": "t", "args": {}, "id": f"c{i}"}]) for i in range(10)]
    )
    runtime = AgentRuntime(
        agent="researcher",
        model=model,
        tools=[_Tool()],
        system_prompt="sys",
        budget=Budget(max_llm_calls=2, max_tool_calls=100),
    )
    outcome = _run(runtime.run([_Msg("问题")], emit=lambda _e: None))
    assert outcome.stopped_reason in ("budget_llm_calls", "max_iterations")
    assert model.calls <= 2


def test_runtime_respects_tool_call_budget() -> None:
    model = _Model(
        [_Reply(tool_calls=[{"name": "t", "args": {}, "id": f"c{i}"}]) for i in range(10)]
    )
    runtime = AgentRuntime(
        agent="researcher",
        model=model,
        tools=[_Tool()],
        system_prompt="sys",
        budget=Budget(max_llm_calls=100, max_tool_calls=1),
    )
    outcome = _run(runtime.run([_Msg("问题")], emit=lambda _e: None))
    assert outcome.tool_calls <= 2


def test_runtime_token_budget() -> None:
    model = _Model([_Reply(tool_calls=[{"name": "t", "args": {}, "id": "c1"}]) for _ in range(5)])
    runtime = AgentRuntime(
        agent="researcher",
        model=model,
        tools=[_Tool()],
        system_prompt="sys",
        token_budget=5,
    )
    outcome = _run(runtime.run([_Msg("问题")], emit=lambda _e: None))
    assert outcome.stopped_reason == "budget_tokens"


def test_runtime_cancel() -> None:
    runtime = AgentRuntime(
        agent="researcher",
        model=_Model([_Reply("x")]),
        tools=[],
        system_prompt="sys",
        is_cancelled=lambda: True,
    )
    outcome = _run(runtime.run([_Msg("问题")], emit=lambda _e: None))
    assert outcome.stopped_reason == "cancelled"


def test_runtime_deadline() -> None:
    import time

    runtime = AgentRuntime(
        agent="researcher",
        model=_Model([_Reply("x")]),
        tools=[],
        system_prompt="sys",
        deadline=time.monotonic() - 1,
    )
    outcome = _run(runtime.run([_Msg("问题")], emit=lambda _e: None))
    assert outcome.stopped_reason == "deadline"


def test_runtime_emits_events() -> None:
    events: list[dict] = []
    runtime = AgentRuntime(
        agent="researcher",
        model=_Model([_Reply(tool_calls=[{"name": "t", "args": {}, "id": "c1"}]), _Reply("完")]),
        tools=[_Tool()],
        system_prompt="sys",
    )
    _run(runtime.run([_Msg("问题")], emit=events.append))
    kinds = [item["event"] for item in events]
    assert "agent_start" in kinds
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert kinds[-1] == "agent_end"


def test_runtime_reports_stats() -> None:
    runtime = AgentRuntime(
        agent="researcher",
        model=_Model([_Reply(tool_calls=[{"name": "t", "args": {}, "id": "c1"}]), _Reply("完")]),
        tools=[_Tool()],
        system_prompt="sys",
    )
    outcome = _run(runtime.run([_Msg("问题")], emit=lambda _e: None))
    assert outcome.stats["tool_calls_total"] == 1
    assert outcome.stats["tool_calls_succeeded"] == 1


def test_estimate_tokens_helper() -> None:
    assert estimate_tokens("") == 1
    assert estimate_tokens("abcd" * 4) == 4


@pytest.mark.parametrize("layer", [Layer.CORE, Layer.SUMMARY, Layer.DETAIL])
def test_items_accumulate_tokens(layer: Layer) -> None:
    context = AgentContext()
    context.add(_Msg("x" * 400), layer)
    assert context.tokens() >= 100
