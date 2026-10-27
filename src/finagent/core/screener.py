"""基本面 + 技术面选股。

三个约定：

* :class:`ScreenCriteria` 是一个带类型的过滤描述（也正是 LLM 要填的东西），市值阈值
  全程使用**亿元**；
* :func:`screen` 是在 provider 选股池上的纯函数，取数、过滤、格式化互不纠缠；
* :func:`long_tail` 复用它，再叠加「横盘」与「放量」两类进阶检查。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from finagent.errors import InvalidArgument
from finagent.providers.base import MarketDataProvider

SORT_FIELDS = {
    "total_mv": False,
    "pe": True,
    "pb": True,
    "pct_chg": False,
    "turnover_rate": True,
    "amount": False,
    "close": False,
}

_COLUMN_LABELS = {
    "ts_code": "代码",
    "name": "名称",
    "industry": "行业",
    "close": "最新价",
    "pct_chg": "涨跌幅",
    "pe": "市盈率",
    "pb": "市净率",
    "total_mv_yi": "总市值(亿)",
    "turnover_rate": "换手率",
}


@dataclass(slots=True)
class ScreenCriteria:
    """:func:`screen` 的过滤描述。

    所有市值参数的单位都是亿元（100  million CNY）——也就是用户实际说的那个单位。
    """

    pe_min: float | None = None
    pe_max: float | None = None
    pb_min: float | None = None
    pb_max: float | None = None
    mv_min: float | None = None
    mv_max: float | None = None
    turnover_min: float | None = None
    turnover_max: float | None = None
    pct_chg_min: float | None = None
    pct_chg_max: float | None = None
    industries: list[str] = field(default_factory=list)
    exclude_industries: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    require_profit: bool = True
    sort_by: str = "total_mv"
    ascending: bool | None = None
    limit: int = 20

    def describe(self) -> str:
        parts: list[str] = []
        if self.pe_max is not None:
            parts.append(f"PE<= {self.pe_max:g}")
        if self.pe_min is not None:
            parts.append(f"PE>= {self.pe_min:g}")
        if self.pb_max is not None:
            parts.append(f"PB<= {self.pb_max:g}")
        if self.mv_min is not None:
            parts.append(f"市值>= {self.mv_min:g}亿")
        if self.mv_max is not None:
            parts.append(f"市值<= {self.mv_max:g}亿")
        if self.industries:
            parts.append("行业∈ " + "/".join(self.industries))
        if self.turnover_max is not None:
            parts.append(f"换手<= {self.turnover_max:g}%")
        return ", ".join(parts) or "无额外条件"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _match_industry(frame: pd.DataFrame, industries: list[str]) -> pd.Series:
    mask = pd.Series(False, index=frame.index)
    for industry in industries:
        mask |= frame["industry"].astype(str).str.contains(str(industry), na=False, regex=False)
    return mask


def screen(provider: MarketDataProvider, criteria: ScreenCriteria) -> dict[str, Any]:
    """把 ``criteria`` 应用到 provider 的选股池上。"""

    if criteria.limit <= 0:
        raise InvalidArgument("limit 必须为正数。")
    if criteria.sort_by not in SORT_FIELDS and criteria.sort_by != "none":
        raise InvalidArgument(
            f"无法按 '{criteria.sort_by}' 排序。",
            hint=f"可用字段：{', '.join(sorted(SORT_FIELDS))}",
        )

    universe = provider.screening_universe()
    if universe is None or universe.empty:
        raise InvalidArgument("数据源返回的选股池为空。")

    frame = universe.copy()
    frame["total_mv_yi"] = frame["total_mv"] / 10_000.0
    total_before = len(frame)

    def apply(mask: pd.Series, label: str) -> None:
        nonlocal frame
        frame = frame[mask]

    if criteria.require_profit:
        apply(frame["pe"].notna() & (frame["pe"] > 0), "pe>0")
    if criteria.pe_min is not None:
        apply(frame["pe"] >= float(criteria.pe_min), "pe_min")
    if criteria.pe_max is not None:
        apply(frame["pe"] <= float(criteria.pe_max), "pe_max")
    if criteria.pb_min is not None:
        apply(frame["pb"] >= float(criteria.pb_min), "pb_min")
    if criteria.pb_max is not None:
        apply(frame["pb"] <= float(criteria.pb_max), "pb_max")
    if criteria.mv_min is not None:
        apply(frame["total_mv_yi"] >= float(criteria.mv_min), "mv_min")
    if criteria.mv_max is not None:
        apply(frame["total_mv_yi"] <= float(criteria.mv_max), "mv_max")
    if criteria.turnover_min is not None:
        apply(frame["turnover_rate"] >= float(criteria.turnover_min), "turnover_min")
    if criteria.turnover_max is not None:
        apply(frame["turnover_rate"] <= float(criteria.turnover_max), "turnover_max")
    if criteria.pct_chg_min is not None:
        apply(frame["pct_chg"] >= float(criteria.pct_chg_min), "pct_chg_min")
    if criteria.pct_chg_max is not None:
        apply(frame["pct_chg"] <= float(criteria.pct_chg_max), "pct_chg_max")
    if criteria.industries:
        apply(_match_industry(frame, criteria.industries), "industries")
    if criteria.exclude_industries:
        apply(~_match_industry(frame, criteria.exclude_industries), "exclude_industries")
    if criteria.symbols:
        codes = {str(code).upper() for code in criteria.symbols}
        apply(frame["ts_code"].str.upper().isin(codes), "symbols")

    ascending = criteria.ascending
    if ascending is None and criteria.sort_by in SORT_FIELDS:
        ascending = SORT_FIELDS[criteria.sort_by]
    if criteria.sort_by != "none" and not frame.empty:
        frame = frame.sort_values(criteria.sort_by, ascending=bool(ascending))

    rows = frame.head(criteria.limit)
    records: list[dict[str, Any]] = []
    for row in rows.to_dict("records"):
        records.append(
            {
                "ts_code": row["ts_code"],
                "name": row["name"],
                "industry": row["industry"],
                "close": _round(row.get("close"), 3),
                "pct_chg": _round(row.get("pct_chg"), 2),
                "pe": _round(row.get("pe"), 2),
                "pb": _round(row.get("pb"), 2),
                "total_mv_yi": _round(row.get("total_mv_yi"), 2),
                "turnover_rate": _round(row.get("turnover_rate"), 3),
            }
        )

    return {
        "criteria": criteria.describe(),
        "universe_size": total_before,
        "matched": len(frame),
        "returned": len(records),
        "results": records,
        "columns": _COLUMN_LABELS,
    }


def long_tail(
    provider: MarketDataProvider,
    *,
    mv_min: float = 10.0,
    mv_max: float = 200.0,
    pe_max: float = 40.0,
    pb_max: float = 5.0,
    turnover_max: float = 3.0,
    check_consolidation: bool = False,
    check_volume_spike: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    """被低估且被忽视的中小市值标的。

    ``check_consolidation`` 保留 60 日价格变异系数小于 15% 的标的；
    ``check_volume_spike`` 保留最新成交量至少是前 20 日均量 2 倍的标的。
    """

    criteria = ScreenCriteria(
        mv_min=mv_min,
        mv_max=mv_max,
        pe_max=pe_max,
        pb_max=pb_max,
        turnover_max=turnover_max,
        sort_by="turnover_rate",
        ascending=True,
        limit=60 if (check_consolidation or check_volume_spike) else limit,
    )
    base = screen(provider, criteria)
    candidates = base["results"]

    if not candidates:
        return {**base, "filters_applied": [], "note": "no candidates before advanced filters"}

    filters_applied: list[str] = []
    if check_consolidation or check_volume_spike:
        kept: list[dict[str, Any]] = []
        for candidate in candidates:
            frame = provider.daily_price(candidate["ts_code"])
            if frame is None or len(frame) < 20:
                continue
            usable = True
            if check_consolidation:
                window = frame.tail(60)["close"]
                mean = float(window.mean())
                if mean and float(window.std()) / mean > 0.15:
                    usable = False
                filters_applied.append("60日价格变异系数 < 15%")
            if usable and check_volume_spike:
                latest = float(frame.iloc[-1]["vol"])
                average = float(frame.iloc[-21:-1]["vol"].mean()) if len(frame) > 21 else 0.0
                if average <= 0 or latest / average < 2.0:
                    usable = False
                filters_applied.append("最新成交量 >= 20日均量 2倍")
            if usable:
                kept.append(candidate)
        candidates = kept

    filters_applied = list(dict.fromkeys(filters_applied))
    return {
        "criteria": base["criteria"],
        "universe_size": base["universe_size"],
        "matched": len(candidates),
        "returned": len(candidates[:limit]),
        "results": candidates[:limit],
        "advanced_filters": filters_applied,
        "columns": base["columns"],
    }


def _round(value: Any, digits: int) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)
