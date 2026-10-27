"""各层共用的类型化错误体系。

MCP 工具边界会捕获 :class:`FinAgentError`，转成稳定的
``{"ok": false, "error": {...}}`` 信封，于是线上格式永远不会把 Python 堆栈泄露给模型。
"""

from __future__ import annotations


class FinAgentError(Exception):
    """所有可恢复的、面向用户的错误的基类。"""

    code = "finagent_error"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"code": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
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
