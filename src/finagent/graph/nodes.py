"""图节点：记忆注入/落库、Supervisor、收尾。

节点由 :func:`build_nodes` 构造，闭包捕获本次运行所需的资源（MCP toolkit、聊天模型、
store、settings）。这样图定义本身是纯的，同时每个节点又恰好拿到它需要的依赖——
没有全局变量，没有 import 期单例，整张图用假件就能测。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from finagent.graph.agent_loop import stream_writer
from finagent.graph.critic import (
    is_revisable,
    reconcile,
    review,
    review_with_llm,
    summarise_tools,
)
from finagent.graph.experts import run_expert
from finagent.graph.intents import (
    classify_next,
    extract_profile_hints,
    looks_like_preference,
)
from finagent.graph.mcp_client import Toolkit
from finagent.graph.prompts import build_supervisor_prompt
from finagent.graph.risk import guard, held_codes_from
from finagent.graph.state import AGENT_LABELS, EXPERT_AGENTS, FinState
from finagent.graph.tooling import ToolRunner
from finagent.graph.verify import verify_backtest_answer
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
    #: append-only 事件表（P1-1）。为 None 时退化成「只出 SSE、不落盘」。
    events: Any = None

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
def _recorder_factory(events: Any) -> Callable[[str], Any]:
    """按 task_id 造一个 recorder；没有事件表就是空实现。"""

    from finagent.core.events import TurnRecorder

    def make(task_id: str) -> TurnRecorder:
        return TurnRecorder(events, task_id or "")

    return make


def build_nodes(resources: GraphResources) -> dict[str, Callable[..., Any]]:
    recorder = _recorder_factory(resources.events)

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

        budget = _bump(state)
        steps = budget["steps"]

        # 三重预算先于一切判断：任何一个触顶都不再派发新工作
        exceeded = _turn_exceeded({**state, "steps": steps - 1}, settings)
        if exceeded:
            return Command(
                goto="save_memory",
                update={
                    **budget,
                    "stop_reason": exceeded,
                    "route_reason": _describe_stop(exceeded, state),
                    "next_agent": "finish",
                    "decision_log": [
                        {
                            "step": "budget",
                            "label": "预算用尽",
                            "reason": exceeded,
                            "detail": _describe_stop(exceeded, state),
                        }
                    ],
                },
            )

        if hops >= settings.max_agent_hops:
            return Command(
                goto="save_memory",
                update={
                    **budget,
                    "stop_reason": "hops",
                    "route_reason": f"已达到跳转上限（{settings.max_agent_hops}）",
                    "next_agent": "finish",
                },
            )
        if answers and hops > 0 and not _may_continue(question, completed):
            return Command(
                goto="save_memory",
                update={**budget, "route_reason": "已答完", "next_agent": "finish"},
            )

        agent, reason = await _decide(question, completed, state)

        if agent == "finish" or agent in completed:
            return Command(
                goto="save_memory",
                update={**budget, "route_reason": reason or "finish", "next_agent": "finish"},
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

        recorder(str(state.get("task_id") or "")).record(
            "route:decision", from_node="supervisor", to_node=agent, reason=reason
        )
        return Command(
            goto=agent,
            update={
                **budget,
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
            from langgraph.types import Command, Overwrite

            question = state.get("question") or ""
            critique = state.get("critique") or {}
            is_revision = critique.get("agent") == agent and critique.get("verdict") == "revise"

            step_started = time.monotonic()
            recorder(str(state.get("task_id") or "")).record(
                "step:start",
                agent=agent,
                step_id=f"{agent}-{int(step_started * 1000)}",
                is_revision=is_revision,
            )
            # Steering：在 step 边界消费用户中途下的指令（不打断正在跑的工具）
            steer = _drain_steering(state)
            if steer["cancel"]:
                log.info("收到取消指令，结束本轮")
                return Command(
                    goto="save_memory",
                    update={
                        **_bump(state),
                        "stop_reason": "cancelled",
                        "route_reason": "用户取消了本轮执行",
                        "next_agent": "finish",
                        "decision_log": [
                            {
                                "step": "steer",
                                "label": "用户取消",
                                "detail": "收到 cancel 指令，已停止派发新工作",
                            }
                        ],
                    },
                )
            if steer["instructions"]:
                extra = "\n".join(steer["instructions"])
                question = f"{question}\n\n（执行中收到的新要求，请遵守）\n{extra}"
                recorder(str(state.get("task_id") or "")).record(
                    "steer:applied", instructions=steer["instructions"], focus=steer["focus"]
                )

            passes = _passes_for(agent, question)
            outcome = await run_expert(
                agent,
                question,
                toolkit=toolkit,
                model=get_model(),
                profile_summary=state.get("profile_summary") or "",
                prior_artifacts=state.get("artifacts") or {},
                critique=critique if is_revision else None,
                passes=passes,
                # 三重预算 + 压缩回调：都随调用下传，别只停在 supervisor 那一层
                deadline=_deadline(state, settings),
                token_budget=settings.turn_token_budget,
                context_budget=settings.context_budget_tokens,
                on_compact=_compact_sink(state, recorder),
            )

            stats = outcome.tool_stats or {}
            log.info(
                "专家 %s 完成（模式=%s，工具=%d[成功%d/失败%d/不支持%d/重试%d]，错误=%d，返工=%s）",
                agent,
                outcome.mode,
                int(stats.get("tool_calls_total") or outcome.tool_calls),
                int(stats.get("tool_calls_succeeded") or 0),
                int(stats.get("tool_calls_failed") or 0),
                int(stats.get("tool_calls_unsupported") or 0),
                int(stats.get("provider_retries") or 0),
                len(outcome.errors),
                "是" if is_revision else "否",
            )

            entry: dict[str, Any] = {
                "agent": agent,
                "answer": outcome.answer,
                # 执行协议里的终止事实，随结论一起留给下游与决策轨迹
                "status": str(outcome.status),
                "termination_reason": str(outcome.termination_reason),
            }
            if is_revision:
                entry["revised"] = True
                entry["revision"] = int((state.get("revisions") or {}).get(agent, 0))

            # 工具调用逐一落盘（trace 已含 call_id / 耗时 / 状态）
            for item in outcome.trace:
                record_ = recorder(str(state.get("task_id") or ""))
                record_.record(
                    "tool:call",
                    tool=item.get("tool"),
                    agent=agent,
                    call_id=item.get("call_id"),
                )
                record_.record(
                    "tool:result",
                    tool=item.get("tool"),
                    call_id=item.get("call_id"),
                    status="ok" if item.get("ok") else "error",
                    latency_ms=item.get("latency_ms", 0),
                    error=(item.get("error") or "")[:200],
                )
            recorder(str(state.get("task_id") or "")).record(
                "step:end",
                agent=agent,
                wall_time_ms=int((time.monotonic() - step_started) * 1000),
                termination_reason=str(outcome.termination_reason),
                token_usage=outcome.token_usage.to_dict(),
            )

            update: dict[str, Any] = {
                **_bump(state),
                "completed_agents": [agent],
                "artifacts": {**(state.get("artifacts") or {}), **outcome.artifacts},
                "trace": outcome.trace,
                # 「当前数据源不提供 X」是预期内的能力边界，不是故障：
                # 模型换个工具就能拿到数据，把它当错误弹给用户只会造成困惑。
                "errors": [error for error in outcome.errors if not _expected_gap(error)],
                "next_agent": "critic",
            }

            if is_revision:
                # 修订版要**替换**旧结论，否则最终答案里会出现自相矛盾的两段
                previous = [
                    dict(section)
                    for section in (state.get("answers") or [])
                    if section.get("agent") != agent
                ]
                update["answers"] = Overwrite([*previous, entry])
            else:
                update["answers"] = [entry]

            # 没有可交付内容（契约违反 / 模型失败）时不必再进审查——
            # 拿一段空文本去问「这段靠谱吗」纯属浪费一次模型调用。
            if not outcome.answer.strip():
                log.warning(
                    "专家 %s 没有可交付内容（%s），跳过审查", agent, outcome.termination_reason
                )
                update["next_agent"] = "finish"
                update["decision_log"] = [
                    {
                        "step": "expert",
                        "agent": agent,
                        "label": AGENT_LABELS.get(agent, agent),
                        "termination_reason": str(outcome.termination_reason),
                        "detail": "本轮没有产出可交付结论，已跳过审查",
                    }
                ]
                return Command(goto="supervisor", update=update)

            return Command(goto="critic", update=update)

        expert_node.__name__ = f"expert_{agent}"
        return expert_node

    # ------------------------------------------------------------------
    async def critic(state: FinState) -> Any:
        """审查刚产出的结论：能不能直接交付？

        这是「多空辩论」的低成本版本——不加第二个写作 Agent，只在结论后面加一道结构化
        检查。``revise`` 会把意见打回原专家返工，返工次数由 ``max_revisions`` 封顶。
        """

        from langgraph.types import Command

        answers = state.get("answers") or []
        if not answers:
            return Command(goto="supervisor", update={})

        last = answers[-1]
        agent = str(last.get("agent") or "")
        answer = str(last.get("answer") or "")
        if not agent or not answer.strip():
            return Command(goto="supervisor", update={})

        question = state.get("question") or ""
        # 兼容两种记录格式：裸 agent 名，或早期的 "agent:stage"。
        # 早期格式会让按 agent 精确匹配永远落空，审查者就会一直误判「没调工具」。
        trace = [
            item
            for item in (state.get("trace") or [])
            if item.get("agent") == agent or str(item.get("agent") or "").startswith(f"{agent}:")
        ]
        tool_calls = sum(1 for item in trace if item.get("ok"))
        evidence = " ".join(str(item.get("preview") or "") for item in trace)
        others = [item for item in answers if item.get("agent") != agent]

        model = get_model()
        # 审查者本身是一次完整的模型调用（推理模型常常 60 秒起步）。
        # 剩余时间不够就别叫它了，直接用规则审——规则审是确定性的，也更省额度。
        deadline = _deadline(state, settings)
        if model is not None and deadline is not None:
            import time as _time

            if _time.monotonic() > deadline - 60:
                log.info("剩余时间不足，审查者改用规则审查")
                model = None

        if model is None:
            result = review(
                answer,
                agent=agent,
                question=question,
                tool_calls=tool_calls,
                evidence=evidence,
                other_answers=others,
            )
        else:
            result = await review_with_llm(
                model,
                answer,
                agent=agent,
                question=question,
                tool_summary=summarise_tools(trace),
                other_answers=others,
                tool_calls=tool_calls,
                evidence=evidence,
            )

        # 与客观记录对账：模型偶尔会断言「没调工具」「数字没出处」，
        # 而调用记录和工具返回就在手上——以事实为准，别让审查结论自相矛盾。
        result = reconcile(
            result,
            answer=answer,
            tool_calls=tool_calls,
            evidence=evidence,
        )

        # 证据校验：确定性代码检查「数字有没有出处、涨跌幅能不能重算、数据是否过期」。
        # 这一层不依赖 LLM——Critic 能看出「像不像有依据」，只有代码能算出「对不对」。
        evidence_report = _validate_evidence(answer, trace, question)
        if evidence_report is not None and not evidence_report.ok:
            for blocker in evidence_report.blockers:
                result.issues.append(
                    {
                        "type": "unsupported",
                        "label": "证据不足",
                        "detail": blocker,
                        "severity": "high",
                    }
                )
            result.verdict = "revise"

        # 代码级校验：只要这一轮真的跑过回测，就用子进程复算一遍，和结论里声称的指标比对
        verification = None
        if settings.verify_backtests:
            verification = verify_backtest_answer(answer, trace)
            if verification.attempted:
                result.issues.extend(verification.issues)
                if verification.issues:
                    result.verdict = "revise"
                log.info(
                    "回测复算：%s（声称 %d 项，不符 %d 项）",
                    "通过" if verification.ok else "不通过",
                    len(verification.claimed),
                    len(verification.issues),
                )

        revisions = dict(state.get("revisions") or {})
        total_revisions = int(state.get("total_revisions") or 0)
        used = int(revisions.get(agent, 0))
        budget_left = used < settings.max_revisions
        total_left = total_revisions < settings.max_total_revisions
        revisable = is_revisable(agent, trace) and budget_left and total_left
        wants_revision = result.verdict == "revise"

        # 返工后结论一个字都没变 → 再审一遍也不会有新结果，直接接受，避免来回打乒乓
        previous = state.get("critique") or {}
        if (
            wants_revision
            and previous.get("verdict") == "revise"
            and previous.get("answer_hash")
            and previous["answer_hash"] == _hash(answer)
        ):
            wants_revision = False
            result.issues.append(
                {
                    "type": "note",
                    "label": "返工无变化",
                    "detail": "上一版返工后的结论与之前完全一致，不再重复返工",
                    "severity": "medium",
                }
            )

        if wants_revision and not revisable:
            # 记录但不返工：额度用尽，或这个专家有副作用（持仓/预警重跑会重复下单）
            if not total_left:
                reason = "本轮返工总额度已用尽"
            elif not budget_left:
                reason = "已达单专家修订上限"
            else:
                reason = "该专家有副作用，不重跑"
            result.issues.append(
                {
                    "type": "note",
                    "label": "未返工",
                    "detail": f"审查建议返工，但{reason}；以上问题保留提示",
                    "severity": "medium",
                }
            )
            wants_revision = False

        if wants_revision:
            revisions[agent] = used + 1
            total_revisions += 1

        label = AGENT_LABELS.get(agent, agent)
        payload = {
            "agent": agent,
            "label": label,
            "verdict": "revise" if wants_revision else "pass",
            "issues": result.issues,
            "revision": revisions.get(agent, used) if wants_revision else used,
            "source": result.source,
            "answer_hash": _hash(answer),
            "verified": bool(verification.attempted) if verification else False,
            "verification": verification.to_dict()
            if verification and verification.attempted
            else None,
        }

        # 让前端实时看到审查结论（含「分歧信号」这类不阻断交付的提示）
        stream_writer()(
            {
                "event": "critic",
                "agent": agent,
                "label": label,
                "verdict": payload["verdict"],
                "issues": result.issues,
                "revision": payload["revision"],
            }
        )

        log.info(
            "审查 %s：%s（%d 条意见，来源=%s）",
            agent,
            payload["verdict"],
            len(result.issues),
            result.source,
        )

        recorder(str(state.get("task_id") or "")).record(
            "review:verdict",
            agent=agent,
            verdict=payload["verdict"],
            issues=result.issues,
        )

        log_entry = {
            "step": "critic",
            "agent": agent,
            "label": label,
            "verdict": payload["verdict"],
            "issues": result.issues,
        }

        budget_update = {**_bump(state), "total_revisions": total_revisions}

        if wants_revision:
            return Command(
                goto=agent,
                update={
                    **budget_update,
                    "critique": payload,
                    "revisions": revisions,
                    "decision_log": [log_entry],
                },
            )

        return Command(
            goto="supervisor",
            update={**budget_update, "critique": payload, "decision_log": [log_entry]},
        )

    # ------------------------------------------------------------------
    async def risk_guard(state: FinState) -> dict[str, Any]:
        """确定性护栏：在交付前把结论过一遍硬规则。

        刻意不用 LLM —— 「画像保守却建议加杠杆」这种问题是规则判断，不是推理问题。
        让概率模型去决定要不要拦自己，既不可靠也不可审计。
        """

        body = _compose_answer(state)
        if not settings.risk_guard_enabled or not body.strip():
            return {"answer": body}

        outcome = guard(
            body,
            profile=state.get("user_profile") or {},
            held_codes=held_codes_from(state.get("artifacts")),
            provider=settings.provider,
        )
        if outcome.flags:
            for flag in outcome.flags:
                recorder(str(state.get("task_id") or "")).record(
                    "risk:triggered",
                    rule_name=flag.get("type") or flag.get("label") or "risk",
                    action_taken="已标注并提示",
                    detail=flag.get("detail") or "",
                )

        if not outcome.flags:
            return {"answer": outcome.text}

        stream_writer()(
            {
                "event": "risk",
                "flags": outcome.flags,
                "actions": outcome.actions,
                "blocked": outcome.blocked,
            }
        )

        return {
            "answer": outcome.text,
            "risk_flags": outcome.flags,
            "decision_log": [
                {
                    "step": "risk",
                    "label": AGENT_LABELS["risk_guard"],
                    "flags": outcome.flags,
                    "actions": outcome.actions,
                }
            ],
        }

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
        """收尾：确定最终正文，并把它作为 AI 消息写进会话历史。"""

        # risk_guard 可能已经改写过正文（风控拦截），优先用它的结果
        body = state.get("answer") or _compose_answer(state)

        # 部分成功：按任务分解判定必做/可选，缺什么就明说，但不拖累已成功的内容
        try:
            from finagent.graph.tasking import assess, notice_for, plan_for

            plan = plan_for(state.get("question") or "")
            assessment = assess(plan, list(state.get("trace") or []))
            note = notice_for(assessment)
            if note:
                body = f"{body}\n\n> {note}"
            recorder(str(state.get("task_id") or "")).record(
                "turn:assessment", **assessment.to_dict()
            )
        except Exception as exc:  # noqa: BLE001 - 判定失败不影响交付
            log.debug("任务评估失败：%s", exc)

        recorder(str(state.get("task_id") or "")).record(
            "turn:end",
            status=str(state.get("stop_reason") or "completed"),
            steps=int(state.get("steps") or 0),
            agents=list(state.get("completed_agents") or []),
            answer_chars=len(body),
        )

        return {
            "answer": body,
            "messages": [AIMessage(content=body)],
            "next_agent": "finish",
        }

    def _may_continue(question: str, completed: list[str]) -> bool:
        """这一轮是否还该再派一个 Agent？

        A naive "any other agent scored above zero" test produces false follow-ups:
        "用 MACD 策略回测 X" also matches the researcher's `macd` keyword, so the
        supervisor would re-run the researcher on the same symbol. Require the runner-up
        to be a substantial fraction of the winning score instead.

        注意：合并成 3 个 Agent 之后，「深度分析」不再需要额外判真——研究 Agent 内部会
        自己跑基本面 + 技术面两个子职责，所以这里只看关键词得分。
        """

        from finagent.graph.intents import classify_candidates

        if any(agent not in completed for agent in EXPERT_AGENTS) is False:
            return False

        ranked = classify_candidates(question)
        if not ranked:
            return False

        top_score = ranked[0][1]
        # 选股请求本来就会提到「市盈率」，而它也是研究 Agent 的关键词；要求次优意图得分足够高，
        # 才能避免估值词汇在同一轮里再触发一个 Agent。
        threshold = max(2.5, top_score * 0.7)
        for agent, score, _matched in ranked:
            if agent in completed:
                continue
            return score >= threshold
        return False

    nodes: dict[str, Callable[..., Any]] = {
        "load_memory": load_memory,
        "supervisor": supervisor,
        "critic": critic,
        "risk_guard": risk_guard,
        "save_memory": save_memory,
        "finalize": finalize,
    }
    for agent in EXPERT_AGENTS:
        nodes[agent] = make_expert_node(agent)
    return nodes


# ----------------------------------------------------------------------
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


# ----------------------------------------------------------------------
#: 触顶原因 → 给用户看的一句话
_STOP_LABELS = {
    "time": "本轮已用完时间预算",
    "steps": "本轮已用完步数预算",
    "hops": "已到跳转次数上限",
}


#: 「数据源不提供某能力」这类错误对用户没有意义（换个工具就有数据），
#: 只留在 trace 与日志里，不进交付内容。
_GAP_MARKERS = ("不提供", "不支持", "not supported")

#: 传输层抖动（断连、超时）同样不该糊到用户脸上：回答里已经会写「接口暂时不可用」，
#: 再贴一遍 ``Remote end closed connection without response`` 只有噪音。
_TRANSPORT_MARKERS = (
    "Remote end closed",
    "Connection",
    "timed out",
    "timeout",
    "URLError",
    "Max retries",
    "执行失败",
)


def _expected_gap(error: str) -> bool:
    """这条错误是否属于「预期内的边界」，不该出现在交付内容里。"""

    text = str(error or "")
    return any(marker in text for marker in (*_GAP_MARKERS, *_TRANSPORT_MARKERS))


def _hash(text: str) -> str:
    """结论指纹：用来判断「返工之后有没有实质变化」。"""

    import hashlib

    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]


def _bump(state: FinState) -> dict[str, Any]:
    """每个节点进入时 +1，用于步数预算。"""

    return {"steps": int(state.get("steps") or 0) + 1}


def _elapsed(state: FinState) -> float | None:
    """本轮已经跑了多久（秒）。基线由 ``_reset_turn`` 写入。"""

    started = state.get("turn_started")
    if not isinstance(started, (int, float)) or started <= 0:
        return None
    import time

    return time.monotonic() - float(started)


def _turn_exceeded(state: FinState, settings: Settings) -> str | None:
    """本轮是否该强制收尾？返回触顶原因（没有则 ``None``）。

    这是「停不下来」的兜底。以前只有 ``max_agent_hops`` 一个限制，它只数跳转次数——
    模型慢的时候一轮照样能跑几分钟、烧掉大量 token。现在时间、步数、返工次数三个维度
    各自封顶，任何一个先到就收尾，并把原因写进决策轨迹。
    """

    elapsed = _elapsed(state)
    if elapsed is not None and elapsed > settings.max_turn_seconds:
        return "time"
    if int(state.get("steps") or 0) >= settings.max_steps:
        return "steps"
    # 注意：返工总额度不在这里判。额度用尽量只该「不再返工」，而不是让整轮停摆——
    # critic 里已经用 total_left 挡住了，这里再拦会把 max_total_revisions=0 变成
    # 「一步都不许走」，那显然不是用户的意图。
    return None


def _drain_steering(state: FinState) -> dict[str, Any]:
    """取出本轮的 Steering 指令。没有 task_id 就当没指令。"""

    from finagent.core.steering import QUEUE, apply

    task_id = str(state.get("task_id") or "")
    if not task_id:
        return {"cancel": False, "instructions": [], "focus": ""}
    return apply(QUEUE.drain(task_id))


def _passes_for(agent: str, question: str) -> list[str]:
    """这一轮要跑哪些子职责。与 experts 内部的默认逻辑保持一致。"""

    from finagent.graph.experts import _default_passes

    return list(_default_passes(agent, question))


def _validate_evidence(answer: str, trace: list[dict[str, Any]], question: str) -> Any:
    """用确定性代码校验证据；失败时返回 None（校验不该拖垮审查）。"""

    try:
        from finagent.graph.evidence_validator import evidence_from_trace, validate

        evidence = evidence_from_trace(trace)
        if not evidence:
            return None
        as_of = ""
        for item in trace:
            preview = str(item.get("preview") or "")
            marker = '"data_as_of":'
            if marker in preview:
                as_of = "".join(ch for ch in preview.split(marker, 1)[1][:20] if ch.isdigit())[:8]
                break
        now = time.strftime("%Y%m%d")
        return validate(answer, evidence=evidence, as_of=as_of, now=now)
    except Exception as exc:  # noqa: BLE001 - 校验失败不影响主流程
        log.debug("证据校验异常：%s", exc)
        return None


def _compact_sink(state: FinState, recorder: Any):
    """把压缩事件写进事件表：恢复时要靠它知道上下文被压过、压到了多少。"""

    task_id = str(state.get("task_id") or "")

    def sink(info: dict[str, Any]) -> None:
        recorder(task_id).record("context:compacted", **info)

    return sink


def _deadline(state: FinState, settings: Settings) -> float | None:
    """本轮的截止时刻；``turn_started`` 缺失时返回 ``None``（不限）。"""

    started = state.get("turn_started")
    if not isinstance(started, (int, float)) or started <= 0:
        return None
    # 多给 5 秒，让节点里能走完收尾逻辑再停
    return float(started) + settings.max_turn_seconds + 5.0


def _describe_stop(reason: str, state: FinState) -> str:
    label = _STOP_LABELS.get(reason, "本轮已收尾")
    steps = int(state.get("steps") or 0)
    text = f"{label}（{steps} 步"
    elapsed = _elapsed(state)
    if elapsed is not None:
        text += f"，{elapsed:.0f} 秒"
    return text + "）"


# ----------------------------------------------------------------------
def _compose_answer(state: FinState) -> str:
    """把各专家的结论拼成交付给用户的正文。

    单独抽出来是因为有两个节点要用它：``risk_guard`` 要在交付前审阅正文，
    ``finalize`` 要在没被改写过时自己拼一份。
    """

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
        # 只有完全没产出时才提技术细节，且不要糊一屏原始报错
        body += "\n\n_部分数据接口暂时不可用，可稍后重试。_"

    if sections:
        # 统一一条收尾，而不是让每个子职责各写一份
        body += (
            "\n\n> 风险提示：以上为公开数据整理与技术判断，不构成投资建议；"
            "市场有风险，决策请自行独立判断。"
        )

    if profile_updates and sections:
        body += (
            "\n\n_已更新你的偏好记忆："
            + "、".join(f"{key}={value}" for key, value in profile_updates.items())
            + "_"
        )

    return body


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
