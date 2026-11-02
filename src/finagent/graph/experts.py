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

from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage

from finagent.graph import offline
from finagent.graph.agent_loop import run_tool_loop
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


@dataclass(slots=True)
class ExpertOutcome:
    agent: str
    answer: str
    artifacts: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: int = 0
    mode: str = "deterministic"
    #: 这个节点内部实际跑了哪些子职责（screening / fundamental / technical …）。
    passes: list[str] = field(default_factory=list)

    def label(self) -> str:
        return AGENT_LABELS.get(self.agent, self.agent)


def tools_for(agent: str) -> tuple[str, ...]:
    """某专家可用的工具名（并集，含画像工具，已去重）。"""

    return tuple(dict.fromkeys([*EXPERT_TOOLS.get(agent, ()), *MEMORY_TOOLS]))


def tools_for_pass(name: str) -> tuple[str, ...]:
    """某个子职责可用的工具名（含画像工具，已去重）。"""

    return tuple(dict.fromkeys([*PASS_TOOLS.get(name, ()), *MEMORY_TOOLS]))


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
) -> ExpertOutcome:
    """执行一个专家，把两种模式统一成 :class:`ExpertOutcome`。

    ``critique`` 非空表示这次是**返工**：把审查意见原样交给专家，要求逐条回应。
    """

    if agent not in AGENT_PASSES:
        return ExpertOutcome(agent=agent, answer="", errors=[f"unknown expert '{agent}'"])

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

    return ExpertOutcome(
        agent=agent,
        answer="\n\n".join(sections),
        artifacts=artifacts,
        errors=errors
        + [record.error or "" for record in runner.records if not record.ok and record.error],
        trace=runner.to_dicts(),
        tool_calls=len(runner.records),
        mode="deterministic",
        passes=list(passes),
    )


_PASS_HEADINGS = {
    "screening": "选股结果",
    "fundamental": "基本面",
    "technical": "技术面",
    "backtest": "回测结果",
    "portfolio": "持仓",
    "alert": "预警",
}


def _with_heading(name: str, body: str) -> str:
    """多子职责合并时加一个小标题，读者才知道这一节在讲什么。"""

    heading = _PASS_HEADINGS.get(name, name)
    return f"## {heading}\n\n{body}"


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
) -> ExpertOutcome:
    sections: list[str] = []
    artifacts: dict[str, Any] = {}
    errors: list[str] = []
    records: list[Any] = []
    total_calls = 0

    for name in passes:
        tools = toolkit.subset(tools_for_pass(name))
        prompt = build_expert_prompt(name, profile_summary)

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
            agent=f"{agent}:{name}",
            model=model,
            tools=tools,
            system_prompt=prompt,
            user_messages=[HumanMessage(content="\n\n".join(context_lines))],
        )
        if loop.text.strip():
            sections.append(loop.text if len(passes) == 1 else _with_heading(name, loop.text))
        records.extend(loop.records)
        total_calls += loop.tool_calls
        if loop.records:
            artifacts[name] = _artifact_from_records(loop.records)
        errors.extend(record.error for record in loop.records if not record.ok and record.error)

    return ExpertOutcome(
        agent=agent,
        answer="\n\n".join(sections),
        artifacts=artifacts,
        errors=errors,
        trace=[record.to_dict() for record in records],
        tool_calls=total_calls,
        mode="llm",
        passes=list(passes),
    )


def _revision_block(critique: dict[str, Any]) -> str:
    """把审查意见组织成「逐条回应」的指令。"""

    lines = [
        "审查者指出上一版结论存在以下问题，请**逐条**修正：",
    ]
    for index, issue in enumerate(critique.get("issues") or [], start=1):
        detail = issue.get("detail") or issue.get("type") or ""
        lines.append(f"{index}. [{issue.get('type', '问题')}] {detail}")
    lines.append(
        "要求：每个问题都要么补上工具数据支撑，要么删掉该论断；不要解释你为什么没错，"
        "直接给出修订后的结论。"
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
