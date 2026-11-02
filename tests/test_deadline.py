"""回归测试：时间预算必须能穿透到节点内部的工具循环。"""

from __future__ import annotations

import time

import pytest

from finagent.graph.agent_loop import run_tool_loop


class _FakeTool:
    name = "fake_tool"
    description = "假的"

    async def ainvoke(self, args, config=None):
        return '{"ok": true, "data": {"rows": []}}'


def test_tool_loop_stops_at_deadline() -> None:
    """截止时间已过：一次模型调用都不该发生。"""

    calls = {"n": 0}

    class Boom:
        async def ainvoke(self, messages, config=None):
            calls["n"] += 1
            raise AssertionError("超时之后不该再调用模型")

    import asyncio

    result = asyncio.run(
        run_tool_loop(
            agent="researcher",
            model=Boom(),
            tools=[],
            system_prompt="x",
            user_messages=[],
            deadline=time.monotonic() - 1,
        )
    )
    assert result.stopped_reason == "deadline"
    assert calls["n"] == 0


def test_tool_loop_runs_without_deadline() -> None:
    """不传 deadline 时行为不变。"""

    class Model:
        async def ainvoke(self, messages, config=None):
            class Reply:
                content = "完成"
                tool_calls = []

            return Reply()

    import asyncio

    result = asyncio.run(
        run_tool_loop(
            agent="researcher",
            model=Model(),
            tools=[],
            system_prompt="x",
            user_messages=[],
        )
    )
    assert result.stopped_reason == "completed"
    assert result.text == "完成"


@pytest.mark.parametrize("agent", ["researcher", "quant"])
def test_deadline_param_is_accepted_by_run_expert(agent: str) -> None:
    import inspect

    from finagent.graph.experts import run_expert

    assert "deadline" in inspect.signature(run_expert).parameters
