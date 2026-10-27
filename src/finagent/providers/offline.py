"""确定性的离线行情数据源。

为什么需要它
------------
没有 token、没有网络时，必须仍有东西能让整套工具面跑起来、可测试、可演示。这个 provider
按标的固定种子合成结构合理的数据，于是：

* 整个 MCP 工具面在 CI 里无需任何凭据即可被完整覆盖；
* LangGraph 图可以离线端到端演示；
* 数字跨运行、跨机器都稳定（不会出现飘忽的断言）。

它**不是**行情模拟器，而是具有真实形态的 fixture：几何随机游走的价格路径、OHLC 包络、
随价格变动的估值倍数、季度财报、资金流、宏观序列，以及拼接好的选股全市场表。
"""

from __future__ import annotations

import zlib
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from finagent.core import calendar as cal
from finagent.core.symbols import INDICES, UNIVERSE, Symbol
from finagent.providers.base import AssetClass, MacroIndicator, MarketDataProvider

_HISTORY_START = date(2018, 1, 2)

#: 每只标的的股本（亿股），在下面确定性生成。
_CONCEPT_MEMBERS: dict[str, tuple[str, ...]] = {
    "人工智能": ("688111.SH", "300124.SZ", "002415.SZ", "000063.SZ"),
    "新能源车": ("300750.SZ", "002594.SZ", "601012.SH", "300124.SZ"),
    "白酒": ("600519.SH", "000858.SZ", "000568.SZ", "002304.SZ"),
    "银行": (
        "000001.SZ",
        "600036.SH",
        "601398.SH",
        "601288.SH",
        "601988.SH",
        "600000.SH",
        "601166.SH",
        "600016.SH",
    ),
    "半导体": ("688981.SH", "688111.SH", "002475.SZ", "000725.SZ"),
    "医药": ("600276.SH", "300760.SZ"),
    "光伏": ("601012.SH", "300274.SZ"),
    "消费": ("600887.SH", "603288.SH", "601888.SH", "000333.SZ", "600690.SH"),
}

_HK_CATALOGUE = (
    ("00700.HK", "腾讯控股", 380.0),
    ("09988.HK", "阿里巴巴-W", 95.0),
    ("01810.HK", "小米集团-W", 18.5),
    ("00941.HK", "中国移动", 72.0),
    ("03690.HK", "美团-W", 120.0),
)

_US_CATALOGUE = (
    ("AAPL.O", "Apple Inc", 195.0),
    ("MSFT.O", "Microsoft Corp", 420.0),
    ("NVDA.O", "NVIDIA Corp", 130.0),
    ("TSLA.O", "Tesla Inc", 250.0),
    ("BABA.N", "Alibaba Group", 78.0),
)

_ETF_CATALOGUE = (
    ("510300.SH", "沪深300ETF", 3.9),
    ("510500.SH", "中证500ETF", 5.8),
    ("159915.SZ", "创业板ETF", 2.1),
    ("512880.SH", "证券ETF", 1.05),
    ("588000.SH", "科创50ETF", 0.95),
)

_CB_CATALOGUE = (
    ("113050.SH", "南银转债", 118.0),
    ("127045.SZ", "牧原转债", 121.5),
    ("110059.SH", "浦发转债", 108.2),
)

_FUTURES_CATALOGUE = (
    ("IF2409.CFX", "沪深300股指期货", 3900.0),
    ("IH2409.CFX", "上证50股指期货", 2650.0),
    ("AU2412.SHF", "沪金2412", 560.0),
    ("RB2410.SHF", "螺纹钢2410", 3400.0),
)

_GLOBAL_INDICES = (
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("000300.SH", "沪深300"),
    ("HSI.HK", "恒生指数"),
    ("SPX.US", "标普500"),
    ("IXIC.US", "纳斯达克"),
    ("N225.JP", "日经225"),
)


def _seed_for(key: str) -> int:
    return zlib.crc32(key.encode("utf-8")) & 0x7FFFFFFF


#: 知名标的的真实锚定价，让演示读起来自然。
_REFERENCE_PRICES: dict[str, tuple[float, float]] = {
    # ts_code: (价格, 股本亿股)
    "600519.SH": (1680.0, 12.56),
    "000858.SZ": (128.0, 38.82),
    "000568.SZ": (142.0, 14.71),
    "002304.SZ": (78.0, 15.06),
    "000001.SZ": (11.2, 194.06),
    "600036.SH": (36.5, 252.20),
    "601398.SH": (6.1, 3564.07),
    "601288.SH": (4.8, 3499.83),
    "601318.SH": (48.0, 182.10),
    "600030.SH": (24.5, 148.21),
    "300750.SZ": (186.0, 44.00),
    "002594.SZ": (245.0, 29.11),
    "601899.SH": (17.8, 265.77),
    "600900.SH": (28.6, 244.68),
    "000333.SZ": (68.0, 68.93),
    "600276.SH": (46.0, 63.79),
    "601888.SH": (72.0, 19.52),
    "600887.SH": (27.5, 63.65),
}

#: 符合行业特征的 PE / PB 区间，用于推导基本面。
_INDUSTRY_MULTIPLES: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {
    "银行": ((4.0, 8.0), (0.45, 1.10)),
    "保险": ((6.0, 12.0), (0.80, 1.80)),
    "证券": ((12.0, 25.0), (1.10, 2.20)),
    "白酒": ((20.0, 40.0), (3.50, 8.00)),
    "食品": ((18.0, 36.0), (2.50, 6.00)),
    "医药": ((25.0, 50.0), (3.00, 7.00)),
    "医疗器械": ((25.0, 48.0), (4.00, 8.00)),
    "半导体": ((30.0, 70.0), (3.00, 8.00)),
    "电子": ((22.0, 50.0), (2.00, 6.00)),
    "软件": ((30.0, 65.0), (4.00, 9.00)),
    "电力": ((12.0, 20.0), (1.60, 3.20)),
    "石油": ((6.0, 12.0), (0.70, 1.40)),
    "房地产": ((5.0, 12.0), (0.40, 1.00)),
    "建筑": ((5.0, 11.0), (0.50, 1.20)),
    "建材": ((6.0, 14.0), (0.60, 1.60)),
    "汽车": ((12.0, 28.0), (1.50, 3.60)),
    "电池": ((15.0, 35.0), (2.50, 6.00)),
    "光伏": ((10.0, 30.0), (1.20, 4.00)),
    "有色金属": ((8.0, 18.0), (1.40, 3.20)),
    "通信": ((14.0, 30.0), (1.20, 3.00)),
    "交通运输": ((8.0, 18.0), (1.00, 2.60)),
    "商业": ((12.0, 28.0), (1.80, 4.50)),
    "家电": ((10.0, 18.0), (1.80, 3.60)),
    "机械": ((14.0, 30.0), (1.80, 4.00)),
    "指数": ((12.0, 22.0), (1.20, 2.60)),
}

_INDUSTRY_BY_CODE = {symbol.ts_code: symbol.industry for symbol in UNIVERSE + INDICES}


def _profile(ts_code: str) -> dict[str, float]:
    """确定性的、符合行业特征的估值档案。"""

    rng = np.random.default_rng(_seed_for(ts_code))
    industry = _INDUSTRY_BY_CODE.get(ts_code, "")
    (pe_low, pe_high), (pb_low, pb_high) = _INDUSTRY_MULTIPLES.get(
        industry, ((10.0, 35.0), (1.20, 4.00))
    )

    reference = _REFERENCE_PRICES.get(ts_code)
    if reference:
        base_price, shares = reference
        base_price = float(np.round(base_price * float(rng.uniform(0.98, 1.02)), 2))
    else:
        base_price = float(np.round(np.exp(rng.uniform(np.log(4.0), np.log(320.0))), 2))
        # 由合理的市值倒推股本，让选股器看到的是真实的亿元分布，而不是随机的乘积。
        target_mv_yi = float(np.exp(rng.uniform(np.log(45.0), np.log(9_000.0))))
        shares = float(np.round(target_mv_yi / base_price, 2))

    target_pe = float(rng.uniform(pe_low, pe_high))
    target_pb = float(rng.uniform(pb_low, pb_high))
    eps = base_price / target_pe
    bvps = base_price / target_pb
    net_margin = float(np.round(rng.uniform(0.06, 0.42), 4))
    net_profit = eps * shares
    revenue = net_profit / net_margin

    return {
        "base_price": base_price,
        "drift": float(rng.uniform(-0.05, 0.18)),
        "volatility": float(rng.uniform(0.012, 0.032)),
        "shares": shares,
        "revenue": round(revenue, 2),
        "net_margin": net_margin,
        # 年化的每股口径，估值 fixture 与财报 fixture 共用，保证 PE/EPS/ROE 互相自洽。
        "eps": round(eps, 4),
        "bvps": round(bvps, 4),
        "net_profit": round(net_profit, 2),
    }


class OfflineProvider(MarketDataProvider):
    """fixture 数据源：确定性、零外部依赖、离线。"""

    name = "offline"
    CAPABILITIES = frozenset(
        {
            "stock_basic",
            "trade_calendar",
            "daily_price",
            "realtime_price",
            "daily_basic",
            "income_statement",
            "index_daily",
            "asset_basic",
            "asset_daily",
            "moneyflow",
            "limit_list",
            "top_list",
            "forecast",
            "concept_detail",
            "macro",
            "screener",
        }
    )

    def __init__(self) -> None:
        self._price_cache: dict[str, pd.DataFrame] = {}

    # ------------------------------------------------------------------
    # 序列生成
    # ------------------------------------------------------------------
    def _daily_series(
        self, ts_code: str, base_price: float, *, vol_scale: float = 1.0, drift_scale: float = 1.0
    ) -> pd.DataFrame:
        """某只标的的完整合成 OHLCV 历史，按日期升序。"""

        end = cal.latest_trading_day()
        days = cal.trading_days(_HISTORY_START, end)
        profile = _profile(ts_code)
        rng = np.random.default_rng(_seed_for(ts_code) ^ 0x5F3759DF)

        n = len(days)
        daily_ret = rng.normal(
            profile["drift"] / 252.0 * drift_scale,
            profile["volatility"] * vol_scale,
            n,
        )
        # 先做随机游走，再整体缩放使*最后一根* K 线等于 ``base_price``。
        # 这样今天的报价/PE/市值与基本面档案对齐，同时历史走势仍像真实价格路径。
        close = np.empty(n)
        level = base_price
        for idx in range(n):
            level = max(0.6, level * float(np.exp(daily_ret[idx])))
            close[idx] = level
        if close[-1] > 0:
            close = close * (base_price / close[-1])

        prev_close = np.concatenate(([close[0]], close[:-1]))
        gap = rng.normal(0.0, 0.004, n)
        open_ = np.maximum(0.5, prev_close * (1.0 + gap))
        spread = np.abs(rng.normal(0.0, 0.009, n)) + 0.002
        high = np.maximum(open_, close) * (1.0 + spread)
        low = np.minimum(open_, close) * (1.0 - spread)
        low = np.maximum(low, 0.3)

        turnover = np.abs(rng.normal(1.0, 0.35, n)) + 0.15
        vol = np.round(profile["shares"] * 1_000_000 * turnover / 100.0, 1)
        amount = np.round(vol * close * 100.0 / 1000.0, 2)

        frame = pd.DataFrame(
            {
                "ts_code": ts_code,
                "trade_date": [d.strftime("%Y%m%d") for d in days],
                "open": np.round(open_, 2),
                "high": np.round(high, 2),
                "low": np.round(low, 2),
                "close": np.round(close, 2),
                "pre_close": np.round(prev_close, 2),
                "vol": vol,
                "amount": amount,
            }
        )
        frame["pct_chg"] = np.round((frame["close"] / frame["pre_close"] - 1.0) * 100.0, 2)
        frame["change"] = np.round(frame["close"] - frame["pre_close"], 2)
        return frame

    def _history(self, ts_code: str) -> pd.DataFrame:
        cached = self._price_cache.get(ts_code)
        if cached is not None:
            return cached

        index_codes = {s.ts_code for s in INDICES}
        profile = _profile(ts_code)
        if ts_code in index_codes:
            frame = self._daily_series(ts_code, 3200.0, vol_scale=0.45, drift_scale=0.4)
        else:
            frame = self._daily_series(ts_code, profile["base_price"])
        self._price_cache[ts_code] = frame
        return frame

    def _synthetic_named_series(self, ts_code: str, base_price: float) -> pd.DataFrame:
        end = cal.latest_trading_day()
        days = cal.trading_days(date(end.year - 3, 1, 2), end)
        rng = np.random.default_rng(_seed_for(ts_code))
        n = len(days)
        ret = rng.normal(0.0002, 0.014, n)
        close = base_price * np.exp(np.cumsum(ret))
        if close[-1] > 0:
            close = close * (base_price / close[-1])
        prev_close = np.concatenate(([base_price], close[:-1]))
        spread = np.abs(rng.normal(0.0, 0.006, n)) + 0.001
        open_ = np.maximum(0.01, prev_close * (1 + rng.normal(0, 0.003, n)))
        frame = pd.DataFrame(
            {
                "ts_code": ts_code,
                "trade_date": [d.strftime("%Y%m%d") for d in days],
                "open": np.round(open_, 3),
                "high": np.round(np.maximum(open_, close) * (1 + spread), 3),
                "low": np.round(np.minimum(open_, close) * (1 - spread), 3),
                "close": np.round(close, 3),
                "pre_close": np.round(prev_close, 3),
                "vol": np.round(np.abs(rng.normal(2e6, 6e5, n)), 1),
                "amount": np.round(np.abs(rng.normal(8e5, 2e5, n)), 2),
            }
        )
        frame["pct_chg"] = np.round((frame["close"] / frame["pre_close"] - 1) * 100, 2)
        return frame

    @staticmethod
    def _slice(
        frame: pd.DataFrame,
        start_date: str | None,
        end_date: str | None,
    ) -> pd.DataFrame:
        if frame.empty:
            return frame
        mask = pd.Series(True, index=frame.index)
        if start_date:
            mask &= frame["trade_date"] >= cal.format_date(start_date)
        if end_date:
            mask &= frame["trade_date"] <= cal.format_date(end_date)
        return frame.loc[mask].reset_index(drop=True)

    # ------------------------------------------------------------------
    # 基础资料
    # ------------------------------------------------------------------
    def stock_basic(self, ts_code: str | None = None, name: str | None = None) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for symbol in UNIVERSE + INDICES:
            profile = _profile(symbol.ts_code)
            list_rng = np.random.default_rng(_seed_for(f"{symbol.ts_code}:listdate"))
            list_year = int(list_rng.integers(1996, 2021))
            list_month = int(list_rng.integers(1, 13))
            list_day = int(list_rng.integers(1, 29))
            rows.append(
                {
                    "ts_code": symbol.ts_code,
                    "symbol": symbol.bare,
                    "name": symbol.name,
                    "area": "深圳" if symbol.ts_code.endswith("SZ") else "上海",
                    "industry": symbol.industry,
                    "market": symbol.market,
                    "exchange": symbol.ts_code.split(".")[-1],
                    "list_date": f"{list_year}{list_month:02d}{list_day:02d}",
                    "total_share": profile["shares"],
                }
            )
        frame = pd.DataFrame(rows)
        if ts_code:
            frame = frame[frame["ts_code"] == ts_code]
        if name:
            frame = frame[frame["name"].str.contains(str(name), na=False)]
        return frame.reset_index(drop=True)

    def trade_calendar(self, start_date: str, end_date: str) -> pd.DataFrame:
        start = cal.parse_date(start_date)
        end = cal.parse_date(end_date)
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        return pd.DataFrame(
            {
                "exchange": "SSE",
                "cal_date": [d.strftime("%Y%m%d") for d in days],
                "is_open": [1 if cal.is_trading_day(d) else 0 for d in days],
            }
        )

    # ------------------------------------------------------------------
    # 行情
    # ------------------------------------------------------------------
    def daily_price(
        self,
        ts_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adj: str | None = None,
    ) -> pd.DataFrame:
        frame = self._history(ts_code)
        sliced = self._slice(frame, start_date, end_date)
        if adj in ("qfq", "hfq") and not sliced.empty:
            # 确定性复权：用稳定的因子整体缩放价格路径，保证复权后的序列内部自洽。
            factor = 1.0 + (_seed_for(f"{ts_code}:{adj}") % 1000) / 20000.0
            sliced = sliced.copy()
            for column in ("open", "high", "low", "close", "pre_close"):
                sliced[column] = np.round(sliced[column] * factor, 3)
        return sliced.reset_index(drop=True)

    def realtime_price(self, ts_code: str) -> dict[str, Any]:
        frame = self._history(ts_code)
        last = frame.iloc[-1]
        symbol_name = next((s.name for s in UNIVERSE + INDICES if s.ts_code == ts_code), ts_code)
        rng = np.random.default_rng(_seed_for(f"{ts_code}:tick"))
        tick = float(last["close"]) * (1.0 + float(rng.normal(0.0, 0.003)))
        pre_close = float(last["close"])
        return {
            "ts_code": ts_code,
            "name": symbol_name,
            "price": round(tick, 3),
            "pre_close": round(pre_close, 3),
            "change": round(tick - pre_close, 3),
            "pct_chg": round((tick / pre_close - 1.0) * 100.0, 3),
            "high": round(max(float(last["high"]), tick), 3),
            "low": round(min(float(last["low"]), tick), 3),
            "open": round(float(last["open"]), 3),
            "vol": float(last["vol"]),
            "amount": float(last["amount"]),
            "trade_date": str(last["trade_date"]),
            "source": "offline-demo",
        }

    def daily_basic(
        self, ts_code: str, start_date: str | None = None, end_date: str | None = None
    ) -> pd.DataFrame:
        prices = self.daily_price(ts_code, start_date, end_date)
        if prices.empty:
            return prices
        profile = _profile(ts_code)
        frame = prices[["ts_code", "trade_date", "close"]].copy()
        frame["pe"] = np.round(frame["close"] / profile["eps"], 2)
        frame["pe_ttm"] = np.round(frame["close"] / (profile["eps"] * 0.95), 2)
        frame["pb"] = np.round(frame["close"] / profile["bvps"], 2)
        ps_per_share = profile["revenue"] / profile["shares"]
        frame["ps"] = np.round(frame["close"] / ps_per_share, 2)
        frame["total_mv"] = np.round(frame["close"] * profile["shares"] * 10_000.0, 2)  # 万元
        frame["circ_mv"] = np.round(frame["total_mv"] * 0.82, 2)
        frame["turnover_rate"] = np.round(
            np.abs(np.random.default_rng(_seed_for(ts_code)).normal(1.6, 0.6, len(frame))), 3
        )
        return frame[
            [
                "ts_code",
                "trade_date",
                "close",
                "pe",
                "pe_ttm",
                "pb",
                "ps",
                "total_mv",
                "circ_mv",
                "turnover_rate",
            ]
        ]

    def income_statement(
        self, ts_code: str, start_date: str | None = None, end_date: str | None = None
    ) -> pd.DataFrame:
        profile = _profile(ts_code)
        next((s.name for s in UNIVERSE if s.ts_code == ts_code), ts_code)
        rng = np.random.default_rng(_seed_for(f"{ts_code}:is"))
        rows: list[dict[str, Any]] = []
        today = cal.today()
        for year in range(today.year - 4, today.year + 1):
            for month, dom in ((3, 31), (6, 30), (9, 30), (12, 31)):
                period = date(year, month, dom)
                if period > today:
                    continue
                growth = float(rng.normal(1.08, 0.16))
                # 取年度档案的四分之一，于是 EPS × 4 近似于估值 fixture 用的年度 EPS。
                revenue = profile["revenue"] * growth / 4.0
                net_profit = revenue * profile["net_margin"]
                equity = profile["bvps"] * profile["shares"]
                rows.append(
                    {
                        "ts_code": ts_code,
                        "ann_date": (period + timedelta(days=30)).strftime("%Y%m%d"),
                        "f_ann_date": (period + timedelta(days=30)).strftime("%Y%m%d"),
                        "end_date": period.strftime("%Y%m%d"),
                        "report_type": "1",
                        "revenue": round(revenue, 2),
                        "total_revenue": round(revenue * 1.02, 2),
                        "operate_profit": round(net_profit * 1.32, 2),
                        "n_income": round(net_profit, 2),
                        "n_income_attr_p": round(net_profit * 0.95, 2),
                        "basic_eps": round(net_profit / profile["shares"], 4),
                        "roe": round(net_profit / equity, 4),
                    }
                )
        frame = pd.DataFrame(rows)
        if frame.empty:
            return self.empty_with(
                ["ts_code", "ann_date", "f_ann_date", "end_date", "revenue", "n_income"]
            )
        frame = frame.sort_values("end_date", ascending=False).reset_index(drop=True)
        subset = self._slice(frame, None, end_date) if end_date else frame
        if start_date:
            subset = subset[subset["end_date"] >= cal.format_date(start_date)]
        return subset.reset_index(drop=True)

    def index_daily(
        self, ts_code: str, start_date: str | None = None, end_date: str | None = None
    ) -> pd.DataFrame:
        return self.daily_price(ts_code, start_date, end_date)

    # ------------------------------------------------------------------
    # 跨市场
    # ------------------------------------------------------------------
    def asset_basic(self, asset: AssetClass, keyword: str | None = None) -> pd.DataFrame:
        catalogue = {
            "hk": _HK_CATALOGUE,
            "us": _US_CATALOGUE,
            "etf": _ETF_CATALOGUE,
            "cb": _CB_CATALOGUE,
            "futures": _FUTURES_CATALOGUE,
        }[asset]
        rows = [
            {"ts_code": code, "name": label, "asset": asset, "last_price": price}
            for code, label, price in catalogue
        ]
        frame = pd.DataFrame(rows)
        if keyword:
            pattern = str(keyword)
            frame = frame[
                frame["ts_code"].str.contains(pattern, case=False, na=False)
                | frame["name"].str.contains(pattern, na=False)
            ]
        return frame.reset_index(drop=True)

    def asset_daily(
        self,
        asset: AssetClass,
        ts_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        catalogue = {
            "hk": _HK_CATALOGUE,
            "us": _US_CATALOGUE,
            "etf": _ETF_CATALOGUE,
            "cb": _CB_CATALOGUE,
            "futures": _FUTURES_CATALOGUE,
        }[asset]
        entry = next((row for row in catalogue if row[0] == ts_code), None)
        if entry is None:
            from finagent.errors import UnknownSymbol

            raise UnknownSymbol(
                f"'{ts_code}' 不在离线 {asset} 目录中。",
                hint="先调用 get_asset_basic 列出可用标的。",
            )
        frame = self._synthetic_named_series(ts_code, entry[2])
        return self._slice(frame, start_date, end_date)

    # ------------------------------------------------------------------
    # 资金与事件
    # ------------------------------------------------------------------
    def moneyflow(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        prices = self.daily_price(ts_code, start_date, end_date)
        if prices.empty:
            return prices
        rng = np.random.default_rng(_seed_for(f"{ts_code}:mf"))
        n = len(prices)
        scale = rng.uniform(2_000.0, 60_000.0)
        buy_lg = np.round(np.abs(rng.normal(scale, scale * 0.4, n)), 2)
        sell_lg = np.round(np.abs(rng.normal(scale, scale * 0.4, n)), 2)
        buy_elg = np.round(np.abs(rng.normal(scale * 0.6, scale * 0.3, n)), 2)
        sell_elg = np.round(np.abs(rng.normal(scale * 0.6, scale * 0.3, n)), 2)
        frame = prices[["ts_code", "trade_date"]].copy()
        frame["buy_lg_amount"] = buy_lg
        frame["sell_lg_amount"] = sell_lg
        frame["buy_elg_amount"] = buy_elg
        frame["sell_elg_amount"] = sell_elg
        frame["net_mf_amount"] = np.round(buy_lg + buy_elg - sell_lg - sell_elg, 2)
        return frame.sort_values("trade_date", ascending=False).reset_index(drop=True)

    def limit_list(self, trade_date: str) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        target = cal.format_date(trade_date)
        for symbol in UNIVERSE:
            frame = self._history(symbol.ts_code)
            match = frame[frame["trade_date"] == target]
            if match.empty:
                continue
            pct = float(match.iloc[0]["pct_chg"])
            if pct >= 4.0:
                rows.append(
                    {
                        "trade_date": target,
                        "ts_code": symbol.ts_code,
                        "name": symbol.name,
                        "industry": symbol.industry,
                        "close": float(match.iloc[0]["close"]),
                        "pct_chg": pct,
                        "limit": "U" if pct >= 9.8 else "Z",
                        "amount": float(match.iloc[0]["amount"]),
                    }
                )
        frame = pd.DataFrame(rows)
        return (
            frame.sort_values("pct_chg", ascending=False).reset_index(drop=True)
            if not frame.empty
            else self.empty_with(
                ["trade_date", "ts_code", "name", "industry", "close", "pct_chg", "limit", "amount"]
            )
        )

    def top_list(self, trade_date: str) -> pd.DataFrame:
        target = cal.format_date(trade_date)
        rows: list[dict[str, Any]] = []
        rng = np.random.default_rng(_seed_for(f"top:{target}"))
        picks = rng.choice(len(UNIVERSE), size=min(8, len(UNIVERSE)), replace=False)
        for index in picks:
            symbol: Symbol = UNIVERSE[int(index)]
            frame = self._history(symbol.ts_code)
            match = frame[frame["trade_date"] == target]
            if match.empty:
                continue
            rows.append(
                {
                    "trade_date": target,
                    "ts_code": symbol.ts_code,
                    "name": symbol.name,
                    "close": float(match.iloc[0]["close"]),
                    "pct_change": float(match.iloc[0]["pct_chg"]),
                    "turnover_rate": round(float(rng.uniform(2.0, 18.0)), 2),
                    "l_buy": round(float(rng.uniform(3_000.0, 90_000.0)), 2),
                    "l_sell": round(float(rng.uniform(2_000.0, 80_000.0)), 2),
                    "net_amount": round(float(rng.normal(0.0, 25_000.0)), 2),
                }
            )
        frame = pd.DataFrame(rows)
        return (
            frame.sort_values("net_amount", ascending=False).reset_index(drop=True)
            if not frame.empty
            else self.empty_with(
                ["trade_date", "ts_code", "name", "close", "pct_change", "net_amount"]
            )
        )

    def forecast(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        rng = np.random.default_rng(_seed_for(f"{ts_code}:fc"))
        today = cal.today()
        rows: list[dict[str, Any]] = []
        for offset in (30, 90, 180):
            ann = today - timedelta(days=offset)
            change = float(rng.normal(35.0, 45.0))
            kind = "预增" if change > 0 else "预减"
            rows.append(
                {
                    "ts_code": ts_code,
                    "ann_date": ann.strftime("%Y%m%d"),
                    "end_date": cal.quarter_end_before(ann).strftime("%Y%m%d"),
                    "type": kind,
                    "p_change_min": round(change * 0.8, 2),
                    "p_change_max": round(change * 1.2, 2),
                    "summary": f"预计净利润同比{kind}{abs(change):.0f}%左右",
                }
            )
        return pd.DataFrame(rows).sort_values("ann_date", ascending=False).reset_index(drop=True)

    def concept_detail(self, concept_name: str | None, ts_code: str | None) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for concept, members in _CONCEPT_MEMBERS.items():
            for member in members:
                symbol_name = next((s.name for s in UNIVERSE if s.ts_code == member), member)
                rows.append(
                    {
                        "concept_name": concept,
                        "ts_code": member,
                        "name": symbol_name,
                        "in_date": "20240101",
                    }
                )
        frame = pd.DataFrame(rows)
        if concept_name:
            frame = frame[frame["concept_name"].str.contains(str(concept_name), na=False)]
        if ts_code:
            frame = frame[frame["ts_code"] == ts_code]
        return frame.reset_index(drop=True)

    # ------------------------------------------------------------------
    # 宏观
    # ------------------------------------------------------------------
    def macro(self, indicator: MacroIndicator) -> pd.DataFrame:
        rng = np.random.default_rng(_seed_for(f"macro:{indicator}"))
        today = cal.today()

        if indicator == "gdp":
            rows = []
            for year in range(today.year - 6, today.year + 1):
                for quarter in (1, 2, 3, 4):
                    if date(year, quarter * 3, 28) > today:
                        continue
                    gdp = (900_000 + (year - 2018) * 55_000) * float(rng.uniform(0.96, 1.04))
                    rows.append(
                        {
                            "quarter": f"{year}Q{quarter}",
                            "gdp": round(gdp, 2),
                            "gdp_yoy": round(float(rng.normal(5.0, 1.2)), 2),
                            "secondary_yoy": round(float(rng.normal(5.4, 1.4)), 2),
                            "tertiary_yoy": round(float(rng.normal(5.2, 1.1)), 2),
                        }
                    )
            return pd.DataFrame(rows).iloc[::-1].reset_index(drop=True)

        if indicator == "cpi":
            rows = []
            for months_back in range(24):
                month = (today.replace(day=1) - timedelta(days=months_back * 30)).replace(day=1)
                rows.append(
                    {
                        "month": month.strftime("%Y%m"),
                        "cpi": round(float(rng.normal(101.6, 0.7)), 2),
                        "cpi_yoy": round(float(rng.normal(1.4, 0.7)), 2),
                    }
                )
            return pd.DataFrame(rows).reset_index(drop=True)

        if indicator == "m2":
            rows = []
            for months_back in range(24):
                month = (today.replace(day=1) - timedelta(days=months_back * 30)).replace(day=1)
                rows.append(
                    {
                        "month": month.strftime("%Y%m"),
                        "m2": round(
                            290_0000 + months_back * -3_500 + float(rng.normal(0, 1_500)), 2
                        ),
                        "m2_yoy": round(float(rng.normal(8.2, 1.1)), 2),
                        "m1_yoy": round(float(rng.normal(6.4, 1.6)), 2),
                    }
                )
            return pd.DataFrame(rows).reset_index(drop=True)

        if indicator == "interest_rate":
            rows = []
            for months_back in range(12):
                month = (today.replace(day=1) - timedelta(days=months_back * 30)).replace(day=1)
                rows.append(
                    {
                        "month": month.strftime("%Y%m"),
                        "shibor_on": round(float(rng.normal(1.62, 0.18)), 4),
                        "shibor_1y": round(float(rng.normal(2.35, 0.14)), 4),
                        "lpr_1y": round(float(rng.normal(3.45, 0.05)), 4),
                        "lpr_5y": round(float(rng.normal(3.95, 0.05)), 4),
                    }
                )
            return pd.DataFrame(rows).reset_index(drop=True)

        from finagent.errors import InvalidArgument

        raise InvalidArgument(
            f"未知宏观指标 '{indicator}'。",
            hint="可用：gdp、cpi、m2、interest_rate。",
        )

    def global_index_comparison(self) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for code, label in _GLOBAL_INDICES:
            rng = np.random.default_rng(_seed_for(f"global:{code}"))
            rows.append(
                {
                    "ts_code": code,
                    "name": label,
                    "close": round(float(rng.uniform(1_800, 42_000)), 2),
                    "pct_chg_1d": round(float(rng.normal(0.1, 0.9)), 2),
                    "pct_chg_5d": round(float(rng.normal(0.4, 2.1)), 2),
                    "pct_chg_20d": round(float(rng.normal(1.1, 4.2)), 2),
                    "pct_chg_ytd": round(float(rng.normal(3.0, 9.0)), 2),
                }
            )
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # 选股池
    # ------------------------------------------------------------------
    def screening_universe(self) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for symbol in UNIVERSE:
            profile = _profile(symbol.ts_code)
            frame = self._history(symbol.ts_code)
            last = frame.iloc[-1]
            close = float(last["close"])
            total_mv = close * profile["shares"] * 10_000.0
            rows.append(
                {
                    "ts_code": symbol.ts_code,
                    "name": symbol.name,
                    "industry": symbol.industry,
                    "market": symbol.market,
                    "close": round(close, 2),
                    "pct_chg": float(last["pct_chg"]),
                    "pe": round(close / profile["eps"], 2),
                    "pb": round(close / profile["bvps"], 2),
                    "total_mv": round(total_mv, 2),
                    "circ_mv": round(total_mv * 0.82, 2),
                    "turnover_rate": round(
                        float(
                            np.abs(
                                np.random.default_rng(_seed_for(f"{symbol.ts_code}:to")).normal(
                                    1.6, 0.6
                                )
                            )
                        ),
                        3,
                    ),
                    "amount": float(last["amount"]),
                    "list_date": "20100101",
                }
            )
        return pd.DataFrame(rows)


def catalogue_frame(asset: AssetClass) -> Iterable[dict[str, Any]]:
    """把 fixture 目录暴露给文档与测试。"""

    catalogue = {
        "hk": _HK_CATALOGUE,
        "us": _US_CATALOGUE,
        "etf": _ETF_CATALOGUE,
        "cb": _CB_CATALOGUE,
        "futures": _FUTURES_CATALOGUE,
    }[asset]
    for code, label, price in catalogue:
        yield {"ts_code": code, "name": label, "asset": asset, "last_price": price}
