"""终止性测试：一轮对话必须在有限步数/时间内收敛。

这些用例保护的是「停不下来」这类故障——Supervisor 反复派活、Critic 反复打回，
都会在这里被抓住。做法是把预算调到极小值，验证：
1. 该停的时候真的停了；
2. 停了之后仍然**有答案**（不是抛异常、不是空响应）；
3. 停的原因被写进 ``stop_reason`` 与决策轨迹。
"""

from __future__ import annotations

import asyncio

import pytest

from finagent.graph import open_app
from finagent.graph.experts import ExpertOutcome
from finagent.settings import reset_settings_cache


def _set(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    reset_settings_cache()


@pytest.fixture(autouse=True)
def _clean_settings():
    yield
    reset_settings_cache()


def _bad_expert(answer: str = "茅台现价 1234 元，必涨。"):
    """专家永远给出没有数据支撑的结论 → 审查者永远想返工。"""

    calls: list[str] = []

    async def runner(agent: str, question: str, **kwargs) -> ExpertOutcome:
        calls.append(agent)
        return ExpertOutcome(agent=agent, answer=answer, trace=[], tool_calls=0)

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


# ----------------------------------------------------------------------
async def test_critic_cannot_loop_forever(offline_env, monkeypatch) -> None:
    """审查者一直打回，也必须收敛：默认 1 次返工后接受。"""

    runner = _bad_expert()
    monkeypatch.setattr("finagent.graph.nodes.run_expert", runner)

    async with open_app() as app:
        result = await asyncio.wait_for(
            app.arun("茅台现在多少钱", user_id="t1", thread_id="t1"), timeout=60
        )

    assert len(runner.calls) == 2, f"应当只返工一次，实际调用了 {len(runner.calls)} 次"
    assert result.answer.strip()


async def test_total_revision_budget_zero_means_no_revision(offline_env, monkeypatch) -> None:
    """``max_total_revisions=0`` 是「不返工」，不是「一步都不走」。"""

    _set(monkeypatch, FINAGENT_MAX_TOTAL_REVISIONS="0")
    runner = _bad_expert()
    monkeypatch.setattr("finagent.graph.nodes.run_expert", runner)

    async with open_app() as app:
        result = await asyncio.wait_for(
            app.arun("茅台现在多少钱", user_id="t2", thread_id="t2"), timeout=60
        )

    assert len(runner.calls) == 1, "额度为 0 时不应该返工"
    assert result.stop_reason == "", "额度为 0 不该让整轮停摆"
    assert result.answer.strip()


async def test_total_revision_budget_caps_across_agents(offline_env, monkeypatch) -> None:
    """返工总额度是跨 Agent 的：多个 Agent 加起来不能超过上限。"""

    _set(monkeypatch, FINAGENT_MAX_REVISIONS="3", FINAGENT_MAX_TOTAL_REVISIONS="1")
    runner = _bad_expert()
    monkeypatch.setattr("finagent.graph.nodes.run_expert", runner)

    async with open_app() as app:
        result = await asyncio.wait_for(
            app.arun("回测茅台，再看看我的持仓", user_id="t3", thread_id="t3"), timeout=90
        )

    # 单 Agent 允许 3 次，但总额度只有 1 次 → 整个回合最多 1 次返工
    assert len(runner.calls) <= 3, f"返工总额度没生效：{len(runner.calls)} 次"
    assert result.answer.strip()


async def test_step_budget_stops_the_turn(offline_env, monkeypatch) -> None:
    _set(monkeypatch, FINAGENT_MAX_STEPS="1")

    async with open_app() as app:
        result = await asyncio.wait_for(
            app.arun("茅台帮我深度分析一下", user_id="t4", thread_id="t4"), timeout=60
        )

    assert result.stop_reason == "steps"
    assert result.answer.strip(), "被预算截断也要给出回答，而不是空响应"
    assert any(item["step"] == "budget" for item in result.decisions)


async def test_time_budget_stops_the_turn(offline_env, monkeypatch) -> None:
    _set(monkeypatch, FINAGENT_MAX_TURN_SECONDS="0.001")

    async with open_app() as app:
        result = await asyncio.wait_for(
            app.arun("茅台现在多少钱", user_id="t5", thread_id="t5"), timeout=60
        )

    assert result.stop_reason == "time"
    assert result.answer.strip()


async def test_hop_limit_still_works(offline_env, monkeypatch) -> None:
    _set(monkeypatch, FINAGENT_MAX_AGENT_HOPS="1")

    async with open_app() as app:
        result = await asyncio.wait_for(
            app.arun("回测茅台，再看看我的持仓", user_id="t6", thread_id="t6"), timeout=60
        )

    assert len(result.agents) <= 1
    assert result.answer.strip()


# ----------------------------------------------------------------------
async def test_unchanged_answer_is_not_revised_twice(offline_env, monkeypatch) -> None:
    """返工后结论一个字都没变 → 不再重复返工（防止两个节点来回打乒乓）。"""

    calls: list[str] = []

    async def stubborn(agent: str, question: str, **kwargs) -> ExpertOutcome:
        calls.append(agent)
        # 每次返工都给出**完全相同**的结论
        return ExpertOutcome(agent=agent, answer="茅台现价 1234 元。", trace=[], tool_calls=0)

    monkeypatch.setattr("finagent.graph.nodes.run_expert", stubborn)

    async with open_app() as app:
        result = await asyncio.wait_for(
            app.arun("茅台现在多少钱", user_id="t7", thread_id="t7"), timeout=60
        )

    assert len(calls) == 2
    notes = [
        issue
        for item in result.decisions
        if item["step"] == "critic"
        for issue in (item.get("issues") or [])
        if issue.get("type") == "note"
    ]
    assert any("无变化" in note.get("label", "") for note in notes)
    assert result.answer.strip()


async def test_recursion_limit_is_derived_from_budget(offline_env) -> None:
    """递归上限按步数预算推导，不写死 60。"""

    async with open_app() as app:
        config = app._config("thread", "user")
    assert config["recursion_limit"] >= 40
    assert config["recursion_limit"] >= app.settings.max_steps


async def test_normal_turn_reports_no_stop_reason(offline_env) -> None:
    """正常一轮不该被标记为「被预算截断」。"""

    async with open_app() as app:
        result = await app.arun("茅台现在多少钱", user_id="t8", thread_id="t8")
    assert result.stop_reason == ""
    assert result.agents == ["researcher"]
