"""生成 ``docs/TOOLS.md``。

在仓库根目录运行：

    python scripts/gen_tool_reference.py

文档直接从线上 FastMCP 工具定义导出，所以不可能与实际暴露的工具漂移。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
os.environ.setdefault("FINAGENT_PROVIDER", "offline")

from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from finagent.graph.mcp_client import SERVERS  # noqa: E402

SERVER_LABELS = {
    "market": ("finagent-market", "行情数据", "`finagent-mcp-market`"),
    "quant": ("finagent-quant", "量化分析", "`finagent-mcp-quant`"),
    "portfolio": ("finagent-portfolio", "持仓、预警与用户画像", "`finagent-mcp-portfolio`"),
}


def _type_name(schema: dict) -> str:
    if "anyOf" in schema:
        names = []
        for option in schema["anyOf"]:
            name = _type_name(option)
            if name not in names:
                names.append(name)
        return " | ".join(names)
    if "enum" in schema:
        return "enum(" + ", ".join(str(item) for item in schema["enum"]) + ")"
    mapping = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "array": "list",
        "object": "dict",
        "null": "None",
    }
    return mapping.get(schema.get("type", ""), schema.get("type", "any"))


async def collect() -> dict[str, list[dict]]:
    catalogues: dict[str, list[dict]] = {}
    for name, module_path in SERVERS.items():
        module = __import__(module_path, fromlist=["build_server"])
        server = module.build_server()
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            listed = await session.list_tools()
            entries = []
            for tool in sorted(listed.tools, key=lambda item: item.name):
                schema = tool.inputSchema or {}
                properties = schema.get("properties", {})
                required = set(schema.get("required", []))
                entries.append(
                    {
                        "name": tool.name,
                        "description": (tool.description or "").strip(),
                        "params": [
                            {
                                "name": key,
                                "type": _type_name(value),
                                "required": key in required,
                                "description": value.get("description", ""),
                                "default": value.get("default"),
                            }
                            for key, value in properties.items()
                        ],
                    }
                )
            catalogues[name] = entries
    return catalogues


def render(catalogues: dict[str, list[dict]]) -> str:
    total = sum(len(entries) for entries in catalogues.values())
    lines: list[str] = [
        "# MCP 工具参考",
        "",
        "> 由 `scripts/gen_tool_reference.py` 从线上 FastMCP 服务器定义生成。",
        "> 不要手工编辑；改完工具后重新运行生成脚本。",
        "",
        f"{len(catalogues)} 个 MCP 服务器，共 {total} 个工具。",
        "",
        "## 返回信封",
        "",
        "每个工具都只返回**一个 JSON 字符串**：",
        "",
        "```json",
        '{"ok": true,  "data": { ... }}',
        '{"ok": false, "error": {"code": "unknown_symbol", "message": "...", "hint": "..."}}',
        "```",
        "",
        "已知错误码：`configuration_error`、`provider_error`、`provider_unavailable`、",
        "`unknown_symbol`、`invalid_argument`、`insufficient_data`、`strategy_not_found`、",
        "`not_found`、`internal_error`。",
        "",
        "## 目录",
        "",
    ]
    for name, entries in catalogues.items():
        server_name = SERVER_LABELS[name][0]
        lines.append(f"- [`{server_name}`](#{server_name}) —— {len(entries)} 个工具")
    lines.append("")

    for name, entries in catalogues.items():
        server_name, label, entry_point = SERVER_LABELS[name]
        lines.extend(
            [
                f"## {server_name}",
                "",
                f"**{label}** · 入口 {entry_point} · `python -m finagent.mcp_servers {name}`",
                "",
                "| 工具 | 说明 |",
                "| --- | --- |",
            ]
        )
        for entry in entries:
            summary = entry["description"].split("\n")[0].strip()
            lines.append(f"| [`{entry['name']}`](#{entry['name']}) | {summary} |")
        lines.append("")

        for entry in entries:
            lines.extend([f"### {entry['name']}", ""])
            lines.extend(entry["description"].splitlines() or ["_无说明。_"])
            lines.append("")
            if entry["params"]:
                lines.extend(
                    [
                        "| 参数 | 类型 | 必填 | 说明 |",
                        "| --- | --- | --- | --- |",
                    ]
                )
                for param in entry["params"]:
                    default = "" if param["default"] is None else f"（默认 `{param['default']}`）"
                    lines.append(
                        "| `{name}` | `{type}` | {req} | {desc}{default} |".format(
                            name=param["name"],
                            type=param["type"],
                            req="是" if param["required"] else "否",
                            desc=(param["description"] or "—").replace("|", "\\|"),
                            default=default,
                        )
                    )
                lines.append("")
            else:
                lines.extend(["_无参数。_", ""])
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    catalogues = asyncio.run(collect())
    output = REPO_ROOT / "docs" / "TOOLS.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(catalogues), encoding="utf-8")
    total = sum(len(entries) for entries in catalogues.values())
    print(f"已写入 {output}（{total} 个工具）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
