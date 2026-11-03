"""工具执行策略（2.0 P0-2）。

三条规矩
--------
1. **重试只在一个层级负责**。provider 网关已经重试过，执行器不再无限重试；
   否则一次网络抖动会被放大成 9 次请求。
2. **超时必须有上限**。工具卡住比工具失败更糟——它把整轮拖死。
3. **参数错误不重试**。重试一个非法的 ``ts_code`` 只是浪费。

三种错误对应三种动作，写在 :data:`ERROR_POLICY` 里，一处定义、处处一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Action(StrEnum):
    RETRY = "retry"
    FAIL = "fail"
    SKIP = "skip"
    FALLBACK = "fallback"


@dataclass(slots=True)
class ErrorPolicy:
    action: Action
    #: 是否值得让模型知道「可以换个工具再试」
    retryable: bool


#: 错误处理策略表。**不重试**是大多数情况的正确答案。
ERROR_POLICY: dict[str, ErrorPolicy] = {
    "CAPABILITY_NOT_SUPPORTED": ErrorPolicy(Action.SKIP, False),
    "invalid_argument": ErrorPolicy(Action.FAIL, False),
    "UNKNOWN_TOOL": ErrorPolicy(Action.SKIP, False),
    "CIRCUIT_OPEN": ErrorPolicy(Action.FAIL, True),
    "TIMEOUT": ErrorPolicy(Action.FAIL, True),
    "REMOTE_DISCONNECTED": ErrorPolicy(Action.FAIL, True),
    "NETWORK_ERROR": ErrorPolicy(Action.FAIL, True),
    "BAD_RESPONSE": ErrorPolicy(Action.FAIL, True),
    "risk_blocked": ErrorPolicy(Action.FAIL, False),
}


@dataclass(slots=True)
class ExecutionPolicy:
    """一次工具执行的约束。"""

    #: 单个工具的超时（秒）。超时即取消，不让它拖住整轮。
    timeout_seconds: float = 30.0
    #: 允许执行器自己重试的次数。默认 0：provider 网关已经重试过了。
    max_retries: int = 0
    #: 同一轮里最多调用多少次工具（预算）
    max_tool_calls: int = 40
    #: 连续失败多少次就停（交给上层判断是否终止）
    max_consecutive_failures: int = 3

    def policy_for(self, code: str | None) -> ErrorPolicy:
        return ERROR_POLICY.get(str(code or ""), ErrorPolicy(Action.FAIL, True))


@dataclass(slots=True)
class Budget:
    """执行预算：防止「为了一个非关键指标反复调失败工具」。"""

    max_llm_calls: int = 15
    max_tool_calls: int = 40
    max_retries: int = 2
    max_review_rounds: int = 2
    timeout_seconds: float = 180.0
    #: 费用上限（美元）。没有价格表时只做记录，不做硬拦。
    max_cost: float | None = None
    _llm_calls: int = 0
    _tool_calls: int = 0
    _cost: float = 0.0
    _extra: dict[str, Any] = field(default_factory=dict)

    def spend_llm(self) -> None:
        self._llm_calls += 1

    def spend_tool(self) -> None:
        self._tool_calls += 1

    def add_cost(self, cost: float) -> None:
        self._cost += float(cost or 0)

    @property
    def llm_calls(self) -> int:
        return self._llm_calls

    @property
    def tool_calls(self) -> int:
        return self._tool_calls

    def exhausted(self) -> str | None:
        """返回用尽的维度名（没有则 None）。"""

        if self._llm_calls >= self.max_llm_calls:
            return "llm_calls"
        if self._tool_calls >= self.max_tool_calls:
            return "tool_calls"
        if self.max_cost is not None and self._cost >= self.max_cost:
            return "cost"
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "llm_calls": self._llm_calls,
            "tool_calls": self._tool_calls,
            "cost": round(self._cost, 4),
            "limits": {
                "max_llm_calls": self.max_llm_calls,
                "max_tool_calls": self.max_tool_calls,
                "timeout_seconds": self.timeout_seconds,
            },
        }
