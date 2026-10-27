"""确定性意图分类与实体抽取。

两个职责：

1. **Supervisor 的兜底**。没有配置 LLM（``FINAGENT_LLM_PROVIDER=none``）时，
   Supervisor 用这些规则路由，而不是调模型。这样整套多 Agent 拓扑在零 API Key 的
   情况下也能运行、测试和演示。

2. **确定性专家的实体抽取**。标的、策略、日期区间、预警阈值和画像变更都用正则和
   映射表从文本里取出来——不会幻觉，而且每条规则都有单元测试。

LLM 模式不依赖这里的任何东西，所以这里的准确率只影响离线模式。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

from finagent.core import calendar as cal
from finagent.core.symbols import INDICES, SHORTHAND, UNIVERSE, normalize_code, resolve

# ----------------------------------------------------------------------
# 意图分类
# ----------------------------------------------------------------------
#: 每个 Agent 的（关键词，权重）列表；同分时按 ``PRIORITY`` 决定顺序。
_INTENT_KEYWORDS: dict[str, tuple[tuple[str, float], ...]] = {
    "alert": (
        ("提醒", 3.0),
        ("预警", 3.0),
        ("报警", 3.0),
        ("alert", 3.0),
        ("通知我", 2.5),
        ("监控", 2.0),
        ("涨到", 2.0),
        ("跌到", 2.0),
        ("突破", 1.5),
        ("触及", 1.5),
        ("提醒我", 3.0),
    ),
    "backtest": (
        ("回测", 3.5),
        ("backtest", 3.5),
        ("复盘", 2.0),
        ("夏普", 2.0),
        ("最大回撤", 1.5),
        ("策略", 1.5),
        ("胜率", 1.5),
        ("收益率", 1.0),
        ("历史表现", 1.5),
    ),
    "portfolio": (
        ("持仓", 3.0),
        ("仓位", 2.5),
        ("组合", 2.0),
        ("盈亏", 2.5),
        ("买入", 2.0),
        ("卖出", 2.0),
        ("加仓", 2.5),
        ("减仓", 2.5),
        ("建仓", 2.5),
        ("平仓", 2.5),
        ("成本价", 2.0),
        ("portfolio", 3.0),
        ("holdings", 3.0),
        ("我买", 2.0),
        ("持有", 1.5),
    ),
    "stock_picker": (
        ("选股", 3.5),
        ("筛选", 3.0),
        ("选出", 3.0),
        ("挑", 2.0),
        ("推荐", 2.0),
        ("长尾", 3.5),
        ("低估", 2.0),
        ("有哪些股票", 2.5),
        ("找一找", 2.0),
        ("screen", 2.5),
        ("符合条件", 2.0),
        ("龙头", 1.5),
    ),
    # 基本面：估值、财报、宏观、指数对比
    "fundamental_analyst": (
        ("估值", 2.5),
        ("市盈率", 2.5),
        ("市净率", 2.5),
        ("pe", 1.5),
        ("pb", 1.5),
        ("财报", 2.5),
        ("财务", 2.5),
        ("利润", 2.0),
        ("营收", 2.0),
        ("净利润", 2.5),
        ("毛利率", 2.5),
        ("基本面", 2.5),
        ("宏观", 2.5),
        ("gdp", 2.0),
        ("cpi", 2.0),
        ("市值", 2.0),
        ("低估", 2.0),
        ("高估", 2.0),
        ("分红", 2.0),
        ("股息", 2.0),
    ),
    # 技术面：量价、指标、形态、资金
    "technical_analyst": (
        ("行情", 2.0),
        ("走势", 2.5),
        ("技术面", 2.5),
        ("技术分析", 2.5),
        ("指标", 2.5),
        ("macd", 2.5),
        ("rsi", 2.0),
        ("kdj", 2.0),
        ("布林", 2.0),
        ("均线", 2.0),
        ("k线", 2.0),
        ("形态", 2.0),
        ("金叉", 2.5),
        ("死叉", 2.5),
        ("超买", 2.0),
        ("超卖", 2.0),
        ("突破", 1.5),
        ("资金流", 2.5),
        ("主力", 2.0),
        ("龙虎榜", 2.5),
        ("涨跌停", 2.0),
        ("成交量", 2.0),
        ("量价", 2.5),
        ("概念", 1.5),
    ),
    # 泛化提问：两者都像，默认给技术面（量价是最常被问的），
    # 但「深度/全面分析」会让 Supervisor 把两个都派出去。
    "analyst:general": (
        ("股价", 1.5),
        ("多少", 1.0),
        ("怎么样", 1.0),
        ("分析", 1.5),
        ("对比", 1.5),
        ("指数", 1.5),
        ("行业", 1.5),
    ),
}

#: ``analyst:general`` 是给「泛化提问」用的虚拟桶，命中后落到技术面分析师。
_GENERAL_ALIAS = {"analyst:general": "technical_analyst"}

#: 同分时的优先顺序（越具体的动作越靠前）。
PRIORITY: tuple[str, ...] = (
    "alert",
    "backtest",
    "portfolio",
    "stock_picker",
    "fundamental_analyst",
    "technical_analyst",
    "analyst:general",
)

#: 「深度 / 全面分析」这类请求要把两个分析师都派出去。
DEEP_ANALYSIS_MARKERS: tuple[str, ...] = (
    "深度分析",
    "全面分析",
    "完整分析",
    "详细分析",
    "多维度",
    "各个维度",
    "基本面和技术面",
    "技术面和基本面",
    "值得投资吗",
    "能不能买",
    "该不该买",
)


def wants_deep_analysis(text: str) -> bool:
    """用户是否明确要求「全面/深度」分析（需要两个分析师都上场）。"""

    lowered = (text or "").lower()
    return any(marker in lowered for marker in DEEP_ANALYSIS_MARKERS)


def classify_intent(text: str) -> tuple[str, str]:
    """用关键词打分返回 ``(agent, reason)``。"""

    ranked = classify_candidates(text)
    if not ranked:
        return "technical_analyst", "没有明显信号，默认交给技术面分析师"
    agent, score, matched = ranked[0]
    return agent, f"匹配关键词 {', '.join(matched[:4])}（得分 {score:g}）"


def classify_candidates(text: str) -> list[tuple[str, float, list[str]]]:
    """所有 Agent 按关键词得分排序（高到低，同分按 ``PRIORITY``）。"""

    lowered = (text or "").lower()
    ranked: list[tuple[str, float, list[str]]] = []
    for agent, keywords in _INTENT_KEYWORDS.items():
        total = 0.0
        matched: list[str] = []
        for keyword, weight in keywords:
            if keyword in lowered:
                total += weight
                matched.append(keyword)
        if total > 0:
            resolved = _GENERAL_ALIAS.get(agent, agent)
            ranked.append((resolved, total, matched))
    ranked.sort(key=lambda item: (-item[1], PRIORITY.index(item[0]) if item[0] in PRIORITY else 99))
    return ranked


def classify_next(text: str, completed: Iterable[str]) -> tuple[str, str]:
    """选出得分最高、且本轮尚未运行过的 Agent，否则返回 ``finish``。

    这正是「回测茅台，再看看我的持仓」这类双专家请求不会在第一个专家上打转的原因：
    第二次调用时第一个已标记为完成，于是把活交给次优匹配。

    「深度分析」类请求例外：它明确要求两个分析师都给意见，所以会把另一个也补上。
    """

    done = set(completed)

    if wants_deep_analysis(text):
        for analyst in ("fundamental_analyst", "technical_analyst"):
            if analyst not in done:
                return analyst, "深度分析请求：逐个分析师给意见，再交叉检查"

    for agent, score, matched in classify_candidates(text):
        if agent in done:
            continue
        return agent, f"匹配关键词 {', '.join(matched[:4])}（得分 {score:g}）"

    ranked = classify_candidates(text)
    if not ranked:
        return "finish", "没有剩余意图"
    return "finish", "所有匹配的专家都已运行"


# ----------------------------------------------------------------------
# 标的抽取
# ----------------------------------------------------------------------
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?:\.(SZ|SH|BJ|HK))?(?!\d)", re.IGNORECASE)


def extract_symbols(text: str) -> list[str]:
    """找出 ``text`` 里提到的所有可解析标的，按出现顺序排列。"""

    if not text:
        return []

    found: list[tuple[int, str]] = []

    for symbol in UNIVERSE + INDICES:
        for alias in (symbol.name, symbol.name.rstrip("AB")):
            if len(alias) < 2:
                continue
            index = text.find(alias)
            if index >= 0:
                found.append((index, symbol.ts_code))
                break

    for shorthand, code in SHORTHAND.items():
        if len(shorthand) < 2:
            continue
        index = text.find(shorthand)
        if index >= 0:
            found.append((index, code))

    for match in _CODE_RE.finditer(text):
        bare, suffix = match.group(1), match.group(2)
        token = f"{bare}.{suffix}" if suffix else bare
        resolved = resolve(token)
        code = resolved.ts_code if resolved else normalize_code(token)
        found.append((match.start(), code))

    ordered: list[str] = []
    for _index, code in sorted(found, key=lambda item: item[0]):
        if code not in ordered:
            ordered.append(code)
    return ordered


def first_symbol(text: str) -> str | None:
    symbols = extract_symbols(text)
    return symbols[0] if symbols else None


# ----------------------------------------------------------------------
# 策略抽取
# ----------------------------------------------------------------------
_STRATEGY_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("海龟", "turtle"), "turtle"),
    (("唐奇安", "donchian", "通道突破"), "donchian_breakout"),
    (("adx",), "adx_macd"),
    (("三均线", "triple"), "triple_ma"),
    (("ema", "乖离"), "ema_sma_bias"),
    (("随机指标", "stochastic", "kdj随机"), "stochastic"),
    (("williams", "威廉", "%r"), "williams_r"),
    (("cci",), "cci"),
    (("obv", "能量潮"), "obv_cross"),
    (("vwap", "均价线"), "vwap_deviation"),
    (("放量", "volume_breakout", "量价突破"), "volume_breakout"),
    (("kdj",), "kdj"),
    (("布林", "boll", "bollinger"), "boll_breakout"),
    (("动量", "roc", "momentum"), "momentum_roc"),
    (("rsi",), "rsi"),
    (("macd",), "macd"),
    (("双均线", "均线交叉", "ma_cross", "ma cross", "均线策略", "均线"), "ma_cross"),
)

_STRATEGY_ALIASES: dict[str, str] = {
    "均线": "ma_cross",
    "双均线": "ma_cross",
    "ma": "ma_cross",
    "macd": "macd",
    "rsi": "rsi",
    "kdj": "kdj",
    "boll": "boll_breakout",
    "布林": "boll_breakout",
    "turtle": "turtle",
    "海龟": "turtle",
}


def extract_strategy(text: str, default: str = "ma_cross") -> tuple[str, bool]:
    """返回 ``(strategy, was_explicit)``。"""

    lowered = (text or "").lower()

    # 「用 MACD 策略回测」——命中已注册策略名时优先级最高
    from finagent.core.backtest.registry import list_registered_strategies

    for name in list_registered_strategies():
        if name in lowered:
            return name, True

    for keywords, strategy in _STRATEGY_RULES:
        for keyword in keywords:
            if keyword in lowered:
                return strategy, True

    # 「布林回归」还是「布林突破」：家族确定后再细分
    if ("布林" in lowered or "boll" in lowered) and ("回归" in lowered or "反转" in lowered):
        return "boll_reversion", True

    return default, False


# ----------------------------------------------------------------------
# 日期区间
# ----------------------------------------------------------------------
_DAYS_RE = re.compile(r"(?:近|最近|过去|last|past)\s*(\d{1,3})\s*(?:个)?\s*(?:交易日|天|日|days?)")
_MONTHS_RE = re.compile(r"(?:近|最近|过去|last|past)\s*(\d{1,2})\s*(?:个)?\s*月")
_YEARS_RE = re.compile(r"(?:近|最近|过去|last|past)\s*(\d{1,2})\s*年")
_EXPLICIT_RANGE = re.compile(
    r"(\d{4}[-/]?\d{2}[-/]?\d{2})\s*(?:到|至|~|-|to)\s*(\d{4}[-/]?\d{2}[-/]?\d{2})"
)


@dataclass(slots=True)
class Window:
    start: str
    end: str
    label: str = ""
    explicit: bool = False

    def to_dict(self) -> dict[str, str]:
        return {"start": self.start, "end": self.end, "label": self.label}


def extract_window(text: str, *, default_days: int = 365) -> Window:
    """从自然语言里解析出日期区间。"""

    lowered = (text or "").lower()
    end = cal.latest_trading_day()

    match = _EXPLICIT_RANGE.search(lowered)
    if match:
        start = cal.format_date(match.group(1))
        end_str = cal.format_date(match.group(2))
        return Window(start, end_str, f"{start}-{end_str}", explicit=True)

    match = _DAYS_RE.search(lowered)
    if match:
        days = int(match.group(1))
        start = end - timedelta(days=days * 2 if days <= 10 else days)
        return Window(cal.format_date(start), cal.format_date(end), f"近{days}个交易日", True)

    match = _MONTHS_RE.search(lowered)
    if match:
        months = int(match.group(1))
        start = end - timedelta(days=int(months * 30.4))
        return Window(cal.format_date(start), cal.format_date(end), f"近{months}个月", True)

    match = _YEARS_RE.search(lowered)
    if match:
        years = int(match.group(1))
        start = end - timedelta(days=int(years * 365.25))
        return Window(cal.format_date(start), cal.format_date(end), f"近{years}年", True)

    if "今年" in lowered or "年初至今" in lowered or "ytd" in lowered:
        start = date(end.year, 1, 1)
        return Window(cal.format_date(start), cal.format_date(end), "今年以来", True)

    keywords: tuple[tuple[tuple[str, ...], int, str], ...] = (
        (("一年", "1年", "12个月", "one year"), 365, "近一年"),
        (("半年", "6个月", "六个月", "half year"), 183, "近半年"),
        (("季度", "3个月", "三个月", "一季度"), 91, "近三个月"),
        (("两周", "2周"), 14, "近两周"),
        (("一周", "1周", "7天", "最近一周", "这周"), 7, "近一周"),
        (("一个月", "1个月", "近一个月", "上月"), 30, "近一个月"),
        (("两年", "2年", "24个月"), 730, "近两年"),
    )
    for needles, days, label in keywords:
        if any(needle in lowered for needle in needles):
            return Window(
                cal.format_date(end - timedelta(days=days)), cal.format_date(end), label, True
            )

    return Window(
        cal.format_date(end - timedelta(days=default_days)),
        cal.format_date(end),
        f"近{default_days}天",
        False,
    )


def extract_start_only(text: str) -> str:
    """选股只关心锚定日期。"""

    lowered = (text or "").lower()
    match = re.search(r"(\d{4}[-/]?\d{2}[-/]?\d{2})", lowered)
    if match:
        try:
            return cal.format_date(match.group(1))
        except ValueError:
            pass
    return cal.format_date(cal.latest_trading_day())


# ----------------------------------------------------------------------
# 预警条件抽取
# ----------------------------------------------------------------------
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_PCT_UP_RE = re.compile(r"(?:上涨|涨|上升|反弹|拉升)\s*(\d+(?:\.\d+)?)\s*%")
_PCT_DOWN_RE = re.compile(r"(?:下跌|跌|下降|回落|回调)\s*(\d+(?:\.\d+)?)\s*%")
_PRICE_TARGET_RE = re.compile(r"(?:涨到|升到|突破|高于|超过|达到|到)\s*(\d+(?:\.\d+)?)")
_PRICE_DOWN_RE = re.compile(r"(?:跌到|跌破|低于|回落到|下探)\s*(\d+(?:\.\d+)?)")


@dataclass(slots=True)
class AlertSpec:
    ts_code: str | None = None
    operator: str | None = None
    threshold: float | None = None
    pct_change: float | None = None
    note: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.threshold is not None or self.pct_change is not None)


def extract_alert_spec(text: str) -> AlertSpec:
    """从请求里取出目标价或百分比变动。"""

    spec = AlertSpec(ts_code=first_symbol(text), note=(text or "").strip()[:120])
    lowered = (text or "").lower()

    up_pct = _PCT_UP_RE.search(lowered)
    if up_pct:
        spec.pct_change = abs(float(up_pct.group(1)))
        spec.operator = ">"
        return spec

    down_pct = _PCT_DOWN_RE.search(lowered)
    if down_pct:
        spec.pct_change = -abs(float(down_pct.group(1)))
        spec.operator = "<"
        return spec

    target = _PRICE_TARGET_RE.search(lowered)
    if target:
        spec.threshold = float(target.group(1))
        spec.operator = ">"
        return spec

    down_target = _PRICE_DOWN_RE.search(lowered)
    if down_target:
        spec.threshold = float(down_target.group(1))
        spec.operator = "<"
        return spec

    pct = _PCT_RE.search(lowered)
    if pct:
        value = float(pct.group(1))
        spec.pct_change = (
            -value if any(word in lowered for word in ("跌", "下降", "回落", "回调")) else value
        )
        spec.operator = "<" if spec.pct_change < 0 else ">"
    return spec


# ----------------------------------------------------------------------
# 画像抽取
# ----------------------------------------------------------------------
_RISK_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("保守", "稳健型", "低风险", "不能亏", "风险承受能力低"), "Conservative"),
    (("激进", "高风险", "追涨", "风险偏好高"), "Aggressive"),
    (("稳健", "平衡", "中等风险", "balanced"), "Balanced"),
)

_HORIZON_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("短线", "短期", "做t", "日内"), "Short-term"),
    (("中线", "中期", "波段"), "Medium-term"),
    (("长线", "长期", "价值投资", "持有多年"), "Long-term"),
)

_AVOID_MARKERS = ("不买", "回避", "不喜欢", "不要", "避开", "禁投", "厌恶")
_PREFER_MARKERS = ("喜欢", "偏好", "看好", "只买", "关注", "重仓")


@dataclass(slots=True)
class ProfileHints:
    risk_tolerance: str | None = None
    investment_horizon: str | None = None
    favorite_sectors: list[str] = field(default_factory=list)
    avoid_sectors: list[str] = field(default_factory=list)
    investment_style: str | None = None

    def is_empty(self) -> bool:
        return not any(
            [
                self.risk_tolerance,
                self.investment_horizon,
                self.favorite_sectors,
                self.avoid_sectors,
                self.investment_style,
            ]
        )

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        if self.risk_tolerance:
            payload["risk_tolerance"] = self.risk_tolerance
        if self.investment_horizon:
            payload["investment_horizon"] = self.investment_horizon
        if self.favorite_sectors:
            payload["favorite_sectors"] = self.favorite_sectors
        if self.avoid_sectors:
            payload["avoid_sectors"] = self.avoid_sectors
        if self.investment_style:
            payload["investment_style"] = self.investment_style
        return payload


def _known_sectors() -> list[str]:
    from finagent.core.symbols import all_industries

    sectors = set(all_industries())
    sectors.update({"科技", "消费", "新能源", "医药", "金融", "半导体", "军工", "地产", "周期"})
    return sorted(sectors, key=len, reverse=True)


def extract_profile_hints(text: str) -> ProfileHints:
    """从一句话里推断持久偏好（供记忆回写使用）。"""

    lowered = (text or "").lower()
    hints = ProfileHints()

    for needles, risk in _RISK_RULES:
        if any(needle in lowered for needle in needles):
            hints.risk_tolerance = risk
            break

    for needles, horizon in _HORIZON_RULES:
        if any(needle in lowered for needle in needles):
            hints.investment_horizon = horizon
            break

    for sector in _known_sectors():
        if sector not in lowered:
            continue
        index = lowered.find(sector)
        prefix = lowered[max(0, index - 8) : index]
        if any(marker in prefix for marker in _AVOID_MARKERS):
            if sector not in hints.avoid_sectors:
                hints.avoid_sectors.append(sector)
        elif any(marker in prefix for marker in _PREFER_MARKERS):
            if sector not in hints.favorite_sectors:
                hints.favorite_sectors.append(sector)

    style_markers = (
        ("高股息", "偏好高股息"),
        ("分红", "偏好稳定分红"),
        ("低估值", "偏好低估值"),
        ("成长", "偏好成长股"),
        ("价值", "价值投资"),
        ("打新", "参与打新"),
    )
    for marker, style in style_markers:
        if marker in lowered:
            hints.investment_style = style
            break

    return hints


def looks_like_preference(text: str) -> bool:
    """廉价的预筛，避免记忆节点每轮都跑抽取。"""

    markers = (
        "我是",
        "我喜欢",
        "我偏好",
        "我不买",
        "我只看",
        "我的风格",
        "风险偏好",
        "投资周期",
        "长期",
        "短期",
        "保守",
        "激进",
        "高股息",
        "分红",
    )
    lowered = (text or "").lower()
    return any(marker in lowered for marker in markers)


# ----------------------------------------------------------------------
# 选股条件抽取
# ----------------------------------------------------------------------
_NUM = r"(\d+(?:\.\d+)?)"


def extract_screen_criteria(text: str) -> dict[str, object]:
    """把自然语言选股请求映射成 ``screen_stocks`` 的参数。"""

    lowered = (text or "").lower()
    criteria: dict[str, object] = {}

    def grab(pattern: str) -> float | None:
        match = re.search(pattern, lowered)
        return float(match.group(1)) if match else None

    pe_max = grab(r"(?:市盈率|pe|PE)\s*(?:小于|低于|不超过|<=|<|≤)\s*" + _NUM)
    if pe_max is None:
        pe_max = grab(r"(?:小于|低于|不超过)\s*" + _NUM + r"\s*倍?(?:市盈率|pe)")
    if pe_max is not None:
        criteria["pe_max"] = pe_max

    pe_min = grab(r"(?:市盈率|pe)\s*(?:大于|高于|超过|>=|>|≥)\s*" + _NUM)
    if pe_min is not None:
        criteria["pe_min"] = pe_min

    pb_max = grab(r"(?:市净率|pb)\s*(?:小于|低于|不超过|<=|<|≤)\s*" + _NUM)
    if pb_max is not None:
        criteria["pb_max"] = pb_max

    mv_min = grab(r"(?:总市值|市值)\s*(?:大于|高于|超过|>=|>|≥)\s*" + _NUM)
    if mv_min is not None:
        criteria["mv_min"] = mv_min

    mv_max = grab(r"(?:总市值|市值)\s*(?:小于|低于|不超过|<=|<|≤)\s*" + _NUM)
    if mv_max is not None:
        criteria["mv_max"] = mv_max

    if re.search(r"(?:股息率|分红率)\s*(?:大于|高于|超过)\s*" + _NUM, lowered):
        # 选股池的字段里没有股息率；把这个偏好留作风格线索，而不是悄悄忽略它。
        pass

    industries = [sector for sector in _known_sectors() if sector in (text or "")]
    if industries:
        criteria["industries"] = industries

    if any(word in lowered for word in ("低估", "便宜", "价值")):
        criteria.setdefault("pe_max", 20.0)
        criteria.setdefault("pb_max", 3.0)

    if re.search(r"(?:跌幅|下跌|跌了)", lowered) and "pct_chg_max" not in criteria:
        criteria["pct_chg_max"] = -1.0

    limit = grab(r"(?:前|top)\s*(\d+)")
    criteria["limit"] = int(limit) if limit else 20
    return criteria


def summarise_intents(agents: Iterable[str]) -> str:
    from finagent.graph.state import AGENT_LABELS

    return " → ".join(AGENT_LABELS.get(agent, agent) for agent in agents)
