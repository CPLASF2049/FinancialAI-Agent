"""图节点：记忆注入/落库、Supervisor、收尾。

节点由 :func:`build_nodes` 构造，闭包捕获本次运行所需的资源（MCP toolkit、聊天模型、
store、settings）。这样图定义本身是纯的，同时每个节点又恰好拿到它需要的依赖——
没有全局变量，没有 import 期单例，整张图用假件就能测。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from finagent.graph.experts import run_expert
from finagent.graph.intents import (
    classify_next,
    extract_profile_hints,
    looks_like_preference,
)
from finagent.graph.mcp_client import Toolkit
from finagent.graph.prompts import build_supervisor_prompt
from finagent.graph.state import AGENT_LABELS, EXPERT_AGENTS, FinState
from finagent.graph.tooling import ToolRunner
from finagent.logging import get_logger
from finagent.settings import Settings

log = get_logger("graph.nodes")

_MARKER = "FIN_AGENT_CHOICES_JSON"


@dataclass(slots=True)
class GraphResources:
    """节点需要的全部资源，每次运行解析一次。

    ``model`` 不直接持有模型实例，而是拿一个**取模型的可调用对象**（:class:`ModelSlot`
    的 ``get``）。这样在界面里换了 LLM 配置之后，只需要替换槽里的模型，图和 MCP 会话都
    不用重建——会话是绑定在启动任务上的，跨任务拆掉会触发 anyio 的 cancel scope 守卫。
    """

    toolkit: Toolkit
    settings: Settings
    store: Any = None
    model_getter: Callable[[], Any] = lambda: None

    @property
    def model(self) -> Any:
        return self.model_getter()

    @property
    def llm_mode(self) -> bool:
        return self.model_getter() is not None


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------
def _store_profile(store: Any, user_id: str) -> dict[str, Any] | None:
    if store is None:
        return None
    try:
        item = store.get(("users", user_id), "profile")
    except Exception as exc:  # noqa: BLE001 - store failures must not break a turn
        log.warning("读取 Store 失败：%s", exc)
        return None
    return getattr(item, "value", None) if item else None


def _store_write_profile(store: Any, user_id: str, profile: dict[str, Any]) -> None:
    if store is None:
        return
    try:
        store.put(("users", user_id), "profile", profile)
    except Exception as exc:  # noqa: BLE001
        log.warning("写入 Store 失败：%s", exc)


def _strip_marker(text: str) -> str:
    if _MARKER in text:
        return text.split(_MARKER, 1)[0].rstrip()
    return text.strip()


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _parse_supervisor(text: str) -> dict[str, Any]:
    """从模型的啰嗦回复里抽出 Supervisor 的路由决策。"""

    match = _JSON_BLOCK.search(text or "")
    if not match:
        return {}
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


# ----------------------------------------------------------------------
def build_nodes(resources: GraphResources) -> dict[str, Callable[..., Any]]:
    toolkit = resources.toolkit
    settings = resources.settings
    store = resources.store
    # 每次用到时现取，这样界面里换配置后不用重建整张图
    get_model = resources.model_getter

    # ------------------------------------------------------------------
    async def load_memory(state: FinState) -> dict[str, Any]:
        """路由前把长期画像注入状态。

        优先级：LangGraph store（最快，权威）→ MCP 画像工具（与其它 MCP 客户端共享）
        → 空。
        """

        user_id = state.get("user_id") or "default"
        profile = _store_profile(store, user_id)

        if not profile:
            runner = ToolRunner(toolkit=toolkit, agent="memory", emit_events=False)
            outcome = await runner.call("get_user_profile")
            if outcome.ok and isinstance(outcome.data, dict):
                profile = outcome.data.get("profile") or {}
                summary = outcome.data.get("summary") or ""
                if profile:
                    _store_write_profile(store, user_id, profile)
                return {
                    "user_profile": profile or {},
                    "profile_summary": summary,
                }
            return {"user_profile": {}, "profile_summary": ""}

        return {"user_profile": profile, "profile_summary": _profile_summary(profile)}

    # ------------------------------------------------------------------
    async def supervisor(state: FinState) -> Any:
        """路由到下一个专家；本轮已答完就收尾。"""

        from langgraph.types import Command

        question = state.get("question") or ""
        completed = list(state.get("completed_agents") or [])
        hops = int(state.get("agent_hops") or 0)
        answers = state.get("answers") or []

        if hops >= settings.max_agent_hops:
            return Command(
                goto="save_memory",
                update={
                    "route_reason": f"已达到跳转上限（{settings.max_agent_hops}）",
                    "next_agent": "finish",
                },
            )
        if answers and hops > 0 and not _may_continue(question, completed):
            return Command(
                goto="save_memory", update={"route_reason": "已答完", "next_agent": "finish"}
            )

        agent, reason = await _decide(question, completed, state)

        if agent == "finish" or agent in completed:
            return Command(
                goto="save_memory",
                update={"route_reason": reason or "finish", "next_agent": "finish"},
            )

        # 立刻把路由决策推给客户端。``Command.update`` 要等节点返回后才进 updates 流，
        # 那时专家已经开始发工具事件了，界面上会变成「先看到工具、后看到交给谁」。
        from finagent.graph.agent_loop import stream_writer

        stream_writer()(
            {
                "event": "route",
                "agent": agent,
                "label": AGENT_LABELS.get(agent, agent),
                "reason": reason,
            }
        )

        return Command(
            goto=agent,
            update={
                "next_agent": agent,
                "route_reason": reason,
                "agent_hops": hops + 1,
            },
        )

    async def _decide(question: str, completed: list[str], state: FinState) -> tuple[str, str]:
        model = get_model()
        if model is None:
            return classify_next(question, completed)

        prompt = build_supervisor_prompt(state.get("profile_summary") or "")
        context = [
            prompt,
            f"User request: {question}",
            f"Experts already run: {', '.join(completed) or '(none)'}",
        ]
        artifacts = state.get("artifacts") or {}
        if artifacts:
            context.append(f"Findings so far: {json.dumps(artifacts, ensure_ascii=False)[:1200]}")
        if completed:
            context.append("If the request is fully answered, return agent=finish.")

        try:
            response = await model.ainvoke([HumanMessage(content="\n\n".join(context))])
            decision = _parse_supervisor(getattr(response, "content", ""))
        except Exception as exc:  # noqa: BLE001 - never fail a turn on routing
            log.warning("Supervisor 的 LLM 调用失败（%s），回退到规则路由", exc)
            return classify_next(question, completed)

        agent = str(decision.get("agent") or "").strip()
        reason = str(decision.get("reason") or "llm decision")
        if agent in EXPERT_AGENTS or agent == "finish":
            return agent, reason
        log.warning("Supervisor 返回了未知专家 %r，回退到规则路由", agent)
        return classify_next(question, completed)

    # ------------------------------------------------------------------
    def make_expert_node(agent: str) -> Callable[..., Any]:
        async def expert_node(state: FinState) -> Any:
            from langgraph.types import Command

            question = state.get("question") or ""
            outcome = await run_expert(
                agent,
                question,
                toolkit=toolkit,
                model=get_model(),
                profile_summary=state.get("profile_summary") or "",
                prior_artifacts=state.get("artifacts") or {},
            )

            log.info(
                "专家 %s 完成（模式=%s，工具调用=%d，错误=%d）",
                agent,
                outcome.mode,
                outcome.tool_calls,
                len(outcome.errors),
            )

            update: dict[str, Any] = {
                "completed_agents": [agent],
                "artifacts": {**(state.get("artifacts") or {}), **outcome.artifacts},
                "answers": [{"agent": agent, "answer": outcome.answer}],
                "trace": outcome.trace,
                "errors": outcome.errors,
                "next_agent": "supervisor",
            }
            return Command(goto="supervisor", update=update)

        expert_node.__name__ = f"expert_{agent}"
        return expert_node

    # ------------------------------------------------------------------
    async def save_memory(state: FinState) -> dict[str, Any]:
        """持久化本轮学到的持久偏好。

        识别是确定性的（见 :func:`extract_profile_hints`），因此记忆写入可审计、可测试；
        绝不让 LLM 悄悄决定要记什么。
        """

        question = state.get("question") or ""
        user_id = state.get("user_id") or "default"
        current = dict(state.get("user_profile") or {})

        if not looks_like_preference(question):
            return {}

        hints = extract_profile_hints(question)
        if hints.is_empty():
            return {}

        payload = hints.to_payload()
        merged = _merge_profile(current, payload)
        if merged == current:
            return {}

        _store_write_profile(store, user_id, merged)

        # 同步一份到 MCP 画像存储，让所有 MCP 客户端都能看到这次更新。
        runner = ToolRunner(toolkit=toolkit, agent="memory", emit_events=False)
        await runner.call("update_user_profile", **payload)

        return {
            "user_profile": merged,
            "profile_summary": _profile_summary(merged),
            "profile_updates": payload,
            "artifacts": {**(state.get("artifacts") or {}), "memory": payload},
        }

    # ------------------------------------------------------------------
    async def finalize(state: FinState) -> dict[str, Any]:
        """汇总最终答案，并结束本轮的消息列表。"""

        sections = [
            _strip_marker(section.get("answer", ""))
            for section in (state.get("answers") or [])
            if section.get("answer")
        ]
        errors = [error for error in (state.get("errors") or []) if error]

        profile_updates = state.get("profile_updates") or {}

        if sections:
            body = "\n\n---\n\n".join(sections) if len(sections) > 1 else sections[0]
        elif profile_updates:
            body = _memory_acknowledgement(profile_updates)
        else:
            body = _apology(state)

        if errors and not sections:
            body += "\n\n_技术细节：\n" + "\n".join(f"- {error}" for error in errors[:5]) + "_"

        if profile_updates and sections:
            body += (
                "\n\n_已更新你的偏好记忆："
                + "、".join(f"{key}={value}" for key, value in profile_updates.items())
                + "_"
            )

        return {
            "answer": body,
            "messages": [AIMessage(content=body)],
            "next_agent": "finish",
        }

    def _memory_acknowledgement(updates: dict[str, Any]) -> str:
        """纯偏好陈述的一轮没有专家答案，改为给出确认。"""

        labels = {
            "risk_tolerance": "风险偏好",
            "investment_horizon": "投资周期",
            "favorite_sectors": "偏好板块",
            "avoid_sectors": "回避板块",
            "investment_style": "投资风格",
        }
        rows = [
            [labels.get(key, key), "、".join(value) if isinstance(value, list) else str(value)]
            for key, value in updates.items()
        ]
        table = "| 项目 | 记录值 |\n| --- | --- |\n" + "\n".join(
            f"| {row[0]} | {row[1]} |" for row in rows
        )
        return (
            "已更新你的投资偏好档案，之后的分析和推荐都会参考这些设置。\n\n"
            f"{table}\n\n"
            "这些偏好会跨会话保留。想看效果可以说「按我的偏好推荐几只股票」。"
        )

    def _apology(state: FinState) -> str:
        errors = [error for error in (state.get("errors") or []) if error]
        if errors:
            return "抱歉，这个请求没有完成。\n\n" + "\n".join(f"- {error}" for error in errors[:5])
        return (
            "抱歉，我没有理解这个请求。可以试试：\n\n"
            "- 「贵州茅台近一年走势和估值」\n"
            "- 「筛选 PE 小于 15、市值大于 500 亿的银行股」\n"
            "- 「用 MACD 策略回测中信证券近一年」\n"
            "- 「我 10.5 元买了 1000 股平安银行」\n"
            "- 「茅台涨到 2000 元提醒我」"
        )

    def _may_continue(question: str, completed: list[str]) -> bool:
        """Does the request plausibly need a *second* expert?

        A naive "any other agent scored above zero" test produces false follow-ups:
        "用 MACD 策略回测 X" also matches the analyst's `macd` keyword, so the
        supervisor would re-run the analyst on the same symbol. Require the runner-up
        to be a substantial fraction of the winning score instead.
        """

        from finagent.graph.intents import classify_candidates

        ranked = classify_candidates(question)
        if not ranked:
            return False

        top_score = ranked[0][1]
        # 选股请求本来就会提到「市盈率」，而它也是分析师的关键词；要求次优意图得分足够高，
        # 才能避免估值词汇在同一轮里再触发一个专家。
        threshold = max(2.5, top_score * 0.7)
        for agent, score, _matched in ranked:
            if agent in completed:
                continue
            return score >= threshold
        return False

    nodes: dict[str, Callable[..., Any]] = {
        "load_memory": load_memory,
        "supervisor": supervisor,
        "save_memory": save_memory,
        "finalize": finalize,
    }
    for agent in EXPERT_AGENTS:
        nodes[agent] = make_expert_node(agent)
    return nodes


# ----------------------------------------------------------------------
def _profile_summary(profile: dict[str, Any]) -> str:
    favorites = "、".join(profile.get("favorite_sectors") or []) or "未指定"
    avoid = "、".join(profile.get("avoid_sectors") or []) or "未指定"
    custom = profile.get("custom_preferences") or {}
    lines = [
        f"- 风险偏好: {profile.get('risk_tolerance') or 'Unknown'}",
        f"- 投资周期: {profile.get('investment_horizon') or 'Unknown'}",
        f"- 偏好板块: {favorites}",
        f"- 回避板块: {avoid}",
        f"- 投资风格: {profile.get('investment_style') or '未指定'}",
    ]
    if custom:
        lines.append("- 其他: " + "; ".join(f"{k}={v}" for k, v in custom.items()))
    return "\n".join(lines)


def _merge_profile(current: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """把线索合并进画像：板块列表取并集，其他键覆盖。"""

    merged = {**current}
    for key, value in updates.items():
        if key in ("favorite_sectors", "avoid_sectors"):
            existing = list(merged.get(key) or [])
            for item in value if isinstance(value, list) else [value]:
                if item not in existing:
                    existing.append(item)
            merged[key] = existing
        elif value:
            merged[key] = value
    return merged


_ = (AGENT_LABELS, HumanMessage)
