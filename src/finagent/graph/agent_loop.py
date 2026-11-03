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
    #: 工具调用统计（成功/失败/跳过/重试），由执行器产生。
    tool_stats: dict[str, Any] = field(default_factory=dict)

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
    #: 上下文预算（token）。超过就先压缩中间工具结果，再发起模型请求。
    context_budget: int = 24_000,
    #: 压缩发生时的回调。把「压掉了什么」记进事件表，恢复时才有上下文可依。
    on_compact: Any = None,
) -> LoopResult:
    """跑到模型不再调用工具为止。

    实现委托给 :class:`finagent.runtime.agent_runtime.AgentRuntime`——
    统一执行循环、预算与三层上下文都在那边，这里只做**记录格式转换**，
    对外行为（stopped_reason / records / tool_stats）保持不变。
    """

    from finagent.runtime import Budget
    from finagent.runtime.agent_runtime import AgentRuntime

    emit = stream_writer()
    budget = Budget(
        max_llm_calls=max_iterations,
        max_tool_calls=10_000,
        max_retries=max(0, max_consecutive_failures - 3),
    )

    runtime = AgentRuntime(
        agent=agent,
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        budget=budget,
        context_budget=context_budget,
        max_iterations=max_iterations,
        deadline=deadline,
        is_cancelled=is_cancelled,
        on_compact=on_compact,
        stage=stage,
        token_budget=token_budget,
    )
    outcome = await runtime.run(user_messages, emit=emit)

    # 把执行器的调用明细搬进 LoopResult（下游 critic / 事件流都读它）
    result = LoopResult(
        text=outcome.text,
        iterations=outcome.iterations,
        stopped_reason=_map_reason(outcome.stopped_reason),
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
        consecutive_failures=runtime.executor.stats.max_consecutive_failures,
        tool_stats=outcome.stats,
    )
    for item in runtime.records:
        result.records.append(
            ToolCallRecord(
                stage=str(item.get("stage") or ""),
                agent=str(item.get("agent") or agent),
                tool=str(item.get("tool") or ""),
                args=dict(item.get("args") or {}),
                ok=bool(item.get("ok")),
                preview=str(item.get("preview") or ""),
                error=str(item.get("error") or "") or None,
                call_id=str(item.get("call_id") or ""),
                latency_ms=int(item.get("latency_ms") or 0),
                result_tokens=int(item.get("result_tokens") or 0),
            )
        )
    emit(
        {
            "event": "agent_end",
            "agent": agent,
            "tool_calls": result.tool_calls,
            "stopped": result.stopped_reason,
            "tool_stats": result.tool_stats,
        }
    )
    return result


#: 执行循环的停止原因 -> 对外契约里的原因。保持既有测试与事件语义不变。
_REASON_MAP = {
    "completed": "completed",
    "max_iterations": "max_iterations",
    "deadline": "deadline",
    "budget_llm_calls": "max_iterations",
    "budget_tool_calls": "max_iterations",
    "budget_cost": "max_iterations",
    "tool_failure": "tool_failure",
    "cancelled": "cancelled",
}


def _map_reason(reason: str) -> str:
    return _REASON_MAP.get(str(reason), str(reason))


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
