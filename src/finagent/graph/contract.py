"""Agent 执行协议：一次专家执行的统一结果。

为什么要这个
------------
改造前每个专家返回的是「自由文本 + 可能有的 tool_calls」，终止靠
「模型不再调工具」这个**隐式信号**。于是下游（critic / risk / 汇总）只能读文本，
异常路径只能靠 LangGraph 的默认行为兜，没人说得清这一轮到底是「做完了」还是
「预算耗尽」还是「工具全挂了」——而这三种情况该做的事完全不同。

这里把一次执行收敛成一条**可枚举的事实**：

    status            —— 这次执行的结果性质（完成/失败/取消/部分完成）
    termination_reason—— 为什么结束（五类，显式记录，不靠推断）
    output            —— 结构化结论（正文 + 数字声明 + 引用的工具调用）
    tool_trace        —— 每次工具调用：名字、耗时、状态、是否重试
    token_usage       —— 输入/输出 token 与估算成本
    error             —— 失败时的结构化错误码 + 可读消息

下游节点只消费 :class:`ExpertResult`，不再直接读原始文本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    """执行结果的性质。"""

    COMPLETED = "completed"
    #: 拿到了部分结论，但没走完（例如预算耗尽、部分工具失败）
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Termination(StrEnum):
    """**为什么**结束。显式枚举，不允许隐式推断。

    五类终止路径，每一类都对应一个明确的处理策略：

    * ``submit``            —— 专家主动声明完成（模型不再调工具）
    * ``budget_iterations``  —— 循环轮数用尽
    * ``budget_tokens``      —— token 预算用尽
    * ``budget_wallclock``   —— 墙钟预算用尽
    * ``tool_failure``       —— 连续工具失败达到阈值
    * ``model_failure``      —— 模型调用失败且重试无果
    * ``contract_violation`` —— 输出违反契约（无法解析、空结论）
    * ``cancelled``          —— 外部取消
    """

    SUBMIT = "submit"
    BUDGET_ITERATIONS = "budget_iterations"
    BUDGET_TOKENS = "budget_tokens"
    BUDGET_WALLCLOCK = "budget_wallclock"
    TOOL_FAILURE = "tool_failure"
    MODEL_FAILURE = "model_failure"
    CONTRACT_VIOLATION = "contract_violation"
    CANCELLED = "cancelled"


#: 这些终止原因意味着「没做完」，结果应当降级为 ``PARTIAL`` 或 ``FAILED``。
INCOMPLETE = frozenset(
    {
        Termination.BUDGET_ITERATIONS,
        Termination.BUDGET_TOKENS,
        Termination.BUDGET_WALLCLOCK,
        Termination.TOOL_FAILURE,
    }
)


@dataclass(slots=True)
class TokenUsage:
    """token 消耗。没装计费表时只给数量，不编成本。"""

    input_tokens: int = 0
    output_tokens: int = 0
    #: 估算成本（美元）。没有价格表就留 ``None``——宁缺毋滥。
    cost_estimate: float | None = None

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total,
            "cost_estimate": self.cost_estimate,
        }


@dataclass(slots=True)
class StructuredError:
    """结构化错误：机器能分支，人能读懂。"""

    code: str
    message: str
    hint: str = ""
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.hint:
            payload["hint"] = self.hint
        return payload


@dataclass(slots=True)
class Claim:
    """结论里的一个数字声明，带它的来源。

    ``source_call_ids`` 指向 :attr:`ExpertResult.tool_trace` 里的调用记录，
    于是「这句话是从哪来的」可以被机器核对，而不是靠再问一遍模型。
    """

    text: str
    value: str = ""
    source_call_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "value": self.value,
            "source_call_ids": list(self.source_call_ids),
        }


@dataclass(slots=True)
class ToolTraceEntry:
    """一次工具调用的执行记录。"""

    call_id: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"  # ok / error / unknown_tool / timeout
    latency_ms: int = 0
    result_tokens: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "call_id": self.call_id,
            "tool": self.tool,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "result_tokens": self.result_tokens,
        }
        if self.args:
            payload["args"] = self.args
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(slots=True)
class ExpertOutput:
    """结构化结论。``text`` 是给人看的正文，``claims`` 是给机器核对的事实。"""

    text: str = ""
    claims: list[Claim] = field(default_factory=list)
    #: 结论里声明用到的数据时点（例如 20261103），用于和决策时刻做因果比对。
    data_as_of: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "claims": [claim.to_dict() for claim in self.claims],
            "data_as_of": self.data_as_of,
            "length": len(self.text),
        }


@dataclass(slots=True)
class ExpertResult:
    """一次专家执行的完整结果。**下游只认这个类型。**"""

    agent_name: str
    status: Status = Status.COMPLETED
    termination_reason: Termination = Termination.SUBMIT
    output: ExpertOutput = field(default_factory=ExpertOutput)
    tool_trace: list[ToolTraceEntry] = field(default_factory=list)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    error: StructuredError | None = None
    #: 内部跑过哪些子职责（screening / fundamental / technical …）。
    passes: list[str] = field(default_factory=list)
    #: 执行模式：``llm`` 或 ``deterministic``。
    mode: str = "llm"
    #: 子职责产出的结构化中间结果（选股条件、回测指标…），供后续节点复用。
    artifacts: dict[str, Any] = field(default_factory=dict)
    #: 原始工具调用记录（dict 形态）。critic 靠它挑证据，保留原样不改。
    trace: list[dict[str, Any]] = field(default_factory=list)
    #: 工具调用统计（成功/失败/不支持/重试）。
    #: 单独于 ``error`` 存在：**失败次数不等于错误条数**，混在一起就说不清。
    tool_stats: dict[str, Any] = field(default_factory=dict)

    # ---- 兼容旧接口的只读视图（下游改造期间不用一次性全改） ----
    @property
    def answer(self) -> str:
        return self.output.text

    @property
    def tool_calls(self) -> int:
        return len(self.tool_trace)

    @property
    def errors(self) -> list[str]:
        return [self.error.message] if self.error else []

    def failed_tools(self) -> list[ToolTraceEntry]:
        return [entry for entry in self.tool_trace if entry.status != "ok"]

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "agent_name": self.agent_name,
            "status": str(self.status),
            "termination_reason": str(self.termination_reason),
            "output": self.output.to_dict(),
            "tool_trace": [entry.to_dict() for entry in self.tool_trace],
            "token_usage": self.token_usage.to_dict(),
            "passes": list(self.passes),
            "mode": self.mode,
        }
        if self.tool_stats:
            payload["tool_stats"] = self.tool_stats
        if self.artifacts:
            payload["artifacts"] = sorted(self.artifacts)
        if self.error:
            payload["error"] = self.error.to_dict()
        return payload


# ----------------------------------------------------------------------
def derive_status(termination: Termination, *, has_output: bool) -> Status:
    """由终止原因 + 有没有产出推导结果性质。

    这条规则只有一处，避免每个调用点各自判断「这算不算完成」。
    """

    if termination is Termination.CANCELLED:
        return Status.CANCELLED
    if termination in (Termination.MODEL_FAILURE, Termination.CONTRACT_VIOLATION):
        return Status.FAILED if not has_output else Status.PARTIAL
    if termination in INCOMPLETE:
        return Status.PARTIAL if has_output else Status.FAILED
    return Status.COMPLETED if has_output else Status.FAILED


def normalize_result(
    *,
    agent_name: str,
    text: str,
    termination: Termination,
    tool_trace: list[ToolTraceEntry] | None = None,
    usage: TokenUsage | None = None,
    error: StructuredError | None = None,
    passes: list[str] | None = None,
    mode: str = "llm",
    claims: list[Claim] | None = None,
    data_as_of: str = "",
    artifacts: dict[str, Any] | None = None,
    trace: list[dict[str, Any]] | None = None,
    tool_stats: dict[str, Any] | None = None,
) -> ExpertResult:
    """把任意来源的原始输出收敛成 :class:`ExpertResult`。

    每个执行器（LLM 循环 / 确定性路径）都必须经过这个函数，保证下游拿到的东西
    形状一致：不会出现「某个专家没有 termination_reason」这种半成品。
    """

    body = (text or "").strip()
    status = derive_status(termination, has_output=bool(body))

    # 契约违反：声明完成却没有正文，这不是完成，是失败
    if termination is Termination.SUBMIT and not body:
        termination = Termination.CONTRACT_VIOLATION
        status = Status.FAILED
        error = error or StructuredError(
            code="empty_output",
            message="执行结束但没有产出任何结论",
            hint="检查提示词与工具返回；空结论不应被当作成功。",
        )

    return ExpertResult(
        agent_name=agent_name,
        status=status,
        termination_reason=termination,
        output=ExpertOutput(text=body, claims=claims or [], data_as_of=data_as_of),
        tool_trace=list(tool_trace or []),
        token_usage=usage or TokenUsage(),
        error=error,
        passes=list(passes or []),
        mode=mode,
        artifacts=dict(artifacts or {}),
        trace=list(trace or []),
        tool_stats=dict(tool_stats or {}),
    )
