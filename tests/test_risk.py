"""风控护栏（risk_guard）测试。

这一层的价值在于**确定性**：同一份输入永远得到同一份拦截结果，不依赖模型。
"""

from __future__ import annotations

from finagent.graph.risk import (
    check_risk_preference,
    check_source_annotation,
    check_strong_directives,
    guard,
    held_codes_from,
    risk_level,
)


# ----------------------------------------------------------------------
def test_risk_level_normalisation() -> None:
    assert risk_level({"risk_tolerance": "Conservative"}) == "conservative"
    assert risk_level({"risk_tolerance": "保守型"}) == "conservative"
    assert risk_level({"risk_tolerance": "Aggressive"}) == "aggressive"
    assert risk_level({}) == "unknown"


def test_conservative_profile_blocks_leverage() -> None:
    text, flags, actions = check_risk_preference(
        "建议加杠杆买入，最好满仓。", {"risk_tolerance": "Conservative"}
    )
    assert "加杠杆" not in text
    assert "满仓" not in text
    assert "控制仓位" in text
    assert flags and flags[0]["code"] == "risk_preference"
    assert flags[0]["severity"] == "high"
    assert actions


def test_aggressive_profile_is_not_restricted() -> None:
    original = "建议加杠杆买入。"
    text, flags, _ = check_risk_preference(original, {"risk_tolerance": "Aggressive"})
    assert text == original
    assert flags == []


def test_absolute_claims_are_downgraded() -> None:
    text, flags, _ = check_strong_directives("现在立即买入，必涨，保证收益。")
    assert "立即买入" not in text
    assert "必涨" not in text
    # 断言式说法被换成带否定的版本（「保证收益」→「不保证收益」）
    assert "不保证收益" in text
    assert "可关注" in text
    assert flags and flags[0]["code"] == "strong_directive"
    assert flags[0]["severity"] == "medium"


def test_buy_directive_on_unheld_symbol_is_flagged() -> None:
    text, flags, _ = check_strong_directives(
        "建议买入 601398.SH，立即买入。", held_codes={"600519.SH"}
    )
    codes = {flag["code"] for flag in flags}
    assert "unheld_buy" in codes
    unheld = next(flag for flag in flags if flag["code"] == "unheld_buy")
    assert "601398.SH" in unheld["detail"]
    assert text  # 不改写正文，只提示


def test_buy_directive_on_held_symbol_is_fine() -> None:
    _, flags, _ = check_strong_directives("建议买入 600519.SH。", held_codes={"600519.SH"})
    assert flags == []


def test_holdings_unknown_skips_the_check() -> None:
    """没有持仓产物时不该瞎猜，直接跳过这一项。"""

    _, flags, _ = check_strong_directives("建议买入 601398.SH。", held_codes=None)
    assert flags == []


def test_missing_source_is_appended() -> None:
    text, flags, _ = check_source_annotation("茅台现价 1650.96 元。", provider="offline")
    assert "数据来源" in text
    assert "不构成投资建议" in text
    assert flags and flags[0]["code"] == "missing_source"


def test_existing_source_is_respected() -> None:
    original = "茅台现价 1650.96 元。_数据截至 20261028_"
    text, flags, _ = check_source_annotation(original, provider="offline")
    assert text == original
    assert flags == []


def test_tushare_source_wording() -> None:
    text, _, _ = check_source_annotation("收盘 10.5 元。", provider="tushare")
    assert "Tushare" in text


def test_text_without_numbers_needs_no_source() -> None:
    assert check_source_annotation("请补充股票代码。", provider="offline")[1] == []


# ----------------------------------------------------------------------
def test_guard_combines_checks_and_reports_blocked() -> None:
    outcome = guard(
        "建议满仓买入，必涨。",
        profile={"risk_tolerance": "Conservative"},
        held_codes=set(),
        provider="offline",
    )
    assert outcome.blocked is True
    codes = {flag["code"] for flag in outcome.flags}
    assert {"risk_preference", "strong_directive"} <= codes
    assert "满仓" not in outcome.text
    assert outcome.actions


def test_guard_is_idempotent() -> None:
    """同一份文本跑两遍，第二遍不该再改出新花样。"""

    first = guard("建议满仓买入，必涨。", profile={"risk_tolerance": "Conservative"})
    second = guard(first.text, profile={"risk_tolerance": "Conservative"})
    assert second.text == first.text


def test_empty_text_is_a_noop() -> None:
    outcome = guard("", profile={"risk_tolerance": "Conservative"})
    assert outcome.flags == []
    assert outcome.text == ""


def test_held_codes_from_artifacts() -> None:
    artifacts = {"portfolio": {"positions": [{"ts_code": "600519.SH"}, {"ts_code": "000001.SZ"}]}}
    assert held_codes_from(artifacts) == {"600519.SH", "000001.SZ"}

    assert held_codes_from({}) is None
    assert held_codes_from({"portfolio": {"positions": []}}) == set()
