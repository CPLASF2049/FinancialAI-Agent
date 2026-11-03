"""统一事件流（P1-1）验收测试。

命题：**给定 task_id 就能还原执行链路**，且三个常见问题不用额外追踪系统：
耗时最长的节点、失败的工具有哪些、为什么返工。
"""

from __future__ import annotations

import asyncio

import pytest

from finagent.core.events import EVENT_TYPES, EventStore, TurnRecorder


@pytest.fixture
def store(data_dir):
    return EventStore(data_dir / "events.db")


# ----------------------------------------------------------------------
# 存储层
# ----------------------------------------------------------------------
def test_append_assigns_monotonic_seq(store: EventStore) -> None:
    first = store.append("task-a", "turn:start", question="q")
    second = store.append("task-a", "route:decision", to_node="researcher")
    assert first.seq == 1
    assert second.seq == 2


def test_tasks_are_isolated(store: EventStore) -> None:
    store.append("task-a", "turn:start")
    store.append("task-b", "turn:start")
    assert len(store.query("task-a")) == 1
    assert len(store.query("task-b")) == 1
    assert set(store.tasks()) == {"task-a", "task-b"}


def test_replay_returns_ordered_timeline(store: EventStore) -> None:
    store.append("t1", "turn:start")
    store.append("t1", "step:start", agent="researcher")
    store.append("t1", "turn:end")
    timeline = store.replay("t1")
    assert [item["type"] for item in timeline] == ["turn:start", "step:start", "turn:end"]
    assert [item["seq"] for item in timeline] == [1, 2, 3]


def test_query_by_type(store: EventStore) -> None:
    store.append("t1", "tool:result", status="ok")
    store.append("t1", "tool:result", status="error")
    store.append("t1", "turn:end")
    assert len(store.query("t1", event_type="tool:result")) == 2


# ----------------------------------------------------------------------
# 三个开箱即用的查询
# ----------------------------------------------------------------------
def test_slowest_steps(store: EventStore) -> None:
    store.append("t1", "step:end", agent="researcher", wall_time_ms=500)
    store.append("t1", "step:end", agent="quant", wall_time_ms=9000)
    top = store.slowest_steps("t1", limit=1)
    assert top[0]["agent"] == "quant"
    assert top[0]["wall_time_ms"] == 9000


def test_failed_tools(store: EventStore) -> None:
    store.append("t1", "tool:result", tool="a", status="ok")
    store.append("t1", "tool:result", tool="b", status="error", error="boom")
    failed = store.failed_tools("t1")
    assert len(failed) == 1
    assert failed[0]["tool"] == "b"


def test_revisions_link_to_next_step(store: EventStore) -> None:
    store.append("t1", "review:verdict", agent="researcher", verdict="revise", issues=[{"detail": "x"}])
    store.append("t1", "step:start", agent="researcher")
    store.append("t1", "review:verdict", agent="researcher", verdict="pass")
    revisions = store.revisions("t1")
    assert len(revisions) == 1
    assert revisions[0]["next_agent"] == "researcher"


# ----------------------------------------------------------------------
# recorder
# ----------------------------------------------------------------------
def test_recorder_tolerates_missing_store() -> None:
    recorder = TurnRecorder(None, "t1")
    recorder.record("turn:start")  # 不应抛异常


def test_recorder_does_not_duplicate_task_id(store: EventStore) -> None:
    """调用方重复传 task_id 会撞参数——recorder 负责吞掉。"""

    TurnRecorder(store, "t1").record("turn:start", task_id="t1", question="q")
    event = store.query("t1")[0]
    assert event.task_id == "t1"
    assert "task_id" not in event.payload


# ----------------------------------------------------------------------
# 端到端：一次真实执行必须有完整时间线
# ----------------------------------------------------------------------
async def test_turn_produces_full_timeline(offline_env) -> None:
    from finagent.graph import open_app
    from finagent.settings import get_settings

    async with open_app() as app:
        await asyncio.wait_for(
            app.arun("茅台现在多少钱", user_id="ev", thread_id="ev-e2e"), timeout=120
        )
        store = app.events

    task_id = store.tasks()[0]
    types = [event.type for event in store.query(task_id)]

    assert types[0] == "turn:start"
    assert types[-1] == "turn:end"
    for required in ("route:decision", "step:start", "tool:call", "tool:result", "step:end"):
        assert required in types, f"缺少 {required}"

    # 三个查询都能用
    assert store.slowest_steps(task_id)
    assert store.failed_tools(task_id) == []
    assert get_settings().provider == "offline"


def test_event_types_cover_the_contract() -> None:
    """事件类型是公开契约，少一类就意味着回放缺一块。"""

    for required in (
        "turn:start",
        "route:decision",
        "step:start",
        "tool:call",
        "tool:result",
        "step:end",
        "review:verdict",
        "risk:triggered",
        "turn:end",
    ):
        assert required in EVENT_TYPES
