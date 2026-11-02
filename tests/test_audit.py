"""前视偏差审计（截断检验）的测试。

关键不只是「全部策略都通过」，而是**证明这个检验抓得住泄漏**——否则「全过」只能说明
测试本身是瞎的。所以这里有两个临时注册的策略作为正负对照。
"""

from __future__ import annotations

import pytest

from finagent.core.backtest import audit, registry
from finagent.core.backtest.base import BacktestStrategy
from finagent.providers.registry import get_provider


class _LeakyStrategy(BacktestStrategy):
    """用**全样本分位数**当买卖阈值——典型的数据窥探型未来函数。

    价格排名是用整段数据算出来的，所以只要样本变长，历史每根的排名都会变，
    早期信号跟着变。这正是「回测时用了全样本统计量」这类最常见的前视偏差。
    """

    name = "leaky_demo"
    description = "测试用：故意引入前视偏差"

    def enrich(self, frame, params):  # noqa: ANN001, ARG002
        frame = frame.copy()
        frame["rank_pct"] = frame["close"].rank(pct=True)
        return frame

    def signal(self, row, prev_row, params, context=None):  # noqa: ANN001, ARG002
        if self._na(row.get("rank_pct")):
            return 0
        return 1 if row["rank_pct"] < 0.35 else -1


class _CleanStrategy(BacktestStrategy):
    """只看已经发生的收盘价——没有未来函数。"""

    name = "clean_demo"
    description = "测试用：无前视偏差"

    def enrich(self, frame, params):  # noqa: ANN001, ARG002
        return frame

    def signal(self, row, prev_row, params, context=None):  # noqa: ANN001, ARG002
        if prev_row is None:
            return 0
        return 1 if row["close"] > prev_row["close"] else 0


@pytest.fixture
def temp_strategies():
    """临时把两个对照策略注册进真实注册表，用完恢复。"""

    snapshot = dict(registry._REGISTRY)  # noqa: SLF001 - 测试需要快照/恢复
    registry.register_strategy(_LeakyStrategy)
    registry.register_strategy(_CleanStrategy)
    try:
        yield
    finally:
        registry._REGISTRY.clear()  # noqa: SLF001
        registry._REGISTRY.update(snapshot)  # noqa: SLF001


# ----------------------------------------------------------------------
def test_leaky_strategy_is_caught(offline_env, temp_strategies, data_dir) -> None:
    """负向对照：偷看未来的策略必须被判为泄漏。"""

    report = audit.check_strategy(
        get_provider(),
        "600519.SH",
        "leaky_demo",
        start_date="20250101",
        end_date="20251231",
    )
    assert report.passed is False
    assert report.leaks, "泄漏策略没有被标记"
    assert "成交不一致" in report.leaks[0] or "权益值" in report.leaks[0]


def test_clean_strategy_passes(offline_env, temp_strategies, data_dir) -> None:
    """正向对照：同样结构的干净策略必须通过，证明上面的失败不是因为检验太严。"""

    report = audit.check_strategy(
        get_provider(),
        "600519.SH",
        "clean_demo",
        start_date="20250101",
        end_date="20251231",
    )
    assert report.passed is True
    assert report.leaks == []
    assert report.cut_date


# ----------------------------------------------------------------------
def test_all_builtin_strategies_have_no_lookahead(offline_env, data_dir) -> None:
    """内置策略必须全部通过截断检验（这是 CI 级的回归保护）。"""

    reports = audit.audit_strategies(
        get_provider(), "600519.SH", start_date="20250101", end_date="20251231"
    )
    checked = [r for r in reports if not r.skipped_reason]
    assert checked, "没有可审计的策略"
    failed = [(r.strategy, r.leaks) for r in checked if not r.passed]
    assert not failed, f"存在前视偏差：{failed}"

    summary = audit.summarise(reports)
    assert summary["ok"] is True
    assert summary["passed"] == len(checked)


def test_multi_asset_strategy_is_skipped(offline_env, data_dir) -> None:
    reports = audit.audit_strategies(
        get_provider(), "600519.SH", start_date="20250101", end_date="20251231"
    )
    skipped = [r for r in reports if r.skipped_reason]
    assert all(r.passed for r in skipped)
    assert all(r.skipped_reason for r in skipped)


def test_report_is_serialisable(offline_env, data_dir) -> None:
    report = audit.check_strategy(
        get_provider(), "600519.SH", "ma_cross", start_date="20250101", end_date="20251231"
    )
    payload = report.to_dict()
    assert set(payload) >= {"ts_code", "strategy", "cut_date", "passed", "leaks"}
    assert payload["ts_code"] == "600519.SH"


@pytest.mark.parametrize("ratio", [0.3, 0.5, 0.7])
def test_cut_ratio_can_be_moved(offline_env, data_dir, ratio: float) -> None:
    report = audit.check_strategy(
        get_provider(),
        "600519.SH",
        "ma_cross",
        start_date="20250101",
        end_date="20251231",
        cut_ratio=ratio,
    )
    assert report.passed is True
