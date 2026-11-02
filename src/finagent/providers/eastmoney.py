"""东方财富行情数据源：**免费、免 token 的真实 A 股数据**。

为什么加它
----------
Tushare Pro 需要 token，而且不少接口按积分放行。东方财富的行情接口是公开的，注册都不用，
拿到的就是真实盘口与历史 K 线——对「我只想看真数据」的场景，这是成本最低的一条路。
（akshare 这类库本质也是取同一批接口，这里直接调，省掉一个重依赖。）

能提供什么
----------
* 日线 OHLCV（前复权，``fqt=1``）
* 实时快照（最新价/涨跌/量额/PE/PB/市值）
* 最新估值行（PE、PB、总市值、流通市值、换手率）
* 指数日线
* 全市场选股宽表（约 5500 只，含行业、PE、PB、市值、换手率）
* 交易日历（由指数 K 线反推）

**不能**提供：利润表、宏观、资金流、龙虎榜、涨跌停、业绩预告、跨市场标的。这些方法沿用
基类的 ``ProviderUnavailable``，工具会返回明确错误而不是编造数字——这比「假装有数据」
重要得多。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import pandas as pd

from finagent.logging import get_logger
from finagent.providers.base import MarketDataProvider

log = get_logger("providers.eastmoney")

_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"

#: A 股全市场（沪 + 深 + 北交所）。
_A_SHARE_FILTER = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

#: 全市场列表缓存时长（秒）。5500 行的请求不便宜，但也不能太旧。
_UNIVERSE_TTL = 600.0
#: 两次请求之间的最小间隔，礼貌一点，也降低被限流的概率。
_MIN_INTERVAL = 0.22


class EastmoneyProvider(MarketDataProvider):
    """基于东方财富公开接口的真实数据源。"""

    name = "eastmoney"

    CAPABILITIES = frozenset(
        {
            "stock_basic",
            "trade_calendar",
            "daily_price",
            "realtime_price",
            "daily_basic",
            "index_daily",
            "screener",
        }
    )

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self._last_request = 0.0
        self._universe_cache: tuple[float, pd.DataFrame] | None = None
        #: 上一次全市场拉取是否完整。选股结果据此决定要不要提示「可能不全」。
        self.last_universe_complete: bool | None = None

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def _http_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        # 简单节流：连续请求之间留一点间隔
        gap = time.monotonic() - self._last_request
        if gap < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - gap)
        self._last_request = time.monotonic()

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Referer": "https://quote.eastmoney.com/",
                "Accept": "*/*",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            from finagent.errors import ProviderUnavailable

            raise ProviderUnavailable(
                f"东方财富接口不可达：{exc}",
                hint="检查网络；离线环境请把 FINAGENT_PROVIDER 设为 offline。",
            ) from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            from finagent.errors import ProviderUnavailable

            raise ProviderUnavailable(f"东方财富返回了非 JSON 内容：{raw[:80]!r}") from exc

        if not isinstance(payload, dict):
            from finagent.errors import ProviderUnavailable

            raise ProviderUnavailable("东方财富返回格式异常。")
        return payload

    @staticmethod
    def _secid(ts_code: str) -> str:
        """``600519.SH`` → ``1.600519``；沪市 1、深市/北交所 0、港股 116。"""

        code, _, suffix = str(ts_code).partition(".")
        suffix = suffix.upper()
        if suffix == "SH":
            return f"1.{code}"
        if suffix in ("SZ", "BJ"):
            return f"0.{code}"
        if suffix == "HK":
            return f"116.{code}"
        # 没有后缀时按首位猜：6/9 开头是沪市，其余按深市
        return f"1.{code}" if code.startswith(("6", "9", "5")) else f"0.{code}"

    # ------------------------------------------------------------------
    # 基础资料
    # ------------------------------------------------------------------
    def stock_basic(self, ts_code: str | None = None, name: str | None = None) -> pd.DataFrame:
        frame = self.screening_universe()
        if frame.empty:
            return frame
        columns = ["ts_code", "symbol", "name", "area", "industry", "market", "list_date"]
        result = frame.assign(
            symbol=frame["ts_code"].str.split(".").str[0],
            area="",
            market=frame["ts_code"].str.split(".").str[1],
        )
        if ts_code:
            result = result[result["ts_code"] == ts_code]
        if name:
            result = result[result["name"].str.contains(name, na=False)]
        return result[[c for c in columns if c in result.columns]].reset_index(drop=True)

    def trade_calendar(self, start_date: str, end_date: str) -> pd.DataFrame:
        """用上证指数的 K 线反推交易日：有 K 线的日子就是开市日。"""

        frame = self.index_daily("000001.SH", start_date, end_date)
        if frame.empty:
            return self.empty_with(["exchange", "cal_date", "is_open", "pretrade_date"])

        dates = sorted(frame["trade_date"].astype(str).tolist())
        previous = {dates[i]: dates[i - 1] for i in range(1, len(dates))}
        return pd.DataFrame(
            {
                "exchange": "SSE",
                "cal_date": dates,
                "is_open": 1,
                "pretrade_date": [previous.get(day, "") for day in dates],
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
        params = {
            "secid": self._secid(ts_code),
            "klt": "101",  # 日线
            "fqt": "0" if adj == "none" else "1",  # 默认前复权
            "beg": (start_date or "19900101").replace("-", ""),
            "end": (end_date or "20500101").replace("-", ""),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        }
        payload = self._http_json(_KLINE_URL, params)
        data = payload.get("data") or {}
        lines = data.get("klines") or []
        if not lines:
            return self.empty_with(
                [
                    "ts_code",
                    "trade_date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "pre_close",
                    "vol",
                    "amount",
                    "pct_chg",
                    "change",
                ]
            )

        rows: list[dict[str, Any]] = []
        for line in lines:
            parts = str(line).split(",")
            if len(parts) < 7:
                continue
            rows.append(
                {
                    "trade_date": parts[0].replace("-", ""),
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "vol": float(parts[5]),  # 手
                    "amount": float(parts[6]),  # 元
                }
            )

        frame = pd.DataFrame(rows).sort_values("trade_date").reset_index(drop=True)
        frame["ts_code"] = ts_code
        frame["pre_close"] = frame["close"].shift(1)
        frame.loc[0, "pre_close"] = frame.loc[0, "open"]
        frame["change"] = (frame["close"] - frame["pre_close"]).round(4)
        frame["pct_chg"] = (frame["change"] / frame["pre_close"] * 100).round(4)
        return frame[
            [
                "ts_code",
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "pre_close",
                "vol",
                "amount",
                "pct_chg",
                "change",
            ]
        ]

    def _quote(self, ts_code: str) -> dict[str, Any]:
        params = {
            "secid": self._secid(ts_code),
            "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f116,f117,f162,f167,f168,f169,f170",
            "fltt": "2",
            "invt": "2",
        }
        payload = self._http_json(_QUOTE_URL, params)
        data = payload.get("data")
        if not data:
            from finagent.errors import ProviderUnavailable

            raise ProviderUnavailable(
                f"东方财富没有返回 {ts_code} 的行情（代码可能不存在或已退市）。",
                hint="用 resolve_symbol 确认代码；科创板/北交所请在代码后加 .SH/.BJ。",
            )
        return data

    @staticmethod
    def _num(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        # 东方财富用 "-" 表示缺失
        return None if number != number else number

    def realtime_price(self, ts_code: str) -> dict[str, Any]:
        data = self._quote(ts_code)
        price = self._num(data.get("f43"))
        pre_close = self._num(data.get("f60"))
        change = self._num(data.get("f169"))
        pct_chg = self._num(data.get("f170"))
        if price is None:
            from finagent.errors import ProviderUnavailable

            raise ProviderUnavailable(f"{ts_code} 当前没有有效报价（可能停牌）。")

        return {
            "ts_code": ts_code,
            "name": str(data.get("f58") or ""),
            "price": price,
            "pre_close": pre_close,
            "change": change,
            "pct_chg": pct_chg,
            "high": self._num(data.get("f44")),
            "low": self._num(data.get("f45")),
            "open": self._num(data.get("f46")),
            "vol": self._num(data.get("f47")),  # 手
            "amount": self._num(data.get("f48")),  # 元
            "trade_date": time.strftime("%Y%m%d"),
            "source": "eastmoney",
        }

    def daily_basic(
        self, ts_code: str, start_date: str | None = None, end_date: str | None = None
    ) -> pd.DataFrame:
        """只给**最新**一行估值。历史估值没有稳定的公开接口，宁缺毋滥。"""

        data = self._quote(ts_code)
        total_mv = self._num(data.get("f116"))
        circ_mv = self._num(data.get("f117"))
        row = {
            "ts_code": ts_code,
            "trade_date": time.strftime("%Y%m%d"),
            "close": self._num(data.get("f43")),
            "pe": self._num(data.get("f162")),
            "pe_ttm": self._num(data.get("f162")),
            "pb": self._num(data.get("f167")),
            "ps": None,
            # 与 Tushare 口径一致：万元
            "total_mv": round(total_mv / 10000.0, 2) if total_mv is not None else None,
            "circ_mv": round(circ_mv / 10000.0, 2) if circ_mv is not None else None,
            "turnover_rate": self._num(data.get("f168")),
        }
        return pd.DataFrame([row])

    def index_daily(
        self, ts_code: str, start_date: str | None = None, end_date: str | None = None
    ) -> pd.DataFrame:
        return self.daily_price(ts_code, start_date, end_date)

    def income_statement(
        self, ts_code: str, start_date: str | None = None, end_date: str | None = None
    ) -> pd.DataFrame:
        """东方财富的财报接口结构不稳定，这里明确不支持，而不是返回半成品。"""

        from finagent.errors import ProviderUnavailable

        raise ProviderUnavailable(
            "东方财富数据源不提供利润表。",
            hint="需要财报/宏观数据请配置 Tushare（FINAGENT_PROVIDER=tushare）。",
        )

    # ------------------------------------------------------------------
    # 选股池
    # ------------------------------------------------------------------
    def _list_page(self, page: int) -> dict[str, Any] | None:
        """取一页股票列表；退避重试 3 次仍失败则返回 ``None``。"""

        params = {
            "pn": page,
            "pz": 100,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f12",
            "fs": _A_SHARE_FILTER,
            "fields": "f12,f14,f2,f3,f8,f9,f23,f20,f21,f6,f26,f100",
        }
        for attempt in range(3):
            try:
                return self._http_json(_LIST_URL, params)
            except Exception as exc:  # noqa: BLE001 - 限流/断连都走退避
                if attempt == 2:
                    log.debug("股票列表第 %d 页失败：%s", page, exc)
                    return None
                time.sleep(0.6 * (attempt + 1))
        return None

    def screening_universe(self) -> pd.DataFrame:
        """全市场宽表；5 分钟内复用缓存。"""

        if self._universe_cache is not None:
            stamp, cached = self._universe_cache
            if time.monotonic() - stamp < _UNIVERSE_TTL:
                return cached

        rows: list[dict[str, Any]] = []
        page = 1
        total = 0
        missing: list[int] = []
        # 接口每页最多 100 条，A 股约 5900 只 → 需要翻 60 页。
        # 翻到一半被限流是常态，所以：单页退避重试 → 记下失败页 → 循环结束后补抓两轮。
        while page <= 80:
            payload = self._list_page(page)
            if payload is None:
                missing.append(page)
                page += 1
                continue

            data = payload.get("data") or {}
            diff = data.get("diff") or []
            total = int(data.get("total") or total or 0)
            if not diff:
                break
            rows.extend(diff)
            if total and len(rows) >= total:
                break
            page += 1

        # 补抓：被限流的页往往过几秒就好了
        for round_index in range(2):
            if not missing:
                break
            time.sleep(1.5 + round_index)
            still: list[int] = []
            for bad_page in list(missing):
                payload = self._list_page(bad_page)
                if payload is None:
                    still.append(bad_page)
                    continue
                rows.extend((payload.get("data") or {}).get("diff") or [])
            missing = still

        self.last_universe_complete = not missing
        if missing:
            log.warning(
                "股票列表有 %d 页没取到（%s），本次选股结果可能不完整",
                len(missing),
                missing[:5],
            )

        if not rows:
            from finagent.errors import ProviderUnavailable

            raise ProviderUnavailable(
                "东方财富没有返回股票列表。",
                hint="可能被临时限流，等一两分钟再试。",
            )

        records: list[dict[str, Any]] = []
        for item in rows:
            code = str(item.get("f12") or "")
            if not code:
                continue
            suffix = (
                "SH"
                if code.startswith(("6", "9", "5"))
                else ("BJ" if code.startswith(("4", "8")) else "SZ")
            )
            total_mv = self._num(item.get("f20"))
            circ_mv = self._num(item.get("f21"))
            records.append(
                {
                    "ts_code": f"{code}.{suffix}",
                    "name": str(item.get("f14") or ""),
                    "industry": str(item.get("f100") or "") or "未分类",
                    "market": suffix,
                    "close": self._num(item.get("f2")),
                    "pct_chg": self._num(item.get("f3")),
                    "pe": self._num(item.get("f9")),
                    "pb": self._num(item.get("f23")),
                    "total_mv": round(total_mv / 10000.0, 2) if total_mv is not None else None,
                    "circ_mv": round(circ_mv / 10000.0, 2) if circ_mv is not None else None,
                    "turnover_rate": self._num(item.get("f8")),
                    "amount": self._num(item.get("f6")),
                    "list_date": str(item.get("f26") or ""),
                }
            )

        frame = pd.DataFrame(records)
        self._universe_cache = (time.monotonic(), frame)
        log.debug("东方财富选股池已更新：%d 只", len(frame))
        return frame
