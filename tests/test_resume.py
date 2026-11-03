"""P1-2 验收测试：断点阶段判定 + 上下文压缩。"""

from __future__ import annotations

import pytest

from finagent.core.events import EventStore
from finagent.core.phase import detect_phase, resume_hint
from finagent.graph.compaction import compact, context_tokens, estimate_tokens


# ----------------------------------------------------------------------
# 阶段判定：用最后一条事件说话，不猜
# ----------------------------------------------------------------------
def _ev(type_: str, **payload) -> dict:
    return {"type": type_, "payload": payload}


def test_no_events_means_inbox() -> None:
    assert detect_phase([]).name == "inbox"


def test_turn_started_but_no_step_is_provider() -> None:
    phase = detect_phase([_ev("turn:start"), _ev("route:decision", to_node="researcher")])
    assert phase.name == "provider"


def test_step_started_without_output_is_provider() -> None:
    phase = detect_phase([_ev("turn:start"), _ev("step:start", agent="researcher")])
    assert phase.name == "provider"


def test_tool_call_without_result_is_tool_phase() -> None:
    phase = detect_phase(
        [
            _ev("turn:start"),
            _ev("step:start", agent="researcher"),
            _ev("tool:call", tool="get_realtime_price", call_id="c1"),
        ]
    )
    assert phase.name == "tool"
    assert phase.pending_calls == ["c1"]


def test_completed_tool_is_not_pending() -> None:
    phase = detect_phase(
        [
            _ev("turn:start"),
            _ev("step:start", agent="researcher"),
            _ev("tool:call", tool="x", call_id="c1"),
            _ev("tool:result", call_id="c1", status="ok"),
        ]
    )
    assert phase.pending_calls == []


def test_turn_end_means_done() -> None:
    assert detect_phase([_ev("turn:start"), _ev("turn:end")]).name == "done"


def test_resume_hint_mentions_idempotency() -> None:
    hint = resume_hint(
        [_ev("turn:start"), _ev("tool:call", tool="add_position", call_id="c9")]
    )
    assert hint["phase"] == "tool"
    assert "c9" in hint["pending_calls"]
    assert "call_id" in hint["action"]


def test_store_integration(data_dir) -> None:
    """真实事件表也能算阶段。"""

    store = EventStore(data_dir / "events.db")
    store.append("t1", "turn:start")
    store.append("t1", "step:start", agent="researcher")
    store.append("t1", "tool:call", tool="x", call_id="c1")
    assert detect_phase(store.query("t1")).name == "tool"


# ----------------------------------------------------------------------
# 上下文压缩
# ----------------------------------------------------------------------
class _Msg:
    def __init__(self, content: str, role: str = "tool") -> None:
        self.content = content
        self.role = role


def test_estimate_tokens_is_positive() -> None:
    assert estimate_tokens("") == 1
    assert estimate_tokens("abcd" * 3) == 3


def test_under_budget_is_untouched() -> None:
    messages = [_Msg("short") for _ in range(3)]
    assert compact(messages, budget=10_000) is messages


def test_over_budget_trims_middle_tool_output() -> None:
    messages = [_Msg("system"), _Msg("question")] + [
        _Msg("x" * 4000, role="tool") for _ in range(8)
    ]
    out = compact(messages, budget=200, keep_recent=3)
    assert len(out) == len(messages)
    # 中间的被压短，最近的保持原样
    assert len(out[3].content) < 4000
    assert out[-1].content == "x" * 4000


def test_review_evidence_is_never_compacted() -> None:
    """审查意见与风控命中是审计证据，压缩会毁掉可追溯性。"""

    evidence = _Msg("审查意见：本轮数字缺少来源，verdict=revise；风险提示：……", role="tool")
    messages = [_Msg("system"), _Msg("q")] + [_Msg("x" * 4000, role="tool") for _ in range(6)]
    messages.insert(4, evidence)

    out = compact(messages, budget=200, keep_recent=2)
    assert any("verdict=revise" in str(getattr(item, "content", "")) for item in out)


def test_compaction_brings_tokens_down() -> None:
    messages = [_Msg("system"), _Msg("q")] + [_Msg("y" * 8000) for _ in range(10)]
    before = context_tokens(messages)
    after = context_tokens(compact(messages, budget=500, keep_recent=2))
    assert after < before


@pytest.mark.parametrize("budget", [0, -1])
def test_non_positive_budget_disables_compaction(budget: int) -> None:
    messages = [_Msg("z" * 100)]
    assert compact(messages, budget=budget) is messages
