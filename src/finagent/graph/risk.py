"""确定性风控护栏（Risk Guard）。

这一层**刻意不用 LLM**。理由很实际：金融场景里有两类判断，一类需要推理，一类只需要规则。
「用户画像写着保守，结论却在建议加杠杆」属于后者——让概率模型去判断「要不要拦自己」，
既不可靠也不可审计。

三道硬检查：

1. **风险偏好冲突**：画像保守/稳健，结论却出现加杠杆、满仓、梭哈这类表述 → 改写并提示。
2. **强指令降级**：出现「立即买入」「必涨」「保证收益」这类绝对化表述 → 降级为可检验的说法，
   并检查该标的是否真的在用户持仓里（未持有就更不该给强指令）。
3. **数据来源标注**：结论里有数字，却没有标注数据来源与日期 → 自动补一条脚注。

命中任何一条都会写入 ``risk_flags``，并在「决策轨迹」里展示——用户看得到它被拦了什么。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from finagent.core import calendar as cal
from finagent.logging import get_logger

log = get_logger("graph.risk")

#: 画像里代表「低风险承受」的取值（中英文都认）。
CONSERVATIVE_VALUES = ("conservative", "balanced", "保守", "稳健", "低风险", "稳健型")

#: 与保守画像冲突的表述。
AGGRESSIVE_PHRASES = (
    "加杠杆",
    "上杠杆",
    "融资买入",
    "满仓",
    "梭哈",
    "全仓",
    "借贷",
    "借钱",
    "重仓买入",
)

#: 绝对化指令 → 降级后的说法。
DOWNGRADES: tuple[tuple[str, str], ...] = (
    ("立即买入", "可关注"),
    ("马上买入", "可关注"),
    ("立刻买入", "可关注"),
    ("果断买入", "可关注"),
    ("强烈建议买入", "可作为关注标的"),
    ("建议满仓", "建议控制仓位"),
    ("必涨", "存在上涨可能"),
    ("一定会涨", "可能上涨"),
    ("稳赚", "存在亏损可能"),
    ("保证收益", "不保证收益"),
    ("无风险", "风险有限但存在"),
    ("肯定能涨", "可能上涨"),
)

#: 强买入指令（用于判断「未持有却给强指令」）。
_STRONG_BUY_RE = re.compile(r"(立即买入|马上买入|建议买入|强烈建议买入|果断买入|可以买入)")

#: 数据来源标注的痕迹。
_SOURCE_MARKERS = ("数据来源", "数据截至", "来源：", "来源:", "数据日", "截至 20")

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


@dataclass(slots=True)
class RiskOutcome:
    text: str
    flags: list[dict[str, str]] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(flag.get("severity") == "high" for flag in self.flags)

    def to_dict(self) -> dict[str, Any]:
        return {"blocked": self.blocked, "flags": self.flags, "actions": self.actions}


def _flag(code: str, label: str, detail: str, severity: str = "high") -> dict[str, str]:
    return {"code": code, "label": label, "detail": detail, "severity": severity}


def risk_level(profile: dict[str, Any] | None) -> str:
    """把画像里的风险偏好归一成 ``conservative`` / ``aggressive`` / ``unknown``。"""

    value = str((profile or {}).get("risk_tolerance") or "").strip().lower()
    if not value:
        return "unknown"
    if any(needle in value for needle in CONSERVATIVE_VALUES):
        return "conservative"
    if "aggressive" in value or "激进" in value or "高风险" in value:
        return "aggressive"
    return "unknown"


# ----------------------------------------------------------------------
def check_risk_preference(
    text: str, profile: dict[str, Any] | None
) -> tuple[str, list[dict[str, str]], list[str]]:
    """保守画像 + 激进表述 → 拦截并改写。"""

    if risk_level(profile) != "conservative":
        return text, [], []

    hits = [phrase for phrase in AGGRESSIVE_PHRASES if phrase in text]
    if not hits:
        return text, [], []

    rewritten = text
    for phrase in hits:
        rewritten = rewritten.replace(phrase, "控制仓位")
    flag = _flag(
        "risk_preference",
        "风险偏好冲突",
        f"你的画像标记为保守型，但结论里出现「{'、'.join(hits)}」，已改写为控制仓位",
    )
    return rewritten, [flag], ["已将激进表述改写为「控制仓位」"]


def check_strong_directives(
    text: str, held_codes: set[str] | None = None
) -> tuple[str, list[dict[str, str]], list[str]]:
    """绝对化表述降级；顺带检查未持有却给强指令。"""

    flags: list[dict[str, str]] = []
    actions: list[str] = []
    rewritten = text

    downgraded: list[str] = []
    for original, replacement in DOWNGRADES:
        if original in rewritten:
            rewritten = rewritten.replace(original, replacement)
            downgraded.append(f"{original}→{replacement}")

    if downgraded:
        flags.append(
            _flag(
                "strong_directive",
                "绝对化表述已降级",
                "把不可验证的断言改成可检验的说法：" + "、".join(downgraded[:4]),
                "medium",
            )
        )
        actions.append("绝对化表述已降级为可检验措辞")

    # 未持有却给强买指令
    strong = _STRONG_BUY_RE.search(text)
    if strong and held_codes is not None:
        mentioned = set(re.findall(r"\d{6}\.(?:SH|SZ|BJ)", text))
        unheld = {code for code in mentioned if code not in held_codes}
        if unheld:
            flags.append(
                _flag(
                    "unheld_buy",
                    "未持有标的被给出买入指令",
                    f"结论对 {'、'.join(sorted(unheld)[:3])} 给出「{strong.group(1)}」，"
                    "但你当前没有这些标的的持仓，已提示需自行确认",
                    "medium",
                )
            )
            actions.append("对未持有标的的买入指令已加注提示")

    return rewritten, flags, actions


def check_source_annotation(
    text: str, *, provider: str, as_of: str | None = None
) -> tuple[str, list[dict[str, str]], list[str]]:
    """有数字却没标数据来源 → 补一条脚注。"""

    if not _NUMBER_RE.search(text):
        return text, [], []
    if any(marker in text for marker in _SOURCE_MARKERS):
        return text, [], []

    date = as_of or cal.format_date(cal.latest_trading_day())
    source = "Tushare" if provider == "tushare" else "离线演示数据"
    footer = (
        f"\n\n_数据来源：{source}，截至 {date}。以上为工具返回的原始数据整理，不构成投资建议。_"
    )
    flag = _flag(
        "missing_source",
        "缺少数据来源标注",
        f"结论含数字但未标注来源，已补注「{source}，截至 {date}」",
        "medium",
    )
    return text + footer, [flag], ["已补充数据来源与日期标注"]


# ----------------------------------------------------------------------
def guard(
    text: str,
    *,
    profile: dict[str, Any] | None = None,
    held_codes: set[str] | None = None,
    provider: str = "offline",
    as_of: str | None = None,
) -> RiskOutcome:
    """依次跑三道检查，返回改写后的正文与命中记录。"""

    if not (text or "").strip():
        return RiskOutcome(text=text or "")

    outcome = RiskOutcome(text=text)

    outcome.text, flags, actions = check_risk_preference(outcome.text, profile)
    outcome.flags += flags
    outcome.actions += actions

    outcome.text, flags, actions = check_strong_directives(outcome.text, held_codes)
    outcome.flags += flags
    outcome.actions += actions

    outcome.text, flags, actions = check_source_annotation(
        outcome.text, provider=provider, as_of=as_of
    )
    outcome.flags += flags
    outcome.actions += actions

    if outcome.flags:
        log.info(
            "风控命中 %d 条：%s",
            len(outcome.flags),
            "、".join(flag["label"] for flag in outcome.flags),
        )
    return outcome


def held_codes_from(artifacts: dict[str, Any] | None) -> set[str] | None:
    """从持仓产物里取出已持有的代码；没有产物时返回 ``None``（表示未知，跳过该检查）。"""

    portfolio = (artifacts or {}).get("portfolio")
    if not isinstance(portfolio, dict):
        return None
    positions = portfolio.get("positions") or []
    return {
        str(item.get("ts_code"))
        for item in positions
        if isinstance(item, dict) and item.get("ts_code")
    }
