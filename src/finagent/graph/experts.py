"""六个专家 Agent。

每个专家声明**自己拥有的那部分 MCP 工具**。这份白名单是解耦的核心：模型看不到 41 个
工具，只看到与它任务相关的 4–17 个，工具选择准确率因此明显提升，提示词也更小。

分析被拆成**基本面**与**技术面**两条独立推理链路，而不是一个什么都能干的「分析师」：

* 基本面只看估值、财报、宏观、指数对比；
* 技术面只看量价、指标、形态、资金流、龙虎榜。

两份白名单刻意不重叠——同一个角色既算 MACD 又读利润表，结果往往是两个维度都浅尝辄止。
两条链路各自出结论，再由审查节点（:mod:`finagent.graph.critic`）做交叉一致性检查。

专家有两种运行模式：

* **llm** —— 在自己的工具上跑 ReAct 循环（:mod:`finagent.graph.agent_loop`）；
* **deterministic** —— :mod:`finagent.graph.offline` 里的手写流程。

模式由图运行时依据 ``FINAGENT_LLM_PROVIDER`` 决定一次。
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


#: 每个专家可调用的 MCP 工具名。尽量精简且互不重叠——
#: 这既是安全边界，也是提示词瘦身手段。
EXPERT_TOOLS: dict[str, tuple[str, ...]] = {
    # 基本面：估值、财报、宏观、指数对比。**不含任何 K 线与指标工具**。
    "fundamental_analyst": (
        "get_current_time",
        "resolve_symbol",
        "get_daily_basic",
        "get_income_statement",
        "get_macro",
        "get_index_daily",
        "get_global_index_comparison",
    ),
    # 技术面：量价、指标、形态、资金流。**不含任何财报与估值工具**。
    "technical_analyst": (
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
    "stock_picker": (
        "get_current_time",
        "resolve_symbol",
        "screen_stocks",
        "get_long_tail_stocks",
        "get_realtime_price",
        "get_daily_basic",
        "get_user_profile",
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

    def label(self) -> str:
        return AGENT_LABELS.get(self.agent, self.agent)


def tools_for(agent: str) -> tuple[str, ...]:
    """某专家可用的工具名（含画像工具，已去重）。"""

    return tuple(dict.fromkeys([*EXPERT_TOOLS.get(agent, ()), *MEMORY_TOOLS]))


async def run_expert(
    agent: str,
    question: str,
    *,
    toolkit: Toolkit,
    model: Any = None,
    profile_summary: str = "",
    prior_artifacts: dict[str, Any] | None = None,
    critique: dict[str, Any] | None = None,
) -> ExpertOutcome:
    """执行一个专家，把两种模式统一成 :class:`ExpertOutcome`。

    ``critique`` 非空表示这次是**返工**：把审查意见原样交给专家，要求逐条回应，
    而不是从头再写一遍。
    """

    if agent not in EXPERT_TOOLS:
        return ExpertOutcome(agent=agent, answer="", errors=[f"unknown expert '{agent}'"])

    if model is None:
        runner = ToolRunner(toolkit=toolkit, agent=agent)
        result = await offline.run(agent, question, runner, critique=critique)
        return ExpertOutcome(
            agent=agent,
            answer=result.answer,
            artifacts=result.artifacts,
            errors=[error for error in result.errors if error]
            + [record.error or "" for record in runner.records if not record.ok and record.error],
            trace=runner.to_dicts(),
            tool_calls=len(runner.records),
            mode="deterministic",
        )

    tools = toolkit.subset(tools_for(agent))
    prompt = build_expert_prompt(agent, profile_summary)

    context_lines = [f"用户请求：{question}"]
    if prior_artifacts:
        summary = _summarise_artifacts(prior_artifacts)
        if summary:
            context_lines.append(
                "其他专家已经得出的中间结论（可直接引用，必要时自行核实）：\n" + summary
            )
    if critique and critique.get("issues"):
        context_lines.append(_revision_block(critique))

    loop = await run_tool_loop(
        agent=agent,
        model=model,
        tools=tools,
        system_prompt=prompt,
        user_messages=[HumanMessage(content="\n\n".join(context_lines))],
    )

    return ExpertOutcome(
        agent=agent,
        answer=loop.text,
        artifacts={agent: _artifact_from_records(loop.records)} if loop.records else {},
        errors=[record.error for record in loop.records if not record.ok and record.error],
        trace=[record.to_dict() for record in loop.records],
        tool_calls=loop.tool_calls,
        mode="llm",
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
        elif agent in ("fundamental_analyst", "technical_analyst"):
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
        }
        for agent in EXPERT_AGENTS
    ]
