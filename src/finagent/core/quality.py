"""金融数据质量契约：在数据进入 Agent 上下文之前做可执行校验。

为什么不是「让模型看一眼」
--------------------------
审查者能判断的是「这段话像不像有依据」，判断不了「这一行数据在决策时刻是否可得」。
后者是**可计算**的：给定事件时间、已知时间、决策时刻，答案只有「合法 / 不合法」。

三个时间概念
------------
* ``event_time``      —— 事情发生的时间（交易日、报告期）
* ``known_at``        —— 这条记录**首次可被观测**的时间（收盘后、公告日）
* ``eligible_from``   —— 经过延迟与政策约束后，最早允许影响决策的时间

因果要求不是 ``event_time <= known_at``，而是 **``known_at <= 决策时刻``**。
财报尤其如此：报告期是 2026-09-30，但公告可能 10-28 才出，用报告期当已知时间
就是 A 股回测里最隐蔽的前视偏差。

设计原则：**拒绝而非解释**
--------------------------
违反契约时抛 :class:`DataQualityViolation`（错误码 ``DATA_QUALITY_VIOLATION``），
而不是让模型在结论里写一句「数据可能存在延迟」——那种句子既不可测，也没人真的会看。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import pandas as pd

from finagent.errors import FinAgentError

#: A 股交易所能出现的后缀。
VALID_SUFFIXES = frozenset({"SH", "SZ", "BJ"})

#: 代码首位 -> 应属交易所。
_SUFFIX_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(60|68|90|51|58)\d{4}$"), "SH"),  # 沪市主板/科创板/沪 B/沪基金
    (re.compile(r"^(00|30|20|15|16|18)\d{4}$"), "SZ"),  # 深市主板/创业板/深 B/基金
    (re.compile(r"^(4|8|92)\d{5}$"), "BJ"),  # 北交所
)


class RuleCode(StrEnum):
    """契约规则编号。每条都能被单独关闭或单独测试。"""

    KNOWN_AT_MISSING = "known_at_missing"
    KNOWN_AT_IN_FUTURE = "known_at_in_future"
    SYMBOL_SUFFIX_MISMATCH = "symbol_suffix_mismatch"
    UNIT_UNDECLARED = "unit_undeclared"
    REPORT_PERIOD_UNKNOWN = "report_period_unknown"
    MISSING_VALUES = "missing_values"
    ADJUSTMENT_DISCONTINUITY = "adjustment_discontinuity"


class DataQualityViolation(FinAgentError):
    """数据违反质量契约。**不降级、不猜测**，直接把这一行/这批数据拒掉。"""

    code = "DATA_QUALITY_VIOLATION"

    def __init__(self, message: str, *, rule: str = "", detail: str = "") -> None:
        super().__init__(
            message,
            hint="该数据在决策时刻不可得或口径不明，已拒绝使用；请改用更早时点或核实来源。",
        )
        self.rule = rule
        self.detail = detail

    def to_payload(self) -> dict[str, Any]:
        payload = super().to_payload()
        if self.rule:
            payload["rule"] = self.rule
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(slots=True)
class Violation:
    rule: RuleCode
    message: str
    #: 阻断性违规会让整批数据被拒；非阻断的只记一条 flag。
    blocking: bool = True
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {"rule": str(self.rule), "message": self.message, "blocking": self.blocking}
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(slots=True)
class QualityReport:
    """一批数据的质量结论。``ok`` 为 False 意味着**不许进入上下文**。"""

    checked: int = 0
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(item.blocking for item in self.violations)

    def blocking(self) -> list[Violation]:
        return [item for item in self.violations if item.blocking]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "violations": [item.to_dict() for item in self.violations],
        }


# ----------------------------------------------------------------------
# 单条规则
# ----------------------------------------------------------------------
def check_symbol(ts_code: str, *, exchange: str | None = None) -> Violation | None:
    """代码与后缀/交易所必须一致，防止跨市场污染（把 601398.SZ 当成平安银行）。"""

    code, _, suffix = str(ts_code or "").partition(".")
    if not code or not suffix:
        return Violation(
            RuleCode.SYMBOL_SUFFIX_MISMATCH,
            f"代码缺少市场后缀：{ts_code!r}",
            detail="期望形如 600519.SH；缺失后缀无法判断市场。",
        )

    suffix = suffix.upper()
    if suffix not in VALID_SUFFIXES:
        return Violation(
            RuleCode.SYMBOL_SUFFIX_MISMATCH,
            f"未知市场后缀：{ts_code!r}",
        )

    expected = None
    for pattern, market in _SUFFIX_RULES:
        if pattern.match(code):
            expected = market
            break
    if expected and expected != suffix:
        return Violation(
            RuleCode.SYMBOL_SUFFIX_MISMATCH,
            f"代码 {code} 应属 {expected}，实际标注 {suffix}",
            detail="后缀与代码段不匹配，可能把不同市场的标的混在一起。",
        )

    if exchange:
        declared = str(exchange).upper()
        if declared not in (suffix, "SSE" if suffix == "SH" else ""):
            if not (suffix == "SH" and declared in ("SSE", "SH")):
                return Violation(
                    RuleCode.SYMBOL_SUFFIX_MISMATCH,
                    f"交易所字段 {exchange} 与后缀 .{suffix} 不一致",
                )
    return None


_DATE_RE = re.compile(r"^\d{8}$")


def _to_date(value: Any) -> pd.Timestamp | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return pd.Timestamp(pd.to_datetime(text, format=fmt))
        except (ValueError, TypeError):
            continue
    return None


def check_known_at(
    frame: pd.DataFrame,
    *,
    decision_time: Any,
    column: str = "known_at",
    fallback: str | None = "trade_date",
) -> QualityReport:
    """因果校验：``known_at <= 决策时刻``，缺失 ``known_at`` 直接拒绝。

    允许 ``fallback`` 指定一个列作为 ``known_at`` 的兜底（例如日线的 ``trade_date``：
    收盘后即可观测）。**没有兜底且没有 known_at 就是违规**——默认放行等于放弃校验。
    """

    report = QualityReport(checked=0)
    stamp = _to_date(decision_time)
    if stamp is None:
        raise DataQualityViolation(
            f"决策时刻无法解析：{decision_time!r}",
            rule=str(RuleCode.KNOWN_AT_MISSING),
        )

    if column in frame.columns:
        known = frame[column]
    elif fallback and fallback in frame.columns:
        known = frame[fallback]
    else:
        report.violations.append(
            Violation(
                RuleCode.KNOWN_AT_MISSING,
                f"数据缺少 {column} 字段，无法判断在决策时刻是否可得",
                detail=f"可用列：{', '.join(map(str, frame.columns))}",
            )
        )
        return report

    report.checked = len(frame)
    for index, raw in enumerate(known.tolist()):
        parsed = _to_date(raw)
        if parsed is None:
            report.violations.append(
                Violation(
                    RuleCode.KNOWN_AT_MISSING,
                    f"第 {index} 行缺少可解析的时间",
                    detail=f"{column}={raw!r}",
                )
            )
            continue
        if parsed > stamp:
            report.violations.append(
                Violation(
                    RuleCode.KNOWN_AT_IN_FUTURE,
                    f"第 {index} 行的数据在决策时刻尚不可得（{parsed.date()} > {stamp.date()}）",
                    detail="这是前视偏差：用了未来数据做当下的判断。",
                )
            )
    return report


def check_report_period(
    frame: pd.DataFrame, *, announce_col: str = "announce_date"
) -> QualityReport:
    """财报必须区分「报告期」与「公告日」。

    只有报告期（``end_date``）而拿不到公告日的财报，**不能**在回测里当已知数据用——
    但那需要产品决策，所以这里记为**非阻断** flag：允许用，但必须被标注出来。
    """

    report = QualityReport(checked=len(frame))
    if "end_date" in frame.columns and announce_col not in frame.columns:
        report.violations.append(
            Violation(
                RuleCode.REPORT_PERIOD_UNKNOWN,
                "财报数据只有报告期，没有公告日",
                blocking=False,
                detail="报告期不等于可得时间；回测中可能构成前视偏差。",
            )
        )
    return report


def check_units(frame: pd.DataFrame, *, required: dict[str, str] | None = None) -> QualityReport:
    """关键指标必须声明口径与单位。

    ``required`` 形如 ``{"pe": "TTM", "total_mv": "亿元"}``——声明的值会在
    ``unit_declared`` 列里核对。没有声明就记一条违规。
    """

    report = QualityReport(checked=len(frame))
    declared = frame.attrs.get("units", {}) if hasattr(frame, "attrs") else {}
    for column, expected in (required or {}).items():
        if column not in frame.columns:
            continue
        actual = declared.get(column) if isinstance(declared, dict) else None
        if not actual:
            report.violations.append(
                Violation(
                    RuleCode.UNIT_UNDECLARED,
                    f"{column} 未声明口径/单位（期望 {expected}）",
                    blocking=False,
                    detail="估值与市值口径不明会让结论失去可比性。",
                )
            )
    return report


def check_missing(frame: pd.DataFrame, *, columns: list[str] | None = None) -> QualityReport:
    """缺失值必须被显式标记，不允许静默 fillna(0)。

    这里只**检测**：哪些关键字段有多少缺失，记一条非阻断 flag 供结论标注置信度。
    """

    report = QualityReport(checked=len(frame))
    targets = columns or [str(name) for name in frame.columns]
    empty: list[str] = []
    for column in targets:
        if column in frame.columns and frame[column].isna().any():
            empty.append(f"{column}({int(frame[column].isna().sum())})")
    if empty:
        report.violations.append(
            Violation(
                RuleCode.MISSING_VALUES,
                "存在缺失值，结论置信度应相应降低",
                blocking=False,
                detail=", ".join(empty),
            )
        )
    return report


# ----------------------------------------------------------------------
def check_adjustment_continuity(
    previous: pd.DataFrame,
    current: pd.DataFrame,
    *,
    tolerance: float = 0.005,
    column: str = "close",
) -> QualityReport:
    """复权因子链连续性：两次取到的同一段历史，价格比例不该跳变。

    供应商切换复权口径（前复权 / 后复权 / 不复权）时**不会报错**，只会让历史价格
    整体平移。这在回测里表现为「昨天还是 100 元的股票，今天历史变成 110 元」——
    所有基于价格比例的指标都会悄悄失真。这里用重叠区间的比值来检测。
    """

    report = QualityReport()
    if previous is None or current is None or previous.empty or current.empty:
        return report
    if "trade_date" not in previous.columns or "trade_date" not in current.columns:
        return report

    left = previous[["trade_date", column]].dropna().set_index("trade_date")[column]
    right = current[["trade_date", column]].dropna().set_index("trade_date")[column]
    common = left.index.intersection(right.index)
    if len(common) == 0:
        return report

    report.checked = len(common)
    ratios = (right.loc[common] / left.loc[common]).astype(float)
    drift = (ratios - 1.0).abs().max()
    if drift > tolerance:
        report.violations.append(
            Violation(
                RuleCode.ADJUSTMENT_DISCONTINUITY,
                f"复权口径不一致：重叠区间价格比例最大偏离 {drift:.2%}",
                detail=(
                    "同一段历史两次取到的价格不成比例，供应商可能换了复权方式；"
                    "回测结果会因此失真。"
                ),
            )
        )
    return report


def validate_series(
    frame: pd.DataFrame,
    *,
    decision_time: Any,
    units: dict[str, str] | None = None,
    require_known_at: bool = True,
) -> QualityReport:
    """时间序列的统一入口：符号 + 因果 + 单位 + 缺失。

    返回的 ``report.ok`` 为 False 时调用方**必须**拒绝使用这批数据。
    """

    report = QualityReport()

    if "ts_code" in frame.columns:
        for code in frame["ts_code"].dropna().unique():
            issue = check_symbol(str(code))
            if issue:
                report.violations.append(issue)

    if require_known_at:
        report.violations.extend(check_known_at(frame, decision_time=decision_time).violations)

    if units:
        report.violations.extend(check_units(frame, required=units).violations)

    report.violations.extend(check_missing(frame).violations)
    report.checked = len(frame)
    return report


def enforce(report: QualityReport, *, context: str = "") -> None:
    """把阻断性违规升级为异常——这是「拒绝而非解释」的落点。"""

    if report.ok:
        return
    first = report.blocking()[0]
    prefix = f"{context}：" if context else ""
    raise DataQualityViolation(
        f"{prefix}{first.message}",
        rule=str(first.rule),
        detail=first.detail,
    )
