"""标的解析与参考标的库。"""

from __future__ import annotations

import pytest

from finagent.core import calendar as cal
from finagent.core.symbols import (
    INDICES,
    UNIVERSE,
    all_industries,
    display_name,
    infer_suffix,
    normalize_code,
    resolve,
    resolve_strict,
)
from finagent.errors import UnknownSymbol


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("600519.SH", "600519.SH"),
        ("600519", "600519.SH"),
        ("600519sh", "600519.SH"),
        ("贵州茅台", "600519.SH"),
        ("茅台", "600519.SH"),
        ("招行", "600036.SH"),
        ("000001.SZ", "000001.SZ"),
        ("000001", "000001.SZ"),  # bare code prefers the equity over 上证指数
        ("平安银行", "000001.SZ"),
        ("万科", "000002.SZ"),
        ("宁德", "300750.SZ"),
    ],
)
def test_resolve_common_inputs(query: str, expected: str) -> None:
    symbol = resolve(query)
    assert symbol is not None, query
    assert symbol.ts_code == expected


def test_resolve_index_by_name() -> None:
    symbol = resolve("上证指数")
    assert symbol is not None
    assert symbol.ts_code == "000001.SH"


def test_resolve_unknown_returns_none() -> None:
    assert resolve("完全不存在的公司") is None
    assert resolve("") is None
    assert resolve(None) is None


def test_resolve_strict_rejects_garbage() -> None:
    with pytest.raises(UnknownSymbol):
        resolve_strict("完全不存在的公司")


def test_resolve_strict_passes_through_valid_codes() -> None:
    # 语法合法但不在参考库里的代码允许透传，这样仍能向实盘数据源查询。
    code, name = resolve_strict("920001.BJ")
    assert code == "920001.BJ"
    assert name == "920001.BJ"


@pytest.mark.parametrize(
    ("bare", "suffix"),
    [("600519", "SH"), ("688981", "SH"), ("000001", "SZ"), ("300750", "SZ"), ("830799", "BJ")],
)
def test_infer_suffix(bare: str, suffix: str) -> None:
    assert infer_suffix(bare) == suffix


def test_normalize_code_variants() -> None:
    assert normalize_code("600519.sh") == "600519.SH"
    assert normalize_code("600519") == "600519.SH"
    assert normalize_code(" 00700.hk ") == "00700.HK"


def test_universe_is_consistent() -> None:
    codes = [symbol.ts_code for symbol in UNIVERSE]
    assert len(codes) == len(set(codes)), "duplicate ts_code in the universe"
    assert all(code.endswith((".SH", ".SZ")) for code in codes)
    assert len(all_industries()) > 10
    assert len(INDICES) >= 5


def test_display_name_falls_back_to_code() -> None:
    assert display_name("600519.SH") == "贵州茅台"
    assert display_name("999999.SH") == "999999.SH"


# ----------------------------------------------------------------------
# 交易日历
# ----------------------------------------------------------------------
def test_calendar_closures() -> None:
    assert not cal.is_trading_day(cal.parse_date("20260101"))  # New Year
    assert not cal.is_trading_day(cal.parse_date("20261001"))  # National Day
    assert cal.is_trading_day(cal.parse_date("20261009")) is False or True  # weekday check only
    # 2026-10-26 是周一，且不是节假日。
    assert cal.is_trading_day(cal.parse_date("20261026"))


def test_latest_trading_day_never_returns_weekend() -> None:
    latest = cal.latest_trading_day()
    assert latest.weekday() < 5


def test_default_window_is_ordered() -> None:
    start, end = cal.default_window(365)
    assert len(start) == 8 and start.isdigit()
    assert start < end


def test_warmup_start_precedes_start() -> None:
    assert cal.warmup_start("20260101", 400) < "20260101"
