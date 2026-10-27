"""LangGraph 状态定义。

状态是一个显式、带类型的对象，在 Supervisor、专家、审查者、风控之间流转，并由
checkpointer 按 ``thread_id`` 持久化。

reducer 很重要：

* ``messages`` 用 ``add_messages``，每个节点都能追加对话；
* ``trace`` / ``errors`` / ``decision_log`` / ``risk_flags`` 是只追加列表，形成可回放的
  审计轨迹；
* ``completed_agents`` 会去重，Supervisor 才知道自己走过哪些路。

``answers`` 虽然是追加通道，但修订时会用 ``Overwrite`` 整体替换——同一个专家的旧版结论
不应该和新版一起出现在最终答案里。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph.message import add_messages

AgentName = Literal[
    "stock_picker",
    "fundamental_analyst",
    "technical_analyst",
    "backtest",
    "portfolio",
    "alert",
    "finish",
]

#: 可执行的专家（不含 ``finish``）。顺序即 Supervisor 兜底路由的优先级。
EXPERT_AGENTS: tuple[str, ...] = (
    "stock_picker",
    "fundamental_analyst",
    "technical_analyst",
    "backtest",
    "portfolio",
    "alert",
)

AGENT_LABELS: dict[str, str] = {
    "stock_picker": "选股专家",
    "fundamental_analyst": "基本面分析专家",
    "technical_analyst": "技术面分析专家",
    "backtest": "策略回测专家",
    "portfolio": "持仓管理专家",
    "alert": "预警监控专家",
    "critic": "审查者",
    "risk_guard": "风控",
    "finish": "汇总",
}

#: 属于「分析师」这一族的专家，用于判断是否需要交叉一致性检查。
ANALYST_AGENTS: tuple[str, ...] = ("fundamental_analyst", "technical_analyst")


def merge_unique(left: list[str] | None, right: list[str] | None) -> list[str]:
    """只追加的 reducer：保序并去重。"""

    merged: list[str] = []
    for item in [*(left or []), *(right or [])]:
        if item not in merged:
            merged.append(item)
    return merged


class ToolCallRecord(TypedDict, total=False):
    agent: str
    tool: str
    args: dict[str, Any]
    ok: bool
    preview: str
    error: str


class FinState(TypedDict, total=False):
    """整张 Supervisor/专家图共享的状态。"""

    # 会话 -------------------------------------------------------------
    #: 完整消息历史，由 checkpointer 持久化。
    messages: Annotated[list[Any], add_messages]
    #: 当前用户请求，给只能看到部分状态的节点用。
    question: str
    #: 本轮最终的 Markdown 答案。
    answer: str

    # 身份与记忆 ----------------------------------------------------------
    #: 稳定标识，用于给长期记忆划分命名空间。
    user_id: str
    #: 会话 id，也就是 checkpointer 的线程。
    thread_id: str
    #: 注入的用户画像，每个专家不调工具也能拿到。
    user_profile: dict[str, Any]
    #: 可直接放进提示词的画像摘要。
    profile_summary: str

    # 路由 -------------------------------------------------------------
    #: Supervisor 为下一跳选定的专家。
    next_agent: str
    #: Supervisor 选择该专家的理由（在 trace 里展示，评测时有用）。
    route_reason: str
    #: 本轮已经执行过的专家。
    completed_agents: Annotated[list[str], merge_unique]
    #: 跳转计数，受 ``max_agent_hops`` 限制。
    agent_hops: int

    # 工作记忆 -----------------------------------------------------------
    #: 按专家归档的结构化结论，例如 ``{"backtest": {...}}``。
    artifacts: dict[str, Any]
    #: 本轮每个运行过的专家贡献一段 Markdown。
    answers: Annotated[list[dict[str, Any]], operator.add]
    #: 本轮执行的工具调用，便于透明展示与调试。
    trace: Annotated[list[ToolCallRecord], operator.add]
    #: 可恢复的问题；要呈现给用户，而不是吞掉。
    errors: Annotated[list[str], operator.add]
    #: 本轮检测到的待写入画像变更。
    profile_updates: dict[str, Any]

    # 自我审查 -----------------------------------------------------------
    #: 最近一次审查的结论：``{"agent", "verdict", "issues": [...]}``。
    critique: dict[str, Any]
    #: 每个专家被要求返工了几次，用来封顶修订循环。
    revisions: dict[str, int]

    # 可观测性 -----------------------------------------------------------
    #: 结构化决策轨迹：路由、审查、风控各占一条，前端据此渲染「决策轨迹」面板。
    decision_log: Annotated[list[dict[str, Any]], operator.add]
    #: 风控护栏命中的问题。
    risk_flags: Annotated[list[dict[str, Any]], operator.add]


def initial_state(
    question: str,
    *,
    user_id: str = "default",
    thread_id: str = "cli",
) -> FinState:
    """构造一轮新的对话状态。"""

    return FinState(
        messages=[],
        question=question,
        answer="",
        user_id=user_id,
        thread_id=thread_id,
        user_profile={},
        profile_summary="",
        next_agent="supervisor",
        route_reason="",
        completed_agents=[],
        agent_hops=0,
        artifacts={},
        answers=[],
        trace=[],
        errors=[],
        profile_updates={},
        critique={},
        revisions={},
        decision_log=[],
        risk_flags=[],
    )
