"""意图分类与实体抽取（确定性 NLU）。"""

from __future__ import annotations

from datetime import datetime

import pytest

from finagent.graph.intents import (
    classify_candidates,
    classify_intent,
    classify_next,
    extract_alert_spec,
    extract_profile_hints,
    extract_screen_criteria,
    extract_start_only,
    extract_strategy,
    extract_symbols,
    extract_window,
    looks_like_preference,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("贵州茅台现在多少钱", "technical_analyst"),
        ("茅台最近一个月走势怎么样", "technical_analyst"),
        ("茅台估值贵不贵", "fundamental_analyst"),
        ("茅台的市盈率和市净率是多少", "fundamental_analyst"),
        ("茅台财报里的净利润怎么样", "fundamental_analyst"),
        ("茅台 MACD 是不是金叉了", "technical_analyst"),
        ("帮我筛选 PE 小于 15 的银行股", "stock_picker"),
        ("有哪些低估的长尾股票", "stock_picker"),
        ("用 MACD 策略回测中信证券近一年", "backtest"),
        ("回测一下平安银行的均线策略", "backtest"),
        ("我以 10.5 元买入 1000 股平安银行", "portfolio"),
        ("现在我的持仓盈亏怎么样", "portfolio"),
        ("茅台涨到 2000 元提醒我", "alert"),
        ("如果平安银行上涨 5% 就提醒我", "alert"),
        ("列出我的预警", "alert"),
    ],
)
def test_classify_intent(text: str, expected: str) -> None:
    agent, reason = classify_intent(text)
    assert agent == expected, f"{text!r} -> {agent} ({reason})"


def test_alert_beats_backtest_on_tie() -> None:
    # 句子里同时有「突破」和「提醒」时，预警必须压过策略词
    agent, _ = classify_intent("茅台突破 2000 就提醒我")
    assert agent == "alert"


def test_classify_next_skips_completed() -> None:
    agent, _ = classify_next("回测茅台并看看我的持仓", [])
    assert agent == "backtest"
    agent, _ = classify_next("回测茅台并看看我的持仓", ["backtest"])
    assert agent == "portfolio"
    agent, _ = classify_next("回测茅台并看看我的持仓", ["backtest", "portfolio"])
    assert agent == "finish"


def test_classify_candidates_scores_are_sorted() -> None:
    ranked = classify_candidates("回测 MACD 策略")
    assert ranked
    assert ranked[0][0] == "backtest"
    assert all(ranked[i][1] >= ranked[i + 1][1] for i in range(len(ranked) - 1))


# ----------------------------------------------------------------------
def test_extract_symbols_in_order() -> None:
    assert extract_symbols("对比茅台和平安银行") == ["600519.SH", "000001.SZ"]
    assert extract_symbols("600519 与 000001") == ["600519.SH", "000001.SZ"]
    assert extract_symbols("看看上证指数") == ["000001.SH"]


def test_extract_symbols_deduplicates() -> None:
    assert extract_symbols("茅台，贵州茅台，600519.SH") == ["600519.SH"]


def test_extract_symbols_empty() -> None:
    assert extract_symbols("今天天气不错") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("用 MACD 策略回测", "macd"),
        ("双均线策略", "ma_cross"),
        ("海龟交易法", "turtle"),
        ("唐奇安通道", "donchian_breakout"),
        ("KDJ 金叉", "kdj"),
        ("布林带突破", "boll_breakout"),
        ("布林带回归", "boll_breakout"),
        ("动量策略", "momentum_roc"),
        ("放量突破策略", "volume_breakout"),
        ("rsi 超卖", "rsi"),
        ("三均线", "triple_ma"),
        ("随机指标", "stochastic"),
    ],
)
def test_extract_strategy(text: str, expected: str) -> None:
    strategy, explicit = extract_strategy(text)
    assert strategy == expected
    assert explicit is True


def test_extract_strategy_default() -> None:
    strategy, explicit = extract_strategy("回测一下茅台")
    assert strategy == "ma_cross"
    assert explicit is False


@pytest.mark.parametrize(
    ("text", "days"),
    [
        ("近一年", 365),
        ("过去半年", 183),
        ("最近三个月", 91),
        ("近一个月", 30),
        ("最近一周", 7),
        ("近两年", 730),
    ],
)
def test_extract_window_relative(text: str, days: int) -> None:
    window = extract_window(text)
    assert window.explicit is True
    assert len(window.start) == 8 and len(window.end) == 8
    assert window.start < window.end
    # 按真实日期比较，而不是把 YYYYMMDD 当整数相减。
    start = datetime.strptime(window.start, "%Y%m%d").date()
    end = datetime.strptime(window.end, "%Y%m%d").date()
    assert abs((end - start).days - days) <= 40


def test_extract_window_default() -> None:
    window = extract_window("回测茅台")
    assert window.explicit is False
    assert window.end > window.start


def test_extract_window_explicit_range() -> None:
    window = extract_window("回测 2024-01-02 到 2024-12-31")
    assert window.start == "20240102"
    assert window.end == "20241231"


def test_extract_start_only_falls_back_to_latest() -> None:
    value = extract_start_only("选股")
    assert len(value) == 8


# ----------------------------------------------------------------------
def test_alert_spec_absolute_up() -> None:
    spec = extract_alert_spec("茅台涨到 2000 元提醒我")
    assert spec.ts_code == "600519.SH"
    assert spec.operator == ">"
    assert spec.threshold == 2000.0


def test_alert_spec_absolute_down() -> None:
    spec = extract_alert_spec("平安银行跌破 8 元告诉我")
    assert spec.operator == "<"
    assert spec.threshold == 8.0


def test_alert_spec_percentage_up() -> None:
    spec = extract_alert_spec("茅台上涨 5% 提醒我")
    assert spec.pct_change == 5.0
    assert spec.operator == ">"


def test_alert_spec_percentage_down() -> None:
    spec = extract_alert_spec("平安银行下跌 3% 提醒我")
    assert spec.pct_change == -3.0
    assert spec.operator == "<"


def test_alert_spec_bare_percentage() -> None:
    spec = extract_alert_spec("茅台涨 10% 的时候提醒")
    assert spec.pct_change == 10.0


# ----------------------------------------------------------------------
def test_profile_hints_risk_and_sectors() -> None:
    hints = extract_profile_hints("我是保守型投资者，偏好银行和白酒板块")
    assert hints.risk_tolerance == "Conservative"
    assert "银行" in hints.favorite_sectors
    assert "白酒" in hints.favorite_sectors


def test_profile_hints_avoid() -> None:
    hints = extract_profile_hints("我不买房地产，也不喜欢医药")
    assert "房地产" in hints.avoid_sectors
    assert "医药" in hints.avoid_sectors


def test_profile_hints_horizon_and_style() -> None:
    hints = extract_profile_hints("我偏好长期持有高股息的股票")
    assert hints.investment_horizon == "Long-term"
    assert hints.investment_style == "偏好高股息"


def test_profile_hints_empty() -> None:
    assert extract_profile_hints("茅台今天多少钱").is_empty()


def test_looks_like_preference_gate() -> None:
    assert looks_like_preference("我是激进型投资者")
    assert not looks_like_preference("茅台今天多少钱")


# ----------------------------------------------------------------------
def test_screen_criteria_from_natural_language() -> None:
    criteria = extract_screen_criteria("帮我筛选市盈率小于 15，市值大于 500 亿的银行股，前 5 只")
    assert criteria["pe_max"] == 15.0
    assert criteria["mv_min"] == 500.0
    assert criteria["industries"] == ["银行"]
    assert criteria["limit"] == 5


def test_screen_criteria_underscores_valuation_words() -> None:
    criteria = extract_screen_criteria("找出被低估的股票")
    assert criteria["pe_max"] == 20.0
    assert criteria["pb_max"] == 3.0


def test_screen_criteria_defaults_limit() -> None:
    criteria = extract_screen_criteria("选股")
    assert criteria["limit"] == 20
