"""Fin-Agent：面向中国 A 股的 MCP + LangGraph 智能投研助手。

分层（自下而上）：

``finagent.providers``
    行情数据适配器（Tushare 实盘、确定性离线 fixture）。
``finagent.core``
    纯领域逻辑：技术指标、选股、回测、持仓、预警、画像。
``finagent.mcp_servers``
    三个 stdio MCP 服务器，把 :mod:`finagent.core` 暴露成可复用的工具。
``finagent.graph``
    LangGraph 状态机：Supervisor + 专家 Agent，状态可 checkpoint。
``finagent.cli``
    运维入口。
"""

from finagent.settings import Settings, get_settings

__all__ = ["Settings", "get_settings", "__version__"]

__version__ = "1.0.0"
