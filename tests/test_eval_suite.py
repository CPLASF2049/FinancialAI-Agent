"""评测集（P1-3）的测试：任务集完整性 + 指标口径 + 单条运行。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import eval_suite  # noqa: E402


# ----------------------------------------------------------------------
# 任务集本身
# ----------------------------------------------------------------------
def test_task_set_is_large_and_categorised() -> None:
    payload = json.loads((REPO / "eval" / "tasks.json").read_text(encoding="utf-8"))
    tasks = payload["tasks"]

    assert len(tasks) >= 30, "黄金任务集至少 30 条才有一点统计意义"
    categories = {task["category"] for task in tasks}
    for required in ("quote", "history", "valuation", "screening", "backtest", "risk", "honesty"):
        assert required in categories, f"缺少类别 {required}"


def test_every_task_is_well_formed() -> None:
    for task in eval_suite.load_tasks():
        assert task["id"]
        assert task["query"].strip()
        assert isinstance(task["must_call"], list)
        assert isinstance(task["forbid_call"], list)
        assert task["risk_expected"] in ("any", "none")


def test_no_duplicate_task_ids() -> None:
    ids = [task["id"] for task in eval_suite.load_tasks()]
    assert len(ids) == len(set(ids))


# ----------------------------------------------------------------------
# 指标口径
# ----------------------------------------------------------------------
def _record(**overrides) -> dict:
    base = {
        "id": "T1",
        "category": "quote",
        "query": "q",
        "tool_accuracy": True,
        "missing_tools": [],
        "forbidden_called": [],
        "called_tools": ["get_realtime_price"],
        "tool_calls": 1,
        "revisions": 0,
        "risk_hits": 0,
        "risk_expected": "none",
        "faithfulness": {"supported": 4, "total": 4, "ratio": 1.0},
        "tokens": {"input": 10, "output": 5},
        "wall_ms": 100,
        "stop_reason": "",
        "answer_chars": 100,
        "answered": True,
    }
    base.update(overrides)
    return base


def test_summary_metrics() -> None:
    summary = eval_suite.summarise([_record(), _record(faithfulness={"supported": 1, "total": 4, "ratio": 0.25})])
    assert summary["tasks"] == 2
    assert summary["tool_accuracy"] == 1.0
    assert summary["faithfulness_mean"] == pytest.approx(0.625)
    assert summary["hallucination_rate"] == pytest.approx(0.375)
    assert summary["tokens_total"] == 30
    assert summary["answered_rate"] == 1.0


def test_risk_rates_split_by_expectation() -> None:
    records = [
        _record(risk_expected="any", risk_hits=1),
        _record(risk_expected="any", risk_hits=0),
        _record(risk_expected="none", risk_hits=0),
    ]
    summary = eval_suite.summarise(records)
    assert summary["risk_hit_rate"] == 0.5
    assert summary["risk_false_positive_rate"] == 0.0


def test_faithfulness_counts_only_numbers_with_source() -> None:
    faith = eval_suite._faithfulness("PE 21.5，市值 1.8 万亿", "PE 21.5 市值 1.8")
    assert faith["total"] >= 2
    assert 0 < faith["ratio"] <= 1.0


def test_faithfulness_without_numbers_is_perfect() -> None:
    assert eval_suite._faithfulness("没有数字", "")["ratio"] == 1.0


def test_empty_summary_is_safe() -> None:
    assert eval_suite.summarise([]) == {}


# ----------------------------------------------------------------------
# 单条运行（离线、确定性、不联网）
# ----------------------------------------------------------------------
async def test_run_single_task_offline(offline_env) -> None:
    from finagent.graph import open_app

    task = {
        "id": "T999",
        "category": "quote",
        "query": "茅台现在多少钱",
        "must_call": ["get_realtime_price"],
        "forbid_call": [],
        "risk_expected": "none",
    }
    async with open_app() as app:
        record = await eval_suite.run_task(app, task)

    assert record["id"] == "T999"
    assert record["answered"] is True
    assert "get_realtime_price" in record["called_tools"]
    assert record["tool_accuracy"] is True
    assert record["wall_ms"] >= 0
