"""命令行界面。

不带任何子命令时直接打开**浏览器聊天界面**（``finagent``），这是日常用法。其余子命令
覆盖从排查到运维的各个场景::

    finagent                                   # 打开浏览器界面（推荐）
    finagent web --no-browser --port 8100      # 只起服务，自己开浏览器
    finagent run "用 MACD 回测中信证券近一年"   # 单次提问，适合脚本
    finagent chat                              # 终端里的交互式对话
    finagent tools                             # MCP 工具清单
    finagent graph                             # 图拓扑与工具归属
    finagent mcp quant --transport stdio        # 单个 MCP 服务器，可被外部使用
    finagent alerts --watch                    # 无界面的预警 worker
    finagent doctor                            # 环境与可用性自检
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from finagent import __version__
from finagent.graph.mcp_client import SERVERS, open_toolkit
from finagent.llm import describe_llm
from finagent.logging import configure_logging
from finagent.settings import get_settings, reset_settings_cache

BANNER = """\
  ______ _        _                    _
 |  ____(_)      / \\   __ _  ___ _ __ | |_
 | |__  _ _ __  / _ \\ / _` |/ _ \\ '_ \\| __|
 |  __|| | '_ \\/ ___ \\ (_| |  __/ | | | |_
 |_|   |_|_| |_/_/   \\_\\__, |\\___|_| |_|\\__|
                       |___/   MCP + LangGraph
"""


# ----------------------------------------------------------------------
def _console() -> Any:
    from rich.console import Console

    return Console()


def _parse_date(value: str | None) -> str | None:
    return value


# ----------------------------------------------------------------------
async def _cmd_run(args: argparse.Namespace) -> int:
    from finagent.graph import open_app

    console = _console()
    settings = get_settings()

    async with open_app(settings, transport=args.transport) as app:
        if args.json:
            result = await app.arun(args.question, user_id=args.user, thread_id=args.thread)
            console.print_json(json.dumps(result.to_dict(), ensure_ascii=False))
            if args.verbose:
                console.print_json(json.dumps(result.trace, ensure_ascii=False))
            return 0 if not result.errors else 1

        async for event in app.astream(args.question, user_id=args.user, thread_id=args.thread):
            kind = event.get("type")
            if kind == "route" and args.verbose:
                console.print(f"[dim]→ {event.get('agent')}: {event.get('reason')}[/dim]")
            elif kind == "event" and args.verbose:
                _render_agent_event(console, event)
            elif kind == "final":
                result = event["result"]
                _render_answer(console, result)
                return 0 if not result.errors else 1
    return 1


def _render_agent_event(console: Any, event: dict[str, Any]) -> None:
    name = event.get("event")
    if name == "tool_call":
        console.print(
            f"  [cyan]· {event.get('tool')}[/cyan] [dim]{_short(event.get('args'))}[/dim]"
        )
    elif name == "tool_result":
        mark = "[green]✓[/green]" if event.get("ok") else "[red]✗[/red]"
        console.print(f"  {mark} [dim]{_short(event.get('preview'), 160)}[/dim]")
    elif name == "critic":
        verdict = event.get("verdict")
        mark = "[yellow]退回返工[/yellow]" if verdict == "revise" else "[green]通过[/green]"
        console.print(f"  [magenta]⚖ 审查 {event.get('label')}[/magenta] {mark}")
        for issue in event.get("issues") or []:
            style = "yellow" if issue.get("severity") == "high" else "dim"
            console.print(
                f"    [{style}]· {issue.get('label')}：{_short(issue.get('detail'), 120)}[/]"
            )
    elif name == "risk":
        marks = "、".join(flag.get("label", "") for flag in event.get("flags") or [])
        console.print(f"  [red]🛡 风控[/red] 命中：{marks}")
        for action in event.get("actions") or []:
            console.print(f"    [dim]· {action}[/dim]")


def _short(value: Any, limit: int = 100) -> str:
    text = (
        json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    )
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[:limit] + "…"


def _render_answer(console: Any, result: Any) -> None:
    from rich.markdown import Markdown

    console.print()
    console.print(Markdown(result.answer or "_（回答为空）_"))
    console.print()
    console.print(
        f"[dim]路由：{' → '.join(result.agents) or '无'} | "
        f"工具调用：{len(result.trace)} | 用户：{result.user_id}[/dim]"
    )


# ----------------------------------------------------------------------
def _cmd_web(args: argparse.Namespace) -> int:
    """启动浏览器聊天界面。"""

    from rich.console import Console

    from finagent.web.server import serve

    settings = get_settings()
    console = Console()
    llm = describe_llm(settings)
    if not llm["enabled"]:
        console.print(
            "[yellow]提示[/yellow]：当前未接大模型，界面会用确定性规划器回答。\n"
            "想要真正的对话式推理，在项目根目录的 .env 里加上：\n"
            "    [bold]FINAGENT_LLM_PROVIDER=deepseek[/bold]\n"
            "    [bold]FINAGENT_LLM_API_KEY=sk-你的key[/bold]\n"
        )
    return serve(
        host=args.host,
        port=args.port,
        open_browser=not getattr(args, "no_browser", False),
        transport=getattr(args, "transport", None),
        settings=settings,
    )


# ----------------------------------------------------------------------
async def _cmd_chat(args: argparse.Namespace) -> int:
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.prompt import Prompt

    from finagent.graph import open_app

    console = _console()
    settings = get_settings()
    console.print(BANNER, style="bold cyan")
    console.print(
        Panel.fit(
            f"provider=[bold]{settings.provider}[/bold]  "
            f"llm=[bold]{describe_llm(settings)['mode']}[/bold]  "
            f"user=[bold]{args.user}[/bold]  thread=[bold]{args.thread}[/bold]\n"
            "输入问题开始对话；/new 新会话，/tools 查看工具，/exit 退出",
            title=f"Fin-Agent {__version__}",
            border_style="cyan",
        )
    )

    async with open_app(settings, transport=args.transport) as app:
        while True:
            try:
                question = Prompt.ask("[bold green]你[/bold green]").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]再见[/dim]")
                return 0

            if not question:
                continue
            if question.lower() in ("/exit", "exit", "quit", "/quit"):
                return 0
            if question == "/new":
                args.thread = f"cli-{int(__import__('time').time())}"
                console.print(f"[dim]新会话：{args.thread}[/dim]")
                continue
            if question == "/tools":
                _print_tools(console, app.toolkit.describe())
                continue
            if question == "/graph":
                _print_graph(console)
                continue

            try:
                async for event in app.astream(question, user_id=args.user, thread_id=args.thread):
                    kind = event.get("type")
                    if kind == "route":
                        console.print(f"[dim]→ {event.get('agent')} · {event.get('reason')}[/dim]")
                    elif kind == "event":
                        _render_agent_event(console, event)
                    elif kind == "memory":
                        console.print(f"[dim]已记住偏好：{event.get('updates')}[/dim]")
                    elif kind == "final":
                        console.print()
                        console.print(Markdown(event["result"].answer or "_（空）_"))
                        console.print()
            except KeyboardInterrupt:
                console.print("\n[yellow]已中断[/yellow]")
            except Exception as exc:  # noqa: BLE001 - keep the REPL alive
                console.print(f"[red]错误：{exc}[/red]")


# ----------------------------------------------------------------------
def _cmd_tools(args: argparse.Namespace) -> int:
    async def _run() -> int:
        console = _console()
        async with open_toolkit(get_settings(), transport=args.transport) as toolkit:
            console.print(
                f"[bold]{len(toolkit.tools)} 个 MCP 工具[/bold]（transport={toolkit.transport}）\n"
            )
            _print_tools(console, toolkit.describe())
        return 0

    return asyncio.run(_run())


def _print_tools(console: Any, catalog: list[dict[str, Any]]) -> None:
    from rich.table import Table

    table = Table(show_lines=False, header_style="bold cyan")
    table.add_column("工具", style="bold")
    table.add_column("参数", style="dim")
    table.add_column("说明")
    for entry in catalog:
        table.add_row(entry["name"], ", ".join(entry.get("args", [])), entry["description"])
    console.print(table)


# ----------------------------------------------------------------------
def _cmd_graph(args: argparse.Namespace) -> int:
    console = _console()
    _print_graph(console, verbose=True)
    return 0


def _print_graph(console: Any, verbose: bool = False) -> None:
    from rich.table import Table

    from finagent.graph.experts import describe_experts
    from finagent.graph.state import AGENT_LABELS
    from finagent.settings import get_settings as _settings

    resolved = _settings()
    table = Table(title="Supervisor + 专家拓扑", header_style="bold cyan")
    table.add_column("节点")
    table.add_column("类型")
    table.add_column("拥有的 MCP 工具")
    table.add_row("load_memory", "节点", "get_user_profile")
    table.add_row("supervisor", "路由", "由 LLM 决策，或按关键词规则")
    for expert in describe_experts():
        table.add_row(
            expert["agent"],
            "专家",
            f"{expert['tool_count']} 个："
            + ", ".join(expert["tools"][:6])
            + ("…" if expert["tool_count"] > 6 else ""),
        )
    table.add_row("critic", "审查", "判定 pass / revise（论据、矛盾、谄媚）")
    table.add_row("save_memory", "节点", "update_user_profile")
    table.add_row("risk_guard", "风控", "确定性护栏：偏好冲突 / 强指令 / 来源标注")
    table.add_row("finalize", "节点", "汇总最终答案")
    console.print(table)
    console.print(
        f"\n[dim]max_agent_hops={resolved.max_agent_hops} · "
        f"max_revisions={resolved.max_revisions} · "
        f"risk_guard={'开' if resolved.risk_guard_enabled else '关'} · "
        f"llm={describe_llm(resolved)['mode']} · "
        f"transport={os.environ.get('FINAGENT_MCP_TRANSPORT', 'inprocess')}[/dim]"
    )
    if verbose:
        console.print("\n[bold]连边[/bold]")
        console.print("START → load_memory → supervisor")
        for agent in AGENT_LABELS:
            if agent in ("finish", "critic", "risk_guard"):
                continue
            console.print(f"  supervisor ──{agent}──▶ {agent} ──▶ critic")
        console.print("  critic ──pass──▶ supervisor")
        console.print("  critic ──revise──▶ 打回该专家返工（max_revisions 封顶）")
        console.print("  supervisor ──finish──▶ save_memory → risk_guard → finalize → END")


# ----------------------------------------------------------------------
def _cmd_mcp(args: argparse.Namespace) -> int:
    """在前台启动一个 MCP 服务器。"""

    sys.argv = [f"finagent-mcp-{args.server}", *args.mcp_args]
    if args.server == "market":
        from finagent.mcp_servers.market import main as run
    elif args.server == "quant":
        from finagent.mcp_servers.quant import main as run
    elif args.server == "portfolio":
        from finagent.mcp_servers.portfolio import main as run
    else:  # pragma: no cover - argparse restricts choices
        print(f"未知服务器 '{args.server}'", file=sys.stderr)
        return 2
    run()
    return 0


# ----------------------------------------------------------------------
def _cmd_alerts(args: argparse.Namespace) -> int:
    from finagent.worker import check_once, watch

    console = _console()
    if args.watch:
        summary = asyncio.run(
            watch(
                get_settings(),
                interval_seconds=args.interval,
                iterations=args.iterations,
            )
        )
        console.print_json(json.dumps(summary.to_dict(), ensure_ascii=False))
        return 0

    summary = asyncio.run(check_once(get_settings(), trading_hours_only=args.trading_hours))
    if summary.triggered:
        for event in summary.triggered:
            console.print(f"[bold yellow]⏰ {event.get('message')}[/bold yellow]")
    else:
        console.print(
            f"[dim]已评估 {summary.evaluated} 条预警，均未触发"
            + (f"（{summary.skipped}）" if summary.skipped else "")
            + "[/dim]"
        )
    return 0 if not summary.skipped else 1


# ----------------------------------------------------------------------
def _cmd_doctor(args: argparse.Namespace) -> int:
    from rich.table import Table

    console = _console()
    settings = get_settings()
    llm = describe_llm(settings)

    table = Table(title="Fin-Agent 自检", header_style="bold cyan")
    table.add_column("项目")
    table.add_column("取值")

    table.add_row("版本", __version__)
    table.add_row("Python", sys.version.split()[0])
    table.add_row("数据目录", str(settings.resolve_data_dir()))
    table.add_row("checkpoint 数据库", str(settings.resolve_checkpoint_path()))
    table.add_row("行情数据源", settings.provider)
    table.add_row("LLM", f"{llm['provider']} / {llm['model']} ({llm['mode']})")
    table.add_row("最大专家跳转次数", str(settings.max_agent_hops))
    table.add_row("MCP 传输方式", os.environ.get("FINAGENT_MCP_TRANSPORT", "inprocess"))
    table.add_row("Tushare token", "已配置" if settings.tushare_token else "未配置")
    table.add_row("SMTP", "已配置" if settings.email_smtp_server else "未配置")

    for label, module in (
        ("mcp", "mcp"),
        ("langgraph", "langgraph"),
        ("langchain-core", "langchain_core"),
        ("langchain-mcp-adapters", "langchain_mcp_adapters"),
        ("pandas", "pandas"),
    ):
        try:
            import importlib

            importlib.import_module(module)
            table.add_row(f"依赖:{label}", "[green]正常[/green]")
        except Exception as exc:  # noqa: BLE001
            table.add_row(f"依赖:{label}", f"[red]{exc}[/red]")

    try:
        table.add_row("tushare extra", "已安装" if _installed("tushare") else "未安装")
    except Exception:  # noqa: BLE001
        pass

    console.print(table)

    async def _probe() -> None:
        async with open_toolkit(settings, transport=args.transport) as toolkit:
            console.print(
                f"[green]MCP 正常[/green]：{len(toolkit.tools)} 个工具，transport={toolkit.transport}"
            )

    try:
        asyncio.run(_probe())
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]MCP 启动失败：[/red] {exc}")
        return 1
    if not settings.llm_enabled:
        if settings.llm_api_key:
            console.print(
                "[yellow]只差一步[/yellow]：已检测到 API Key，但 "
                "[bold]FINAGENT_LLM_PROVIDER[/bold] 还是 `none`。\n"
                "在 .env 里加一行 [bold]FINAGENT_LLM_PROVIDER=deepseek[/bold] 即可启用大模型。"
            )
        else:
            console.print(
                "[yellow]LLM 未启用[/yellow]：Agent 走确定性规划器。\n"
                "要接入 DeepSeek，在项目根目录的 .env 里写：\n"
                "    FINAGENT_LLM_PROVIDER=deepseek\n"
                "    FINAGENT_LLM_API_KEY=sk-你的key\n"
                "改完再跑一次 finagent doctor 确认。"
            )
    else:
        console.print(
            f"[green]LLM 已启用[/green]：{llm['provider']} / {llm['model']}（{llm['base_url']}）"
        )
    return 0


def _installed(name: str) -> bool:
    import importlib.metadata as md

    try:
        md.version(name)
        return True
    except md.PackageNotFoundError:
        return False


# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="finagent",
        description=(
            "Fin-Agent：面向中国 A 股的智能投研助手（MCP 工具服务器 + LangGraph 多 Agent 调度）。\n"
            "不带子命令时直接打开浏览器聊天界面。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  finagent                                  打开浏览器界面\n"
            "  finagent web --no-browser --port 8100     只起服务\n"
            '  finagent run "用 MACD 策略回测中信证券近一年"\n'
            "  finagent chat                             终端里对话\n"
            "  finagent alerts --watch                   后台预警\n"
            "  finagent doctor                           自检\n"
        ),
    )
    parser.add_argument("-v", "--version", action="version", version=f"finagent {__version__}")
    parser.add_argument("--log-level", default=None, help="日志级别：DEBUG/INFO/WARNING/ERROR")

    def add_transport(sub_parser: argparse.ArgumentParser) -> None:
        sub_parser.add_argument(
            "--transport",
            default=argparse.SUPPRESS,
            choices=["stdio", "inprocess"],
            help=(
                "图如何接入 MCP 服务器：'inprocess'（默认，不起子进程）"
                "或 'stdio'（拉起真实的 MCP 服务器子进程）。"
            ),
        )

    sub = parser.add_subparsers(dest="command")

    web = sub.add_parser("web", help="打开浏览器聊天界面（默认行为）")
    web.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，仅本机）")
    web.add_argument(
        "--port", type=int, default=8000, help="监听端口（默认 8000，被占用会自动顺延）"
    )
    web.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    add_transport(web)

    run = sub.add_parser("run", help="回答一个问题后退出")
    run.add_argument("question", help="用户的问题")
    run.add_argument("--user", default="default")
    run.add_argument("--thread", default="cli")
    run.add_argument("--json", action="store_true", help="输出机器可读的 JSON")
    run.add_argument("--verbose", action="store_true", help="显示路由与工具调用过程")
    add_transport(run)

    chat = sub.add_parser("chat", help="交互式对话")
    chat.add_argument("--user", default="default")
    chat.add_argument("--thread", default="cli")
    add_transport(chat)

    add_transport(sub.add_parser("tools", help="列出 MCP 工具清单"))

    sub.add_parser("graph", help="打印 Agent 拓扑")

    mcp = sub.add_parser("mcp", help="启动单个 MCP 服务器")
    mcp.add_argument("server", choices=sorted(SERVERS))
    mcp.add_argument("mcp_args", nargs=argparse.REMAINDER)

    alerts = sub.add_parser("alerts", help="评估价格预警")
    alerts.add_argument("--watch", action="store_true", help="持续轮询")
    alerts.add_argument("--interval", type=int, default=None, help="两次轮询的间隔秒数")
    alerts.add_argument("--iterations", type=int, default=None, help="轮询 N 次后停止")
    alerts.add_argument(
        "--trading-hours",
        action="store_true",
        help="仅在 A 股交易时段评估",
    )

    sub.add_parser("doctor", help="环境与可用性自检")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    reset_settings_cache()
    configure_logging(args.log_level, force=True)

    # ``--transport`` 在每个子命令上用 SUPPRESS 声明，未指定时不会覆盖取值；
    # ``None`` 表示「交给环境变量决定」。
    if not hasattr(args, "transport"):
        args.transport = None

    if args.command is None:
        # 不带子命令 = 打开浏览器界面。这是日常用法：命令行只是运维入口。
        return _cmd_web(
            argparse.Namespace(
                host="127.0.0.1",
                port=8000,
                no_browser=False,
                transport=args.transport,
            )
        )
    if args.command == "web":
        return _cmd_web(args)
    if args.command == "run":
        return asyncio.run(_cmd_run(args))
    if args.command == "chat":
        return asyncio.run(_cmd_chat(args))
    if args.command == "tools":
        return _cmd_tools(args)
    if args.command == "graph":
        return _cmd_graph(args)
    if args.command == "mcp":
        return _cmd_mcp(args)
    if args.command == "alerts":
        return _cmd_alerts(args)
    if args.command == "doctor":
        return _cmd_doctor(args)

    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
