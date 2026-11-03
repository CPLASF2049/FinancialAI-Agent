"""后台运行与断流重连（2.0 P1-6）。

问题
----
现在一轮对话的生命周期挂在 HTTP 请求上：浏览器一切换会话 / 刷新 / 断网，
`StreamingResponse` 的生成器被取消，整轮执行随之而死。用户回来看到的是一片空白，
前面的算力与 token 全白费。

做法
----
把「执行」与「投递」拆开：

    run（后台任务） ----事件---> 缓冲（有序、可重放）
                                   |
                              订阅者（若干 SSE 连接）

* 客户端断开只结束**投递**，执行继续跑完
* 重连时带 ``after=<seq>``，从断点续播，不重跑任务
* 想真取消，用显式 cancel（Steering 的 ``cancel`` 指令），而不是靠关页面

策略可配：``FINAGENT_DISCONNECT_POLICY=cancel|continue``。
交互式短任务可以选 cancel；长研究任务应当 continue。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Run:
    """一次后台执行。"""

    run_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    #: 最终 done 事件（便于重连时直接收尾）
    final: dict[str, Any] | None = None
    task: asyncio.Task | None = None
    started: float = field(default_factory=time.time)
    #: 正在等待新事件的订阅者。用显式列表而不是 Condition：
    #: Condition 的通知如果早于 wait() 注册就会丢，表现为「重连后卡住」。
    waiters: list[asyncio.Event] = field(default_factory=list)

    @property
    def seq(self) -> int:
        return len(self.events)


class StreamHub:
    """按 run_id 管理后台执行与事件重放。"""

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}

    def create(self, run_id: str) -> Run:
        run = Run(run_id=run_id)
        self._runs[run_id] = run
        return run

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def status(self, run_id: str) -> dict[str, Any]:
        run = self._runs.get(run_id)
        if run is None:
            return {"run_id": run_id, "exists": False}
        return {
            "run_id": run_id,
            "exists": True,
            "events": len(run.events),
            "done": run.done,
            "elapsed_seconds": round(time.time() - run.started, 1),
        }

    # ------------------------------------------------------------------
    def start(
        self,
        run_id: str,
        producer: Callable[[Run], Awaitable[None]],
    ) -> Run:
        """启动后台执行。producer 负责往 run 里追加事件并标记 done。"""

        run = self.create(run_id)
        run.task = asyncio.create_task(self._wrap(run, producer))
        return run

    async def _wrap(self, run: Run, producer: Callable[[Run], Awaitable[None]]) -> None:
        try:
            await producer(run)
        except asyncio.CancelledError:  # 显式取消
            self.emit(run, "cancelled", {"run_id": run.run_id})
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务不能静默死掉
            self.emit(run, "error", {"message": str(exc)[:300]})
        finally:
            run.done = True
            self._wake(run)

    def emit(self, run: Run, event: str, payload: dict[str, Any] | None = None) -> None:
        """追加一条事件并唤醒所有订阅者。"""

        run.events.append({"seq": len(run.events) + 1, "event": event, "data": payload or {}})
        if event in ("done", "error", "cancelled"):
            run.final = run.events[-1]
        self._wake(run)

    @staticmethod
    def _wake(run: Run) -> None:
        """唤醒所有等待者。设置后立即移出列表，由订阅者自己决定是否再等。"""

        for waiter in list(run.waiters):
            waiter.set()
        run.waiters.clear()

    # ------------------------------------------------------------------
    async def subscribe(self, run_id: str, after: int = 0) -> AsyncIterator[dict[str, Any]]:
        """从 ``after`` 之后开始投递；执行跑完后自动结束。"""

        run = self.get(run_id)
        if run is None:
            yield {"seq": 0, "event": "error", "data": {"message": f"未知 run {run_id}"}}
            return

        index = max(0, int(after))
        while True:
            while index < len(run.events):
                yield run.events[index]
                index += 1
            if run.done:
                return
            waiter = asyncio.Event()
            run.waiters.append(waiter)
            # 注册之后再检查一次：注册前刚发出的通知不能丢
            if index < len(run.events):
                run.waiters.remove(waiter)
                continue
            try:
                await asyncio.wait_for(waiter.wait(), timeout=15.0)
            except TimeoutError:
                # 没有新事件时发心跳，避免代理把连接判死
                yield {"seq": index, "event": "ping", "data": {}}
            finally:
                if waiter in run.waiters:
                    run.waiters.remove(waiter)
            if run.done and index >= len(run.events):
                return

    def cancel(self, run_id: str) -> bool:
        run = self.get(run_id)
        if run is None or run.task is None or run.done:
            return False
        run.task.cancel()
        return True

    def cleanup(self, older_than_seconds: float = 3600.0) -> int:
        """清掉太久远且已结束的 run。"""

        now = time.time()
        stale = [
            run_id
            for run_id, run in self._runs.items()
            if run.done and now - run.started > older_than_seconds
        ]
        for run_id in stale:
            self._runs.pop(run_id, None)
        return len(stale)
