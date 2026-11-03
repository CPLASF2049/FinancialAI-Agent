"""P0-2 数据质量契约的验收测试。

核心命题：**拒绝而非解释**。数据在决策时刻不可得，就返回明确的
DATA_QUALITY_VIOLATION，而不是让模型在结论里写「可能有延迟」。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from finagent.core.quality import (
    DataQualityViolation,
    RuleCode,
    check_known_at,
    check_missing,
    check_report_period,
    check_symbol,
    check_units,
    enforce,
    validate_series,
)


def _frame(**overrides) -> pd.DataFrame:
    base = {
        "ts_code": ["600519.SH", "600519.SH"],
        "trade_date": ["20261101", "20261103"],
        "close": [1400.0, 1439.1],
    }
    base.update(overrides)
    return pd.DataFrame(base)


# ----------------------------------------------------------------------
# 因果：known_at 必须在决策时刻之前
# ----------------------------------------------------------------------
def test_future_row_is_rejected() -> None:
    report = check_known_at(_frame(), decision_time="20261102")
    assert not report.ok
    assert any(item.rule is RuleCode.KNOWN_AT_IN_FUTURE for item in report.blocking())


def test_past_rows_pass() -> None:
    assert check_known_at(_frame(), decision_time="20261105").ok


def test_missing_known_at_is_rejected_not_defaulted() -> None:
    """没有 known_at 又没有兜底列 → 拒绝。默认放行等于放弃校验。"""

    frame = pd.DataFrame({"ts_code": ["600519.SH"], "close": [1439.1]})
    report = check_known_at(frame, decision_time="20261103", fallback=None)
    assert not report.ok
    assert any(item.rule is RuleCode.KNOWN_AT_MISSING for item in report.violations)


def test_trade_date_works_as_known_at_fallback() -> None:
    """日线收盘后即可观测，用 trade_date 兜底是合理的。"""

    assert check_known_at(_frame(), decision_time="20261103").ok


def test_unparsable_decision_time_raises() -> None:
    with pytest.raises(DataQualityViolation):
        check_known_at(_frame(), decision_time="不是日期")


# ----------------------------------------------------------------------
# 证券代码一致性
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("code", "ok"),
    [
        ("600519.SH", True),
        ("601398.SH", True),
        ("000001.SZ", True),
        ("300750.SZ", True),
        ("830799.BJ", True),
        ("601398.SZ", False),  # 601 开头属于沪市
        ("000001.SH", False),  # 000 开头属于深市
        ("600519", False),  # 缺后缀
        ("600519.XX", False),  # 未知后缀
    ],
)
def test_symbol_suffix_consistency(code: str, ok: bool) -> None:
    issue = check_symbol(code)
    assert (issue is None) is ok


# ----------------------------------------------------------------------
# 单位与口径、财报期间
# ----------------------------------------------------------------------
def test_units_must_be_declared() -> None:
    frame = _frame()
    report = check_units(frame, required={"close": "元"})
    assert not report.ok or any(item.rule is RuleCode.UNIT_UNDECLARED for item in report.violations)
    # 非阻断：只是提醒口径不明，不拒绝数据
    assert all(not item.blocking for item in report.violations)


def test_units_declared_via_attrs() -> None:
    frame = _frame()
    frame.attrs["units"] = {"close": "元"}
    report = check_units(frame, required={"close": "元"})
    assert report.violations == []


def test_report_period_without_announce_date_is_flagged() -> None:
    frame = pd.DataFrame({"end_date": ["20260930"], "revenue": [1310]})
    report = check_report_period(frame)
    assert any(item.rule is RuleCode.REPORT_PERIOD_UNKNOWN for item in report.violations)
    # 非阻断：可以用，但必须被标注
    assert report.ok


# ----------------------------------------------------------------------
# 缺失值
# ----------------------------------------------------------------------
def test_missing_values_are_flagged() -> None:
    frame = _frame(close=[1400.0, None])
    report = check_missing(frame)
    assert any(item.rule is RuleCode.MISSING_VALUES for item in report.violations)
    assert "close(1)" in report.violations[0].detail


# ----------------------------------------------------------------------
# 统一入口与「拒绝而非解释」
# ----------------------------------------------------------------------
def test_validate_series_blocks_future_data() -> None:
    report = validate_series(_frame(), decision_time="20261102")
    assert not report.ok
    assert report.to_dict()["ok"] is False


def test_enforce_raises_with_rule_code() -> None:
    report = validate_series(_frame(), decision_time="20261102")
    with pytest.raises(DataQualityViolation) as excinfo:
        enforce(report, context="日线校验")
    payload = excinfo.value.to_payload()
    assert payload["code"] == "DATA_QUALITY_VIOLATION"
    assert payload["rule"] == str(RuleCode.KNOWN_AT_IN_FUTURE)
    assert "日线校验" in payload["message"]


def test_enforce_is_noop_when_clean() -> None:
    report = validate_series(_frame(), decision_time="20261105")
    enforce(report)  # 不抛异常即通过


# ----------------------------------------------------------------------
# 端点：工具层必须返回 DATA_QUALITY_VIOLATION 而不是「解释」
# ----------------------------------------------------------------------
def test_market_tool_rejects_future_data(monkeypatch) -> None:
    """构造一条「晚于决策时刻」的记录，工具必须报错而不是放行。"""

    from finagent.mcp_servers import market

    class _FakeProvider:
        def daily_price(self, code, start, end, adj=None):
            return pd.DataFrame(
                {
                    "ts_code": [code],
                    "trade_date": ["20991231"],  # 未来
                    "open": [1.0],
                    "high": [1.0],
                    "low": [1.0],
                    "close": [1.0],
                    "pre_close": [1.0],
                    "vol": [1.0],
                    "amount": [1.0],
                }
            )

    monkeypatch.setattr(market, "provider", lambda: _FakeProvider())

    server = market.build_server()
    tool = server._tool_manager.get_tool("get_daily_price")
    payload = json.loads(
        tool.fn(
            ts_code="600519.SH",
            start_date="20260101",
            end_date="20260105",
            adj=None,
            limit=10,
        )
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "DATA_QUALITY_VIOLATION"
