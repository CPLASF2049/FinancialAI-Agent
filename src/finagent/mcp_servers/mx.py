"""MCP 服务器：东方财富妙想 Skills（mx）。

把 ``~/skills/mx-*`` 三个**读取型**技能包包装成 MCP 工具：

===========================  ============================================
工具                          能力
===========================  ============================================
``mx_financial_data``        财报、股东、高管、公司简介、行情快照
``mx_news_search``           公告与研报资讯搜索
``mx_stock_screen``          自然语言选股
``mx_quota_status``          今日额度用量（不消耗额度）
===========================  ============================================

**为什么只包这三个**：``mx-zixuan``（自选股）、``mx-moni``（模拟组合）、``mx-poster``
（社区发帖）都是**写操作**，会改用户账户里的真实数据，而且在限额下不可重复执行。
它们保持独立命令行工具的形态，需要时人工运行。

**额度**：接口按天限次。这里做了「当日计数 + 当日缓存」，同一问句重复调用不会重复扣次数；
额度用尽会返回明确错误而不是无限重试。上限由 ``FINAGENT_MX_DAILY_LIMIT`` 控制。

用 ``finagent-mcp-mx`` 或 ``python -m finagent.mcp_servers mx`` 启动。
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from finagent.core import mx
from finagent.mcp_servers._common import error_boundary, ok, serve

try:  # FastMCP 由 mcp>=1.9 提供
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("MCP 服务器需要 'mcp' 包（>=1.9,<2）。") from exc


INSTRUCTIONS = """\
Fin-Agent 妙想数据服务器。

提供东方财富妙想的金融数据、资讯搜索与智能选股。适合查询「结构化、权威、本地数据源
拿不到」的内容：财报科目、股东与高管、公司简介、公告研报、按自然语言条件选股。

注意事项：
- 接口**按天限次**，同一问句当天重复调用会命中缓存、不再消耗额度；
- 额度用尽会直接报错，不会重试；用 mx_quota_status 查看剩余次数；
- 实时行情/日线请优先用行情服务器（更快、不限次）。
"""


def build_server() -> FastMCP:
    mcp = FastMCP("finagent-mx", instructions=INSTRUCTIONS)

    @mcp.tool(
        description=(
            "查询东方财富妙想的金融数据：财报科目（净利润、营业收入、毛利率等）、"
            "股东结构、高管信息、公司简介、主营业务、实时行情快照。"
            "用自然语言提问，一次问一件事效果最好。"
        )
    )
    @error_boundary
    def mx_financial_data(
        query: Annotated[
            str, Field(description="自然语言查询，如「贵州茅台近三年净利润 营业收入」")
        ],
        max_rows: Annotated[int, Field(description="每张表最多返回多少行", ge=1, le=200)] = 30,
    ) -> str:
        payload = mx.run("data", query, max_rows=max_rows)
        return ok(_trim(payload, max_rows))

    @mcp.tool(
        description=(
            "搜索东方财富妙想的财经资讯：个股公告、研报观点、行业新闻。"
            "适合回答「最近有什么消息」「机构怎么看」这类问题。"
        )
    )
    @error_boundary
    def mx_news_search(
        query: Annotated[str, Field(description="搜索问句，如「贵州茅台 最新研报」")],
    ) -> str:
        return ok(_trim(mx.run("search", query), 30))

    @mcp.tool(
        description=(
            "用自然语言条件选股（支持 A 股/港股/美股/板块/基金/ETF）。"
            "例：「市盈率小于 15 的银行股」「今天涨幅大于 5% 的半导体股」。"
            "结果会带当日额度用量。"
        )
    )
    @error_boundary
    def mx_stock_screen(
        query: Annotated[str, Field(description="自然语言选股条件")],
        max_rows: Annotated[int, Field(description="最多返回多少只", ge=1, le=200)] = 30,
    ) -> str:
        return ok(_trim(mx.run("screen", query, max_rows=max_rows), max_rows))

    @mcp.tool(description="查看妙想接口今天的调用额度与剩余次数（不消耗额度）。")
    @error_boundary
    def mx_quota_status() -> str:
        return ok({**mx.quota(), "installed": mx.installed()})

    return mcp


def _trim(payload: dict[str, Any], max_rows: int) -> dict[str, Any]:
    """再兜一层行数上限，避免单次结果淹没模型上下文。"""

    tables = payload.get("tables") or []
    trimmed: list[dict[str, Any]] = []
    for entry in tables:
        rows = entry.get("rows") or []
        trimmed.append({**entry, "rows": rows[:max_rows]})
    return {**payload, "tables": trimmed}


def main() -> None:
    serve(build_server, prog="finagent-mcp-mx")


if __name__ == "__main__":
    main()
