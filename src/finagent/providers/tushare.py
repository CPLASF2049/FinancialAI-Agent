"""Tushare Pro 数据源。

``tushare`` 包推迟到第一次使用时才 import，所以即使没装 ``finagent[tushare]`` 这个
可选依赖，系统其余部分照常运行。所有 Tushare ``DataFrame`` 都会在离开本模块前被
规范化（日期列转 ``YYYYMMDD`` 字符串，``NaN`` 转 ``None``）。
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, cast

import pandas as pd

from finagent.core import calendar as cal
from finagent.core.symbols import UNIVERSE
from finagent.errors import ConfigurationError, ProviderError
from finagent.providers.base import AssetClass, MacroIndicator, MarketDataProvider

_PRICE_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"


def _clean(frame: pd.DataFrame) -> pd.DataFrame:
    """规范化 Tushare 的 DataFrame：丢弃全 NaN 列、NaN→None、日期→字符串。"""

    if frame is None or frame.empty:
        return pd.DataFrame()
    out = frame.copy()
    for column in ("trade_date", "cal_date", "ann_date", "f_ann_date", "end_date"):
        if column in out.columns:
            out[column] = out[column].astype(str)
    out = out.dropna(axis=1, how="all")
    return out.where(pd.notna(out), None)


class TushareProvider(MarketDataProvider):
    """Tushare Pro 实盘后端。"""

    name = "tushare"
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

    def __init__(self, token: str | None, *, requests_per_second: float = 4.0) -> None:
        if not token:
            raise ConfigurationError(
                "已选择 Tushare 数据源，但没有配置 token。",
                hint="设置 FINAGENT_TUSHARE_TOKEN（或 TUSHARE_TOKEN）后重试。",
            )
        self._token = token
        self._min_interval = 1.0 / max(requests_per_second, 0.5)
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._pro: Any = None

    # ------------------------------------------------------------------
    def _client(self) -> Any:
        if self._pro is not None:
            return self._pro
        try:
            import tushare as ts
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ConfigurationError(
                "未安装 'tushare' 包。",
                hint="执行 pip install 'finagent-mcp[tushare]'",
            ) from exc
        ts.set_token(self._token)
        self._pro = ts.pro_api()
        return self._pro

    def _throttle(self) -> None:
        with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _call(self, api: str, **kwargs: Any) -> pd.DataFrame:
        self._throttle()
        client = self._client()
        try:
            frame = getattr(client, api)(**kwargs)
        except Exception as exc:  # noqa: BLE001 - Tushare raises bare Exception
            raise ProviderError(
                f"Tushare 接口 '{api}' 调用失败：{exc}",
                hint="请检查 token 是否有效、积分/权限是否足够，以及参数格式（日期为 YYYYMMDD）。",
            ) from exc
        return _clean(frame) if isinstance(frame, pd.DataFrame) else pd.DataFrame()

    # ------------------------------------------------------------------
    # 基础资料
    # ------------------------------------------------------------------
    def stock_basic(self, ts_code: str | None = None, name: str | None = None) -> pd.DataFrame:
        kwargs: dict[str, Any] = {
            "fields": "ts_code,symbol,name,area,industry,market,"
            "exchange,list_date,total_share,act_name"
        }
        if ts_code:
            kwargs["ts_code"] = ts_code
        frame = self._call("stock_basic", **kwargs)
        if name and not frame.empty:
            frame = frame[frame["name"].str.contains(str(name), na=False)]
        return frame.reset_index(drop=True)

    def trade_calendar(self, start_date: str, end_date: str) -> pd.DataFrame:
        return self._call(
            "trade_cal",
            exchange="SSE",
            start_date=cal.format_date(start_date),
            end_date=cal.format_date(end_date),
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
        end = cal.format_date(end_date) if end_date else cal.format_date(cal.latest_trading_day())
        start = cal.format_date(start_date) if start_date else cal.warmup_start(end, 365)

        if adj in ("qfq", "hfq"):
            try:
                import tushare as ts
            except ImportError as exc:  # pragma: no cover
                raise ConfigurationError("未安装 tushare 包") from exc
            ts.set_token(self._token)
            self._throttle()
            try:
                frame = ts.pro_bar(ts_code=ts_code, start_date=start, end_date=end, adj=adj)
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(f"tushare.pro_bar 调用失败：{exc}") from exc
            frame = _clean(frame)
        else:
            frame = self._call(
                "daily", ts_code=ts_code, start_date=start, end_date=end, fields=_PRICE_FIELDS
            )

        if frame.empty:
            return frame
        return frame.sort_values("trade_date", ascending=True).reset_index(drop=True)

    def realtime_price(self, ts_code: str) -> dict[str, Any]:
        frame = self.daily_price(ts_code, cal.warmup_start(cal.format_date(cal.today()), 30))
        basic = self.stock_basic(ts_code=ts_code)
        name = str(basic.iloc[0]["name"]) if not basic.empty else ts_code
        if frame.empty:
            raise ProviderError(f"{ts_code} 没有历史行情数据。")
        last = frame.iloc[-1]
        return {
            "ts_code": ts_code,
            "name": name,
            "price": float(last["close"]),
            "pre_close": float(last.get("pre_close") or last["close"]),
            "change": float(last.get("change") or 0.0),
            "pct_chg": float(last.get("pct_chg") or 0.0),
            "high": float(last["high"]),
            "low": float(last["low"]),
            "open": float(last["open"]),
            "vol": float(last["vol"]),
            "amount": float(last["amount"]),
            "trade_date": str(last["trade_date"]),
            "source": "tushare:eod",
        }

    def daily_basic(
        self, ts_code: str, start_date: str | None = None, end_date: str | None = None
    ) -> pd.DataFrame:
        end = cal.format_date(end_date) if end_date else cal.format_date(cal.today())
        start = cal.format_date(start_date) if start_date else cal.warmup_start(end, 365)
        frame = self._call(
            "daily_basic",
            ts_code=ts_code,
            start_date=start,
            end_date=end,
            fields="ts_code,trade_date,close,pe,pe_ttm,pb,ps,total_mv,circ_mv,turnover_rate",
        )
        if frame.empty:
            return frame
        return frame.sort_values("trade_date", ascending=True).reset_index(drop=True)

    def income_statement(
        self, ts_code: str, start_date: str | None = None, end_date: str | None = None
    ) -> pd.DataFrame:
        end = cal.format_date(end_date) if end_date else cal.format_date(cal.today())
        start = cal.format_date(start_date) if start_date else cal.warmup_start(end, 1100)
        frame = self._call(
            "income",
            ts_code=ts_code,
            start_date=start,
            end_date=end,
            fields="ts_code,ann_date,f_ann_date,end_date,report_type,revenue,total_revenue,"
            "operate_profit,n_income,n_income_attr_p,basic_eps",
        )
        if frame.empty:
            return frame
        return frame.sort_values("end_date", ascending=False).reset_index(drop=True)

    def index_daily(
        self, ts_code: str, start_date: str | None = None, end_date: str | None = None
    ) -> pd.DataFrame:
        end = cal.format_date(end_date) if end_date else cal.format_date(cal.today())
        start = cal.format_date(start_date) if start_date else cal.warmup_start(end, 365)
        frame = self._call("index_daily", ts_code=ts_code, start_date=start, end_date=end)
        if frame.empty:
            return frame
        return frame.sort_values("trade_date", ascending=True).reset_index(drop=True)

    # ------------------------------------------------------------------
    # 跨市场
    # ------------------------------------------------------------------
    _ASSET_APIS: dict[AssetClass, tuple[str, str]] = {
        "hk": ("hk_basic", "hk_daily"),
        "us": ("us_basic", "us_daily"),
        "etf": ("fund_basic", "fund_daily"),
        "cb": ("cb_basic", "cb_daily"),
        "futures": ("fut_basic", "fut_daily"),
    }

    def asset_basic(self, asset: AssetClass, keyword: str | None = None) -> pd.DataFrame:
        basic_api, _ = self._ASSET_APIS[asset]
        kwargs: dict[str, Any] = {}
        if asset == "etf":
            kwargs["market"] = "E"
        frame = self._call(basic_api, **kwargs)
        if keyword and not frame.empty:
            pattern = str(keyword)
            mask = pd.Series(False, index=frame.index)
            for column in ("ts_code", "name"):
                if column in frame.columns:
                    mask |= frame[column].astype(str).str.contains(pattern, case=False, na=False)
            frame = frame[mask]
        return frame.head(100).reset_index(drop=True)

    def asset_daily(
        self,
        asset: AssetClass,
        ts_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        _, daily_api = self._ASSET_APIS[asset]
        end = cal.format_date(end_date) if end_date else cal.format_date(cal.today())
        start = cal.format_date(start_date) if start_date else cal.warmup_start(end, 365)
        kwargs: dict[str, Any] = {
            "ts_code": ts_code,
            "start_date": start,
            "end_date": end,
        }
        if asset == "futures":
            kwargs.pop("ts_code")
            kwargs["trade_date"] = end
        frame = self._call(daily_api, **kwargs)
        if frame.empty:
            return frame
        if "trade_date" in frame.columns:
            frame = frame.sort_values("trade_date", ascending=True)
        return frame.reset_index(drop=True)

    # ------------------------------------------------------------------
    # 资金与事件
    # ------------------------------------------------------------------
    def moneyflow(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        frame = self._call(
            "moneyflow",
            ts_code=ts_code,
            start_date=cal.format_date(start_date),
            end_date=cal.format_date(end_date),
        )
        if frame.empty:
            return frame
        return frame.sort_values("trade_date", ascending=False).reset_index(drop=True)

    def limit_list(self, trade_date: str) -> pd.DataFrame:
        return self._call("limit_list_d", trade_date=cal.format_date(trade_date))

    def top_list(self, trade_date: str) -> pd.DataFrame:
        return self._call("top_list", trade_date=cal.format_date(trade_date))

    def forecast(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        frame = self._call(
            "forecast",
            ts_code=ts_code,
            start_date=cal.format_date(start_date),
            end_date=cal.format_date(end_date),
        )
        if frame.empty:
            return frame
        return frame.sort_values("ann_date", ascending=False).reset_index(drop=True)

    def concept_detail(self, concept_name: str | None, ts_code: str | None) -> pd.DataFrame:
        if not concept_name and not ts_code:
            from finagent.errors import InvalidArgument

            raise InvalidArgument("请提供 concept_name 或 ts_code 之一。")
        kwargs: dict[str, Any] = {}
        if concept_name:
            concepts = self._call("concept", src="ts")
            if concepts.empty:
                return concepts
            matched = concepts[concepts["name"].str.contains(str(concept_name), na=False)]
            if matched.empty:
                return pd.DataFrame()
            kwargs["id"] = str(matched.iloc[0]["code"])
        else:
            kwargs["ts_code"] = ts_code
        return self._call("concept_detail", **kwargs)

    # ------------------------------------------------------------------
    # 宏观
    # ------------------------------------------------------------------
    def macro(self, indicator: MacroIndicator) -> pd.DataFrame:
        if indicator == "gdp":
            return self._call("cn_gdp")
        if indicator == "cpi":
            return self._call("cn_cpi")
        if indicator == "m2":
            return self._call("cn_m")
        if indicator == "interest_rate":
            shibor = self._call("shibor")
            lpr = self._call("lpr_data") if self._has_api("lpr_data") else pd.DataFrame()
            if lpr is None or lpr.empty:
                return shibor
            return pd.concat([shibor, lpr], axis=1)
        from finagent.errors import InvalidArgument

        raise InvalidArgument(f"未知宏观指标 '{indicator}'。")

    def _has_api(self, name: str) -> bool:
        try:
            return hasattr(self._client(), name)
        except Exception:  # noqa: BLE001
            return False

    def global_index_comparison(self) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        universe = [
            ("000001.SH", "上证指数"),
            ("399001.SZ", "深证成指"),
            ("399006.SZ", "创业板指"),
            ("000300.SH", "沪深300"),
            ("HSI", "恒生指数"),
        ]
        for code, label in universe:
            try:
                frame = self.index_daily(code, cal.warmup_start(cal.format_date(cal.today()), 60))
            except ProviderError:
                continue
            if frame is None or frame.empty or len(frame) < 2:
                continue
            closes = frame["close"].astype(float).tolist()
            rows.append(
                {
                    "ts_code": code,
                    "name": label,
                    "close": closes[-1],
                    "pct_chg_1d": round((closes[-1] / closes[-2] - 1) * 100, 2),
                    "pct_chg_5d": round((closes[-1] / closes[-6] - 1) * 100, 2)
                    if len(closes) >= 6
                    else None,
                    "pct_chg_20d": round((closes[-1] / closes[-21] - 1) * 100, 2)
                    if len(closes) >= 21
                    else None,
                }
            )
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # 选股器
    # ------------------------------------------------------------------
    def screening_universe(self) -> pd.DataFrame:
        """把最新交易日的 daily_basic 快照与基础资料拼在一起。"""

        anchor = cal.latest_trading_day()
        frame = pd.DataFrame()
        for back in range(0, 8):
            probe = anchor - timedelta(days=back)
            if not cal.is_trading_day(probe):
                continue
            candidate = self._call(
                "daily_basic",
                trade_date=probe.strftime("%Y%m%d"),
                fields="ts_code,close,pe,pe_ttm,pb,ps,total_mv,circ_mv,turnover_rate",
            )
            if not candidate.empty:
                frame = candidate
                anchor = probe
                break
        if frame.empty:
            return frame

        basics = self._call(
            "stock_basic",
            fields="ts_code,name,industry,market,exchange,list_date",
            list_status="L",
        )
        merged = frame.merge(basics, on="ts_code", how="left")
        daily = self._call(
            "daily", trade_date=anchor.strftime("%Y%m%d"), fields="ts_code,close,amount,pct_chg"
        )
        if not daily.empty:
            merged = merged.merge(daily[["ts_code", "amount", "pct_chg"]], on="ts_code", how="left")
        return merged

    # ------------------------------------------------------------------
    def _daily_basic_series(self, ts_code: str) -> pd.DataFrame:  # pragma: no cover
        return self.daily_basic(
            ts_code, start_date=cal.warmup_start(cal.format_date(cal.today()), 10)
        )


def catalogue() -> list[str]:
    """已知的离线标的，用于诊断。"""

    return [symbol.ts_code for symbol in UNIVERSE]


_ = (date, datetime, cast)  # keep imports meaningful for typing tools
