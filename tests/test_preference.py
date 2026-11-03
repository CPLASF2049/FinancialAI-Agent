"""回归测试：偏好陈述必须被识别并确认，不能变成道歉。"""

from __future__ import annotations

import asyncio

import pytest

from finagent.graph.intents import extract_profile_hints, looks_like_preference
from finagent.graph.risk import check_source_annotation
from finagent.graph.tasking import Overall, assess, notice_for, plan_for

PREFERENCE = "我是保守型投资者，偏好银行和白酒板块"


# ----------------------------------------------------------------------
# 识别层
# ----------------------------------------------------------------------
def test_preference_is_detected() -> None:
    assert looks_like_preference(PREFERENCE) is True


def test_preference_hints() -> None:
    payload = extract_profile_hints(PREFERENCE).to_payload()
    assert payload["risk_tolerance"].lower().startswith("conservative")
    assert set(payload["favorite_sectors"]) == {"白酒", "银行"}


# ----------------------------------------------------------------------
# 端到端：不能变成「抱歉，我没有理解」
# ----------------------------------------------------------------------
async def test_preference_turn_acknowledges_not_apologises(offline_env) -> None:
    from finagent.graph import open_app

    async with open_app() as app:
        result = await asyncio.wait_for(
            app.arun(PREFERENCE, user_id="pref-e2e", thread_id="pref-e2e"), timeout=120
        )

    assert "已更新你的投资偏好" in result.answer
    assert "抱歉" not in result.answer
    assert "银行" in result.answer


async def test_preference_turn_has_no_partial_notice(offline_env) -> None:
    """纯对话不该被判成「部分完成：缺少可核验的数据」。"""

    from finagent.graph import open_app

    async with open_app() as app:
        result = await asyncio.wait_for(
            app.arun(PREFERENCE, user_id="pref2", thread_id="pref2"), timeout=120
        )

    assert "部分完成" not in result.answer
    assert "可核验的数据" not in result.answer


async def test_preference_turn_has_no_source_footer(offline_env) -> None:
    """对话类回复不该被补「数据来源：东方财富行情」。"""

    from finagent.graph import open_app

    async with open_app() as app:
        result = await asyncio.wait_for(
            app.arun(PREFERENCE, user_id="pref3", thread_id="pref3"), timeout=120
        )

    assert "数据来源" not in result.answer


async def test_preference_persists_across_turns(offline_env) -> None:
    """偏好要跨轮保留——这是「记忆」的意义。"""

    from finagent.graph import open_app

    async with open_app() as app:
        await asyncio.wait_for(
            app.arun(PREFERENCE, user_id="pref4", thread_id="pref4"), timeout=120
        )
        second = await asyncio.wait_for(
            app.arun("茅台现在多少钱", user_id="pref4", thread_id="pref4"), timeout=120
        )

    assert second.answer.strip()
    assert "抱歉" not in second.answer


# ----------------------------------------------------------------------
# 判定层：对话类不产生数据需求
# ----------------------------------------------------------------------
def test_conversational_plan_is_not_required() -> None:
    plan = plan_for(PREFERENCE)
    assert all(task.need.value == "optional" for task in plan.subtasks)


def test_conversational_plan_never_partial() -> None:
    plan = plan_for(PREFERENCE)
    assessment = assess(plan, [])
    assert assessment.overall is Overall.COMPLETE
    assert notice_for(assessment) == ""


def test_data_question_still_required() -> None:
    """别把「不误伤」做成「全放过」。"""

    plan = plan_for("茅台现在多少钱")
    assert any(task.need.value == "required" for task in plan.subtasks)


# ----------------------------------------------------------------------
# 风控层：道歉不补来源
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "抱歉，我没有理解这个请求。可以试试「我 10.5 元买了 1000 股平安银行」",
        "已更新你的投资偏好档案，风险偏好 Conservative",
    ],
)
def test_conversational_text_gets_no_footer(text: str) -> None:
    result, flags, _ = check_source_annotation(text, provider="eastmoney")
    assert "数据来源" not in result
    assert not any(flag.get("code") == "missing_source" for flag in flags)


def test_data_text_still_gets_footer() -> None:
    result, _flags, _ = check_source_annotation("茅台现价 1439.1 元", provider="eastmoney")
    assert "数据来源" in result
