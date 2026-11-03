"""回归测试：审查者的「与事实冲突」意见必须被对账掉。"""

from __future__ import annotations

from finagent.graph.critic import ReviewResult, reconcile


def _result(*issues: dict[str, str], verdict: str = "revise") -> ReviewResult:
    return ReviewResult(verdict=verdict, issues=list(issues), checks={}, source="llm")


def _issue(detail: str, severity: str = "high", kind: str = "unsupported") -> dict[str, str]:
    return {"type": kind, "severity": severity, "detail": detail, "label": "测试"}


def test_false_no_tool_claim_is_dropped() -> None:
    """明明调了工具却说没调——这是「每次都被逼返工」的头号原因。"""

    out = reconcile(
        _result(_issue("本轮未调用任何工具，却给出了 24 处数字")),
        answer="命中 24 只，PE 9.28",
        tool_calls=4,
        evidence="命中 24 只 PE 9.28",
    )
    assert out.verdict == "pass"
    assert out.issues == []


def test_no_tool_claim_kept_when_really_no_calls() -> None:
    """真的没调工具时要保留这条意见。"""

    out = reconcile(
        _result(_issue("本轮未调用任何工具，却给出了 24 处数字")),
        answer="命中 24 只",
        tool_calls=0,
        evidence="",
    )
    assert out.verdict == "revise"
    assert len(out.issues) == 1


def test_numeric_claim_contradicted_by_rules_is_dropped() -> None:
    """规则能逐个数比对，模型不行——数字类指控以规则为准。"""

    out = reconcile(
        _result(_issue("结论中的 9.28、31549 在工具返回中未出现")),
        answer="PE 9.28，市值 31549 亿元",
        tool_calls=3,
        evidence="PE 9.28 市值 31549",
    )
    assert out.verdict == "pass"


def test_qualitative_claim_is_kept() -> None:
    """定性判断（没有数字）不归规则管，仍听模型的。"""

    out = reconcile(
        _result(_issue("整体估值偏低等判断没有数据支撑，属于无依据推断")),
        answer="整体估值偏低",
        tool_calls=3,
        evidence="PE 9.28",
    )
    assert out.verdict == "revise"
    assert len(out.issues) == 1


def test_numeric_claim_kept_when_rules_also_fail() -> None:
    """规则也认为数字没出处时，模型的意见要保留（不能借对账放水）。"""

    out = reconcile(
        _result(_issue("结论中的 999.99 在工具返回中未出现")),
        answer="PE 999.99",
        tool_calls=3,
        evidence="PE 9.28",
    )
    assert out.verdict == "revise"


def test_untouched_issues_pass_through() -> None:
    """其它类型的意见原样保留。"""

    out = reconcile(
        _result(_issue("前后矛盾：既说增长又说回落", kind="contradiction")),
        answer="增长，同时回落",
        tool_calls=2,
        evidence="增长 回落",
    )
    assert out.verdict == "revise"
    assert len(out.issues) == 1


def test_empty_issues_is_noop() -> None:
    out = reconcile(_result(verdict="pass"), answer="x", tool_calls=1, evidence="x")
    assert out.verdict == "pass"
    assert out.issues == []
