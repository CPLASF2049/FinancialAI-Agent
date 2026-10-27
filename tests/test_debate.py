"""自我纠错链路的集成测试：审查 → 返工 → 替换答案 → 风控。

与 test_critic / test_risk 的分工：
* 那两个文件测**规则本身**；
* 这个文件测**图里真的按预期走**——返工次数受限、旧答案被替换、副作用专家不被重跑。
"""

from __future__ import annotations

import pytest

from finagent.graph import open_app
from finagent.graph.experts import ExpertOutcome
from finagent.graph.state import EXPERT_AGENTS


def _outcome(agent: str, answer: str, *, tool_calls: int = 1, preview: str = "") -> ExpertOutcome:
    trace = (
        [
            {
                "agent": agent,
                "tool": "get_realtime_price",
                "args": {"ts_code": "600519.SH"},
                "ok": True,
                "preview": preview or '{"price": 1650.956}',
            }
        ]
        if tool_calls
        else []
    )
    return ExpertOutcome(
        agent=agent,
        answer=answer,
        trace=trace,
        tool_calls=tool_calls,
        mode="deterministic",
    )


# ----------------------------------------------------------------------
async def test_critic_runs_after_every_expert(offline_env) -> None:
    async with open_app() as app:
        result = await app.arun("茅台估值贵不贵", user_id="cc1", thread_id="cc1")

    steps = [item["step"] for item in result.decisions]
    assert "critic" in steps
    # 每个跑过的专家都要被审一次
    assert steps.count("critic") == len(result.agents)


async def test_revision_replaces_the_old_answer(
    offline_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """初版结论没有数据支撑 → 退回返工 → 旧答案必须被替换掉，而不是两版并存。"""

    calls: list[str] = []

    async def fake_run_expert(agent: str, question: str, **kwargs):
        calls.append(agent)
        if len(calls) == 1:
            return _outcome(agent, "茅台现价 1500 元，建议买入。", tool_calls=0)
        return _outcome(agent, "茅台现价 1650.96 元。", tool_calls=1)

    monkeypatch.setattr("finagent.graph.nodes.run_expert", fake_run_expert)

    async with open_app() as app:
        result = await app.arun("茅台现在多少钱", user_id="rv1", thread_id="rv1")

    assert len(calls) == 2, "应当恰好返工一次"
    assert "1500" not in result.answer, "旧结论没有被替换"
    assert "1650.96" in result.answer
    assert result.answer.count("---") == 0, "不该把两版结论拼在一起"

    verdicts = [item["verdict"] for item in result.decisions if item["step"] == "critic"]
    assert verdicts == ["revise", "pass"]


async def test_revision_budget_is_capped(offline_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """专家一直不合格时，也只能返工 max_revisions 次，然后带着提示交付。"""

    calls: list[str] = []

    async def always_bad(agent: str, question: str, **kwargs):
        calls.append(agent)
        return _outcome(agent, "茅台现价 1234 元。", tool_calls=0)

    monkeypatch.setattr("finagent.graph.nodes.run_expert", always_bad)

    async with open_app() as app:
        result = await app.arun("茅台现在多少钱", user_id="rv2", thread_id="rv2")

    assert len(calls) == 2  # 初版 + 1 次返工（默认 FINAGENT_MAX_REVISIONS=1）
    notes = [
        issue
        for item in result.decisions
        if item["step"] == "critic"
        for issue in (item.get("issues") or [])
        if issue.get("type") == "note"
    ]
    assert notes, "额度用尽后要留下「未返工」的提示"


async def test_side_effect_agents_never_rerun(offline_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """持仓写入有副作用：即使结论不合格也不能重跑。"""

    calls: list[str] = []

    async def bad_portfolio(agent: str, question: str, **kwargs):
        calls.append(agent)
        return _outcome(agent, "已记录买入，市值 999999 元。", tool_calls=0)

    monkeypatch.setattr("finagent.graph.nodes.run_expert", bad_portfolio)

    async with open_app() as app:
        result = await app.arun("我的持仓怎么样", user_id="rv3", thread_id="rv3")

    assert calls == ["portfolio"], "持仓专家被重跑了，会造成重复下单"
    notes = [
        issue
        for item in result.decisions
        if item["step"] == "critic"
        for issue in (item.get("issues") or [])
        if issue.get("type") == "note"
    ]
    assert notes and "副作用" in notes[0]["detail"]


async def test_risk_guard_rewrites_conservative_profile(
    offline_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def leverage_happy(agent: str, question: str, **kwargs):
        return _outcome(agent, "建议满仓买入，必涨。数据来源：离线，截至 20261028。")

    monkeypatch.setattr("finagent.graph.nodes.run_expert", leverage_happy)

    async with open_app() as app:
        app.store.put(("users", "rg1"), "profile", {"risk_tolerance": "Conservative"})
        result = await app.arun("茅台现在多少钱", user_id="rg1", thread_id="rg1")

    assert "满仓" not in result.answer
    assert "控制仓位" in result.answer
    assert result.risk_flags
    assert any(flag["code"] == "risk_preference" for flag in result.risk_flags)
    assert any(item["step"] == "risk" for item in result.decisions)


async def test_risk_guard_appends_source_footer(
    offline_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_source(agent: str, question: str, **kwargs):
        return _outcome(agent, "茅台现价 1650.96 元。")

    monkeypatch.setattr("finagent.graph.nodes.run_expert", no_source)

    async with open_app() as app:
        result = await app.arun("茅台现在多少钱", user_id="rg2", thread_id="rg2")

    assert "数据来源" in result.answer
    assert any(flag["code"] == "missing_source" for flag in result.risk_flags)


# ----------------------------------------------------------------------
async def test_deep_analysis_runs_both_analysts(offline_env) -> None:
    async with open_app() as app:
        result = await app.arun("茅台帮我深度分析一下", user_id="dp1", thread_id="dp1")

    assert result.agents == ["fundamental_analyst", "technical_analyst"]
    assert "基本面" in result.answer
    assert "技术面" in result.answer


async def test_single_dimension_question_runs_one_analyst(offline_env) -> None:
    async with open_app() as app:
        result = await app.arun("茅台估值贵不贵", user_id="dp2", thread_id="dp2")

    assert result.agents == ["fundamental_analyst"]


@pytest.mark.parametrize("agent", EXPERT_AGENTS)
async def test_every_expert_has_a_label(offline_env, agent: str) -> None:
    from finagent.graph.state import AGENT_LABELS

    assert agent in AGENT_LABELS
