"""MCP 服务器：模拟持仓、价格预警与长期用户画像。

这三块能力有一个共同点：它们都是**有状态**的，而且归属于「人」，而不是某一次会话。
把它们单独放进一个 MCP 服务器，LangGraph 的专家、CLI 和任何第三方 MCP 客户端读写的
就是同一本账。

用 ``finagent-mcp-portfolio`` 或 ``python -m finagent.mcp_servers portfolio`` 启动。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from finagent.core.alerts import AlertStore, build_notifier
from finagent.core.portfolio import PortfolioStore
from finagent.core.profile import ProfileStore
from finagent.core.symbols import resolve_strict
from finagent.errors import InvalidArgument
from finagent.mcp_servers._common import (
    error_boundary,
    ok,
    provider,
    serve,
    settings,
)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("MCP 服务器需要 'mcp' 包（>=1.9,<2）。") from exc


INSTRUCTIONS = """\
Fin-Agent 持仓服务器。

* 持仓工具（`add_position`、`remove_position`、`get_portfolio_status` 等）管理一本
  模拟持仓账，并按实时价格估值。
* 预警工具（`add_price_alert`、`list_alerts`、`check_alerts` 等）管理价格预警。
  百分比预警由**服务端**换算：传 `pct_change=5` 即可，阈值由实时报价算出来，
  不要自己做这个乘法。
* 画像工具（`get_user_profile`、`update_user_profile`）跨会话保留投资者的风险偏好、
  投资周期、偏好与回避板块。

每个结果都是 `{"ok": true, "data": ...}` 或 `{"ok": false, "error": {...}}`。
"""


# ----------------------------------------------------------------------
# 存储构造
# ----------------------------------------------------------------------
def _data_dir():
    return settings().resolve_data_dir()


def portfolio_store() -> PortfolioStore:
    return PortfolioStore(_data_dir() / "portfolios.json")


def alert_store() -> AlertStore:
    resolved = settings()
    notifier = build_notifier(
        data_dir=_data_dir(),
        smtp={
            "server": resolved.email_smtp_server,
            "port": resolved.email_smtp_port,
            "sender": resolved.email_sender,
            "password": resolved.email_password,
            "receiver": resolved.email_receiver,
        },
    )
    return AlertStore(_data_dir() / "alerts.json", notifier=notifier)


def profile_store() -> ProfileStore:
    return ProfileStore(_data_dir() / "profile.json")


def build_server() -> FastMCP:
    mcp = FastMCP("finagent-portfolio", instructions=INSTRUCTIONS, log_level="WARNING")

    # ------------------------------------------------------------------
    # 组合
    # ------------------------------------------------------------------
    @mcp.tool()
    @error_boundary
    def list_portfolios() -> str:
        """列出所有组合，并标出当前生效的那一个。"""
        books = portfolio_store().list_portfolios()
        return ok({"count": len(books), "portfolios": books})

    @mcp.tool()
    @error_boundary
    def create_portfolio(
        name: Annotated[str, Field(description="新组合的显示名称。")],
    ) -> str:
        """新建一个空组合。"""
        created = portfolio_store().create_portfolio(name)
        return ok(created)

    @mcp.tool()
    @error_boundary
    def add_position(
        ts_code: Annotated[str, Field(description="标的或名称，例如 '000001.SZ' 或 '平安银行'。")],
        amount: Annotated[float, Field(description="买入股数。")],
        price: Annotated[float, Field(description="每股买入价格。")],
        portfolio: Annotated[
            str | None, Field(description="组合 id 或名称；省略则用当前组合。")
        ] = None,
        note: Annotated[str, Field(description="备注。")] = "",
    ) -> str:
        """记录一笔买入。重复添加同一标的会摊薄成本。"""
        position = portfolio_store().add_position(
            ts_code, amount, price, portfolio=portfolio, note=note
        )
        return ok(position)

    @mcp.tool()
    @error_boundary
    def remove_position(
        ts_code: Annotated[str, Field(description="要卖出的标的或名称。")],
        amount: Annotated[float | None, Field(description="卖出股数；省略表示清仓。")] = None,
        price: Annotated[float | None, Field(description="卖出价格，用于计算已实现盈亏。")] = None,
        portfolio: Annotated[str | None, Field(description="组合 id 或名称。")] = None,
    ) -> str:
        """卖出（或清仓）并报告已实现盈亏。"""
        result = portfolio_store().remove_position(
            ts_code, amount=amount, price=price, portfolio=portfolio
        )
        return ok(result)

    @mcp.tool()
    @error_boundary
    def update_position(
        ts_code: Annotated[str, Field(description="已持有的标的或名称。")],
        amount: Annotated[float | None, Field(description="新的股数。")] = None,
        cost: Annotated[float | None, Field(description="新的每股平均成本。")] = None,
        note: Annotated[str | None, Field(description="新的备注。")] = None,
        portfolio: Annotated[str | None, Field(description="组合 id 或名称。")] = None,
    ) -> str:
        """更正某个持仓的股数、成本或备注。"""
        result = portfolio_store().update_position(
            ts_code, amount=amount, cost=cost, note=note, portfolio=portfolio
        )
        return ok(result)

    @mcp.tool()
    @error_boundary
    def get_portfolio_status(
        portfolio: Annotated[
            str | None, Field(description="组合 id 或名称；省略则用当前组合。")
        ] = None,
    ) -> str:
        """按实时价格给组合估值：逐仓盈亏与合计。"""
        status = portfolio_store().status(provider(), portfolio=portfolio)
        status["provider"] = settings().provider
        return ok(status)

    @mcp.tool()
    @error_boundary
    def clear_portfolio(
        portfolio: Annotated[str | None, Field(description="组合 id 或名称。")] = None,
    ) -> str:
        """清空某个组合的全部持仓。"""
        return ok(portfolio_store().clear(portfolio=portfolio))

    # ------------------------------------------------------------------
    # 预警
    # ------------------------------------------------------------------
    @mcp.tool()
    @error_boundary
    def add_price_alert(
        ts_code: Annotated[str, Field(description="要监控的标的或名称。")],
        operator: Annotated[
            Literal[">", ">=", "<", "<=", "cross_up", "cross_down"] | None,
            Field(description="绝对价格阈值用的比较符；传 pct_change 时会被忽略。"),
        ] = None,
        threshold: Annotated[float | None, Field(description="绝对价格阈值。")] = None,
        pct_change: Annotated[
            float | None,
            Field(
                description="以百分比表示的相对触发条件（+5 表示上涨 5%，-5 表示下跌 5%），由服务端换算。"
            ),
        ] = None,
        note: Annotated[str, Field(description="这条预警的原因/备注。")] = "",
    ) -> str:
        """创建一条价格预警。

        `threshold` + `operator` 与 `pct_change` 二者择一。用户说「涨 5% 提醒我」时
        直接传 pct_change=5：服务端会读取现价并算出目标价，不需要模型做任何算术。
        """
        store = alert_store()
        alert = store.add(
            ts_code,
            operator or ">",
            threshold=threshold,
            pct_change=pct_change,
            note=note,
            provider=provider(),
        )
        return ok(alert.to_dict())

    @mcp.tool()
    @error_boundary
    def list_alerts(
        include_disabled: Annotated[bool, Field(description="是否包含已经触发过的预警。")] = True,
    ) -> str:
        """列出价格预警：阈值、状态与触发记录。"""
        alerts = alert_store().list(include_disabled=include_disabled)
        return ok({"count": len(alerts), "alerts": alerts})

    @mcp.tool()
    @error_boundary
    def update_alert(
        alert_id: Annotated[str, Field(description="预警 id，来自 list_alerts。")],
        operator: Annotated[str | None, Field(description="新的比较符。")] = None,
        threshold: Annotated[float | None, Field(description="新的绝对价格阈值。")] = None,
        note: Annotated[str | None, Field(description="新的备注。")] = None,
        enabled: Annotated[bool | None, Field(description="重新启用或暂停该预警。")] = None,
    ) -> str:
        """修改一条预警。改动阈值会让已触发过的预警重新生效。"""
        alert = alert_store().update(
            alert_id,
            operator=operator,
            threshold=threshold,
            note=note,
            enabled=enabled,
        )
        return ok(alert.to_dict())

    @mcp.tool()
    @error_boundary
    def remove_alert(
        alert_id: Annotated[str, Field(description="要删除的预警 id。")],
    ) -> str:
        """删除一条预警。"""
        return ok(alert_store().remove(alert_id))

    @mcp.tool()
    @error_boundary
    def check_alerts(
        trading_hours_only: Annotated[
            bool, Field(description="非 A 股交易时段是否跳过检查。")
        ] = False,
        notify: Annotated[bool, Field(description="是否对触发的预警发送通知。")] = True,
    ) -> str:
        """立即评估所有启用的预警，触发命中项。"""
        result = alert_store().check(
            provider(), notify=notify, trading_hours_only=trading_hours_only
        )
        return ok(result)

    @mcp.tool()
    @error_boundary
    def get_alert_history(
        limit: Annotated[int, Field(description="返回最近多少条触发记录。")] = 20,
    ) -> str:
        """最近已触发的预警。"""
        history = alert_store().history(limit=limit)
        return ok({"count": len(history), "history": history})

    # ------------------------------------------------------------------
    # 画像
    # ------------------------------------------------------------------
    @mcp.tool()
    @error_boundary
    def get_user_profile() -> str:
        """读取持久化的投资者画像（风险偏好、投资周期、板块偏好、风格）。"""
        store = profile_store()
        return ok({"profile": store.get(), "summary": store.summary()})

    @mcp.tool()
    @error_boundary
    def update_user_profile(
        risk_tolerance: Annotated[
            str | None,
            Field(
                description="'Conservative' | 'Balanced' | 'Aggressive'（也接受「保守/稳健/激进」等中文输入）。"
            ),
        ] = None,
        investment_horizon: Annotated[
            str | None,
            Field(
                description="'Short-term' | 'Medium-term' | 'Long-term'（也接受短期/中期/长期）。"
            ),
        ] = None,
        favorite_sectors: Annotated[
            list[str] | None, Field(description="投资者偏好的板块。")
        ] = None,
        avoid_sectors: Annotated[list[str] | None, Field(description="投资者回避的板块。")] = None,
        investment_style: Annotated[
            str | None, Field(description="自由文本风格描述，例如「只买高股息」。")
        ] = None,
        custom_preferences: Annotated[
            dict[str, Any] | None, Field(description="其他任意键值偏好。")
        ] = None,
        merge_sectors: Annotated[
            bool, Field(description="与已有板块列表合并，而不是整体替换。")
        ] = True,
    ) -> str:
        """持久化投资者偏好。用户明确表达或隐含表达偏好时调用。"""
        if (
            risk_tolerance is None
            and investment_horizon is None
            and favorite_sectors is None
            and avoid_sectors is None
            and investment_style is None
            and not custom_preferences
        ):
            raise InvalidArgument("没有要更新的字段，请至少提供一个。")
        store = profile_store()
        profile = store.update(
            risk_tolerance=risk_tolerance,
            investment_horizon=investment_horizon,
            favorite_sectors=favorite_sectors,
            avoid_sectors=avoid_sectors,
            investment_style=investment_style,
            custom_preferences=custom_preferences,
            merge_sectors=merge_sectors,
        )
        return ok({"profile": profile, "summary": store.summary()})

    @mcp.tool()
    @error_boundary
    def get_held_symbols() -> str:
        """当前持有的标的列表，便于做组合视角的分析或批量设置预警。"""
        store = portfolio_store()
        codes = store.held_codes()
        resolved = []
        for code in codes:
            try:
                _, name = resolve_strict(code)
            except Exception:  # noqa: BLE001
                name = code
            resolved.append({"ts_code": code, "name": name})
        return ok({"count": len(resolved), "symbols": resolved})

    return mcp


def main() -> None:
    serve(build_server, prog="finagent-mcp-portfolio")
