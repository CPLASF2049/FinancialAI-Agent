"""各层共用的类型化错误体系。

MCP 工具边界会捕获 :class:`FinAgentError`，转成稳定的
``{"ok": false, "error": {...}}`` 信封，于是线上格式永远不会把 Python 堆栈泄露给模型。
"""

from __future__ import annotations


class FinAgentError(Exception):
    """所有可恢复的、面向用户的错误的基类。

    ``code`` 默认取类属性，但允许实例覆盖——这是 2.0 里统一错误码的基础：
    同一个异常类型在不同场景要能给出稳定、可分支的码
    （``CAPABILITY_NOT_SUPPORTED`` / ``REMOTE_DISCONNECTED`` / ``CIRCUIT_OPEN``…），
    而不是让上层去解析异常文本。

    ``retryable`` 同理：它决定「要不要重试」，必须由抛出方声明，
    不能让调用方去猜「这个报错是不是网络抖动」。
    """

    code = "finagent_error"

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        code: str | None = None,
        retryable: bool = False,
        provider: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        if code:
            self.code = code
        self.retryable = retryable
        self.provider = provider

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"code": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        if self.retryable:
            payload["retryable"] = True
        if self.provider:
            payload["provider"] = self.provider
        return payload


class ConfigurationError(FinAgentError):
    code = "configuration_error"


class ProviderError(FinAgentError):
    code = "provider_error"


class ProviderUnavailable(ProviderError):
    code = "provider_unavailable"


class UnknownSymbol(FinAgentError):
    code = "unknown_symbol"


class InvalidArgument(FinAgentError):
    code = "invalid_argument"


class InsufficientData(FinAgentError):
    code = "insufficient_data"


class StrategyNotFound(FinAgentError):
    code = "strategy_not_found"


class NotFound(FinAgentError):
    code = "not_found"
