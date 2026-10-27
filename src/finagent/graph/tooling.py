"""确定性专家共用的轻量工具调用helper。

集中处理三件事，让每个专家行为一致：

* 按名查找工具，服务器没暴露时给出清晰错误；
* 解析信封（``ok`` / ``error.code`` / ``hint``）；
* 把每次调用记进 ``trace`` 状态通道。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from finagent.errors import FinAgentError
from finagent.graph.agent_loop import (
    ToolCallRecord,
    stream_writer,
    stringify_tool_output,
)
from finagent.graph.mcp_client import Toolkit
from finagent.logging import get_logger

log = get_logger("graph.tooling")


@dataclass(slots=True)
class ToolOutcome:
    ok: bool
    data: Any = None
    error: str | None = None
    code: str | None = None
    hint: str | None = None
    raw: str = ""

    def need(self) -> Any:
        """返回 ``data``，失败则抛出携带服务端消息的 FinAgentError。"""

        if self.ok:
            return self.data
        raise FinAgentError(self.error or "tool failed", hint=self.hint)


@dataclass(slots=True)
class ToolRunner:
    """调用 MCP 工具并累积 trace。"""

    toolkit: Toolkit
    agent: str
    records: list[ToolCallRecord] = field(default_factory=list)
    #: 是否把每次调用推给前端。记忆注入这类内部管线动作设为 False，避免刷屏。
    emit_events: bool = True

    async def call(self, name: str, **arguments: Any) -> ToolOutcome:
        record = ToolCallRecord(agent=self.agent, tool=name, args=arguments)
        emit = stream_writer() if self.emit_events else (lambda _payload: None)
        tool = self.toolkit.by_name.get(name)
        if tool is None:
            record.ok = False
            record.error = f"tool '{name}' is not exposed by the MCP servers"
            self.records.append(record)
            log.error("%s", record.error)
            return ToolOutcome(ok=False, error=record.error, code="unknown_tool")

        emit({"event": "tool_call", "agent": self.agent, "tool": name, "args": arguments})
        try:
            payload = await tool.ainvoke(arguments)
        except Exception as exc:  # noqa: BLE001
            record.ok = False
            record.error = str(exc)
            record.preview = ""
            self.records.append(record)
            log.warning("%s/%s raised: %s", self.agent, name, exc)
            emit(
                {
                    "event": "tool_result",
                    "agent": self.agent,
                    "tool": name,
                    "ok": False,
                    "preview": str(exc)[:220],
                }
            )
            return ToolOutcome(ok=False, error=str(exc), code="tool_exception")

        raw = stringify_tool_output(payload)
        record.preview = raw[:600]
        parsed: Any
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            record.ok = True
            self.records.append(record)
            emit(
                {
                    "event": "tool_result",
                    "agent": self.agent,
                    "tool": name,
                    "ok": True,
                    "preview": raw[:220],
                }
            )
            return ToolOutcome(ok=True, data=raw, raw=raw)

        if isinstance(parsed, dict) and parsed.get("ok") is False:
            error = parsed.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            hint = error.get("hint") if isinstance(error, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            record.ok = False
            record.error = message
            self.records.append(record)
            log.info("%s/%s -> %s: %s", self.agent, name, code, message)
            emit(
                {
                    "event": "tool_result",
                    "agent": self.agent,
                    "tool": name,
                    "ok": False,
                    "preview": str(message)[:220],
                }
            )
            return ToolOutcome(ok=False, error=message, code=code, hint=hint, raw=raw)

        record.ok = True
        self.records.append(record)
        emit(
            {
                "event": "tool_result",
                "agent": self.agent,
                "tool": name,
                "ok": True,
                "preview": raw[:220],
            }
        )
        data = parsed.get("data") if isinstance(parsed, dict) and "data" in parsed else parsed
        return ToolOutcome(ok=True, data=data, raw=raw)

    def to_dicts(self) -> list[dict[str, Any]]:
        return [record.to_dict() for record in self.records]

    @property
    def failures(self) -> list[ToolCallRecord]:
        return [record for record in self.records if not record.ok]
