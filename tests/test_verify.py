"""回测结论的代码级复核（子进程复算）。

两条主线：
* 复算本身能跑通，且和引擎结果一致；
* 结论里把数字写错（哪怕是 LLM 编的）必须被抓出来。
"""

from __future__ import annotations

import pytest

from finagent.graph import verify

SPEC = {
    "ts_code": "600519.SH",
    "strategy": "ma_cross",
    "start_date": "20250101",
    "end_date": "20251231",
}


# ----------------------------------------------------------------------
def test_spec_is_recovered_from_trace() -> None:
    trace = [
        {"tool": "get_current_time", "ok": True, "args": {}},
        {"tool": "run_backtest", "ok": True, "args": SPEC},
    ]
    assert verify.spec_from_trace(trace) == {
        "ts_code": "600519.SH",
        "strategy": "ma_cross",
        "start_date": "20250101",
        "end_date": "20251231",
        "params": {},
        "benchmark": None,
    }


def test_spec_ignores_failed_or_missing_backtest() -> None:
    assert verify.spec_from_trace([]) is None
    assert verify.spec_from_trace([{"tool": "run_backtest", "ok": False, "args": SPEC}]) is None
    assert verify.spec_from_trace([{"tool": "screen_stocks", "ok": True, "args": {}}]) is None


# ----------------------------------------------------------------------
def test_subprocess_runs_the_engine(offline_env, data_dir) -> None:
    """子进程真的把回测跑起来了，并且返回可用的指标。"""

    payload = verify.run_in_subprocess(SPEC, timeout=120)
    assert payload["ok"] is True, payload.get("error")
    metrics = payload["metrics"]
    assert metrics["trades_count"] >= 0
    assert "sharpe" in metrics
    assert payload["data"]["bars"] > 0


def test_unknown_strategy_is_rejected(offline_env, data_dir) -> None:
    """不接受任意代码：策略名必须在注册表里。"""

    payload = verify.run_in_subprocess({**SPEC, "strategy": "rm -rf /"}, timeout=60)
    assert payload["ok"] is False
    assert "未知策略" in payload["error"]


# ----------------------------------------------------------------------
def test_claimed_metrics_are_parsed() -> None:
    answer = """### 回测结果
| 指标 | 数值 |
| --- | --- |
| 累计收益 | +12.34% |
| 最大回撤 | 8.21% |
| 夏普比率 | 1.42 |
| 胜率 | 55.5% |
| 交易次数 | 17 |
"""
    claimed = verify.claimed_metrics(answer)
    assert claimed["total_return_pct"] == pytest.approx(12.34)
    assert claimed["max_drawdown_pct"] == pytest.approx(8.21)
    assert claimed["sharpe"] == pytest.approx(1.42)
    assert claimed["trades_count"] == pytest.approx(17)


def test_compare_flags_mismatch_beyond_tolerance() -> None:
    actual = {"sharpe": 0.85, "total_return_pct": 10.0}
    issues = verify.compare({"sharpe": 9.99, "total_return_pct": 10.4}, actual)
    assert len(issues) == 1
    assert "夏普" in issues[0]["detail"]
    assert issues[0]["severity"] == "high"


def test_compare_accepts_rounding() -> None:
    actual = {"sharpe": 0.854, "total_return_pct": 10.0}
    assert verify.compare({"sharpe": 0.85, "total_return_pct": 9.8}, actual) == []


# ----------------------------------------------------------------------
def test_fabricated_sharpe_is_caught_end_to_end(offline_env, data_dir) -> None:
    """完整的「复算 + 比对」链路：编造的夏普必须被拦下。"""

    payload = verify.run_in_subprocess(SPEC, timeout=120)
    assert payload["ok"] is True
    metrics = payload["metrics"]

    trace = [{"tool": "run_backtest", "ok": True, "args": SPEC}]
    answer = (
        f"累计收益 {metrics['total_return_pct']}%，"
        f"最大回撤 {metrics['max_drawdown_pct']}%，"
        "夏普比率 9.99，"
        f"交易次数 {metrics['trades_count']}。"
    )
    result = verify.verify_backtest_answer(answer, trace, timeout=120)
    assert result.attempted is True
    assert result.ok is False
    assert any("夏普" in issue["detail"] for issue in result.issues)


def test_faithful_answer_passes(offline_env, data_dir) -> None:
    payload = verify.run_in_subprocess(SPEC, timeout=120)
    metrics = payload["metrics"]
    trace = [{"tool": "run_backtest", "ok": True, "args": SPEC}]
    answer = (
        f"累计收益 {metrics['total_return_pct']}%，"
        f"最大回撤 {metrics['max_drawdown_pct']}%，"
        f"夏普比率 {metrics['sharpe']}，"
        f"交易次数 {metrics['trades_count']}。"
    )
    result = verify.verify_backtest_answer(answer, trace, timeout=120)
    assert result.ok is True
    assert result.issues == []


def test_no_backtest_means_no_verification() -> None:
    result = verify.verify_backtest_answer("茅台现价 1650 元。", [])
    assert result.attempted is False
