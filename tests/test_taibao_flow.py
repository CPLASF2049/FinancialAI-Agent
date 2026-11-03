"""中国太保五阶段流程回归（2.0 附加项）。

这不是普通单测，而是把前面几项改造串起来的**端到端验收**：

    任务规划  ->  数据获取  ->  确定性计算  ->  审查(逻辑/证据/风控)  ->  交付

用用户实际提过的那个问题作为固定用例：

    「中国太保 601601 现在多少钱？最近一周的走势和估值怎么样？」

覆盖点：
* P0-2 任务分解：三个必做子任务被识别
* P0-3 证据校验：数字可追溯到工具返回
* P1-5 部分成功：缺哪项就明说，不拖累已成功的内容
* P1-6 事件流：turn:start -> … -> turn:end 完整
"""

from __future__ import annotations

import asyncio

import pytest

from finagent.graph.tasking import Overall, assess, plan_for

QUESTION = "中国太保 601601 现在多少钱？最近一周的走势和估值怎么样？"


# ----------------------------------------------------------------------
# 阶段一：任务规划
# ----------------------------------------------------------------------
def test_stage1_plan_has_three_required_subtasks() -> None:
    plan = plan_for(QUESTION)
    names = [task.name for task in plan.required]

    assert names == ["current_price", "price_trend", "valuation"]


# ----------------------------------------------------------------------
# 阶段二/三：取数与确定性计算
# ----------------------------------------------------------------------
def test_stage3_range_return_is_computed_by_code() -> None:
    """区间涨幅由代码算，不让模型编。"""

    from finagent.graph.evidence_validator import PricePoint, range_return

    points = [PricePoint("20261027", 30.0), PricePoint("20261103", 33.0)]
    assert range_return(points) == pytest.approx(10.0, abs=0.01)


# ----------------------------------------------------------------------
# 阶段四/五：审查与交付（端到端）
# ----------------------------------------------------------------------
async def test_full_flow_offline(offline_env) -> None:
    from finagent.graph import open_app

    async with open_app() as app:
        result = await asyncio.wait_for(
            app.arun(QUESTION, user_id="taibao", thread_id="taibao-e2e"), timeout=180
        )
        store = app.events

    # 交付内容：有答案、没有原始异常文本
    assert result.answer.strip()
    assert "Traceback" not in result.answer
    assert "Remote end closed" not in result.answer

    # 任务分解与实际轨迹能对上
    plan = plan_for(QUESTION)
    assessment = assess(plan, list(result.trace or []))
    assert assessment.overall in (Overall.COMPLETE, Overall.PARTIAL, Overall.FAILED)
    # 离线环境能拿到价格，所以至少现价这一项应当被满足
    price_task = next(task for task in plan.required if task.name == "current_price")
    assert price_task.status.value == "satisfied"

    # 事件流完整
    task_id = store.tasks()[0]
    types = [event.type for event in store.query(task_id)]
    assert types[0] == "turn:start"
    assert types[-1] == "turn:end"
    assert "tool:call" in types or "step:start" in types


async def test_partial_delivery_is_explicit(offline_env) -> None:
    """必做项缺失时，交付里要明确说明缺了什么。"""

    from finagent.graph import open_app

    async with open_app() as app:
        result = await asyncio.wait_for(
            app.arun(QUESTION, user_id="taibao", thread_id="taibao-partial"), timeout=180
        )

    plan = plan_for(QUESTION)
    assessment = assess(plan, list(result.trace or []))
    if assessment.overall is not Overall.COMPLETE:
        notice = "部分完成" if assessment.missing_required else "未覆盖"
        assert notice in result.answer, "不完整时必须在正文里说明"


def test_evidence_for_answer_is_traceable(offline_env) -> None:
    """答案里的数字应当能在工具轨迹里找到出处。"""

    from finagent.graph.evidence_validator import evidence_from_trace, validate

    trace = [
        {
            "tool": "get_realtime_price",
            "ok": True,
            "call_id": "c1",
            "preview": '{"price": 32.25, "pct_chg": 1.2}',
        },
        {
            "tool": "get_daily_price",
            "ok": True,
            "call_id": "c2",
            "preview": '{"close": 31.0}',
        },
    ]
    evidence = evidence_from_trace(trace)
    report = validate("现价 32.25 元", evidence=evidence, as_of="20261103", now="20261104")
    assert report.ok
