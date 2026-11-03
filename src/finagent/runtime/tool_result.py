"""统一工具执行结果（2.0 P0-2）。

为什么需要它
------------
现在工具失败有三种表现：抛异常、返回 ``{"ok": false}``、返回空 DataFrame 当成功。
统计口径因此对不上——日志里出现过「连续 4 次工具失败」和「错误=0」同时出现，
因为「失败计数」和「错误列表」是两套互不相干的变量。

这里把一次工具执行收敛成**一条事实**，统计、日志、前端都从它派生：

    status             success / partial / failed / unsupported / timeout / cancelled
    error_code         稳定错误码（可分支，不用解析文本）
    retryable          是否值得重试（由抛出方声明）
    provider           哪个数据源给的（或失败在哪个源）
    execution_time_ms  本次执行耗时
    data_as_of         数据时点
    warnings           部分成功时的说明
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ToolStatus(StrEnum):
    SUCCESS = "success"
    #: 拿到了部分数据（字段缺失、部分日期缺失），仍可用
    PARTIAL = "partial"
    FAILED = "failed"
    #: 当前数据源不提供该能力——不是故障，换工具即可
    UNSUPPORTED = "unsupported"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


#: 这些状态算「失败」（用于统计与连续失败判定），但 unsupported 不算——
#: 它不是故障，而是能力边界，重复出现不该触发熔断式刹车。
FAILURE_STATUSES = frozenset({ToolStatus.FAILED, ToolStatus.TIMEOUT})


@dataclass(slots=True)
class ToolResult:
    tool: str
    status: ToolStatus = ToolStatus.SUCCESS
    data: Any = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    provider: str | None = None
    execution_time_ms: int = 0
    data_as_of: str | None = None
    warnings: list[str] = field(default_factory=list)
    #: 工具调用标识，与 tool:call 事件、幂等键一致
    call_id: str = ""
    #: 已经重试过几次（由 provider 网关或执行器累计）
    retries: int = 0

    @property
    def ok(self) -> bool:
        return self.status in (ToolStatus.SUCCESS, ToolStatus.PARTIAL)

    @property
    def failed(self) -> bool:
        return self.status in FAILURE_STATUSES

    def to_envelope(self) -> dict[str, Any]:
        """转成模型看的 ``{"ok": ...}`` 信封。"""

        if self.ok:
            payload: dict[str, Any] = {"ok": True, "data": self.data}
            if self.status is ToolStatus.PARTIAL or self.warnings:
                payload["warnings"] = list(self.warnings)
            if self.provider:
                payload["provider"] = self.provider
            if self.data_as_of:
                payload["data_as_of"] = self.data_as_of
            return payload

        error: dict[str, Any] = {
            "code": self.error_code or "tool_failed",
            "message": self.error_message or f"{self.tool} 执行失败",
        }
        if self.retryable:
            error["retryable"] = True
        if self.provider:
            error["provider"] = self.provider
        return {"ok": False, "error": error}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool": self.tool,
            "status": str(self.status),
            "execution_time_ms": self.execution_time_ms,
            "retries": self.retries,
        }
        if self.call_id:
            payload["call_id"] = self.call_id
        if self.provider:
            payload["provider"] = self.provider
        if self.error_code:
            payload["error_code"] = self.error_code
        if self.error_message:
            payload["error_message"] = self.error_message[:200]
        if self.retryable:
            payload["retryable"] = True
        if self.warnings:
            payload["warnings"] = list(self.warnings)
        return payload


@dataclass(slots=True)
class ToolStats:
    """一次执行里的工具调用统计。

    这是修「工具调用=10，错误=0」那个矛盾的关键：**分开计数**，
    不再让「失败次数」「错误列表」「跳过」混成一锅。
    """

    total: int = 0
    succeeded: int = 0
    partial: int = 0
    failed: int = 0
    unsupported: int = 0
    skipped: int = 0
    provider_retries: int = 0
    #: 连续失败的最大长度（诊断「连续 4 次失败」这类问题）
    max_consecutive_failures: int = 0

    def record(self, result: ToolResult) -> None:
        self.total += 1
        self.provider_retries += max(0, int(result.retries))
        if result.status is ToolStatus.SUCCESS:
            self.succeeded += 1
        elif result.status is ToolStatus.PARTIAL:
            self.partial += 1
        elif result.status is ToolStatus.UNSUPPORTED:
            self.unsupported += 1
        else:
            self.failed += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_calls_total": self.total,
            "tool_calls_succeeded": self.succeeded,
            "tool_calls_partial": self.partial,
            "tool_calls_failed": self.failed,
            "tool_calls_unsupported": self.unsupported,
            "tool_calls_skipped": self.skipped,
            "provider_retries": self.provider_retries,
            "max_consecutive_failures": self.max_consecutive_failures,
        }


# ----------------------------------------------------------------------
#: provider 错误码 -> 工具状态。让「为什么失败」能被分支，而不是全归成 failed。
_ERROR_CODE_TO_STATUS: dict[str, ToolStatus] = {
    "CAPABILITY_NOT_SUPPORTED": ToolStatus.UNSUPPORTED,
    "TIMEOUT": ToolStatus.TIMEOUT,
    "CIRCUIT_OPEN": ToolStatus.FAILED,
}


def status_from_error(code: str | None) -> ToolStatus:
    return _ERROR_CODE_TO_STATUS.get(str(code or ""), ToolStatus.FAILED)
