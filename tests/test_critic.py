"""审查者（Critic）的规则层测试。

三项检查各有正反例：既要能抓住问题，也不能把正常结论误判成问题。
"""

from __future__ import annotations

import pytest

from finagent.graph.critic import (
    REVISABLE_AGENTS,
    check_contradictions,
    check_evidence,
    check_sycophancy,
    parse_critic_output,
    review,
    summarise_tools,
)


# ----------------------------------------------------------------------
# 1. 论据支撑
# ----------------------------------------------------------------------
def test_no_tool_call_with_numbers_is_unsupported() -> None:
    issues = check_evidence("茅台现价 1500 元，PE 22 倍，建议买入。", tool_calls=0, evidence="")
    assert issues and issues[0]["type"] == "unsupported"
    assert issues[0]["severity"] == "high"


def test_numbers_backed_by_tool_output_pass() -> None:
    """四舍五入不能算「没有出处」。"""

    issues = check_evidence(
        "茅台现价 1,650.96 元，涨跌 -0.48%。",
        tool_calls=2,
        evidence='{"price": 1650.956, "pct_chg": -0.475}',
    )
    assert issues == []


def test_fabricated_number_is_caught() -> None:
    issues = check_evidence("茅台现价 1500 元。", tool_calls=2, evidence='{"price": 1650.956}')
    assert issues and issues[0]["type"] == "unsupported"


def test_answer_without_numbers_is_not_flagged() -> None:
    assert check_evidence("请提供股票代码。", tool_calls=0, evidence="") == []


def test_low_support_ratio_is_medium() -> None:
    issues = check_evidence(
        "价格 10.11，涨跌 20.22，成交 30.33，换手 40.44，市值 50.55，估值 60.66。",
        tool_calls=1,
        evidence='{"price": 10.11}',
    )
    assert issues and issues[0]["severity"] == "medium"


# ----------------------------------------------------------------------
# 2. 内部矛盾
# ----------------------------------------------------------------------
def test_undervalued_claim_contradicts_high_pe() -> None:
    issues = check_contradictions(
        "目前估值偏低，PE 35 倍。", agent="fundamental_analyst", other_answers=[]
    )
    assert issues and issues[0]["type"] == "contradiction"


def test_bullish_and_bearish_in_one_answer() -> None:
    issues = check_contradictions(
        "技术面看多，但基本面看空。", agent="technical_analyst", other_answers=[]
    )
    assert issues and issues[0]["severity"] == "medium"


def test_cross_expert_divergence_is_a_signal_not_an_error() -> None:
    """技术面看多 + 基本面偏空 = 分歧信号，有价值但不阻断交付。"""

    others = [{"agent": "fundamental_analyst", "answer": "估值偏高，基本面偏空。"}]
    issues = check_contradictions(
        "技术面强势突破，金叉形成，看多。",
        agent="technical_analyst",
        other_answers=others,
    )
    assert issues and "分歧信号" in issues[0]["detail"]
    assert issues[0]["severity"] == "medium"


def test_no_divergence_when_both_agree() -> None:
    others = [{"agent": "fundamental_analyst", "answer": "基本面看多，估值偏低。"}]
    issues = check_contradictions(
        "技术面强势突破，看多。", agent="technical_analyst", other_answers=others
    )
    assert issues == []


# ----------------------------------------------------------------------
# 3. 谄媚倾向
# ----------------------------------------------------------------------
def test_leading_question_with_plain_confirmation() -> None:
    issues = check_sycophancy("茅台是不是要涨了？", "是的，会涨，可以买入。")
    assert issues and issues[0]["type"] == "sycophancy"


def test_hedged_answer_is_not_sycophancy() -> None:
    """与用户预期一致不是问题，缺反面证据才是。"""

    issues = check_sycophancy(
        "茅台是不是要涨了？", "存在上涨可能，但估值偏高、流动性存疑，风险不小。"
    )
    assert issues == []


def test_neutral_question_is_never_sycophancy() -> None:
    assert check_sycophancy("茅台现在多少钱？", "会涨，建议买入。") == []


# ----------------------------------------------------------------------
# 组合与判定
# ----------------------------------------------------------------------
def test_review_returns_revise_only_for_high_severity() -> None:
    revise = review(
        "茅台现价 1500 元。",
        agent="technical_analyst",
        question="茅台多少",
        tool_calls=0,
        evidence="",
    )
    assert revise.verdict == "revise"

    pass_with_signal = review(
        "技术面看多，基本面看空。",
        agent="technical_analyst",
        question="茅台怎么样",
        tool_calls=3,
        evidence="x",
    )
    assert pass_with_signal.verdict == "pass"
    assert pass_with_signal.issues


def test_clean_answer_passes_with_no_issues() -> None:
    result = review(
        "### 茅台\n- 最新价 **1,650.96** 元",
        agent="technical_analyst",
        question="茅台现在多少钱",
        tool_calls=3,
        evidence='{"price": 1650.956}',
    )
    assert result.verdict == "pass"
    assert result.issues == []


@pytest.mark.parametrize(
    "payload",
    [
        '{"verdict": "revise", "issues": [{"type": "sycophancy", "detail": "顺着提问说"}]}',
        '废话一段 ```json\n{"verdict": "revise", "issues": [{"type": "unsupported", "detail": "无出处"}]}\n```',
    ],
)
def test_parse_critic_output(payload: str) -> None:
    parsed = parse_critic_output(payload)
    assert parsed is not None
    assert parsed.verdict == "revise"
    assert parsed.issues


def test_parse_rejects_revise_without_reason() -> None:
    """模型说返工却给不出理由 → 不采信，避免无意义返工。"""

    assert parse_critic_output('{"verdict": "revise", "issues": []}') is None


def test_parse_escalates_pass_with_high_severity_issue() -> None:
    parsed = parse_critic_output(
        '{"verdict": "pass", "issues": [{"type": "unsupported", "detail": "数字没出处"}]}'
    )
    assert parsed is not None and parsed.verdict == "revise"


def test_parse_rejects_garbage() -> None:
    assert parse_critic_output("我觉得还行") is None
    assert parse_critic_output('{"verdict": "maybe"}') is None


def test_side_effect_agents_are_not_revisable() -> None:
    """持仓和预警重跑会重复下单/重复建预警，必须排除在返工之外。"""

    assert "portfolio" not in REVISABLE_AGENTS
    assert "alert" not in REVISABLE_AGENTS
    assert "technical_analyst" in REVISABLE_AGENTS


def test_summarise_tools_marks_failures() -> None:
    text = summarise_tools(
        [
            {
                "tool": "get_realtime_price",
                "args": {"ts_code": "600519.SH"},
                "ok": True,
                "preview": "{}",
            },
            {"tool": "get_daily_basic", "args": {}, "ok": False, "error": "boom"},
        ]
    )
    assert "[OK]" in text
    assert "[FAIL]" in text
    assert "boom" in text
