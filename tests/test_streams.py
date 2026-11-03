"""P1-6 验收测试：后台运行与断流重连。"""

from __future__ import annotations

import asyncio

import pytest

from finagent.web.streams import StreamHub


async def _collect(hub: StreamHub, run_id: str, after: int = 0, limit: int = 100) -> list[dict]:
    out: list[dict] = []
    async for item in hub.subscribe(run_id, after=after):
        out.append(item)
        if len(out) >= limit:
            break
    return out


def test_run_completes_and_events_are_ordered() -> None:
    hub = StreamHub()

    async def producer(run):
        for index in range(3):
            hub.emit(run, "token", {"text": str(index)})
        hub.emit(run, "done", {"answer": "ok"})

    async def scenario():
        run = hub.start("r1", producer)
        return await _collect(hub, "r1"), run

    events, run = asyncio.run(scenario())
    assert [item["event"] for item in events] == ["token", "token", "token", "done"]
    assert [item["seq"] for item in events] == [1, 2, 3, 4]
    assert run.done is True


def test_reconnect_resumes_from_seq() -> None:
    """断线重连必须从断点续播，而不是重跑整个任务。"""

    hub = StreamHub()

    async def producer(run):
        for index in range(3):
            hub.emit(run, "token", {"text": str(index)})
        hub.emit(run, "done", {})

    async def scenario():
        hub.start("r2", producer)
        await asyncio.sleep(0.05)
        # 前 2 条已经收到过（断线前），重连只该补发 3、4
        return await _collect(hub, "r2", after=2)

    events = asyncio.run(scenario())
    assert [item["seq"] for item in events] == [3, 4]


def test_subscriber_gets_live_events() -> None:
    hub = StreamHub()
    gate = asyncio.Event()

    async def producer(run):
        await gate.wait()
        hub.emit(run, "token", {"text": "live"})
        hub.emit(run, "done", {})

    async def scenario():
        hub.start("r3", producer)
        task = asyncio.create_task(_collect(hub, "r3"))
        await asyncio.sleep(0.05)
        gate.set()
        return await asyncio.wait_for(task, timeout=5)

    events = asyncio.run(scenario())
    assert any(item["event"] == "token" for item in events)
    assert events[-1]["event"] == "done"


def test_consumer_disconnect_does_not_kill_run() -> None:
    """订阅者中途退出，执行应当继续跑完——这是「切走就白跑」的修复点。"""

    hub = StreamHub()
    finished = asyncio.Event()

    async def producer(run):
        for index in range(5):
            hub.emit(run, "token", {"text": str(index)})
            await asyncio.sleep(0.01)
        finished.set()
        hub.emit(run, "done", {})

    async def scenario():
        run = hub.start("r4", producer)
        # 只读一条就"断开"
        async for _ in hub.subscribe("r4"):
            break
        await asyncio.wait_for(finished.wait(), timeout=5)
        return run

    run = asyncio.run(scenario())
    assert run.done is True
    # 断开之后事件仍在累积
    assert len(run.events) == 6


def test_unknown_run_reports_error() -> None:
    hub = StreamHub()
    events = asyncio.run(_collect(hub, "nope", limit=1))
    assert events[0]["event"] == "error"


def test_cancel_stops_the_run() -> None:
    hub = StreamHub()

    async def producer(run):
        await asyncio.sleep(5)
        hub.emit(run, "done", {})

    async def scenario():
        hub.start("r5", producer)
        await asyncio.sleep(0.05)
        return hub.cancel("r5")

    assert asyncio.run(scenario()) is True


def test_status_reports_progress() -> None:
    hub = StreamHub()

    async def producer(run):
        hub.emit(run, "token", {})
        hub.emit(run, "done", {})

    async def scenario():
        hub.start("r6", producer)
        await asyncio.sleep(0.05)
        return hub.status("r6")

    status = asyncio.run(scenario())
    assert status["exists"] is True
    assert status["events"] == 2
    assert status["done"] is True


def test_cleanup_removes_finished_runs() -> None:
    hub = StreamHub()

    async def producer(run):
        hub.emit(run, "done", {})

    async def scenario():
        hub.start("r7", producer)
        await asyncio.sleep(0.05)
        return hub.cleanup(older_than_seconds=-1)

    assert asyncio.run(scenario()) == 1
    assert hub.get("r7") is None
