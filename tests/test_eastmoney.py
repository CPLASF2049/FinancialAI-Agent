"""东方财富数据源测试。

默认全部走 mock：CI 不该依赖外网。想跑真实连通性检查，设置
``FINAGENT_LIVE_TESTS=1`` 后执行 ``pytest tests/test_eastmoney.py -m live``。
"""

from __future__ import annotations

import os

import pytest

from finagent.errors import ProviderUnavailable
from finagent.providers.eastmoney import EastmoneyProvider

# ----------------------------------------------------------------------
# 固定响应：字段与缩放都照抄真实接口（fltt=2 已缩放）
# ----------------------------------------------------------------------
QUOTE = {
    "rc": 0,
    "data": {
        "f43": 1439.1,
        "f44": 1447.0,
        "f45": 1395.02,
        "f46": 1410.0,
        "f47": 82691,
        "f48": 11849258015.0,
        "f57": "600519",
        "f58": "贵州茅台",
        "f60": 1392.05,
        "f116": 1798992431999.1,
        "f117": 1798992431999.1,
        "f162": 20.93,
        "f167": 6.63,
        "f168": 0.66,
        "f169": 47.05,
        "f170": 3.38,
    },
}

KLINES = {
    "rc": 0,
    "data": {
        "code": "600519",
        "klines": [
            "2025-10-29,1388.04,1379.92,1394.57,1378.06,33937,4868780319.00,1.19",
            "2025-10-30,1369.83,1374.76,1384.65,1369.83,47456,6768821311.00,1.07",
            "2025-10-31,1370.02,1378.03,1384.00,1368.13,35694,5109623861.00,1.15",
        ],
    },
}


def _list_page(page: int) -> dict:
    rows = [
        {
            "f12": "600519",
            "f14": "贵州茅台",
            "f2": 1439.1,
            "f3": 3.38,
            "f8": 0.66,
            "f9": 20.93,
            "f23": 6.63,
            "f20": 1798992431999.1,
            "f21": 1798992431999.1,
            "f6": 11849258015.0,
            "f26": 20010827,
            "f100": "白酒Ⅱ",
        },
        {
            "f12": "000001",
            "f14": "平安银行",
            "f2": 11.2,
            "f3": -0.5,
            "f8": 1.2,
            "f9": 5.1,
            "f23": 0.6,
            "f20": 217000000000.0,
            "f21": 217000000000.0,
            "f6": 900000000.0,
            "f26": 19910403,
            "f100": "银行",
        },
        {
            "f12": "830799",
            "f14": "艾融软件",
            "f2": 20.0,
            "f3": 1.0,
            "f8": 2.0,
            "f9": 30.0,
            "f23": 3.0,
            "f20": 1000000000.0,
            "f21": 1000000000.0,
            "f6": 10000000.0,
            "f26": 20180701,
            "f100": "软件开发",
        },
    ]
    return {"rc": 0, "data": {"total": 3 if page == 1 else 0, "diff": rows if page == 1 else []}}


@pytest.fixture
def provider(monkeypatch) -> EastmoneyProvider:
    instance = EastmoneyProvider()

    def fake(url: str, params: dict | None = None) -> dict:
        if "clist" in url:
            return _list_page(int((params or {}).get("pn", 1)))
        if "stock/get" in url:
            return QUOTE
        if "kline" in url:
            return KLINES
        raise AssertionError(f"未预期的 URL: {url}")

    monkeypatch.setattr(instance, "_http_json", fake)
    return instance


# ----------------------------------------------------------------------
def test_secid_mapping() -> None:
    assert EastmoneyProvider._secid("600519.SH") == "1.600519"
    assert EastmoneyProvider._secid("000001.SZ") == "0.000001"
    assert EastmoneyProvider._secid("830799.BJ") == "0.830799"
    assert EastmoneyProvider._secid("00700.HK") == "116.00700"
    # 没后缀时按首位猜
    assert EastmoneyProvider._secid("601398") == "1.601398"
    assert EastmoneyProvider._secid("300750") == "0.300750"


def test_daily_price_shape_and_derived_fields(provider: EastmoneyProvider) -> None:
    frame = provider.daily_price("600519.SH", "20251029", "20251031")

    assert list(frame.columns) == [
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
    assert len(frame) == 3
    assert frame.iloc[-1]["trade_date"] == "20251031"
    # 第二根的 pre_close 应当是第一根的 close（1379.92）
    second = frame.iloc[1]
    assert second["pre_close"] == pytest.approx(1379.92)
    assert second["change"] == pytest.approx(1374.76 - 1379.92, abs=1e-3)
    assert second["pct_chg"] == pytest.approx((1374.76 - 1379.92) / 1379.92 * 100, abs=1e-2)


def test_realtime_price_is_real_shape(provider: EastmoneyProvider) -> None:
    quote = provider.realtime_price("600519.SH")

    assert quote["name"] == "贵州茅台"
    assert quote["price"] == pytest.approx(1439.1)
    assert quote["pre_close"] == pytest.approx(1392.05)
    assert quote["pct_chg"] == pytest.approx(3.38)
    assert quote["source"] == "eastmoney"
    assert quote["vol"] == 82691  # 手


def test_daily_basic_uses_wan_yuan(provider: EastmoneyProvider) -> None:
    """总市值口径必须与 Tushare 一致（万元），否则选股的市值条件会差 4 个数量级。"""

    row = provider.daily_basic("600519.SH").iloc[0]
    assert row["pe"] == pytest.approx(20.93)
    assert row["pb"] == pytest.approx(6.63)
    assert row["total_mv"] == pytest.approx(1798992431999.1 / 10000.0, rel=1e-6)


def test_screening_universe_builds_codes_and_scales(provider: EastmoneyProvider) -> None:
    frame = provider.screening_universe()

    assert len(frame) == 3
    codes = set(frame["ts_code"])
    assert codes == {"600519.SH", "000001.SZ", "830799.BJ"}
    maotai = frame[frame["ts_code"] == "600519.SH"].iloc[0]
    assert maotai["industry"] == "白酒Ⅱ"
    assert maotai["total_mv"] == pytest.approx(179899243.2, rel=1e-6)


def test_universe_is_cached(provider: EastmoneyProvider) -> None:
    calls = {"n": 0}
    original = provider._http_json

    def counting(url: str, params: dict | None = None) -> dict:
        calls["n"] += 1
        return original(url, params)

    provider._http_json = counting  # type: ignore[method-assign]
    provider.screening_universe()
    first = calls["n"]
    provider.screening_universe()
    assert calls["n"] == first, "第二次应当走缓存"


def test_stock_basic_filters(provider: EastmoneyProvider) -> None:
    one = provider.stock_basic(ts_code="600519.SH")
    assert len(one) == 1 and one.iloc[0]["name"] == "贵州茅台"

    named = provider.stock_basic(name="银行")
    assert len(named) == 1 and named.iloc[0]["ts_code"] == "000001.SZ"


def test_income_statement_is_explicitly_unsupported(provider: EastmoneyProvider) -> None:
    """不支持就要明确报错，不能返回半成品让上层以为有数据。"""

    with pytest.raises(ProviderUnavailable):
        provider.income_statement("600519.SH")


def test_capabilities_are_honest(provider: EastmoneyProvider) -> None:
    assert provider.supports("daily_price")
    assert provider.supports("screener")
    assert not provider.supports("income_statement")
    assert not provider.supports("macro")


def test_empty_quote_raises(monkeypatch) -> None:
    instance = EastmoneyProvider()
    monkeypatch.setattr(instance, "_http_json", lambda url, params=None: {"rc": 0, "data": None})
    with pytest.raises(ProviderUnavailable):
        instance.realtime_price("999999.SH")


# ----------------------------------------------------------------------
@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("FINAGENT_LIVE_TESTS") != "1",
    reason="联网用例：设置 FINAGENT_LIVE_TESTS=1 才跑",
)
def test_live_quote_reachable() -> None:
    quote = EastmoneyProvider().realtime_price("600519.SH")
    assert quote["price"] and quote["price"] > 0
    assert quote["name"]


def test_universe_marks_incomplete_when_a_page_fails(monkeypatch) -> None:
    """中途被限流时，必须把「不完整」标记出来，不能假装拿全了。"""

    instance = EastmoneyProvider()
    monkeypatch.setattr("finagent.providers.eastmoney.time.sleep", lambda *_: None)

    def flaky(url: str, params: dict | None = None) -> dict:
        page = int((params or {}).get("pn", 1))
        if page == 1:
            # 声称有 300 条 → 必须继续翻页
            return {"rc": 0, "data": {"total": 300, "diff": _list_page(1)["data"]["diff"]}}
        raise OSError("连接被重置")

    monkeypatch.setattr(instance, "_http_json", flaky)
    frame = instance.screening_universe()

    assert len(frame) == 3  # 第 1 页拿到了
    assert instance.last_universe_complete is False
    assert instance._universe_cache is not None


def test_complete_universe_is_marked_complete(provider: EastmoneyProvider) -> None:
    provider.screening_universe()
    assert provider.last_universe_complete is True


def test_screener_surfaces_partial_warning(provider: EastmoneyProvider, monkeypatch) -> None:
    """部分数据要让选股结果自己说出来，否则「命中 N 只」会被当成全量。"""

    from finagent.core.screener import ScreenCriteria, screen

    frame = provider.screening_universe()

    def partial() -> object:
        provider.last_universe_complete = False
        return frame

    monkeypatch.setattr(provider, "screening_universe", partial)
    result = screen(provider, ScreenCriteria(pe_max=15, limit=5))

    assert result.get("universe_complete") is False
    assert "warning" in result
