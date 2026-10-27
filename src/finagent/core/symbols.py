"""标的库与自然语言标的解析。

用户的输入千奇百怪：中文全名、口语简称、6 位裸代码、带后缀的 ts_code。解析在这里统一
收敛成一个确定性、可测试的服务，同时也作为 MCP 工具（``resolve_symbol``）暴露出去。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from finagent.errors import UnknownSymbol

# 为 6 位裸代码推断交易所后缀。
_SH_PREFIXES = ("5", "6", "9", "11", "13")
_BJ_PREFIXES = ("4", "8")


@dataclass(frozen=True, slots=True)
class Symbol:
    """一个可解析的标的。"""

    ts_code: str
    name: str
    industry: str
    market: str = "主板"

    @property
    def bare(self) -> str:
        return self.ts_code.split(".")[0]


#: 离线数据源与名称解析共用的参考标的库。
UNIVERSE: tuple[Symbol, ...] = (
    Symbol("000001.SZ", "平安银行", "银行"),
    Symbol("600519.SH", "贵州茅台", "白酒"),
    Symbol("600036.SH", "招商银行", "银行", "主板"),
    Symbol("000858.SZ", "五粮液", "白酒"),
    Symbol("601318.SH", "中国平安", "保险"),
    Symbol("600030.SH", "中信证券", "证券"),
    Symbol("300750.SZ", "宁德时代", "电池", "创业板"),
    Symbol("002594.SZ", "比亚迪", "汽车"),
    Symbol("601899.SH", "紫金矿业", "有色金属"),
    Symbol("600900.SH", "长江电力", "电力"),
    Symbol("601398.SH", "工商银行", "银行"),
    Symbol("601288.SH", "农业银行", "银行"),
    Symbol("601988.SH", "中国银行", "银行"),
    Symbol("600000.SH", "浦发银行", "银行"),
    Symbol("601166.SH", "兴业银行", "银行"),
    Symbol("600016.SH", "民生银行", "银行"),
    Symbol("000002.SZ", "万科A", "房地产"),
    Symbol("600048.SH", "保利发展", "房地产"),
    Symbol("601012.SH", "隆基绿能", "光伏", "主板"),
    Symbol("002475.SZ", "立讯精密", "电子"),
    Symbol("000725.SZ", "京东方A", "电子"),
    Symbol("002415.SZ", "海康威视", "电子"),
    Symbol("688981.SH", "中芯国际", "半导体", "科创板"),
    Symbol("688111.SH", "金山办公", "软件", "科创板"),
    Symbol("600276.SH", "恒瑞医药", "医药"),
    Symbol("300760.SZ", "迈瑞医疗", "医疗器械", "创业板"),
    Symbol("600887.SH", "伊利股份", "食品"),
    Symbol("603288.SH", "海天味业", "食品"),
    Symbol("601888.SH", "中国中免", "商业"),
    Symbol("600009.SH", "上海机场", "交通运输"),
    Symbol("601111.SH", "中国国航", "交通运输"),
    Symbol("600028.SH", "中国石化", "石油"),
    Symbol("601857.SH", "中国石油", "石油"),
    Symbol("600050.SH", "中国联通", "通信"),
    Symbol("601728.SH", "中国电信", "通信"),
    Symbol("000063.SZ", "中兴通讯", "通信"),
    Symbol("300059.SZ", "东方财富", "证券", "创业板"),
    Symbol("600585.SH", "海螺水泥", "建材"),
    Symbol("601390.SH", "中国中铁", "建筑"),
    Symbol("000568.SZ", "泸州老窖", "白酒"),
    Symbol("600690.SH", "海尔智家", "家电"),
    Symbol("000333.SZ", "美的集团", "家电"),
    Symbol("601668.SH", "中国建筑", "建筑"),
    Symbol("601601.SH", "中国太保", "保险"),
    Symbol("002304.SZ", "洋河股份", "白酒"),
    Symbol("300124.SZ", "汇川技术", "机械", "创业板"),
)

#: 基准指数。
INDICES: tuple[Symbol, ...] = (
    Symbol("000001.SH", "上证指数", "指数"),
    Symbol("399001.SZ", "深证成指", "指数"),
    Symbol("399006.SZ", "创业板指", "指数"),
    Symbol("000300.SH", "沪深300", "指数"),
    Symbol("000905.SH", "中证500", "指数"),
)

_BY_CODE = {s.ts_code.upper(): s for s in UNIVERSE + INDICES}
#: 6 位裸代码优先匹配个股而不是指数（``000001`` → 平安银行）。
_BY_BARE: dict[str, Symbol] = {}
for _symbol in UNIVERSE + INDICES:
    _BY_BARE.setdefault(_symbol.bare, _symbol)
_BY_NAME = {s.name: s for s in UNIVERSE + INDICES}

#: 对话中常见的中文简称，映射到 ts_code。
_SHORTHAND: dict[str, str] = {
    "茅台": "600519.SH",
    "五粮液": "000858.SZ",
    "泸州老窖": "000568.SZ",
    "洋河": "002304.SZ",
    "招行": "600036.SH",
    "招商银行": "600036.SH",
    "工行": "601398.SH",
    "农行": "601288.SH",
    "中行": "601988.SH",
    "建行": "601398.SH",
    "民生": "600016.SH",
    "兴业": "601166.SH",
    "浦发": "600000.SH",
    "平安": "000001.SZ",
    "平安银行": "000001.SZ",
    "中国平安": "601318.SH",
    "中信": "600030.SH",
    "中信证券": "600030.SH",
    "招商证券": "600030.SH",
    "宁德": "300750.SZ",
    "宁德时代": "300750.SZ",
    "比亚迪": "002594.SZ",
    "隆基": "601012.SH",
    "紫金": "601899.SH",
    "长电": "600900.SH",
    "长江电力": "600900.SH",
    "中免": "601888.SH",
    "海康": "002415.SZ",
    "京东方": "000725.SZ",
    "立讯": "002475.SZ",
    "恒瑞": "600276.SH",
    "迈瑞": "300760.SZ",
    "中芯": "688981.SH",
    "金山": "688111.SH",
    "美的": "000333.SZ",
    "海尔": "600690.SH",
    "伊利": "600887.SH",
    "海天": "603288.SH",
    "万科": "000002.SZ",
    "保利": "600048.SH",
    "中石化": "600028.SH",
    "中石油": "601857.SH",
    "上证": "000001.SH",
    "上证指数": "000001.SH",
    "沪指": "000001.SH",
    "深指": "399001.SZ",
    "创业板指": "399006.SZ",
    "沪深300": "000300.SH",
    "中证500": "000905.SH",
}

_BARE_RE = re.compile(r"^\d{6}$")
_FULL_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ|HK)$", re.IGNORECASE)


def infer_suffix(bare: str) -> str:
    """为 6 位裸代码猜交易所后缀。"""

    if bare in _BY_BARE:
        return _BY_BARE[bare].ts_code.split(".")[-1]
    if bare.startswith(_SH_PREFIXES):
        return "SH"
    if bare.startswith(_BJ_PREFIXES):
        return "BJ"
    return "SZ"


def normalize_code(raw: str) -> str:
    """把用户输入规范成 ``NNNNNN.XX`` 形式，但不校验是否存在。"""

    token = str(raw).strip().upper().replace(" ", "")
    if not token:
        raise UnknownSymbol("标的为空。")

    match = _FULL_RE.match(token)
    if match:
        return f"{match.group(1)}.{match.group(2).upper()}"

    # "600519SH" 这种写法
    match = re.match(r"^(\d{6})(SH|SZ|BJ|HK)$", token)
    if match:
        return f"{match.group(1)}.{match.group(2)}"

    if _BARE_RE.match(token):
        return f"{token}.{infer_suffix(token)}"

    return token


def _aliases(symbol: Symbol) -> Iterable[str]:
    yield symbol.name
    yield symbol.bare
    yield symbol.ts_code
    # 去掉 A/B 股标记，让「万科」也能匹配「万科A」。
    if len(symbol.name) > 1 and symbol.name[-1] in "AB":
        yield symbol.name[:-1]


def resolve(raw: str | None) -> Symbol | None:
    """把用户文本解析成已知 :class:`Symbol`；解析不出时返回 ``None``。"""

    if raw is None:
        return None
    token = str(raw).strip()
    if not token:
        return None

    upper = token.upper()
    if upper in _BY_CODE:
        return _BY_CODE[upper]
    if token in _BY_NAME:
        return _BY_NAME[token]

    stripped = token.replace(" ", "")
    if stripped in _BY_NAME:
        return _BY_NAME[stripped]

    if _BARE_RE.match(stripped) and stripped in _BY_BARE:
        return _BY_BARE[stripped]

    try:
        normalized = normalize_code(token)
    except UnknownSymbol:
        normalized = upper
    if normalized in _BY_CODE:
        return _BY_CODE[normalized]

    # 对显示名做忽略大小写、去掉 A/B 标记的匹配。
    for symbol in UNIVERSE + INDICES:
        for alias in _aliases(symbol):
            if alias.upper() == upper:
                return symbol

    # 口语简称（「茅台」「招行」「沪指」）。
    shorthand_code = _SHORTHAND.get(stripped) or _SHORTHAND.get(token)
    if shorthand_code:
        return _BY_CODE.get(shorthand_code)

    # 唯一子串匹配让「茅台」这类简称可用，同时不在歧义时乱猜。
    if len(stripped) >= 2:
        candidates = [
            symbol
            for symbol in UNIVERSE + INDICES
            if stripped in symbol.name or symbol.name in stripped
        ]
        if len(candidates) == 1:
            return candidates[0]
    return None


def resolve_strict(raw: str | None) -> tuple[str, str]:
    """解析为 ``(ts_code, name)``，失败则抛 :class:`UnknownSymbol`。"""

    symbol = resolve(raw)
    if symbol is not None:
        return symbol.ts_code, symbol.name

    if raw is None or not str(raw).strip():
        raise UnknownSymbol(
            "未提供标的。",
            hint="可以传 ts_code（如 '600519.SH'）、6 位裸代码（如 '600519'）或中文名（如 '贵州茅台'）。",
        )

    # 未知但语法合法的代码：透传给数据源去试。
    try:
        normalized = normalize_code(str(raw))
    except UnknownSymbol:
        normalized = ""
    if _FULL_RE.match(normalized):
        return normalized, normalized
    raise UnknownSymbol(
        f"无法解析标的 '{raw}'。",
        hint="请使用 ts_code（'600519.SH'）、6 位代码，或已收录的中文名称。",
    )


def display_name(ts_code: str) -> str:
    """尽力给出显示名，找不到就退回代码。"""

    symbol = _BY_CODE.get(ts_code.upper())
    return symbol.name if symbol else ts_code


def by_industry(industry: str) -> list[Symbol]:
    return [s for s in UNIVERSE if s.industry == industry]


def all_industries() -> list[str]:
    return sorted({s.industry for s in UNIVERSE})


def universe_frame() -> list[dict[str, str]]:
    return [
        {
            "ts_code": s.ts_code,
            "name": s.name,
            "industry": s.industry,
            "market": s.market,
        }
        for s in UNIVERSE
    ]


#: 公开别名：口语简称 -> ts_code（供意图抽取使用）。
SHORTHAND: dict[str, str] = _SHORTHAND
