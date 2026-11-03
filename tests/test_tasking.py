"""P1-5 验收测试：任务分解与部分成功。"""

from __future__ import annotations

import pytest

from finagent.graph.tasking import (
    Need,
    Overall,
    Status,
    assess,
    notice_for,
    plan_for,
)


def _ok(tool: str) -> dict:
    return {"tool": tool, "ok": True}


# ----------------------------------------------------------------------
# 计划
# ----------------------------------------------------------------------
def test_taibao_question_decomposes_into_three_required() -> None:
    """「现在多少钱 + 最近一周走势 + 估值」= 三个必做项。"""

    plan = plan_for("中国太保现在多少钱？最近一周的走势和估值怎么样？")
    names = [task.name for task in plan.subtasks]

    assert "current_price" in names
    assert "price_trend" in names
    assert "valuation" in names
    assert all(task.need is Need.REQUIRED for task in plan.subtasks)


def test_technical_and_moneyflow_are_optional() -> None:
    plan = plan_for("中国太保技术指标和资金流怎么样")
    optional = {task.name for task in plan.optional}
    assert "technical" in optional
    assert "moneyflow" in optional


def test_fundamental_is_required_when_asked() -> None:
    plan = plan_for("茅台近三年净利润多少")
    assert any(task.name == "fundamental" and task.need is Need.REQUIRED for task in plan.subtasks)


def test_unmatched_question_falls_back_to_any_evidence() -> None:
    plan = plan_for("帮我看看这家公司")
    assert [task.name for task in plan.subtasks] == ["any_evidence"]


def test_trend_needs_a_series_not_a_quote() -> None:
    """单次报价满足不了「走势」——它要的是价格序列。"""

    plan = plan_for("最近一周走势怎么样")
    trend = next(task for task in plan.subtasks if task.name == "price_trend")
    assert "get_daily_price" in trend.tools
    assert "get_realtime_price" not in trend.tools


# ----------------------------------------------------------------------
# 判定
# ----------------------------------------------------------------------
def test_complete_when_all_required_satisfied() -> None:
    plan = plan_for("中国太保现在多少钱？最近一周的走势和估值怎么样？")
    assessment = assess(
        plan,
        [_ok("get_realtime_price"), _ok("get_daily_price"), _ok("get_daily_basic")],
    )
    assert assessment.overall is Overall.COMPLETE
    assert notice_for(assessment) == ""


def test_partial_when_one_required_missing() -> None:
    plan = plan_for("中国太保现在多少钱？最近一周的走势和估值怎么样？")
    assessment = assess(plan, [_ok("get_realtime_price"), _ok("get_daily_basic")])

    assert assessment.overall is Overall.PARTIAL
    assert [task.label for task in assessment.missing_required] == ["近期走势"]
    assert "部分完成" in notice_for(assessment)


def test_failed_when_nothing_satisfied() -> None:
    plan = plan_for("中国太保现在多少钱？")
    assert assess(plan, []).overall is Overall.FAILED


def test_optional_failure_does_not_block_delivery() -> None:
    """可选项失败不该把已成功的必做项拖下水。"""

    plan = plan_for("中国太保现在多少钱，顺便看下技术指标和资金流")
    assessment = assess(plan, [_ok("get_realtime_price")])

    assert assessment.overall is Overall.COMPLETE
    assert {task.name for task in assessment.missing_optional} == {"technical", "moneyflow"}
    notice = notice_for(assessment)
    assert "未覆盖" in notice
    assert "部分完成" not in notice


def test_unsupported_is_distinguished_from_missing() -> None:
    plan = plan_for("资金流怎么样")
    trace = [{"tool": "get_moneyflow", "ok": False, "status": "unsupported"}]
    assessment = assess(plan, trace)
    task = assessment.plan.subtasks[0]
    assert task.status is Status.UNSUPPORTED


def test_assessment_is_serialisable() -> None:
    plan = plan_for("中国太保现在多少钱")
    payload = assess(plan, []).to_dict()
    assert payload["overall"] == "failed"
    assert "missing_required" in payload


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("现在多少钱", "current_price"),
        ("最近一个月走势", "price_trend"),
        ("市盈率高不高", "valuation"),
        ("财报怎么样", "fundamental"),
    ],
)
def test_single_intent_detection(query: str, expected: str) -> None:
    plan = plan_for(query)
    assert expected in [task.name for task in plan.subtasks]
