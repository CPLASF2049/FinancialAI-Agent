"""工具执行器（2.0 P0-2）：所有 MCP 工具的唯一执行入口。

职责边界
--------
* **registry** 知道有哪些工具、各自什么能力；
* **executor** 负责「怎么执行」：超时、异常归一、状态判定、统计；
* **policy** 定义「出错怎么办」：重试 / 放弃 / 跳过。

一次执行产出 :class:`ToolResult`，统计与日志都从它派生——
不再出现「失败 4 次但错误 0 条」这种自相矛盾。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from finagent.logging import get_logger
from finagent.runtime.execution_policy import Action, ExecutionPolicy
from finagent.runtime.tool_result import (
    ToolResult,
    ToolStats,
    ToolStatus,
    status_from_error,
)

log = get_logger("runtime.executor")


@dataclass(slots=True)
class ToolRegistry:
    """工具注册表：名字 -> 可调用对象 + 能力标签。"""

    tools: dict[str, Any] = field(default_factory=dict)
    #: 工具 -> 它依赖的数据能力（用于执行前预检）
    capabilities: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_toolkit(cls, toolkit: Any) -> ToolRegistry:
        tools = {tool.name: tool for tool in getattr(toolkit, "tools", [])}
        return cls(tools=tools)

    def get(self, name: str) -> Any | None:
        return self.tools.get(name)

    def names(self) -> list[str]:
        return sorted(self.tools)

    def __len__(self) -> int:
        return len(self.tools)


#: 由 Web 层注入的审批钩子。CLI 不注入 -> 敏感操作直接执行（本地单用户）。
#: 用 list 包一层是为了能在运行期替换而不必改签名。
GLOBAL_APPROVE: list = [None]


class ToolExecutor:
    """带超时与统一状态判定地执行一个工具。"""

    def __init__(self, registry: ToolRegistry, policy: ExecutionPolicy | None = None) -> None:
        self.registry = registry
        self.policy = policy or ExecutionPolicy()
        self.stats = ToolStats()
        #: 审批钩子：`async (call_id, tool, args) -> bool`。None 表示直通。
        self.approve: Any = None

    async def execute(
        self, name: str, arguments: dict[str, Any], *, call_id: str = ""
    ) -> ToolResult:
        started = time.monotonic()

        def elapsed() -> int:
            return int((time.monotonic() - started) * 1000)

        tool = self.registry.get(name)
        if tool is None:
            result = ToolResult(
                tool=name,
                status=ToolStatus.UNSUPPORTED,
                error_code="UNKNOWN_TOOL",
                error_message=f"未知工具 '{name}'（未注册）",
                call_id=call_id,
                execution_time_ms=elapsed(),
            )
            self._record(result)
            return result

        # 敏感操作先过审批。拒绝就不执行——不是「执行后提示」。
        if self.approve is not None:
            allowed = await self.approve(call_id, name, arguments)
            if not allowed:
                result = ToolResult(
                    tool=name,
                    status=ToolStatus.CANCELLED,
                    error_code="APPROVAL_DENIED",
                    error_message=f"用户拒绝了 {name} 的执行",
                    call_id=call_id,
                    execution_time_ms=elapsed(),
                )
                self._record(result)
                return result

        try:
            payload = await asyncio.wait_for(
                tool.ainvoke(arguments), timeout=self.policy.timeout_seconds
            )
        except TimeoutError:
            result = ToolResult(
                tool=name,
                status=ToolStatus.TIMEOUT,
                error_code="TIMEOUT",
                error_message=f"{name} 超时（>{self.policy.timeout_seconds:g}s）",
                retryable=True,
                call_id=call_id,
                execution_time_ms=elapsed(),
            )
            self._record(result)
            return result
        except Exception as exc:  # noqa: BLE001 - 异常不外泄，统一成状态
            result = ToolResult(
                tool=name,
                status=ToolStatus.FAILED,
                error_code=_classify(exc),
                error_message=str(exc)[:300],
                retryable=True,
                call_id=call_id,
                execution_time_ms=elapsed(),
            )
            self._record(result)
            return result

        result = _parse_payload(name, payload, call_id=call_id, elapsed=elapsed())
        self._record(result)
        return result

    # ------------------------------------------------------------------
    def _record(self, result: ToolResult) -> None:
        self.stats.record(result)
        if result.failed:
            self.stats.max_consecutive_failures = max(
                self.stats.max_consecutive_failures, self._consecutive(result)
            )
        else:
            self._streak = 0

    _streak: int = 0

    def _consecutive(self, result: ToolResult) -> int:
        self._streak = self._streak + 1 if result.failed else 0
        return self._streak

    def summary(self) -> dict[str, Any]:
        return self.stats.to_dict()


def _text_of(payload: Any) -> str:
    """把 MCP 工具返回值摊平成文本。

    LangChain 的 MCP 适配器会返回 ``[{"type": "text", "text": "{...}"}]`` 这种
    content-block 列表，而不是裸 JSON 字符串。不处理它就会把完整的错误信封
    当成「一段成功返回的文本」——状态判断全错。
    """

    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        parts: list[str] = []
        for item in payload:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        if parts:
            return "\n".join(parts)
        return json.dumps(payload, ensure_ascii=False, default=str)
    if isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False, default=str)
    return str(payload)


def _parse_payload(name: str, payload: Any, *, call_id: str, elapsed: int) -> ToolResult:
    """把工具返回值（信封 / content-block / 裸数据）归一成 ToolResult。"""

    if isinstance(payload, dict):
        parsed = payload
    else:
        raw = _text_of(payload)
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # 非 JSON 文本：当成成功但无结构的数据
            return ToolResult(
                tool=name,
                status=ToolStatus.SUCCESS,
                data=raw,
                call_id=call_id,
                execution_time_ms=elapsed,
            )

    if isinstance(parsed, dict) and parsed.get("ok") is False:
        error = parsed.get("error") or {}
        code = str(error.get("code") or "tool_failed") if isinstance(error, dict) else "tool_failed"
        return ToolResult(
            tool=name,
            status=status_from_error(code),
            error_code=code,
            error_message=(error.get("message") if isinstance(error, dict) else str(error)),
            retryable=bool(error.get("retryable")) if isinstance(error, dict) else False,
            provider=error.get("provider") if isinstance(error, dict) else None,
            call_id=call_id,
            execution_time_ms=elapsed,
        )

    data = parsed.get("data") if isinstance(parsed, dict) and "ok" in parsed else parsed
    warnings = parsed.get("warnings") if isinstance(parsed, dict) else None
    status = ToolStatus.PARTIAL if warnings else ToolStatus.SUCCESS
    return ToolResult(
        tool=name,
        status=status,
        data=data,
        warnings=list(warnings or []),
        provider=parsed.get("provider") if isinstance(parsed, dict) else None,
        data_as_of=parsed.get("data_as_of") if isinstance(parsed, dict) else None,
        call_id=call_id,
        execution_time_ms=elapsed,
    )


def _classify(exc: Exception) -> str:
    """异常 -> 稳定错误码。"""

    text = f"{type(exc).__name__} {exc}".lower()
    if "remote end closed" in text or "connection reset" in text:
        return "REMOTE_DISCONNECTED"
    if "timed out" in text or "timeout" in text:
        return "TIMEOUT"
    if "not available" in text or "不支持" in text or "不提供" in text:
        return "CAPABILITY_NOT_SUPPORTED"
    return "TOOL_EXCEPTION"


def action_for(code: str | None) -> Action:
    from finagent.runtime.execution_policy import ERROR_POLICY
    from finagent.runtime.execution_policy import Action as _Action

    return ERROR_POLICY.get(str(code or ""), _Action.FAIL).action
