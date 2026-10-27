"""后台预警 worker。

预警的求值与投递放在一个独立进程里，而不是寄生在聊天会话中，因此：

* 可以无界面运行（``finagent alerts --watch``），不需要开着聊天窗口；
* 走的是 Agent 创建预警时同一套代码路径；
* 投递是可插拔的通知链（控制台、JSONL 审计文件、可选 SMTP）。
"""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass
from typing import Any

from finagent.core import calendar as cal
from finagent.graph.mcp_client import open_toolkit
from finagent.graph.tooling import ToolRunner
from finagent.logging import get_logger
from finagent.settings import Settings, get_settings

log = get_logger("worker")


@dataclass(slots=True)
class CheckSummary:
    evaluated: int = 0
    triggered: list[dict[str, Any]] | None = None
    skipped: str | None = None
    checks: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "checks": self.checks,
            "evaluated": self.evaluated,
            "triggered": len(self.triggered or []),
            "skipped": self.skipped,
        }


async def check_once(
    settings: Settings | None = None, *, trading_hours_only: bool | None = None
) -> CheckSummary:
    """通过 MCP 持仓服务器把所有预警评估一次。"""

    resolved = settings or get_settings()
    only_hours = (
        resolved.alert_trading_hours_only if trading_hours_only is None else trading_hours_only
    )

    async with open_toolkit(resolved) as toolkit:
        runner = ToolRunner(toolkit=toolkit, agent="worker")
        outcome = await runner.call("check_alerts", trading_hours_only=only_hours)
        if not outcome.ok:
            log.error("预警检查失败：%s", outcome.error)
            return CheckSummary(skipped=outcome.error, checks=1)
        data = outcome.data or {}
        triggered = data.get("triggered") or []
        if triggered:
            for event in triggered:
                log.warning("已触发 %s", event.get("message"))
        return CheckSummary(
            evaluated=int(data.get("evaluated") or 0),
            triggered=triggered,
            skipped=data.get("skipped"),
            checks=1,
        )


async def watch(
    settings: Settings | None = None,
    *,
    interval_seconds: int | None = None,
    iterations: int | None = None,
) -> CheckSummary:
    """持续轮询预警，直到被打断（或达到 ``iterations``）。"""

    resolved = settings or get_settings()
    interval = max(5, int(interval_seconds or resolved.alert_poll_seconds))
    stop = asyncio.Event()

    def _request_stop(*_args: Any) -> None:
        log.info("收到停止信号，跑完当前轮次")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows
            pass

    total = CheckSummary(triggered=[], checks=0)
    cycle = 0
    log.info(
        "预警 worker 启动（间隔=%ss，仅交易时段=%s）",
        interval,
        resolved.alert_trading_hours_only,
    )

    while not stop.is_set():
        cycle += 1
        summary = await check_once(resolved)
        total.checks += 1
        total.evaluated = summary.evaluated
        total.skipped = summary.skipped
        total.triggered = [*(total.triggered or []), *(summary.triggered or [])]
        log.info(
            "第 %d 轮：评估 %d 条，触发 %d 条%s",
            cycle,
            summary.evaluated,
            len(summary.triggered or []),
            f"（跳过：{summary.skipped}）" if summary.skipped else "",
        )

        if iterations is not None and cycle >= iterations:
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue

    log.info("预警 worker 已停止，共运行 %d 轮", total.checks)
    return total


def is_market_open() -> bool:
    return cal.is_trading_now()


async def run_once() -> CheckSummary:
    return await check_once()
