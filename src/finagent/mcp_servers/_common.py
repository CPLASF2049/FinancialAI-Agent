"""MCP 服务器的公共管道。

每个工具都只返回**一个 JSON 字符串**，形态为：

::

    {"ok": true,  "data": ...}
    {"ok": false, "error": {"code": "...", "message": "...", "hint": "..."}}

统一信封让 Agent（和测试）不必解析散文就能区分「没有数据」和「数据源失败」，也能让错误
带上可执行的修复提示。
"""

from __future__ import annotations

import functools
import json
import math
import sys
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

import pandas as pd

from finagent.errors import FinAgentError
from finagent.logging import get_logger
from finagent.providers import get_provider
from finagent.providers.base import MarketDataProvider
from finagent.settings import get_settings

log = get_logger("mcp")

F = TypeVar("F", bound=Callable[..., Any])

DEFAULT_MAX_ROWS = 120


# ----------------------------------------------------------------------
# 序列化
# ----------------------------------------------------------------------
def to_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=_default)


def _default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp,)):
        return value.strftime("%Y%m%d")
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:  # noqa: BLE001
            pass
    return str(value)


def scrub(value: Any) -> Any:
    """递归把 NaN/Inf 换成 ``None``，保证 JSON 合法。"""

    if value is None:
        return None
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, dict):
        return {str(key): scrub(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    if hasattr(value, "item"):  # numpy scalar
        try:
            return scrub(value.item())
        except Exception:  # noqa: BLE001
            return str(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.strftime("%Y%m%d")
    return value


def ok(data: Any) -> str:
    return to_json({"ok": True, "data": scrub(data)})


def fail(error: FinAgentError | str, *, code: str | None = None, hint: str | None = None) -> str:
    if isinstance(error, FinAgentError):
        payload = error.to_payload()
    else:
        payload = {"code": code or "error", "message": str(error)}
        if hint:
            payload["hint"] = hint
    return to_json({"ok": False, "error": scrub(payload)})


def error_boundary(function: F) -> F:
    """把异常转成失败信封，而不是抛出堆栈。"""

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return function(*args, **kwargs)
        except FinAgentError as exc:
            log.info("%s -> %s: %s", function.__name__, exc.code, exc.message)
            return fail(exc)
        except (TypeError, ValueError) as exc:
            log.info("%s -> invalid argument: %s", function.__name__, exc)
            return fail(str(exc), code="invalid_argument")
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to a model
            log.exception("%s crashed", function.__name__)
            return fail(
                f"{function.__name__} 执行失败：{exc}",
                code="internal_error",
                hint="请查看服务器日志里的完整堆栈。",
            )

    return wrapper  # type: ignore[return-value]


# ----------------------------------------------------------------------
# 表格
# ----------------------------------------------------------------------
def frame_payload(
    frame: pd.DataFrame,
    *,
    limit: int = DEFAULT_MAX_ROWS,
    columns: Iterable[str] | None = None,
    newest_first: bool = False,
) -> dict[str, Any]:
    """把 provider 的 DataFrame 转成有行数上限、JSON 安全的表格载荷。"""

    if frame is None or frame.empty:
        return {"rows": [], "row_count": 0, "truncated": False, "columns": []}

    working = frame
    if columns is not None:
        keep = [column for column in columns if column in working.columns]
        working = working[keep]
    if newest_first:
        working = working.iloc[::-1]

    total = len(working)
    clipped = working.head(max(limit, 1))
    records = scrub(clipped.to_dict("records"))
    return {
        "rows": records,
        "row_count": len(records),
        "total_rows": total,
        "truncated": total > len(records),
        "columns": list(clipped.columns),
    }


def table(frame: pd.DataFrame, **kwargs: Any) -> str:
    return ok(frame_payload(frame, **kwargs))


# ----------------------------------------------------------------------
# 数据源与配置访问
# ----------------------------------------------------------------------
def provider() -> MarketDataProvider:
    return get_provider()


def settings():
    return get_settings()


def provider_badge() -> dict[str, Any]:
    resolved = get_settings()
    return {"provider": resolved.provider, "llm": resolved.llm_provider}


# ----------------------------------------------------------------------
# 服务器启动
# ----------------------------------------------------------------------
def serve(server_factory: Callable[[], Any], *, prog: str) -> None:
    """CLI 入口：以 stdio（默认）或 HTTP 方式运行 FastMCP 服务器。"""

    import argparse

    parser = argparse.ArgumentParser(prog=prog, description="Fin-Agent MCP 服务器")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=("stdio", "sse", "streamable-http"),
        help="MCP 传输方式（默认 stdio，即 MCP 客户端拉起服务器的方式）。",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    mcp = server_factory()
    if args.transport != "stdio":
        mcp.settings.host = args.host
        mcp.settings.port = args.port

    # stdio 服务器必须保持 stdout 干净：所有诊断输出都走 stderr。
    print(
        f"[{prog}] 启动 MCP 服务器（transport={args.transport}, provider={settings().provider}）",
        file=sys.stderr,
        flush=True,
    )
    mcp.run(transport=args.transport)


def clipped(value: Any, limit: int = 4000) -> str:
    """截断自由文本字段，避免单个工具结果淹没模型上下文。"""

    text = str(value)
    return text if len(text) <= limit else text[:limit] + "...(truncated)"
