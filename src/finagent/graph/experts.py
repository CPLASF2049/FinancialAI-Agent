"""三个核心 Agent：researcher / quant / alert。

图上只有 3 个节点，但每个节点内部仍按**子职责**（pass）分头工作，各跑各的工具白名单：

================  ===========================================================
节点              内部子职责
================  ===========================================================
``researcher``    screening（横截面选股）· fundamental（估值/财报/宏观）·
                  technical（量价/指标/资金）
``quant``         backtest（策略回测）· portfolio（持仓与盈亏）
``alert``         alert（价格预警）
================  ===========================================================

为什么要这样分两层：节点数量决定**上下文传递成本**（每个节点都要重新读一遍问题、画像、
前序结论），子职责决定**判断的专业性**（工具越聚焦，模型选错工具的概率越低）。把两者
绑在一起，只能在"节点多但每个都不精"和"节点少但工具混杂"之间二选一。分层之后，
上下文只传 3 次，而每次推理仍然只看自己那 7–10 个工具。

专家有两种运行模式：

* **llm** —— 在子职责的工具上跑 ReAct 循环（:mod:`finagent.graph.agent_loop`）；
* **deterministic** —— :mod:`finagent.graph.offline` 里的手写流程。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage

from finagent.graph import offline
from finagent.graph.agent_loop import run_tool_loop
from finagent.graph.contract import (
    ExpertResult,
    StructuredError,
    Termination,
    TokenUsage,
    ToolTraceEntry,
    normalize_result,
)
from finagent.graph.mcp_client import Toolkit
from finagent.graph.prompts import build_expert_prompt
from finagent.graph.state import AGENT_LABELS
from finagent.graph.tooling import ToolRunner
from finagent.logging import get_logger

log = get_logger("graph.experts")


#: 每个**子职责**可调用的 MCP 工具。这几段白名单刻意互不重叠，是「认知边界」的载体。
PASS_TOOLS: dict[str, tuple[str, ...]] = {
    # 选股：横截面筛选
    "screening": (
        "get_current_time",
        "resolve_symbol",
        "screen_stocks",
        "get_long_tail_stocks",
        "get_realtime_price",
        "get_daily_basic",
        "get_user_profile",
        "mx_stock_screen",
        "mx_news_search",
    ),
    # 基本面：估值、财报、宏观、指数对比。**不含任何 K 线与指标工具**。
    "fundamental": (
        "get_current_time",
        "resolve_symbol",
        "get_daily_basic",
        "get_income_statement",
        "get_macro",
        "get_index_daily",
        "get_global_index_comparison",
        "mx_financial_data",
        "mx_news_search",
        "mx_quota_status",
    ),
    # 技术面：量价、指标、形态、资金流。**不含任何财报与估值工具**。
    "technical": (
        "get_current_time",
        "resolve_symbol",
        "get_realtime_price",
        "get_daily_price",
        "compute_indicators",
        "detect_patterns",
        "get_moneyflow",
        "get_limit_list",
        "get_top_list",
        "get_concept_detail",
    ),
    "backtest": (
        "get_current_time",
        "resolve_symbol",
        "list_strategies",
        "run_backtest",
        "get_daily_price",
    ),
    "portfolio": (
        "get_current_time",
        "resolve_symbol",
        "get_realtime_price",
        "get_portfolio_status",
        "add_position",
        "remove_position",
        "update_position",
        "clear_portfolio",
        "list_portfolios",
    ),
    "alert": (
        "get_current_time",
        "resolve_symbol",
        "get_realtime_price",
        "add_price_alert",
        "list_alerts",
        "update_alert",
        "remove_alert",
        "check_alerts",
        "get_alert_history",
    ),
}

#: 3 个图节点 → 它们内部会跑的若干子职责。
AGENT_PASSES: dict[str, tuple[str, ...]] = {
    "researcher": ("screening", "fundamental", "technical"),
    "quant": ("backtest", "portfolio"),
    "alert": ("alert",),
}

#: ``EXPERT_TOOLS[agent]`` = 该节点可能用到的全部工具（各子职责的并集）。
#: 用于「这个专家能不能碰这个工具」的粗粒度检查与文档展示。
EXPERT_TOOLS: dict[str, tuple[str, ...]] = {
    agent: tuple(dict.fromkeys(tool for name in passes for tool in PASS_TOOLS[name]))
    for agent, passes in AGENT_PASSES.items()
}

#: 每个专家也都能读取和细化长期画像。
MEMORY_TOOLS: tuple[str, ...] = ("get_user_profile", "update_user_profile")


#: 专家执行结果就是契约类型本身（见 :mod:`finagent.graph.contract`）。
#: 旧名字保留，避免一次性改爆所有调用点；但**新代码请用 ExpertResult**。
ExpertOutcome = ExpertResult


def _label(agent: str) -> str:
    return AGENT_LABELS.get(agent, agent)


def _merge_stats(loops: list[Any]) -> dict[str, Any]:
    """把每轮循环的工具统计合并成一份。"""

    merged: dict[str, int] = {}
    for loop in loops:
        for key, value in (getattr(loop, "tool_stats", None) or {}).items():
            if key == "max_consecutive_failures":
                merged[key] = max(merged.get(key, 0), int(value))
            else:
                merged[key] = merged.get(key, 0) + int(value)
    return merged


def _traces(records: list[Any]) -> list[ToolTraceEntry]:
    """把循环记录转成契约里的工具轨迹。"""

    entries: list[ToolTraceEntry] = []
    for record in records:
        entries.append(
            ToolTraceEntry(
                call_id=getattr(record, "call_id", "") or "",
                tool=getattr(record, "tool", ""),
                args=dict(getattr(record, "args", {}) or {}),
                status="ok" if getattr(record, "ok", False) else "error",
                latency_ms=int(getattr(record, "latency_ms", 0) or 0),
                result_tokens=int(getattr(record, "result_tokens", 0) or 0),
                error=str(getattr(record, "error", "") or ""),
            )
        )
    return entries


#: 工具循环的停止原因 -> 契约终止原因。
_STOP_TO_TERMINATION = {
    "completed": Termination.SUBMIT,
    "max_iterations": Termination.BUDGET_ITERATIONS,
    "deadline": Termination.BUDGET_WALLCLOCK,
    "budget_tokens": Termination.BUDGET_TOKENS,
    "tool_failure": Termination.TOOL_FAILURE,
    "cancelled": Termination.CANCELLED,
}


def _termination_from(*, loops: Any = ()) -> Termination:
    """取最后一个循环的停止原因；没有循环就当作 submit。"""

    reasons = [getattr(item, "stopped_reason", "") for item in loops or ()]
    if not reasons:
        return Termination.SUBMIT
    return _STOP_TO_TERMINATION.get(str(reasons[-1]), Termination.SUBMIT)


#: 妙想工具的前缀。它们是否出现在工具面里取决于用户有没有配 MX_APIKEY，
#: 所以白名单里写了也要按「实际加载到没加载」过滤，否则 toolkit.subset 会报缺工具。
MX_TOOL_PREFIX = "mx_"


def _usable(names: tuple[str, ...], available: set[str] | None) -> tuple[str, ...]:
    if available is None:
        return names
    return tuple(name for name in names if name in available or not name.startswith(MX_TOOL_PREFIX))


def tools_for(agent: str, available: set[str] | None = None) -> tuple[str, ...]:
    """某专家可用的工具名（并集，含画像工具，已去重）。

    ``available`` 是本次实际加载到的工具名集合；传了就顺手滤掉没挂载的可选工具。
    """

    names = tuple(dict.fromkeys([*EXPERT_TOOLS.get(agent, ()), *MEMORY_TOOLS]))
    return _usable(names, available)


def tools_for_pass(name: str, available: set[str] | None = None) -> tuple[str, ...]:
    """某个子职责可用的工具名（含画像工具，已去重）。"""

    names = tuple(dict.fromkeys([*PASS_TOOLS.get(name, ()), *MEMORY_TOOLS]))
    return _usable(names, available)


async def run_expert(
    agent: str,
    question: str,
    *,
    toolkit: Toolkit,
    model: Any = None,
    profile_summary: str = "",
    prior_artifacts: dict[str, Any] | None = None,
    critique: dict[str, Any] | None = None,
    passes: list[str] | None = None,
    #: 本轮截止时刻（monotonic）。传给工具循环，避免单个节点把整轮拖过预算。
    deadline: float | None = None,
    #: token 预算与上下文预算，一并传给工具循环。
    token_budget: int | None = None,
    context_budget: int = 24_000,
    #: 压缩发生时回调（把「压掉了什么」记进事件表，恢复时才说得清）。
    on_compact: Any = None,
) -> ExpertOutcome:
    """执行一个专家，把两种模式统一成 :class:`ExpertOutcome`。

    ``critique`` 非空表示这次是**返工**：把审查意见原样交给专家，要求逐条回应。
    """

    if agent not in AGENT_PASSES:
        return normalize_result(
            agent_name=agent,
            text="",
            termination=Termination.CONTRACT_VIOLATION,
            error=StructuredError(
                code="unknown_agent",
                message=f"未知专家 '{agent}'",
                retryable=False,
            ),
            mode="deterministic",
        )

    selected = passes or _default_passes(agent, question)
    if not selected:
        selected = list(AGENT_PASSES[agent])[:1]

    if model is None:
        return await _run_deterministic(agent, question, toolkit, selected, critique=critique)

    return await _run_llm(
        agent,
        question,
        toolkit,
        selected,
        model=model,
        profile_summary=profile_summary,
        prior_artifacts=prior_artifacts,
        critique=critique,
        deadline=deadline,
        token_budget=token_budget,
        context_budget=context_budget,
        on_compact=on_compact,
    )


def _default_passes(agent: str, question: str) -> list[str]:
    from finagent.graph.intents import quant_passes, researcher_passes

    if agent == "researcher":
        return researcher_passes(question)
    if agent == "quant":
        return quant_passes(question)
    return list(AGENT_PASSES.get(agent, ()))


# ----------------------------------------------------------------------
async def _run_deterministic(
    agent: str,
    question: str,
    toolkit: Toolkit,
    passes: list[str],
    *,
    critique: dict[str, Any] | None,
) -> ExpertOutcome:
    runner = ToolRunner(toolkit=toolkit, agent=agent)
    sections: list[str] = []
    artifacts: dict[str, Any] = {}
    errors: list[str] = []

    for name in passes:
        result = await offline.run_pass(name, question, runner, critique=critique)
        if result.answer.strip():
            sections.append(
                result.answer if len(passes) == 1 else _with_heading(name, result.answer)
            )
        artifacts.update(result.artifacts)
        errors.extend(error for error in result.errors if error)

    body = "\n\n".join(sections)
    # 确定性路径同样要给出显式终止原因：有结论 = submit，没结论 = 契约违反。
    return normalize_result(
        agent_name=agent,
        text=body,
        termination=(Termination.SUBMIT if body.strip() else Termination.CONTRACT_VIOLATION),
        tool_trace=_traces(runner.records),
        error=(
            None
            if body.strip()
            else StructuredError(
                code="empty_output",
                message="确定性路径没有产出任何结论",
                retryable=False,
            )
        ),
        passes=list(passes),
        mode="deterministic",
        artifacts=artifacts,
        trace=runner.to_dicts(),
    )


_PASS_HEADINGS = {
    "screening": "选股结果",
    "fundamental": "基本面",
    "technical": "技术面",
    "backtest": "回测结果",
    "portfolio": "持仓",
    "alert": "预警",
}


#: 每个子职责都会自作主张写一段「风险提示 / 数据来源 / 免责声明」。
#: 两根一拼就变成两份完整报告，用户看到的就是「杂揉」。这些套话由我们统一加一次。
_BOILERPLATE_MARKERS = (
    "风险提示",
    "免责声明",
    "不构成投资建议",
    "数据来源：",
    "数据口径说明",
)
#: 「结论」这类小标题在合并时由 ## 标题承担，不需要各节再写一遍。
_REDUNDANT_HEADINGS = ("结论", "总结", "回答")


def _is_boilerplate_start(line: str) -> bool:
    """这一行是不是「风险提示」这类收尾套话的开头。

    必须容忍 markdown 写法：`**风险提示**`、`## 风险提示`、`> 风险提示：…`
    ——模型经常这么写，只比对裸文本会漏掉，于是每节都带一段免责声明。
    """

    stripped = line.strip().lstrip("#>*- 　").strip()
    if not stripped:
        return False
    # 只把「独立的提示段」当套话，正文里顺带提一句「风险」不算
    for marker in _BOILERPLATE_MARKERS:
        if not stripped.startswith(marker):
            continue
        # 「风险提示」常常单独成行，但「数据来源：…截至 20261103。」是一整句，
        # 所以给足长度余量，避免把正文里的正常句子误判成套话
        if len(stripped) <= len(marker) + 40:
            return True
    return False


def _strip_boilerplate(body: str) -> str:
    """去掉子职责自带的收尾套话，只留事实与判断。"""

    kept: list[str] = []
    for line in body.splitlines():
        if _is_boilerplate_start(line):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def _strip_leading_heading(body: str) -> str:
    """去掉开头自带的「结论」小节，避免和 ## 标题重复。"""

    text = body.lstrip()
    for heading in _REDUNDANT_HEADINGS:
        for prefix in (f"{heading}\n", f"**{heading}**\n", f"## {heading}\n", f"{heading}：\n"):
            if text.startswith(prefix):
                return text[len(prefix) :].lstrip()
    return text


def _with_heading(name: str, body: str) -> str:
    """多子职责合并时加一个小标题，读者才知道这一节在讲什么。"""

    heading = _PASS_HEADINGS.get(name, name)
    cleaned = _strip_leading_heading(_strip_boilerplate(body))
    return f"## {heading}\n\n{cleaned}"


async def _run_llm(
    agent: str,
    question: str,
    toolkit: Toolkit,
    passes: list[str],
    *,
    model: Any,
    profile_summary: str,
    prior_artifacts: dict[str, Any] | None,
    critique: dict[str, Any] | None,
    deadline: float | None = None,
    token_budget: int | None = None,
    context_budget: int = 24_000,
    on_compact: Any = None,
) -> ExpertOutcome:
    sections: list[str] = []
    artifacts: dict[str, Any] = {}
    errors: list[str] = []
    records: list[Any] = []
    loops: list[Any] = []
    total_calls = 0

    for name in passes:
        tools = toolkit.subset(tools_for_pass(name, set(toolkit.by_name)))
        prompt = build_expert_prompt(name, profile_summary)
        if len(passes) > 1:
            # 多子职责时会拼成一整份答案，这时才需要「别重复」的约束
            from finagent.graph.prompts import WRITING_RULES

            prompt = prompt + "\n\n" + WRITING_RULES

        context_lines = [f"用户请求：{question}"]
        if prior_artifacts:
            summary = _summarise_artifacts(prior_artifacts)
            if summary:
                context_lines.append(
                    "其他专家已经得出的中间结论（可直接引用，必要时自行核实）：\n" + summary
                )
        if len(passes) > 1 and sections:
            context_lines.append("你在同一轮里已经完成的其它分析：\n" + "\n".join(sections)[-1200:])
        if critique and critique.get("issues"):
            context_lines.append(_revision_block(critique))

        loop = await run_tool_loop(
            # 传裸 agent：审查者靠它找本轮的证据，拼上子职责就找不到了
            agent=agent,
            stage=name,
            deadline=deadline,
            token_budget=token_budget,
            context_budget=context_budget,
            on_compact=on_compact,
            model=model,
            tools=tools,
            system_prompt=prompt,
            user_messages=[HumanMessage(content="\n\n".join(context_lines))],
        )
        if loop.text.strip():
            sections.append(loop.text if len(passes) == 1 else _with_heading(name, loop.text))
        records.extend(loop.records)
        loops.append(loop)
        total_calls += loop.tool_calls
        if loop.records:
            artifacts[name] = _artifact_from_records(loop.records)
        errors.extend(record.error for record in loop.records if not record.ok and record.error)

    body = "\n\n".join(sections)
    # 终止原因取最后一个子职责的循环停止原因；一节都没产出就是契约违反。
    termination = _termination_from(loops=loops) if body.strip() else Termination.CONTRACT_VIOLATION
    usage = TokenUsage(
        input_tokens=sum(int(getattr(item, "input_tokens", 0) or 0) for item in loops),
        output_tokens=sum(int(getattr(item, "output_tokens", 0) or 0) for item in loops),
    )

    return normalize_result(
        agent_name=agent,
        text=body,
        termination=termination,
        tool_trace=_traces(records),
        usage=usage,
        error=(
            None
            if body.strip()
            else StructuredError(
                code="empty_output",
                message="模型没有产出可交付的结论",
                hint="检查模型可用性与提示词。",
                retryable=True,
            )
        ),
        passes=list(passes),
        mode="llm",
        artifacts=artifacts,
        trace=[record.to_dict() for record in records],
        tool_stats=_merge_stats(loops),
    )


def _revision_block(critique: dict[str, Any]) -> str:
    """把审查意见组织成「重写一版完整答案」的指令。

    这里最容易出问题：如果把指令写成「逐条回应审查意见」，模型就会交一篇**答辩**——
    "关于第一点…关于第二点…"或者"A 与 B 的对比"，而不是用户要的那份答案。所以要求
    写死三件事：重写、完整、精简；并明确禁止复述上一版和逐条辩解。
    """

    lines = [
        "审查者认为上一版结论存在下列问题。请**重写一份完整答案**，把这版直接替换掉：",
    ]
    for index, issue in enumerate(critique.get("issues") or [], start=1):
        detail = issue.get("detail") or issue.get("type") or ""
        lines.append(f"{index}. [{issue.get('type', '问题')}] {detail}")

    lines.append(
        "硬性要求：\n"
        "1. 输出的是**最终答案本身**，可被直接交付；不要逐条回应审查意见，不要辩解。\n"
        "2. 不要提及「修订」「上一版」「审查意见」这类过程词，也不要贴新旧对比。\n"
        "3. 完整：原来成立的结论要保留，别只输出改动的那部分。\n"
        "4. 精简：同一件事只说一遍；没有数据支撑的论断直接删掉，不要为了凑篇幅展开。\n"
        "5. 结论在前，论据在后；篇幅按信息量来，不要为了显得完整而重复。"
    )
    return "\n".join(lines)


def _summarise_artifacts(artifacts: dict[str, Any], limit: int = 1500) -> str:
    parts: list[str] = []
    for agent, payload in artifacts.items():
        if not isinstance(payload, dict):
            continue
        if agent == "screen":
            parts.append(
                f"- 选股：条件 {payload.get('criteria')}，命中 {payload.get('matched')} 只"
            )
        elif agent == "backtest":
            parts.append(
                f"- 回测：{payload.get('ts_code')} / {payload.get('strategy')} "
                f"收益 {payload.get('total_return_pct')}% "
                f"回撤 {payload.get('max_drawdown_pct')}%"
            )
        elif agent == "portfolio":
            totals = payload.get("totals") or {}
            parts.append(
                f"- 持仓：市值 {totals.get('market_value')} 元，"
                f"浮盈 {totals.get('pnl')} 元（{totals.get('pnl_pct')}%）"
            )
        elif agent == "alert":
            parts.append(f"- 预警：{payload.get('label', payload.get('id'))}")
        elif agent in ("fundamental", "technical"):
            for code, facts in payload.items():
                if not isinstance(facts, dict):
                    continue
                if "quote" in facts and isinstance(facts["quote"], dict):
                    parts.append(f"- 行情：{code} 最新价 {facts['quote'].get('price')}")
                if "valuation" in facts and isinstance(facts["valuation"], dict):
                    parts.append(
                        f"- 估值：{code} PE {facts['valuation'].get('pe')} "
                        f"PB {facts['valuation'].get('pb')}"
                    )
                if "indicators" in facts and isinstance(facts["indicators"], dict):
                    parts.append(f"- 技术：{code} MACD RSI 等指标已计算")
    text = "\n".join(parts)
    return text[:limit]


def _artifact_from_records(records: list[Any]) -> dict[str, Any]:
    """每个工具只保留最后一次成功的载荷，供下一跳参考。"""

    collected: dict[str, Any] = {}
    for record in records:
        if record.ok:
            collected[record.tool] = {"args": record.args, "preview": record.preview}
    return collected


def describe_experts() -> list[dict[str, Any]]:
    from finagent.graph.state import EXPERT_AGENTS

    return [
        {
            "agent": agent,
            "label": AGENT_LABELS.get(agent, agent),
            "tools": list(tools_for(agent)),
            "tool_count": len(tools_for(agent)),
            "passes": list(AGENT_PASSES.get(agent, ())),
        }
        for agent in EXPERT_AGENTS
    ]
