"""统一执行循环（2.0 P1-4）。

把「模型调用 + 工具执行」从专家里抽出来，做成一个可复用组件：

    AgentRuntime
      ├─ 执行预算（LLM 次数 / 工具次数 / token / 墙钟 / 取消）
      ├─ 三层上下文（核心 / 摘要 / 详情，按预算压缩）
      ├─ 工具执行器（超时、状态、统计）
      └─ 事件回调（token 增量、工具调用、结束）

LangGraph 仍然是流程引擎；这里只是**一次 step 内部**的执行循环。
好处是预算与压缩只有一份实现，专家不再各写一套。
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from finagent.logging import get_logger
from finagent.runtime.agent_context import AgentContext, Layer
from finagent.runtime.execution_policy import Budget, ExecutionPolicy
from finagent.runtime.tool_executor import ToolExecutor, ToolRegistry

log = get_logger("runtime.agent")


@dataclass(slots=True)
class RuntimeOutcome:
    """一次执行循环的结果。"""

    text: str = ""
    #: completed / max_iterations / deadline / budget_tokens / budget_llm_calls /
    #: budget_tool_calls / tool_failure / cancelled
    stopped_reason: str = "completed"
    iterations: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    stats: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text_length": len(self.text),
            "stopped_reason": self.stopped_reason,
            "iterations": self.iterations,
            "tool_calls": self.tool_calls,
            "tokens": {"input": self.input_tokens, "output": self.output_tokens},
            "stats": self.stats,
            "context": self.context,
        }


class AgentRuntime:
    """一次 step 的执行循环：模型 -> 工具 -> 模型 …… 直到收尾或触顶。"""

    def __init__(
        self,
        *,
        agent: str,
        model: Any,
        tools: Iterable[Any],
        system_prompt: str,
        budget: Budget | None = None,
        executor: ToolExecutor | None = None,
        context_budget: int = 24_000,
        max_iterations: int = 8,
        deadline: float | None = None,
        is_cancelled: Any = None,
        on_compact: Any = None,
        stage: str = "",
        #: token 预算：累计用量超过即停（与预算里的次数限制并列）
        token_budget: int | None = None,
    ) -> None:
        self.agent = agent
        self.stage = stage
        self.tool_list = list(tools)
        self.by_name = {tool.name: tool for tool in self.tool_list}
        self.bound = model.bind_tools(self.tool_list) if self.tool_list else model
        self.budget = budget or Budget()
        self.executor = executor or ToolExecutor(
            ToolRegistry(tools=dict(self.by_name)),
            ExecutionPolicy(max_consecutive_failures=self.budget.max_retries + 3),
        )
        self.context_budget = context_budget
        self.max_iterations = max_iterations
        self.deadline = deadline
        self.is_cancelled = is_cancelled
        self.on_compact = on_compact

        #: 每次工具调用的明细（供上层转成 trace/事件）
        self.records: list[dict[str, Any]] = []
        self.token_budget = token_budget
        self._tokens = 0
        self.context = AgentContext()
        self.context.add(_system(system_prompt), Layer.CORE)

    # ------------------------------------------------------------------
    async def run(self, user_messages: list[Any], *, emit: Any) -> RuntimeOutcome:
        from langchain_core.messages import ToolMessage

        outcome = RuntimeOutcome()
        for message in user_messages:
            self.context.add(message, Layer.CORE)

        emit({"event": "agent_start", "agent": self.agent, "tools": sorted(self.by_name)})

        for iteration in range(1, self.max_iterations + 1):
            stop = self._preflight(iteration, outcome)
            if stop:
                outcome.stopped_reason = stop
                break

            outcome.iterations = iteration
            compacted = self.context.compact(self.context_budget)
            if compacted["before_tokens"] != compacted["after_tokens"]:
                log.info(
                    "上下文压缩：%d -> %d tokens",
                    compacted["before_tokens"],
                    compacted["after_tokens"],
                )
                if self.on_compact is not None:
                    try:
                        self.on_compact(compacted)
                    except Exception:  # noqa: BLE001
                        pass

            self.budget.spend_llm()
            response = await call_model_streaming(
                self.bound, self.context.messages(), agent=self.agent, emit=emit
            )
            self.context.add(response, Layer.SUMMARY)

            usage = getattr(response, "usage_metadata", None) or {}
            if isinstance(usage, dict):
                outcome.input_tokens += int(usage.get("input_tokens") or 0)
                outcome.output_tokens += int(usage.get("output_tokens") or 0)
                self._tokens = outcome.input_tokens + outcome.output_tokens

            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                outcome.text = _content_to_text(response)
                outcome.stopped_reason = "completed"
                break

            if self.executor.stats.max_consecutive_failures >= self.budget.max_retries + 3:
                outcome.stopped_reason = "tool_failure"
                break

            for call in tool_calls:
                name = call.get("name") or ""
                args = call.get("args") or {}
                call_id = call.get("id") or f"{self.agent}-{iteration}-{name}"
                self.budget.spend_tool()
                emit({"event": "tool_call", "agent": self.agent, "tool": name, "args": args})

                result = await self.executor.execute(name, args, call_id=call_id)
                outcome.tool_calls += 1
                self.records.append(
                    {
                        "agent": self.agent,
                        "stage": self.stage,
                        "tool": name,
                        "args": args,
                        "ok": result.ok,
                        "preview": _stringify(result.to_envelope())[:600],
                        "error": result.error_message or "",
                        "call_id": call_id,
                        "latency_ms": result.execution_time_ms,
                        "result_tokens": max(1, len(_stringify(result.to_envelope())) // 4),
                    }
                )
                self.context.add(
                    ToolMessage(
                        content=_stringify(result.to_envelope())[:20_000],
                        tool_call_id=call_id,
                        name=name,
                    ),
                    Layer.DETAIL,
                    ref=f"tool:{call_id}",
                )
                emit(
                    {
                        "event": "tool_result",
                        "agent": self.agent,
                        "tool": name,
                        "ok": result.ok,
                        "status": str(result.status),
                        "error_code": result.error_code,
                        "preview": _stringify(result.to_envelope())[:220],
                    }
                )
        else:
            outcome.stopped_reason = "max_iterations"
            outcome.text = "已达到工具调用上限，以下为目前掌握的信息。"

        outcome.stats = self.executor.summary()
        outcome.context = self.context.summary()
        emit(
            {
                "event": "agent_end",
                "agent": self.agent,
                "tool_calls": outcome.tool_calls,
                "stopped": outcome.stopped_reason,
                "tool_stats": outcome.stats,
            }
        )
        return outcome

    # ------------------------------------------------------------------
    def _preflight(self, iteration: int, outcome: RuntimeOutcome) -> str | None:
        """每轮开始前检查预算。**顺序固定**：取消 > 时间 > token > 次数。"""

        if self.is_cancelled is not None and self.is_cancelled():
            return "cancelled"
        if self.deadline is not None and time.monotonic() > self.deadline:
            return "deadline"
        if self.token_budget is not None and self._tokens > self.token_budget:
            return "budget_tokens"
        if self.budget.max_tool_calls and self.executor.stats.total >= self.budget.max_tool_calls:
            return "budget_tool_calls"
        if self.budget.max_llm_calls and self.budget.llm_calls >= self.budget.max_llm_calls:
            return "budget_llm_calls"
        if self.budget.exhausted() == "cost":
            return "budget_cost"
        return None


# ----------------------------------------------------------------------
# 模型调用（流式优先，失败回退）
# ----------------------------------------------------------------------
async def call_model_streaming(bound: Any, messages: list[Any], *, agent: str, emit: Any) -> Any:
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
    except Exception as exc:  # noqa: BLE001
        log.warning("流式调用失败（%s），回退到非流式", exc)
    return await bound.ainvoke(messages)


def _chunk_text(chunk: Any) -> str:
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


def _stringify(payload: Any) -> str:
    import json

    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(payload)


def _system(prompt: str) -> Any:
    from langchain_core.messages import SystemMessage

    return SystemMessage(content=prompt)
