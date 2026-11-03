"""P0-3 验收测试：独立证据校验器。"""

from __future__ import annotations

import json

import pytest

from finagent.graph.evidence_models import (
    ClaimType,
    EvidenceRecord,
    ValidationReport,
    VerificationStatus,
)
from finagent.graph.evidence_validator import (
    PricePoint,
    change_pct,
    evidence_from_trace,
    range_return,
    validate,
)


def _trace(preview: dict, tool: str = "get_realtime_price", call_id: str = "c1") -> list[dict]:
    return [{"tool": tool, "ok": True, "call_id": call_id, "preview": json.dumps(preview)}]


# ----------------------------------------------------------------------
# 证据模型
# ----------------------------------------------------------------------
def test_record_publishable_flag() -> None:
    verified = EvidenceRecord(
        claim_id="p1",
        claim_type=ClaimType.MARKET_PRICE,
        value=32.25,
        status=VerificationStatus.VERIFIED,
    )
    unverified = EvidenceRecord(
        claim_id="p2",
        claim_type=ClaimType.MARKET_PRICE,
        value=1.0,
        status=VerificationStatus.UNVERIFIED,
    )
    assert verified.publishable is True
    assert unverified.publishable is False


def test_report_summary_counts() -> None:
    report = ValidationReport(
        records=[
            EvidenceRecord(
                claim_id="a",
                claim_type=ClaimType.OTHER,
                value=1,
                status=VerificationStatus.VERIFIED,
            ),
            EvidenceRecord(
                claim_id="b", claim_type=ClaimType.OTHER, value=2, status=VerificationStatus.STALE
            ),
        ]
    )
    summary = report.summary()
    assert summary["total"] == 2
    assert summary["by_status"]["verified"] == 1
    assert summary["ok"] is True


# ----------------------------------------------------------------------
# 确定性计算：不让模型算
# ----------------------------------------------------------------------
def test_range_return_is_deterministic() -> None:
    points = [PricePoint("20261001", 30.0), PricePoint("20261103", 32.25)]
    assert range_return(points) == pytest.approx(7.5, abs=0.01)


def test_range_return_needs_two_points() -> None:
    assert range_return([PricePoint("20261103", 30.0)]) is None


def test_change_pct() -> None:
    assert change_pct(100.0, 110.0) == pytest.approx(10.0)
    assert change_pct(0.0, 10.0) is None


# ----------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------
def test_traceable_number_is_verified() -> None:
    evidence = evidence_from_trace(_trace({"price": 32.25}))
    report = validate("现价 32.25 元", evidence=evidence, as_of="20261103", now="20261104")
    assert report.ok
    assert report.by_status(VerificationStatus.VERIFIED)


def test_fabricated_number_blocks() -> None:
    """一条假价格就足以让整篇结论不可信。"""

    evidence = evidence_from_trace(_trace({"price": 32.25}))
    report = validate("现价 99.99 元", evidence=evidence, as_of="20261103", now="20261104")
    assert not report.ok
    assert any("99.99" in blocker for blocker in report.blockers)


def test_year_is_not_treated_as_data() -> None:
    evidence = evidence_from_trace(_trace({"price": 32.25}))
    report = validate("2026 年现价 32.25 元", evidence=evidence, as_of="20261103", now="20261104")
    assert report.ok


def test_conflicting_change_pct_blocks() -> None:
    prices = [
        {"trade_date": "20261001", "close": 30.0},
        {"trade_date": "20261103", "close": 32.25},
    ]
    trace = [
        {
            "tool": "get_daily_price",
            "ok": True,
            "call_id": "c9",
            "preview": json.dumps(prices),
        }
    ]
    evidence = evidence_from_trace(trace)
    # 真实区间涨幅约 7.5%，结论却说 30%
    report = validate("近一个月上涨 30%", evidence=evidence, as_of="20261103", now="20261104")
    assert not report.ok
    assert any("重算" in blocker for blocker in report.blockers)


def test_stale_data_blocks() -> None:
    evidence = evidence_from_trace(_trace({"price": 32.25}))
    report = validate("现价 32.25 元", evidence=evidence, as_of="20260801", now="20261104")
    assert not report.ok
    assert any("时效" in blocker for blocker in report.blockers)


def test_fresh_data_passes_staleness() -> None:
    evidence = evidence_from_trace(_trace({"price": 32.25}))
    report = validate("现价 32.25 元", evidence=evidence, as_of="20261103", now="20261104")
    assert not any("时效" in blocker for blocker in report.blockers)


def test_evidence_source_is_detected() -> None:
    trace = _trace({"price": 1.0, "source": "eastmoney"})
    evidence = evidence_from_trace(trace)
    assert any(record.source == "eastmoney" for record in evidence)


def test_failed_tool_calls_contribute_no_evidence() -> None:
    trace = [{"tool": "x", "ok": False, "call_id": "c1", "preview": "12345"}]
    assert evidence_from_trace(trace) == []


# ----------------------------------------------------------------------
# 与 Critic 的分工
# ----------------------------------------------------------------------
def test_validator_is_independent_of_llm() -> None:
    """证据校验必须是纯函数式的，不依赖模型返回。"""

    evidence = evidence_from_trace(_trace({"price": 32.25}))
    first = validate("现价 32.25 元", evidence=evidence, as_of="20261103", now="20261104")
    second = validate("现价 32.25 元", evidence=evidence, as_of="20261103", now="20261104")
    assert first.summary() == second.summary()
