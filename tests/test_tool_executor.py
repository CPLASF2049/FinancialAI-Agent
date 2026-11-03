"""P0-2 验收测试：统一工具执行管线 + 统计口径。"""

from __future__ import annotations

import asyncio
import json

import pytest

from finagent.runtime import (
    Budget,
    ExecutionPolicy,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    ToolStats,
    ToolStatus,
)


class _Ok:
    name = "ok_tool"

    async def ainvoke(self, args):
        return json.dumps({"ok": True, "data": {"price": 1.0}})


class _Unsupported:
    name = "no_cap"

    async def ainvoke(self, args):
        return json.dumps(
            {"ok": False, "error": {"code": "CAPABILITY_NOT_SUPPORTED", "message": "不支持"}}
        )


class _Raises:
    name = "boom"

    async def ainvoke(self, args):
        raise OSError("Remote end closed connection without response")


class _Slow:
    name = "slow"

    async def ainvoke(self, args):
        await asyncio.sleep(1)
        return "{}"


class _Partial:
    name = "partial"

    async def ainvoke(self, args):
        return json.dumps({"ok": True, "data": {"rows": []}, "warnings": ["部分字段缺失"]})


def _registry() -> ToolRegistry:
    return ToolRegistry.from_toolkit(
        type("K", (), {"tools": [_Ok(), _Unsupported(), _Raises(), _Slow(), _Partial()]})()
    )


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 状态判定
# ----------------------------------------------------------------------
def test_success_status() -> None:
    executor = ToolExecutor(_registry())
    result = _run(executor.execute("ok_tool", {}))
    assert result.status is ToolStatus.SUCCESS
    assert result.ok is True
    assert result.data == {"price": 1.0}


def test_unsupported_is_not_a_failure() -> None:
    """能力边界不是故障：不该计失败，也不该触发重试。"""

    executor = ToolExecutor(_registry())
    result = _run(executor.execute("no_cap", {}))
    assert result.status is ToolStatus.UNSUPPORTED
    assert result.failed is False
    assert result.retryable is False


def test_exception_maps_to_failed_with_code() -> None:
    executor = ToolExecutor(_registry())
    result = _run(executor.execute("boom", {}))
    assert result.status is ToolStatus.FAILED
    assert result.error_code == "REMOTE_DISCONNECTED"
    assert result.retryable is True


def test_timeout_is_cancelled_not_hung() -> None:
    executor = ToolExecutor(_registry(), ExecutionPolicy(timeout_seconds=0.1))
    result = _run(executor.execute("slow", {}))
    assert result.status is ToolStatus.TIMEOUT
    assert result.error_code == "TIMEOUT"


def test_unknown_tool_reports_unsupported() -> None:
    executor = ToolExecutor(_registry())
    result = _run(executor.execute("不存在", {}))
    assert result.status is ToolStatus.UNSUPPORTED
    assert result.error_code == "UNKNOWN_TOOL"


def test_partial_status_from_warnings() -> None:
    executor = ToolExecutor(_registry())
    result = _run(executor.execute("partial", {}))
    assert result.status is ToolStatus.PARTIAL
    assert result.warnings == ["部分字段缺失"]


# ----------------------------------------------------------------------
# 统计口径：成功/失败/不支持分开数
# ----------------------------------------------------------------------
def test_stats_separate_categories() -> None:
    executor = ToolExecutor(_registry())
    for _ in range(2):
        _run(executor.execute("ok_tool", {}))
    _run(executor.execute("no_cap", {}))
    _run(executor.execute("boom", {}))

    stats = executor.summary()
    assert stats["tool_calls_total"] == 4
    assert stats["tool_calls_succeeded"] == 2
    assert stats["tool_calls_failed"] == 1
    assert stats["tool_calls_unsupported"] == 1


def test_unsupported_does_not_count_as_consecutive_failure() -> None:
    executor = ToolExecutor(_registry())
    for _ in range(5):
        _run(executor.execute("no_cap", {}))
    assert executor.summary()["max_consecutive_failures"] == 0


def test_consecutive_failures_are_tracked() -> None:
    executor = ToolExecutor(_registry())
    for _ in range(3):
        _run(executor.execute("boom", {}))
    assert executor.summary()["max_consecutive_failures"] == 3
    _run(executor.execute("ok_tool", {}))
    assert executor.summary()["max_consecutive_failures"] == 3  # 记录峰值


def test_stats_are_serialisable() -> None:
    stats = ToolStats()
    stats.record(ToolResult(tool="t", status=ToolStatus.SUCCESS))
    stats.record(ToolResult(tool="t", status=ToolStatus.FAILED))
    payload = stats.to_dict()
    assert payload["tool_calls_total"] == 2
    assert json.dumps(payload)  # 可序列化


# ----------------------------------------------------------------------
# 信封
# ----------------------------------------------------------------------
def test_envelope_ok_and_error() -> None:
    ok = ToolResult(tool="t", status=ToolStatus.SUCCESS, data={"a": 1})
    assert ok.to_envelope()["ok"] is True

    bad = ToolResult(
        tool="t",
        status=ToolStatus.FAILED,
        error_code="REMOTE_DISCONNECTED",
        error_message="boom",
        retryable=True,
        provider="eastmoney",
    )
    envelope = bad.to_envelope()
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "REMOTE_DISCONNECTED"
    assert envelope["error"]["retryable"] is True
    assert envelope["error"]["provider"] == "eastmoney"


def test_partial_envelope_carries_warnings() -> None:
    result = ToolResult(tool="t", status=ToolStatus.PARTIAL, data={}, warnings=["缺字段"])
    envelope = result.to_envelope()
    assert envelope["ok"] is True
    assert envelope["warnings"] == ["缺字段"]


# ----------------------------------------------------------------------
# 预算
# ----------------------------------------------------------------------
def test_budget_tracks_and_reports_exhaustion() -> None:
    budget = Budget(max_llm_calls=2, max_tool_calls=1)
    assert budget.exhausted() is None
    budget.spend_llm()
    budget.spend_llm()
    assert budget.exhausted() == "llm_calls"

    other = Budget(max_tool_calls=1)
    other.spend_tool()
    assert other.exhausted() == "tool_calls"


def test_budget_serialises() -> None:
    budget = Budget()
    budget.spend_llm()
    payload = budget.to_dict()
    assert payload["llm_calls"] == 1
    assert payload["limits"]["max_tool_calls"] == 40


# ----------------------------------------------------------------------
# 错误策略表
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("code", "expected_action"),
    [
        ("CAPABILITY_NOT_SUPPORTED", "skip"),
        ("invalid_argument", "fail"),
        ("TIMEOUT", "fail"),
        ("CIRCUIT_OPEN", "fail"),
    ],
)
def test_error_policy_actions(code: str, expected_action: str) -> None:
    policy = ExecutionPolicy()
    assert str(policy.policy_for(code).action) == expected_action


def test_retryability_by_code() -> None:
    policy = ExecutionPolicy()
    assert policy.policy_for("TIMEOUT").retryable is True
    assert policy.policy_for("CAPABILITY_NOT_SUPPORTED").retryable is False
