"""MCP → LangChain 的工具桥。

两种传输，同一协议：

``stdio``（默认生产形态）
    把三个 MCP 服务器作为子进程拉起，用 ``MultiServerMCPClient`` 连接。外部 MCP
    宿主就是这么消费它们的。

``inprocess``
    把同样的 FastMCP 服务器挂在当前解释器里，走内存流连接。没有子进程、没有管道——
    测试套件、把 Fin-Agent 当库嵌入的场景，以及禁止管道式子进程的沙箱都用它。

两种方式下 Agent 调的都是带真实 JSON schema 的真实 MCP 工具；这一层之上的代码
不知道当前是哪种传输。
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from finagent.errors import ConfigurationError
from finagent.logging import get_logger
from finagent.settings import Settings, get_settings

log = get_logger("graph.mcp")

Transport = Literal["stdio", "inprocess"]

#: MCP 服务器名 -> 其 ``build_server`` 工厂的 import 路径。
SERVERS: dict[str, str] = {
    "market": "finagent.mcp_servers.market",
    "quant": "finagent.mcp_servers.quant",
    "portfolio": "finagent.mcp_servers.portfolio",
}


# ----------------------------------------------------------------------
@dataclass(slots=True)
class Toolkit:
    """所有 MCP 服务器工具的并集，支持按名取子集。"""

    tools: list[Any] = field(default_factory=list)
    by_name: dict[str, Any] = field(default_factory=dict)
    transport: str = "inprocess"

    def __post_init__(self) -> None:
        if not self.by_name:
            self.by_name = {tool.name: tool for tool in self.tools}

    def names(self) -> list[str]:
        return sorted(self.by_name)

    def subset(self, names: Iterable[str]) -> list[Any]:
        """按名选择工具；服务器没有暴露任何一个就报错。"""

        wanted = list(dict.fromkeys(names))
        missing = [name for name in wanted if name not in self.by_name]
        if missing:
            raise ConfigurationError(
                f"MCP tool(s) not available: {', '.join(missing)}",
                hint=f"可用工具：{', '.join(self.names())}",
            )
        return [self.by_name[name] for name in wanted]

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": (tool.description or "").splitlines()[0][:120],
                "args": sorted((tool.args or {}).keys()),
            }
            for tool in sorted(self.tools, key=lambda item: item.name)
        ]


# ----------------------------------------------------------------------
def _package_root() -> str:
    """子进程里能 import ``finagent`` 需要挂到 ``PYTHONPATH`` 的目录。"""

    import finagent

    return str(Path(finagent.__file__).resolve().parent.parent)


def _child_env(settings: Settings) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("FINAGENT_MCP_")}
    env.update(
        {
            "FINAGENT_PROVIDER": settings.provider,
            "FINAGENT_DATA_DIR": str(settings.resolve_data_dir()),
            "FINAGENT_LLM_PROVIDER": settings.llm_provider,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": os.pathsep.join(
                filter(None, [_package_root(), os.environ.get("PYTHONPATH", "")])
            ),
        }
    )
    if settings.tushare_token:
        env["FINAGENT_TUSHARE_TOKEN"] = settings.tushare_token
    return env


def _stdio_connections(settings: Settings, servers: Iterable[str]) -> dict[str, Any]:
    env = _child_env(settings)
    connections: dict[str, Any] = {}
    for name in servers:
        if name not in SERVERS:
            raise ConfigurationError(f"未知 MCP 服务器 '{name}'。")
        connections[name] = {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "finagent.mcp_servers", name],
            "env": env,
            "cwd": _package_root(),
        }
    return connections


def _build_server(name: str) -> Any:
    module_path = SERVERS[name]
    module = __import__(module_path, fromlist=["build_server"])
    return module.build_server()


# ----------------------------------------------------------------------
@asynccontextmanager
async def open_toolkit(
    settings: Settings | None = None,
    *,
    transport: Transport | None = None,
    servers: Iterable[str] | None = None,
) -> AsyncIterator[Toolkit]:
    """打开所有 MCP 服务器并产出合并后的工具集。

    请把这个上下文保持到图的整个生命周期，工具才能一直绑定在活的会话上。
    """

    resolved = settings or get_settings()
    chosen = list(servers or SERVERS.keys())
    mode: Transport = transport or os.environ.get("FINAGENT_MCP_TRANSPORT", "inprocess")  # type: ignore[assignment]
    if mode not in ("stdio", "inprocess"):
        raise ConfigurationError(
            f"未知 MCP 传输方式 '{mode}'。",
            hint="把 FINAGENT_MCP_TRANSPORT 设为 'stdio' 或 'inprocess'。",
        )

    if mode == "stdio":
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client = MultiServerMCPClient(_stdio_connections(resolved, chosen))
        tools = await client.get_tools()
        log.info("通过 stdio 加载了 %d 个 MCP 工具（来源：%s）", len(tools), "、".join(chosen))
        _assert_unique(tools)
        yield Toolkit(tools=tools, transport="stdio")
        return

    from langchain_mcp_adapters.tools import load_mcp_tools
    from mcp.shared.memory import create_connected_server_and_client_session

    collected: list[Any] = []
    async with AsyncExitStack() as stack:
        for name in chosen:
            server = _build_server(name)
            session = await stack.enter_async_context(
                create_connected_server_and_client_session(server._mcp_server)
            )
            server_tools = await load_mcp_tools(session, server_name=name)
            collected.extend(server_tools)
        log.info("在进程内加载了 %d 个 MCP 工具（来源：%s）", len(collected), "、".join(chosen))
        _assert_unique(collected)
        yield Toolkit(tools=collected, transport="inprocess")


def _assert_unique(tools: list[Any]) -> None:
    seen: dict[str, int] = {}
    for tool in tools:
        seen[tool.name] = seen.get(tool.name, 0) + 1
    duplicates = [name for name, count in seen.items() if count > 1]
    if duplicates:
        raise ConfigurationError(
            "多个服务器之间存在重名 MCP 工具：" + "、".join(sorted(duplicates)),
            hint="重命名冲突的工具，或用 tool_name_prefix 加载。",
        )
