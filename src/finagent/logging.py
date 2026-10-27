"""日志配置。

MCP 服务器通过 **stdout** 与客户端通信（stdio 传输），所以日志绝不能写 stdout。
全部走 stderr；只有 CLI 自己拥有终端时才启用更丰富的 handler。
"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def configure_logging(level: str | None = None, *, force: bool = False) -> logging.Logger:
    global _CONFIGURED
    if _CONFIGURED and not force:
        return logging.getLogger("finagent")

    resolved = (level or os.environ.get("FINAGENT_LOG_LEVEL") or "INFO").upper()
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)-28s %(message)s", "%H:%M:%S")
    )
    root = logging.getLogger("finagent")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, resolved, logging.INFO))
    root.propagate = False
    _CONFIGURED = True
    return root


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(f"finagent.{name}" if not name.startswith("finagent") else name)
