"""回归测试：LLM 子职责的 trace 必须能被审查者按裸 agent 名找到。

这是「审查者总说没调工具」的根因：trace 被标成 `researcher:screening`，
审查者按 `researcher` 精确匹配，永远落空。
"""

from __future__ import annotations

import pytest

from finagent.graph.agent_loop import ToolCallRecord
from finagent.graph.critic import check_evidence, summarise_tools


def _record(agent: str = "researcher", stage: str = "fundamental") -> ToolCallRecord:
    return ToolCallRecord(
        agent=agent,
        tool="mx_financial_data",
        args={"query": "茅台净利润"},
        ok=True,
        preview='{"rows": [{"净利润": "853.1亿元"}]}',
        stage=stage,
    )


def test_record_keeps_bare_agent_and_stage() -> None:
    payload = _record().to_dict()
    assert payload["agent"] == "researcher"
    assert payload["stage"] == "fundamental"


def test_subset_by_agent_finds_records_with_stage() -> None:
    """按裸 agent 名过滤要能捞到子职责的记录。"""

    trace = [_record("researcher", "screening"), _record("researcher", "fundamental")]
    matched = [item for item in trace if item.to_dict()["agent"] == "researcher"]
    assert len(matched) == 2


def test_critic_evidence_uses_real_tool_output() -> None:
    """有工具返回时不该判定「未调用任何工具」。"""

    trace = [_record().to_dict()]
    issues = check_evidence(
        "贵州茅台 2025 年净利润 853.1 亿元",
        tool_calls=len(trace),
        evidence=" ".join(item["preview"] for item in trace),
    )
    assert not any("没有成功调用任何工具" in issue["detail"] for issue in issues)


def test_summarise_tools_mentions_each_call() -> None:
    summary = summarise_tools([_record().to_dict()])
    assert "mx_financial_data" in summary


def test_critic_flags_when_truly_no_calls() -> None:
    """真的没调工具时仍然要拦——别把这条保护修掉了。"""

    issues = check_evidence("净利润 853.1 亿元", tool_calls=0, evidence="")
    assert any("没有成功调用任何工具" in issue["detail"] for issue in issues)


@pytest.mark.parametrize("stage", ["screening", "fundamental", "technical"])
def test_stage_is_observability_only(stage: str) -> None:
    payload = _record(stage=stage).to_dict()
    assert payload["agent"] == "researcher"
    assert payload["stage"] == stage
