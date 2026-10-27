"""LangGraph 编排层。

模块划分：

``state``       全图共享的、带类型的可 checkpoint 状态
``intents``     确定性意图分类 + 实体抽取
``prompts``     每个专家的系统提示词
``agent_loop``  LLM 模式下专家使用的显式 ReAct 循环
``offline``     无 LLM 模式下的确定性专家实现
``experts``     每个专家的 MCP 工具白名单与执行
``nodes``       记忆注入/落库、Supervisor、收尾
``builder``     图的组装与 ``FinAgentApp`` 运行时
``store``       基于 JSON 的 LangGraph 长期 store
``mcp_client``  MCP → LangChain 的工具桥（stdio 或 in-process）
"""

from finagent.graph.builder import FinAgentApp, TurnResult, build_graph, open_app
from finagent.graph.state import AGENT_LABELS, EXPERT_AGENTS, FinState, initial_state

__all__ = [
    "AGENT_LABELS",
    "EXPERT_AGENTS",
    "FinAgentApp",
    "FinState",
    "TurnResult",
    "build_graph",
    "initial_state",
    "open_app",
]
