"""模拟持仓台账。

支持多个命名组合、加仓时成本摊薄、减仓时结算已实现盈亏，以及通过注入的
provider 做实时估值（所以对接 Tushare 或离线 fixture 的估值逻辑完全一致）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from finagent.core import calendar as cal
from finagent.core.store import JsonStore, now_iso
from finagent.core.symbols import display_name, resolve_strict
from finagent.errors import InvalidArgument, NotFound
from finagent.providers.base import MarketDataProvider

DEFAULT_PORTFOLIO_ID = "default"


def _empty_document() -> dict[str, Any]:
    return {
        "active": DEFAULT_PORTFOLIO_ID,
        "portfolios": {
            DEFAULT_PORTFOLIO_ID: {
                "id": DEFAULT_PORTFOLIO_ID,
                "name": "默认组合",
                "created_at": now_iso(),
                "positions": [],
            }
        },
    }


class PortfolioStore:
    """模拟持仓的增删改查与估值。"""

    def __init__(self, path: Path | str) -> None:
        self._store = JsonStore(path, _empty_document())

    # ------------------------------------------------------------------
    # 文档读写管道
    # ------------------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        data = self._store.read()
        if not data.get("portfolios"):
            data = _empty_document()
        data.setdefault("active", DEFAULT_PORTFOLIO_ID)
        if data["active"] not in data["portfolios"]:
            data["active"] = next(iter(data["portfolios"]))
        return data

    def _mutate(self, mutator) -> dict[str, Any]:
        with self._store.transaction() as data:
            if not data.get("portfolios"):
                data.clear()
                data.update(_empty_document())
            mutator(data)
        return self._read()

    def _resolve(self, data: dict[str, Any], portfolio: str | None) -> dict[str, Any]:
        if not portfolio:
            return data["portfolios"][data["active"]]
        key = str(portfolio).strip()
        if key in data["portfolios"]:
            return data["portfolios"][key]
        for entry in data["portfolios"].values():
            if entry.get("name") == key:
                return entry
        raise NotFound(
            f"未找到组合 '{portfolio}'。",
            hint="调用 list_portfolios 查看可用 id。",
        )

    # ------------------------------------------------------------------
    # 组合
    # ------------------------------------------------------------------
    def list_portfolios(self) -> list[dict[str, Any]]:
        data = self._read()
        active = data["active"]
        return [
            {
                "id": entry["id"],
                "name": entry.get("name", entry["id"]),
                "positions": len(entry.get("positions", [])),
                "active": entry["id"] == active,
                "created_at": entry.get("created_at"),
            }
            for entry in data["portfolios"].values()
        ]

    def create_portfolio(self, name: str) -> dict[str, Any]:
        cleaned = str(name).strip()
        if not cleaned:
            raise InvalidArgument("组合名称不能为空。")
        created: dict[str, Any] = {}

        def mutate(data: dict[str, Any]) -> None:
            for entry in data["portfolios"].values():
                if entry.get("name") == cleaned:
                    raise InvalidArgument(f"组合 '{cleaned}' 已存在。")
            identifier = (
                cleaned
                if cleaned.isascii() and cleaned.isalnum()
                else f"pf{len(data['portfolios']) + 1}"
            )
            while identifier in data["portfolios"]:
                identifier += "_"
            entry = {
                "id": identifier,
                "name": cleaned,
                "created_at": now_iso(),
                "positions": [],
            }
            data["portfolios"][identifier] = entry
            created.update(entry)

        self._mutate(mutate)
        return {"id": created.get("id"), "name": cleaned}

    def rename_portfolio(self, portfolio_id: str, name: str) -> dict[str, Any]:
        cleaned = str(name).strip()
        if not cleaned:
            raise InvalidArgument("组合名称不能为空。")

        def mutate(data: dict[str, Any]) -> None:
            self._resolve(data, portfolio_id)["name"] = cleaned

        self._mutate(mutate)
        return {"id": portfolio_id, "name": cleaned}

    def delete_portfolio(self, portfolio_id: str) -> dict[str, Any]:
        def mutate(data: dict[str, Any]) -> None:
            entry = self._resolve(data, portfolio_id)
            if len(data["portfolios"]) == 1:
                raise InvalidArgument("不能删除唯一的组合。")
            data["portfolios"].pop(entry["id"], None)
            if data["active"] == entry["id"]:
                data["active"] = next(iter(data["portfolios"]))

        self._mutate(mutate)
        return {"deleted": portfolio_id}

    def set_active(self, portfolio_id: str) -> dict[str, Any]:
        def mutate(data: dict[str, Any]) -> None:
            data["active"] = self._resolve(data, portfolio_id)["id"]

        self._mutate(mutate)
        return {"active": portfolio_id}

    # ------------------------------------------------------------------
    # 持仓行
    # ------------------------------------------------------------------
    def _find_position(self, entry: dict[str, Any], ts_code: str) -> dict[str, Any] | None:
        for position in entry.get("positions", []):
            if position["ts_code"] == ts_code:
                return position
        return None

    def add_position(
        self,
        ts_code: str,
        amount: float,
        price: float,
        *,
        portfolio: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        code, name = resolve_strict(ts_code)
        shares = float(amount)
        cost = float(price)
        if shares <= 0:
            raise InvalidArgument("amount 必须为正数。")
        if cost <= 0:
            raise InvalidArgument("price 必须为正数。")

        snapshot: dict[str, Any] = {}

        def mutate(data: dict[str, Any]) -> None:
            entry = self._resolve(data, portfolio)
            existing = self._find_position(entry, code)
            if existing is None:
                position = {
                    "ts_code": code,
                    "name": name,
                    "amount": shares,
                    "cost": cost,
                    "bought_at": cal.format_date(cal.today()),
                    "note": note or "",
                    "updated_at": now_iso(),
                }
                entry.setdefault("positions", []).append(position)
                snapshot.update(position)
                snapshot["action"] = "created"
            else:
                total_shares = float(existing["amount"]) + shares
                total_cost = float(existing["amount"]) * float(existing["cost"]) + shares * cost
                existing["amount"] = round(total_shares, 4)
                existing["cost"] = round(total_cost / total_shares, 4) if total_shares else cost
                existing["updated_at"] = now_iso()
                if note:
                    existing["note"] = note
                snapshot.update(existing)
                snapshot["action"] = "increased"

        self._mutate(mutate)
        snapshot["portfolio_id"] = portfolio or DEFAULT_PORTFOLIO_ID
        return snapshot

    def remove_position(
        self,
        ts_code: str,
        amount: float | None = None,
        price: float | None = None,
        *,
        portfolio: str | None = None,
    ) -> dict[str, Any]:
        code, name = resolve_strict(ts_code)
        snapshot: dict[str, Any] = {}

        def mutate(data: dict[str, Any]) -> None:
            entry = self._resolve(data, portfolio)
            existing = self._find_position(entry, code)
            if existing is None:
                raise NotFound(f"该组合未持有 {code}。")
            held = float(existing["amount"])
            sell = held if amount is None else float(amount)
            if sell <= 0:
                raise InvalidArgument("amount 必须为正数。")
            if sell > held + 1e-9:
                raise InvalidArgument(
                    f"卖出 {sell} 股超过持仓 {held} 股。",
                    hint="请减少卖出数量，或核对持仓。",
                )
            realised = None
            if price is not None:
                realised = round((float(price) - float(existing["cost"])) * sell, 2)
            remaining = round(held - sell, 4)
            if remaining <= 1e-9:
                entry["positions"].remove(existing)
                snapshot["action"] = "closed"
            else:
                existing["amount"] = remaining
                existing["updated_at"] = now_iso()
                snapshot["action"] = "reduced"
            snapshot.update(
                {
                    "ts_code": code,
                    "name": name,
                    "sold": sell,
                    "remaining": max(remaining, 0.0),
                    "realised_pnl": realised,
                }
            )

        self._mutate(mutate)
        return snapshot

    def update_position(
        self,
        ts_code: str,
        *,
        amount: float | None = None,
        cost: float | None = None,
        note: str | None = None,
        portfolio: str | None = None,
    ) -> dict[str, Any]:
        code, _name = resolve_strict(ts_code)

        def mutate(data: dict[str, Any]) -> None:
            entry = self._resolve(data, portfolio)
            existing = self._find_position(entry, code)
            if existing is None:
                raise NotFound(f"该组合未持有 {code}。")
            if amount is not None:
                if float(amount) <= 0:
                    raise InvalidArgument("amount 必须为正数。")
                existing["amount"] = float(amount)
            if cost is not None:
                if float(cost) <= 0:
                    raise InvalidArgument("cost 必须为正数。")
                existing["cost"] = float(cost)
            if note is not None:
                existing["note"] = note
            existing["updated_at"] = now_iso()

        self._mutate(mutate)
        return {"ts_code": code, "updated": True}

    def clear(self, portfolio: str | None = None) -> dict[str, Any]:
        def mutate(data: dict[str, Any]) -> None:
            self._resolve(data, portfolio)["positions"] = []

        self._mutate(mutate)
        return {"cleared": True}

    # ------------------------------------------------------------------
    # 估值
    # ------------------------------------------------------------------
    def status(self, provider: MarketDataProvider, portfolio: str | None = None) -> dict[str, Any]:
        data = self._read()
        entry = self._resolve(data, portfolio)
        positions = entry.get("positions") or []
        if not positions:
            return {
                "portfolio": {"id": entry["id"], "name": entry.get("name")},
                "positions": [],
                "totals": {
                    "cost": 0.0,
                    "market_value": 0.0,
                    "pnl": 0.0,
                    "pnl_pct": 0.0,
                    "count": 0,
                },
            }

        rows: list[dict[str, Any]] = []
        total_cost = 0.0
        total_value = 0.0
        for position in positions:
            code = position["ts_code"]
            price: float | None = None
            as_of = None
            try:
                tick = provider.realtime_price(code)
                price = float(tick.get("price"))
                as_of = tick.get("trade_date")
            except Exception:  # noqa: BLE001 - a single missing quote must not break the book
                try:
                    frame = provider.daily_price(code)
                    if not frame.empty:
                        price = float(frame.iloc[-1]["close"])
                        as_of = str(frame.iloc[-1]["trade_date"])
                except Exception:  # noqa: BLE001
                    price = None

            amount = float(position["amount"])
            cost = float(position["cost"])
            cost_basis = amount * cost
            market_value = amount * price if price is not None else cost_basis
            pnl = market_value - cost_basis
            rows.append(
                {
                    "ts_code": code,
                    "name": position.get("name") or display_name(code),
                    "amount": round(amount, 4),
                    "cost": round(cost, 4),
                    "price": round(price, 3) if price is not None else None,
                    "as_of": as_of,
                    "cost_basis": round(cost_basis, 2),
                    "market_value": round(market_value, 2),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl / cost_basis * 100.0, 2) if cost_basis else 0.0,
                    "note": position.get("note", ""),
                }
            )
            total_cost += cost_basis
            total_value += market_value

        total_pnl = total_value - total_cost
        return {
            "portfolio": {"id": entry["id"], "name": entry.get("name")},
            "positions": sorted(rows, key=lambda row: row["market_value"], reverse=True),
            "totals": {
                "cost": round(total_cost, 2),
                "market_value": round(total_value, 2),
                "pnl": round(total_pnl, 2),
                "pnl_pct": round(total_pnl / total_cost * 100.0, 2) if total_cost else 0.0,
                "count": len(rows),
            },
        }

    def held_codes(self, portfolio: str | None = None) -> list[str]:
        data = self._read()
        entry = self._resolve(data, portfolio)
        return [position["ts_code"] for position in entry.get("positions", [])]
