"""工具审批闸门：敏感操作执行前必须有人点「允许」。

为什么要做在后端
----------------
前端弹个窗、后端照跑，那是假审批。真正的闸门必须在**工具执行之前**拦住，
并且真的能拒绝。这里用「按 call_id 等待一个 future」实现：

    工具执行 -> 需要审批 -> emit approval_request -> 前端弹窗
                                                  -> POST /api/approve
                                                  -> future 完成 -> 执行/放弃
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from finagent.logging import get_logger

log = get_logger("web.approval")

#: 这些工具会改变用户账户里的真实数据，执行前必须确认。
NEEDS_APPROVAL = frozenset(
    {
        "add_position",
        "remove_position",
        "update_position",
        "clear_portfolio",
        "add_price_alert",
        "remove_price_alert",
    }
)


@dataclass(slots=True)
class ApprovalRequest:
    call_id: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    future: asyncio.Future | None = None


class ApprovalBroker:
    """按 call_id 挂起，等人点了再放行（或拒绝）。"""

    def __init__(self) -> None:
        self._pending: dict[str, ApprovalRequest] = {}
        self._emit: Callable[[dict[str, Any]], None] | None = None

    def attach(self, emit: Callable[[dict[str, Any]], None]) -> None:
        """绑上当前的 SSE 推送函数（每个 run 一个）。"""

        self._emit = emit

    def needs(self, tool: str, *, enabled: bool) -> bool:
        return enabled and tool in NEEDS_APPROVAL

    async def request(
        self, call_id: str, tool: str, args: dict[str, Any], *, timeout: float = 120.0
    ) -> bool:
        """挂起等待审批。超时视为拒绝——宁可少做，不可擅自做。"""

        loop = asyncio.get_running_loop()
        request = ApprovalRequest(call_id=call_id, tool=tool, args=args, future=loop.create_future())
        self._pending[call_id] = request

        if self._emit is not None:
            self._emit(
                {
                    "event": "approval_request",
                    "call_id": call_id,
                    "tool": tool,
                    "args": args,
                }
            )

        try:
            return bool(await asyncio.wait_for(request.future, timeout=timeout))
        except TimeoutError:
            log.warning("审批超时，按拒绝处理：%s", tool)
            return False
        finally:
            self._pending.pop(call_id, None)

    def resolve(self, call_id: str, allowed: bool) -> bool:
        request = self._pending.get(call_id)
        if request is None or request.future is None or request.future.done():
            return False
        request.future.set_result(allowed)
        return True

    def pending(self) -> list[str]:
        return list(self._pending)


#: 进程级单例：Web 层注册，工具执行时查询。
BROKER = ApprovalBroker()
