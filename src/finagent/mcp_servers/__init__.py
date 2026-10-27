"""MCP 服务器包。

三个服务器，各自是独立的 stdio 进程，各管一个域：

======================  ===============================  ==========================
服务器                  模块                              域
======================  ===============================  ==========================
``finagent-market``     :mod:`finagent.mcp_servers.market`     行情、基本面、宏观
``finagent-quant``      :mod:`finagent.mcp_servers.quant`      指标、选股、回测
``finagent-portfolio``  :mod:`finagent.mcp_servers.portfolio`  持仓、预警、画像
======================  ===============================  ==========================

可以直接用 ``python -m finagent.mcp_servers <name>`` 启动，或使用
``finagent-mcp-<name>`` 控制台脚本。
"""

from __future__ import annotations

import sys

SERVERS = ("market", "quant", "portfolio")


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(
            "用法：python -m finagent.mcp_servers <server> [options]\n\n"
            f"可用服务器：{', '.join(SERVERS)}\n"
            "其余参数会原样传给 MCP 服务器（--transport/--host/--port）。",
            file=sys.stderr,
        )
        raise SystemExit(0 if args else 2)

    name, rest = args[0], args[1:]
    if name not in SERVERS:
        print(f"未知服务器 '{name}'，可选：{', '.join(SERVERS)}", file=sys.stderr)
        raise SystemExit(2)

    sys.argv = [f"finagent-mcp-{name}", *rest]
    if name == "market":
        from finagent.mcp_servers.market import main as run
    elif name == "quant":
        from finagent.mcp_servers.quant import main as run
    else:
        from finagent.mcp_servers.portfolio import main as run
    run()


if __name__ == "__main__":
    main()
