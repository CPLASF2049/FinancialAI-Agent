"""单个专家 Agent 的显式 ReAct 循环。

为什么不用 ``create_react_agent``（LangGraph 1.x 已废弃）或 ``ToolNode``？因为外层
图需要三样通用助手节点不提供的东西：每次调用都记录「哪个工具跑了、是否成功」
（供 ``trace`` 通道和测试断言）、向 CLI 发自定义流事件，以及一个模型再啰嗦也绕不过
去的硬迭代上限。二十行显式循环，完全可调试，更划算。

循环是 *async* 的，因为 MCP 工具就是异步的。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from finagent.logging import get_logger

log = get_logger("graph.loop")

try:  # custom stream events are optional (only the CLI consumes them)
    from langgraph.config import get_stream_writer

    def stream_writer() -> Callable[[dict[str, Any]], None]:
        """返回 LangGraph 的自定义事件写入器；不在流式运行中时返回空操作。"""

        try:
            return get_stream_writer()
        except Exception:  # noqa: BLE001 - outside a streaming run
            return lambda _payload: None

except Exception:  # pragma: no cover - very old LangGraph

    def stream_writer() -> Callable[[dict[str, Any]], None]:
        return lambda _payload: None


@dataclass(slots=True)
class ToolCallRecord:
    #: **裸的 Agent 名**（researcher / quant / alert）。审查者按这个名字挑出本轮证据，
    #: 所以这里绝不能拼子职责后缀——拼了就变成 "researcher:screening"，按 "researcher"
    #: 精确匹配永远匹配不到，审查者会一直误判「没有调用任何工具」。
    agent: str
    tool: str
    args: dict[str, Any]
    ok: bool = True
    preview: str = ""
    error: str | None = None
    #: 这次调用属于哪个子职责（screening / fundamental / technical …），仅用于观测。
    stage: str = ""
    #: 调用标识，与 ``tool:call`` 事件对应，同时也是幂等键。
    call_id: str = ""
    #: 这次调用花了多久（毫秒）。
    latency_ms: int = 0
    #: 结果占多少 token（粗略估算，用于上下文预算）。
    result_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "agent": self.agent,
            "tool": self.tool,
            "args": self.args,
            "ok": self.ok,
            "preview": self.preview,
        }
        if self.stage:
            payload["stage"] = self.stage
        if self.call_id:
            payload["call_id"] = self.call_id
        payload["latency_ms"] = self.latency_ms
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(slots=True)
class LoopResult:
    text: str = ""
    records: list[ToolCallRecord] = field(default_factory=list)
    iterations: int = 0
    stopped_reason: str = "completed"
    input_tokens: int = 0
    output_tokens: int = 0
    #: 连续失败的工具调用次数（成功一次就清零）。用于 ``tool_failure`` 终止。
    consecutive_failures: int = 0

    @property
    def tool_calls(self) -> int:
        return len(self.records)


def _preview(payload: Any, limit: int = 600) -> str:
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


def stringify_tool_output(payload: Any) -> str:
    """把 ``langchain-mcp-adapters`` 返回的各种形态统一成一段文本。

    MCP 工具可能返回裸字符串、单个 content block，或 content block 列表
    （文本/图片/资源）。Fin-Agent 暴露的全是文本，所以把文本块拼起来，其余情况
    退回 JSON。
    """

    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        if payload.get("type") == "text" and "text" in payload:
            return str(payload["text"])
        if set(payload) == {"content"} and isinstance(payload["content"], (list, str)):
            return stringify_tool_output(payload["content"])
        return json.dumps(payload, ensure_ascii=False, default=str)
    if isinstance(payload, (list, tuple)):
        parts: list[str] = []
        for item in payload:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif "text" in item:
                    parts.append(str(item["text"]))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, default=str))
            else:
                parts.append(stringify_tool_output(item))
        return "".join(parts)
    return str(payload)


def _parse_envelope(payload: Any) -> tuple[bool, str | None]:
    """识别我们 MCP 工具的 ``{"ok": false, "error": ...}`` 信封。"""

    if not isinstance(payload, str):
        return True, None
    stripped = payload.strip()
    if not stripped.startswith("{"):
        return True, None
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return True, None
    if isinstance(parsed, dict) and parsed.get("ok") is False:
        error = parsed.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else str(error)
        hint = error.get("hint") if isinstance(error, dict) else None
        return False, f"{message}" + (f" ({hint})" if hint else "")
    return True, None


async def run_tool_loop(
    *,
    agent: str,
    model: Any,
    tools: Iterable[Any],
    system_prompt: str,
    user_messages: list[Any],
    max_iterations: int = 8,
    #: 子职责名（screening / fundamental …）。只写进记录供观测，
    #: **不**拼进 agent 名——审查者按裸 agent 名挑证据。
    stage: str = "",
    #: 本轮截止时刻（``time.monotonic()`` 绝对值）。到了就停，别把整轮拖成几分钟。
    deadline: float | None = None,
    #: token 预算；模型回报 usage 时累计，超了就停。
    token_budget: int | None = None,
    #: 连续失败多少个工具调用就放弃（防止对着一个坏接口死磕）。
    max_consecutive_failures: int = 3,
    #: 外部取消信号。返回 True 就停。
    is_cancelled: Any = None,
) -> LoopResult:
    """跑到模型不再调用工具为止，并逐次记录每次调用。"""

    import time

    from langchain_core.messages import SystemMessage, ToolMessage

    tool_list = list(tools)
    by_name = {tool.name: tool for tool in tool_list}
    bound = model.bind_tools(tool_list) if tool_list else model
    emit = stream_writer()

    messages: list[Any] = [SystemMessage(content=system_prompt)]
    messages.extend(user_messages)

    result = LoopResult()
    emit({"event": "agent_start", "agent": agent, "tools": sorted(by_name)})

    for iteration in range(1, max_iterations + 1):
        # 时间预算要能穿透到节点内部：模型慢的时候，光靠 supervisor 之间检查
        # 拦不住单个专家——它内部可能连着跑好几次工具调用，把一轮拖到几分钟。
        if is_cancelled is not None and is_cancelled():
            log.info("工具循环收到取消信号，停止")
            result.stopped_reason = "cancelled"
            break

        if token_budget is not None and (result.input_tokens + result.output_tokens) > token_budget:
            log.warning("token 预算用尽，停止继续调用工具")
            result.stopped_reason = "budget_tokens"
            break

        if deadline is not None and time.monotonic() > deadline:
            log.warning("工具循环已超时（第 %d 轮前），停止继续调用工具", iteration)
            result.stopped_reason = "deadline"
            break

        result.iterations = iteration
        response = await _call_model(bound, messages, agent=agent, emit=emit)
        messages.append(response)

        # 计费口径：LangChain 的 usage_metadata 是标准字段，没有就跳过。
        usage = getattr(response, "usage_metadata", None) or {}
        if isinstance(usage, dict):
            result.input_tokens += int(usage.get("input_tokens") or 0)
            result.output_tokens += int(usage.get("output_tokens") or 0)

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            result.text = _content_to_text(response)
            result.stopped_reason = "completed"
            break

        if result.consecutive_failures >= max_consecutive_failures:
            log.warning("连续 %d 次工具失败，停止本轮工具循环", result.consecutive_failures)
            result.stopped_reason = "tool_failure"
            break

        for call in tool_calls:
            name = call.get("name") or ""
            args = call.get("args") or {}
            call_id = call.get("id") or f"{agent}-{iteration}-{name}"

            record = ToolCallRecord(stage=stage, agent=agent, tool=name, args=args)
            tool = by_name.get(name)
            started = time.monotonic()
            if tool is None:
                record.ok = False
                record.error = f"未知工具 '{name}'"
                payload: Any = json.dumps(
                    {"ok": False, "error": {"code": "unknown_tool", "message": record.error}},
                    ensure_ascii=False,
                )
            else:
                emit({"event": "tool_call", "agent": agent, "tool": name, "args": args})
                started = time.monotonic()
                try:
                    payload = await tool.ainvoke(args)
                except Exception as exc:  # noqa: BLE001 - surface, never crash the graph
                    log.warning("%s/%s failed: %s", agent, name, exc)
                    record.ok = False
                    record.error = str(exc)
                    payload = json.dumps(
                        {"ok": False, "error": {"code": "tool_exception", "message": str(exc)}},
                        ensure_ascii=False,
                    )

            if record.ok:
                ok_flag, error = _parse_envelope(stringify_tool_output(payload))
                if not ok_flag:
                    record.ok = False
                    record.error = error
            record.preview = _preview(stringify_tool_output(payload))
            record.call_id = call_id
            record.latency_ms = int((time.monotonic() - started) * 1000)
            record.result_tokens = max(1, len(record.preview) // 4)
            result.consecutive_failures = 0 if record.ok else result.consecutive_failures + 1
            result.records.append(record)
            emit(
                {
                    "event": "tool_result",
                    "agent": agent,
                    "tool": name,
                    "ok": record.ok,
                    "preview": record.preview[:220],
                }
            )

            messages.append(
                ToolMessage(
                    content=stringify_tool_output(payload)[:20_000],
                    tool_call_id=call_id,
                    name=name,
                )
            )
    else:
        result.stopped_reason = "max_iterations"
        result.text = result.text or "已达到工具调用上限，以下为目前掌握的信息。"

    emit(
        {
            "event": "agent_end",
            "agent": agent,
            "tool_calls": result.tool_calls,
            "stopped": result.stopped_reason,
        }
    )
    return result


async def _call_model(
    bound: Any,
    messages: list[Any],
    *,
    agent: str,
    emit: Any,
) -> Any:
    """调用模型；能流式就流式，把增量文本作为 ``token`` 事件抛出去。

    流式对前端体验是决定性的：用户看到字节在长出，而不是盯着空白等 20 秒。但流式也要
    能优雅降级——不支持 ``astream`` 的模型（比如测试里的脚本化模型）直接回退到 ``ainvoke``。
    """

    astream = getattr(bound, "astream", None)
    if astream is None:
        return await bound.ainvoke(messages)

    accumulated: Any = None
    try:
        async for chunk in astream(messages):
            accumulated = chunk if accumulated is None else accumulated + chunk
            text = _chunk_text(chunk)
            if text:
                emit({"event": "token", "agent": agent, "text": text})
        if accumulated is not None:
            return accumulated
    except Exception as exc:  # noqa: BLE001 - 流式失败就退回一次性调用
        log.warning("流式调用失败（%s），回退到非流式", exc)

    return await bound.ainvoke(messages)


def _chunk_text(chunk: Any) -> str:
    """只取出一个流式片段里的可见文本（工具调用片段不含文本）。"""

    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for block in content:
            if isinstance(block, str):
                out.append(block)
            elif isinstance(block, dict) and block.get("type") in (None, "text"):
                out.append(str(block.get("text", "")))
        return "".join(out)
    return ""


def _content_to_text(message: Any) -> str:
    """把 LangChain 的消息内容（字符串或内容块列表）压平成文本。"""

    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif "text" in block:
                    parts.append(str(block["text"]))
        return "".join(parts)
    return str(content)
