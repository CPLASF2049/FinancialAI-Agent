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
import pathlib
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

#: 备用源：新浪。免费接口会封 IP，单源等于随时全瘫，所以做双活。
_SINA_QUOTE_URL = "https://hq.sinajs.cn/list="
_SINA_LIST_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)

#: A 股全市场（沪 + 深 + 北交所）。
_A_SHARE_FILTER = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

#: 内存缓存时长（秒）。
_UNIVERSE_TTL = 600.0
#: 完整快照在磁盘上保留多久（秒）。
_DISK_TTL = 1800.0
#: 不完整快照的磁盘有效期（秒）—— 短一些，好让后续调用尽快补齐缺口。
_DISK_TTL_PARTIAL = 120.0
#: 两次请求之间的最小间隔，礼貌一点，也降低被限流的概率。
_MIN_INTERVAL = 0.22
#: 一次全市场抓取的时间预算（秒）。超了就用手上已有的数据收尾。
_FETCH_BUDGET = 45.0


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
        self._disk_rows: dict[str, dict[str, Any]] = {}
        self._load_disk_cache()
        #: 上一次全市场拉取是否完整。选股结果据此决定要不要提示「可能不全」。
        self.last_universe_complete: bool | None = None
        #: 上一次全市场来自哪个源（`eastmoney` / `sina`）。
        self.last_universe_source = ""

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_request
        if gap < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - gap)
        self._last_request = time.monotonic()

    def _http_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        decode: str = "utf-8",
    ) -> str:
        """取一段文本响应（新浪返回的是 GBK 文本，不是 JSON）。"""

        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        self._throttle()
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "*/*",
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read().decode(decode, "replace")
        except urllib.error.URLError as exc:
            from finagent.errors import ProviderUnavailable

            raise ProviderUnavailable(
                f"数据源接口不可达：{exc}",
                hint="检查网络；离线环境请把 FINAGENT_PROVIDER 设为 offline。",
            ) from exc

    def _http_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        raw = self._http_text(
            url, params=params, headers={"Referer": "https://quote.eastmoney.com/"}
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            from finagent.errors import ProviderUnavailable

            raise ProviderUnavailable(f"接口返回了非 JSON 内容：{raw[:80]!r}") from exc

        if not isinstance(payload, dict):
            from finagent.errors import ProviderUnavailable

            raise ProviderUnavailable("接口返回格式异常。")
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
        try:
            data = self._quote(ts_code)
        except Exception as exc:  # noqa: BLE001 - 东财被封/限流时切新浪
            log.warning("东方财富报价失败（%s），改用新浪备用源", str(exc)[:60])
            return self._sina_quote(ts_code)

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

    # ------------------------------------------------------------------
    # 备用源：新浪（东财封 IP 时接管）
    # ------------------------------------------------------------------
    @staticmethod
    def _sina_symbol(ts_code: str) -> str:
        code, _, suffix = str(ts_code).partition(".")
        if suffix.upper() == "SH":
            return f"sh{code}"
        if suffix.upper() in ("SZ", "BJ"):
            return f"sz{code}"
        return f"sh{code}" if code.startswith(("6", "9", "5")) else f"sz{code}"

    def _sina_quote(self, ts_code: str) -> dict[str, Any]:
        raw = self._http_text(
            _SINA_QUOTE_URL + self._sina_symbol(ts_code),
            headers={"Referer": "https://finance.sina.com.cn"},
            decode="gbk",
        )
        payload = raw.partition('="')[2].rstrip('";\n')
        fields = payload.split(",")
        if len(fields) < 10 or not fields[0]:
            from finagent.errors import ProviderUnavailable

            raise ProviderUnavailable(
                f"东方财富与新浪都没有返回 {ts_code} 的报价。",
                hint="确认代码是否存在；稍后重试。",
            )

        def num(index: int) -> float | None:
            try:
                return float(fields[index])
            except (IndexError, ValueError):
                return None

        price = num(3)
        pre_close = num(2)
        change = (price - pre_close) if (price is not None and pre_close) else None
        pct = (change / pre_close * 100) if (change is not None and pre_close) else None
        volume = num(8)
        return {
            "ts_code": ts_code,
            "name": fields[0],
            "price": price,
            "pre_close": pre_close,
            "change": round(change, 4) if change is not None else None,
            "pct_chg": round(pct, 4) if pct is not None else None,
            "high": num(4),
            "low": num(5),
            "open": num(1),
            # 新浪的成交量是「股」，统一换算成「手」
            "vol": round(volume / 100.0, 2) if volume is not None else None,
            "amount": num(9),
            "trade_date": str(fields[30]) if len(fields) > 30 else time.strftime("%Y-%m-%d"),
            "source": "sina",
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
            "行情数据源不提供利润表。",
            hint="财报数据请用 mx_financial_data（妙想）；本工具只覆盖行情与估值。",
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

    # ------------------------------------------------------------------
    # 磁盘缓存：把每次拿到的行按 ts_code 累积，缺页由后续调用补齐
    # ------------------------------------------------------------------
    @staticmethod
    def _cache_path() -> pathlib.Path | None:
        try:
            from finagent.settings import get_settings

            return get_settings().resolve_data_dir() / "eastmoney_universe.json"
        except Exception:  # noqa: BLE001 - 缓存是优化，取不到就算了
            return None

    def _load_disk_cache(self) -> None:
        path = self._cache_path()
        if path is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        stamp = float(payload.get("stamp") or 0.0)
        complete = bool(payload.get("complete"))
        ttl = _DISK_TTL if complete else _DISK_TTL_PARTIAL
        if time.time() - stamp > ttl:
            return

        self._disk_rows = {
            str(row.get("ts_code")): row for row in payload.get("rows") or [] if row.get("ts_code")
        }
        self._disk_stamp = stamp
        log.debug("载入选股池缓存：%d 只（完整=%s）", len(self._disk_rows), complete)

    def _save_disk_cache(self, complete: bool) -> None:
        path = self._cache_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "stamp": time.time(),
                        "complete": complete,
                        "rows": list(self._disk_rows.values()),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:  # noqa: BLE001 - 缓存失败不影响主流程
            log.debug("写选股池缓存失败：%s", exc)

    # ------------------------------------------------------------------
    def _normalise(self, item: dict[str, Any]) -> dict[str, Any] | None:
        """把一行原始字段转成统一口径（万元、ts_code、行业）。"""

        code = str(item.get("f12") or "")
        if not code:
            return None
        if code.startswith(("6", "9", "5")):
            suffix = "SH"
        elif code.startswith(("4", "8")):
            suffix = "BJ"
        else:
            suffix = "SZ"

        total_mv = self._num(item.get("f20"))
        circ_mv = self._num(item.get("f21"))
        return {
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

    def screening_universe(self) -> pd.DataFrame:
        """全市场宽表。

        接口每页最多 100 条、A 股约 5900 只 → 要翻 60 页，中途被限流是常态。所以：

        1. 单页退避重试，失败的页记下来，循环结束后补抓两轮；
        2. 结果与**磁盘缓存合并**（按 ts_code），上次拿到的行这次还能用；
        3. 仍然缺页就标记 ``last_universe_complete=False``，选股结果会据此提示「可能不完整」。

        第 2 条是关键：反复调用会逐步收敛到全量，而不是每次从零开始漏。
        """

        if self._universe_cache is not None:
            stamp, cached = self._universe_cache
            if time.monotonic() - stamp < _UNIVERSE_TTL:
                return cached

        rows: list[dict[str, Any]] = []
        page = 1
        total = 0
        missing: list[int] = []

        # 先探一页：东财被封（RemoteDisconnected）时立刻切新浪，不要傻等 60 次重试
        probe = self._list_page(1)
        use_sina = probe is None
        if use_sina:
            log.warning("东方财富列表接口不可用，本次全市场改走新浪备用源")

        deadline = time.monotonic() + _FETCH_BUDGET

        if probe is not None:
            while page <= 80:
                payload = probe if page == 1 else self._list_page(page)
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
                if time.monotonic() > deadline:
                    log.warning("全市场抓取超出时间预算，先用手上的 %d 条收尾", len(rows))
                    break
                page += 1

            # 补抓：被限流的页往往过几秒就好了
            for round_index in range(2):
                if not missing or time.monotonic() > deadline:
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

        fresh: dict[str, dict[str, Any]] = {}
        for item in rows:
            record = self._normalise(item)
            if record:
                fresh[record["ts_code"]] = record

        # 东财拿不到 → 走新浪（字段口径已经对齐，只是没有行业分类）
        if use_sina or (not fresh and self._disk_rows):
            sina_rows = self._fetch_sina_universe(deadline)
            if sina_rows:
                fresh = {**fresh, **sina_rows}
                missing = []  # 新浪是顺序翻页，拿到多少算多少，不按「缺页」计
                use_sina = True

        if not fresh and not self._disk_rows:
            from finagent.errors import ProviderUnavailable

            raise ProviderUnavailable(
                "东方财富与新浪都没有返回股票列表。",
                hint="可能被限流，等一两分钟再试。",
            )

        merged = {**self._disk_rows, **fresh}
        self._disk_rows = merged
        complete = not missing
        self.last_universe_complete = complete
        self.last_universe_source = "sina" if use_sina else "eastmoney"
        self._save_disk_cache(complete)

        if missing:
            log.warning(
                "股票列表有 %d 页没取到（%s），本次新抓 %d 只，合并缓存后共 %d 只",
                len(missing),
                missing[:5],
                len(fresh),
                len(merged),
            )

        frame = pd.DataFrame(list(merged.values()))
        self._universe_cache = (time.monotonic(), frame)
        log.debug(
            "选股池：%d 只（来源=%s，完整=%s）", len(frame), self.last_universe_source, complete
        )
        return frame

    def _fetch_sina_universe(self, deadline: float) -> dict[str, dict[str, Any]]:
        """新浪全市场（顺序翻页，每页 100 条）。"""

        collected: dict[str, dict[str, Any]] = {}
        page = 1
        while page <= 80 and time.monotonic() < deadline:
            records = self._sina_page(page)
            if not records:
                break
            for record in records:
                collected[record["ts_code"]] = record
            if len(records) < 100:
                break
            page += 1
        if collected:
            log.info("新浪备用源取到 %d 只（%d 页）", len(collected), page)
        return collected

    def _sina_page(self, page: int, num: int = 100) -> list[dict[str, Any]] | None:
        """新浪全市场列表的一页，归一化成与东财一致的口径。"""

        try:
            raw = self._http_text(
                _SINA_LIST_URL,
                params={"page": page, "num": num, "sort": "symbol", "asc": 1, "node": "hs_a"},
                headers={"Referer": "https://finance.sina.com.cn"},
                decode="gbk",
            )
            items = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            log.debug("新浪列表第 %d 页失败：%s", page, exc)
            return None

        if not isinstance(items, list):
            return None

        def number(item: dict[str, Any], key: str) -> float | None:
            try:
                return float(item.get(key))
            except (TypeError, ValueError):
                return None

        records: list[dict[str, Any]] = []
        for item in items:
            code = str(item.get("code") or "")
            if not code:
                continue
            if code.startswith(("6", "9", "5")):
                suffix = "SH"
            elif code.startswith(("4", "8")):
                suffix = "BJ"
            else:
                suffix = "SZ"
            records.append(
                {
                    "ts_code": f"{code}.{suffix}",
                    "name": str(item.get("name") or ""),
                    # 新浪列表不带行业分类：按行业筛选时会被排除，调用方需要知道这一点
                    "industry": "未分类",
                    "market": suffix,
                    "close": number(item, "trade"),
                    "pct_chg": number(item, "changepercent"),
                    "pe": number(item, "per"),
                    "pb": number(item, "pb"),
                    "total_mv": number(item, "mktcap"),  # 万元
                    "circ_mv": number(item, "nmc"),
                    "turnover_rate": number(item, "turnoverratio"),
                    "amount": number(item, "amount"),
                    "list_date": "",
                }
            )
        return records
