"""组装并运行 Supervisor + 专家图。

拓扑::

    START
      └─ load_memory         从 LangGraph store 注入画像
           └─ supervisor      Command(goto=<专家>) | Command(goto="save_memory")
                ├─ stock_picker ─┐
                ├─ analyst       │  Command(goto="supervisor")  ← 交回去再分发
                ├─ backtest      │，或者收尾
                ├─ portfolio     │
                └─ alert        ─┘
           └─ save_memory     持久化本轮学到的偏好
                └─ finalize   汇总答案，追加 AI 消息
                     └─ END

Supervisor 一轮内可以连发多个专家（受 ``max_agent_hops`` 限制），「回测茅台，再看看
我的持仓」这类复合请求就是这样被回答的，不需要另写规划器。
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Overwrite

from finagent.graph.mcp_client import Toolkit, open_toolkit
from finagent.graph.nodes import GraphResources, build_nodes
from finagent.graph.state import EXPERT_AGENTS, FinState, initial_state
from finagent.graph.store import JsonFileStore
from finagent.llm import build_chat_model
from finagent.logging import get_logger
from finagent.settings import Settings, get_settings

log = get_logger("graph.builder")


@dataclass(slots=True)
class TurnResult:
    """一轮对话产出的全部东西。"""

    answer: str
    question: str
    agents: list[str] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    profile_updates: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    user_id: str = "default"
    thread_id: str = "cli"

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "question": self.question,
            "agents": self.agents,
            "route": " → ".join(self.agents) if self.agents else "",
            "tool_calls": len(self.trace),
            "profile_updates": self.profile_updates,
            "errors": self.errors,
            "user_id": self.user_id,
            "thread_id": self.thread_id,
        }


# ----------------------------------------------------------------------
def build_graph(resources: GraphResources, *, checkpointer: Any = None) -> Any:
    """编译 StateGraph。一次性运行时可把 ``checkpointer`` 传 ``None``。"""

    nodes = build_nodes(resources)

    graph = StateGraph(FinState)
    graph.add_node("load_memory", _reset_turn(nodes["load_memory"]))
    graph.add_node("supervisor", nodes["supervisor"])
    for agent in EXPERT_AGENTS:
        graph.add_node(agent, nodes[agent])
    graph.add_node("save_memory", nodes["save_memory"])
    graph.add_node("finalize", nodes["finalize"])

    graph.add_edge(START, "load_memory")
    graph.add_edge("load_memory", "supervisor")
    graph.add_edge("save_memory", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer)


def _reset_turn(load_memory):
    """包一层 ``load_memory``，让本轮的通道从干净状态开始。

    checkpointer 会跨轮持久化状态，这正是 ``messages`` 和 ``user_profile`` 想要的；但
    本轮的记账信息（``answers``、``trace``、``completed_agents``）必须从空开始，
    否则上一轮的专家会被当成「已经跑过」。``Overwrite`` 只替换这些通道，不动持久通道。
    """

    async def wrapped(state: FinState) -> dict[str, Any]:
        question = state.get("question") or ""
        memory = await load_memory(state)
        update: dict[str, Any] = {
            # 带 reducer 的通道要用 Overwrite 才能清空；普通的 LastValue 通道直接返回新值即可替换。
            "completed_agents": Overwrite([]),
            "answers": Overwrite([]),
            "trace": Overwrite([]),
            "errors": Overwrite([]),
            "artifacts": {},
            "profile_updates": {},
            "agent_hops": 0,
            "next_agent": "supervisor",
            "route_reason": "",
            "answer": "",
        }
        if question:
            update["messages"] = [HumanMessage(content=question)]
        update.update(memory)
        return update

    return wrapped


# ----------------------------------------------------------------------
@dataclass(slots=True)
class ModelSlot:
    """可热替换的模型容器。

    节点每次调用时现取，所以换 LLM 配置只需要给 ``model`` 赋新值——图和 MCP 会话都保持
    原样。反过来说，如果重建整张图，就得在**另一个任务**里关掉 MCP 会话，而 anyio 的
    cancel scope 不允许跨任务退出。
    """

    model: Any = None

    def get(self) -> Any:
        return self.model


class FinAgentApp:
    """编译后图的 open → run → close 生命周期。

    刻意做成显式上下文管理器：MCP 会话、SQLite checkpointer 和 store 只打开一次并跨轮
    复用——在 CLI 里每说一句话就重新拉起三个 MCP 服务器是荒唐的。
    """

    def __init__(self, settings: Settings | None = None, *, transport: str | None = None) -> None:
        self.settings = settings or get_settings()
        self.transport = transport
        self._stack: AsyncExitStack | None = None
        self.toolkit: Toolkit | None = None
        self.store: JsonFileStore | None = None
        self.checkpointer: Any = None
        self.graph: Any = None
        self.model_slot = ModelSlot()

    @property
    def model(self) -> Any:
        return self.model_slot.get()

    def set_llm(self, settings: Settings) -> None:
        """换一份 LLM 配置并让它立刻生效（不重建图）。

        非 LLM 相关的配置（数据源、MCP 传输等）不支持热替换——那些需要重启进程。
        """

        self.settings = settings
        if settings.llm_enabled:
            self.model_slot.model = build_chat_model(settings)
            log.info("已切换大模型：%s / %s", settings.llm_provider, settings.llm_model or "(默认)")
        else:
            self.model_slot.model = None
            log.info("已关闭大模型，回到确定性规划器")

    # ------------------------------------------------------------------
    async def __aenter__(self) -> FinAgentApp:
        await self.open()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def open(self) -> FinAgentApp:
        if self.graph is not None:
            return self

        resolved = self.settings
        data_dir = resolved.resolve_data_dir()
        stack = AsyncExitStack()

        toolkit = await stack.enter_async_context(open_toolkit(resolved, transport=self.transport))

        self.store = JsonFileStore(data_dir / "memory.json")

        checkpoint_path = resolved.resolve_checkpoint_path()
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            self.checkpointer = await stack.enter_async_context(
                AsyncSqliteSaver.from_conn_string(str(checkpoint_path))
            )
        except Exception as exc:  # noqa: BLE001 - degrade to in-memory persistence
            log.warning("SQLite checkpointer 不可用（%s），回退到内存模式", exc)
            from langgraph.checkpoint.memory import InMemorySaver

            self.checkpointer = InMemorySaver()

        if resolved.llm_enabled:
            self.model_slot.model = build_chat_model(resolved)
        else:
            self.model_slot.model = None
            log.info(
                "未配置 LLM，启用确定性规划器（设置 FINAGENT_LLM_PROVIDER 与 FINAGENT_LLM_API_KEY 可启用 Agent）"
            )

        self.toolkit = toolkit
        self.graph = build_graph(
            GraphResources(
                toolkit=toolkit,
                settings=resolved,
                store=self.store,
                model_getter=self.model_slot.get,
            ),
            checkpointer=self.checkpointer,
        )
        self._stack = stack
        log.info(
            "图已就绪：%d 个 MCP 工具（%s），%d 个专家，checkpointer=%s",
            len(toolkit.tools),
            toolkit.transport,
            len(EXPERT_AGENTS),
            type(self.checkpointer).__name__,
        )
        return self

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self.graph = None
        self.toolkit = None

    # ------------------------------------------------------------------
    def _config(self, thread_id: str, user_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": f"{user_id}:{thread_id}"},
            "recursion_limit": 60,
        }

    async def arun(
        self,
        question: str,
        *,
        user_id: str = "default",
        thread_id: str = "cli",
    ) -> TurnResult:
        """跑完一轮对话。"""

        if self.graph is None:
            await self.open()
        config = self._config(thread_id, user_id)
        state = initial_state(question, user_id=user_id, thread_id=thread_id)
        final = await self.graph.ainvoke(state, config=config)
        return _to_turn_result(final, question, user_id, thread_id)

    async def astream(
        self,
        question: str,
        *,
        user_id: str = "default",
        thread_id: str = "cli",
    ) -> AsyncIterator[dict[str, Any]]:
        """产出进度事件，最后一个是 ``{"type": "final", ...}``。

        调用方会实时收到路由决策、工具调用与工具结果——CLI 直接打印，同一套事件也能喂给
        Web 界面。
        """

        if self.graph is None:
            await self.open()
        config = self._config(thread_id, user_id)
        state = initial_state(question, user_id=user_id, thread_id=thread_id)

        yield {"type": "start", "question": question, "thread_id": thread_id}

        async for mode, chunk in self.graph.astream(
            state, config=config, stream_mode=["updates", "custom"]
        ):
            if mode == "custom":
                yield {"type": "event", **chunk}
                continue

            for node, update in (chunk or {}).items():
                if not isinstance(update, dict):
                    continue
                if node == "supervisor":
                    reason = update.get("route_reason")
                    agent = update.get("next_agent")
                    if reason:
                        yield {"type": "route", "agent": agent, "reason": reason}
                elif node in EXPERT_AGENTS:
                    yield {"type": "agent", "agent": node}
                elif node == "save_memory" and update.get("profile_updates"):
                    yield {"type": "memory", "updates": update["profile_updates"]}

        snapshot = await self.graph.aget_state(config)
        final = dict(snapshot.values or {})
        result = _to_turn_result(final, question, user_id, thread_id)
        yield {"type": "final", "result": result}


def _to_turn_result(
    state: dict[str, Any], question: str, user_id: str, thread_id: str
) -> TurnResult:
    answers = state.get("answers") or []
    answer = state.get("answer") or "\n\n".join(
        section.get("answer", "") for section in answers if section.get("answer")
    )
    return TurnResult(
        answer=answer,
        question=question,
        agents=list(state.get("completed_agents") or []),
        trace=list(state.get("trace") or []),
        artifacts=dict(state.get("artifacts") or {}),
        profile_updates=dict(state.get("profile_updates") or {}),
        errors=list(state.get("errors") or []),
        user_id=user_id,
        thread_id=thread_id,
    )


# ----------------------------------------------------------------------
@asynccontextmanager
async def open_app(
    settings: Settings | None = None, *, transport: str | None = None
) -> AsyncIterator[FinAgentApp]:
    """便捷写法：``async with open_app() as app: await app.arun("...")``。"""

    app = FinAgentApp(settings, transport=transport)
    await app.open()
    try:
        yield app
    finally:
        await app.close()


_ = sqlite3  # documented dependency of the SQLite checkpointer
