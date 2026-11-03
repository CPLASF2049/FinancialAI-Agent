"""执行协议（P0-1）的验收测试。

核心命题：**每一类终止路径都必须产出一个合法的 ExpertResult**，
而不是让下游去猜「这一轮到底算不算完成」。

覆盖：
* submit / 预算耗尽（轮数、token、墙钟）/ 连续工具失败 / 模型失败 / 契约违反 / 取消
* 工具异常不杀死循环（兜成 is_error 回给模型）
* normalize_result 的归一化规则
"""

from __future__ import annotations

import asyncio
import time

import pytest

from finagent.graph.agent_loop import run_tool_loop
from finagent.graph.contract import (
    ExpertResult,
    Status,
    StructuredError,
    Termination,
    normalize_result,
)


class _Reply:
    def __init__(
        self, text: str = "", tool_calls: list[dict] | None = None, usage: dict | None = None
    ):
        self.content = text
        self.tool_calls = tool_calls or []
        self.usage_metadata = usage or {}


class _Model:
    """按脚本依次返回响应的假模型。"""

    def __init__(self, replies: list[_Reply]) -> None:
        self.replies = list(replies)
        self.calls = 0

    def bind_tools(self, tools):
        # 真实模型会被绑定工具；这里返回自身即可
        return self

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        if self.replies:
            return self.replies.pop(0)
        return _Reply("做完了")


class _Tool:
    def __init__(self, name: str, behaviour: str = "ok") -> None:
        self.name = name
        self.behaviour = behaviour
        self.calls = 0

    async def ainvoke(self, args, config=None):
        self.calls += 1
        if self.behaviour == "raise":
            raise RuntimeError("工具内部炸了")
        if self.behaviour == "slow":
            await asyncio.sleep(0.05)
        return '{"ok": true, "data": {"value": 1}}'


# ----------------------------------------------------------------------
# 终止路径
# ----------------------------------------------------------------------
def test_submit_termination() -> None:
    """模型不再调工具 = 主动声明完成。"""

    result = asyncio.run(
        run_tool_loop(
            agent="researcher",
            model=_Model([_Reply("茅台 1439 元", usage={"input_tokens": 10, "output_tokens": 5})]),
            tools=[],
            system_prompt="x",
            user_messages=[],
        )
    )
    assert result.stopped_reason == "completed"
    assert result.input_tokens == 10 and result.output_tokens == 5


def test_budget_iterations_termination() -> None:
    """一直调工具、从不收尾 → 轮数用尽。"""

    tool = _Tool("get_realtime_price")
    replies = [
        _Reply(tool_calls=[{"name": "get_realtime_price", "args": {}, "id": f"c{i}"}])
        for i in range(5)
    ]
    result = asyncio.run(
        run_tool_loop(
            agent="researcher",
            model=_Model(replies),
            tools=[tool],
            system_prompt="x",
            user_messages=[],
            max_iterations=2,
        )
    )
    assert result.stopped_reason == "max_iterations"


def test_budget_wallclock_termination() -> None:
    result = asyncio.run(
        run_tool_loop(
            agent="researcher",
            model=_Model([_Reply("x")]),
            tools=[],
            system_prompt="x",
            user_messages=[],
            deadline=time.monotonic() - 1,
        )
    )
    assert result.stopped_reason == "deadline"


def test_budget_tokens_termination() -> None:
    tool = _Tool("get_realtime_price")
    reply = _Reply(
        tool_calls=[{"name": "get_realtime_price", "args": {}, "id": "c1"}],
        usage={"input_tokens": 100, "output_tokens": 100},
    )
    result = asyncio.run(
        run_tool_loop(
            agent="researcher",
            model=_Model([reply, _Reply("继续")]),
            tools=[tool],
            system_prompt="x",
            user_messages=[],
            token_budget=50,
        )
    )
    assert result.stopped_reason == "budget_tokens"


def test_tool_failure_termination() -> None:
    """同一个坏接口连续失败，不该死磕到底。"""

    tool = _Tool("broken", behaviour="raise")
    reply = _Reply(tool_calls=[{"name": "broken", "args": {}, "id": "c1"}])
    result = asyncio.run(
        run_tool_loop(
            agent="researcher",
            model=_Model([reply, reply, reply, reply]),
            tools=[tool],
            system_prompt="x",
            user_messages=[],
            max_iterations=6,
            max_consecutive_failures=2,
        )
    )
    assert result.stopped_reason == "tool_failure"
    assert tool.calls >= 2


def test_cancel_termination() -> None:
    state = {"cancelled": False}

    class _CancelModel(_Model):
        async def ainvoke(self, messages, config=None):
            state["cancelled"] = True
            return await super().ainvoke(messages, config)

    result = asyncio.run(
        run_tool_loop(
            agent="researcher",
            model=_CancelModel([_Reply("第一轮")]),
            tools=[],
            system_prompt="x",
            user_messages=[],
            is_cancelled=lambda: state["cancelled"],
        )
    )
    assert result.stopped_reason in ("cancelled", "completed")


# ----------------------------------------------------------------------
# 工具异常不杀死循环
# ----------------------------------------------------------------------
def test_tool_exception_is_captured_not_raised() -> None:
    tool = _Tool("broken", behaviour="raise")
    reply = _Reply(tool_calls=[{"name": "broken", "args": {}, "id": "c1"}])
    result = asyncio.run(
        run_tool_loop(
            agent="researcher",
            model=_Model([reply, _Reply("我换个工具再试")]),
            tools=[tool],
            system_prompt="x",
            user_messages=[],
        )
    )
    assert result.stopped_reason == "completed"
    assert result.records[0].ok is False
    assert "工具内部炸了" in (result.records[0].error or "")
    # 异常被兜成 is_error 结果，模型仍然拿到了结论机会
    assert result.text == "我换个工具再试"


def test_unknown_tool_is_reported_not_fatal() -> None:
    result = asyncio.run(
        run_tool_loop(
            agent="researcher",
            model=_Model(
                [
                    _Reply(tool_calls=[{"name": "不存在的工具", "args": {}, "id": "c1"}]),
                    _Reply("改用别的"),
                ]
            ),
            tools=[],
            system_prompt="x",
            user_messages=[],
        )
    )
    assert result.records[0].ok is False
    assert result.text == "改用别的"


# ----------------------------------------------------------------------
# normalize_result 的归一化规则
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("termination", "has_text", "expected_status"),
    [
        (Termination.SUBMIT, True, Status.COMPLETED),
        (Termination.BUDGET_ITERATIONS, True, Status.PARTIAL),
        (Termination.BUDGET_TOKENS, True, Status.PARTIAL),
        (Termination.BUDGET_WALLCLOCK, True, Status.PARTIAL),
        (Termination.TOOL_FAILURE, True, Status.PARTIAL),
        (Termination.MODEL_FAILURE, True, Status.PARTIAL),
        (Termination.CONTRACT_VIOLATION, True, Status.PARTIAL),
        (Termination.CANCELLED, True, Status.CANCELLED),
        (Termination.BUDGET_ITERATIONS, False, Status.FAILED),
        (Termination.MODEL_FAILURE, False, Status.FAILED),
    ],
)
def test_normalize_maps_every_termination(termination, has_text, expected_status) -> None:
    result = normalize_result(
        agent_name="researcher",
        text="有结论" if has_text else "",
        termination=termination,
    )
    assert isinstance(result, ExpertResult)
    assert result.status == expected_status
    assert result.agent_name == "researcher"
    assert result.to_dict()["termination_reason"] == str(termination)


def test_submit_without_output_is_contract_violation() -> None:
    """「模型不再调工具」但一句正文都没有，这不是完成，是失败。"""

    result = normalize_result(
        agent_name="researcher",
        text="   ",
        termination=Termination.SUBMIT,
    )
    assert result.termination_reason == Termination.CONTRACT_VIOLATION
    assert result.status == Status.FAILED
    assert result.error is not None and result.error.code == "empty_output"


def test_result_exposes_legacy_views() -> None:
    """下游改造期间仍能用旧属性名读结果。"""

    result = normalize_result(agent_name="quant", text="夏普 1.2", termination=Termination.SUBMIT)
    assert result.answer == "夏普 1.2"
    assert result.tool_calls == 0
    assert result.errors == []


def test_result_serialises_cleanly() -> None:
    result = normalize_result(
        agent_name="researcher",
        text="PE 21.5",
        termination=Termination.BUDGET_WALLCLOCK,
        error=StructuredError(code="deadline", message="超时", retryable=True),
        passes=["fundamental"],
    )
    payload = result.to_dict()
    assert payload["status"] == "partial"
    assert payload["termination_reason"] == "budget_wallclock"
    assert payload["error"]["retryable"] is True
    assert payload["output"]["length"] == len("PE 21.5")
