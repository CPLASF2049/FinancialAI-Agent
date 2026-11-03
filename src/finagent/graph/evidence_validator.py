"""证据验证器（2.0 P0-3）：核心检查用确定性代码，不靠模型自觉。

分工
----
* **Critic（LLM）**：逻辑、一致性、问题覆盖度
* **Evidence Validator（本模块）**：来源、数值、计算、时效性

为什么必须分开：Critic 说「15/16 个数字有出处」只能说明它没看出问题，
不能说明数字对。而「有没有出处」是可计算的——那就用代码算。

两件确定性的事
--------------
1. **区间涨幅重算**：模型不该自己算涨跌幅。给定首尾收盘价，代码算
   ``(end / start - 1) * 100``，再和结论里声称的比较。
2. **时效检查**：数据时点晚于要求窗口就是 ``STALE``，而不是"看起来有点旧"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from finagent.graph.evidence_models import (
    ClaimType,
    EvidenceRecord,
    ValidationReport,
    VerificationStatus,
)
from finagent.logging import get_logger

log = get_logger("graph.evidence")

_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
#: 百分比单独抓，避免和价格混在一起
_PCT_RE = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)\s*%")
#: 日期（20261103 / 2026-11-03）
_DATE_RE = re.compile(r"\b(20\d{2})-?(\d{2})-?(\d{2})\b")


#: 数字匹配容差：0.5% 或 0.011（与 critic 一致，避免浮点与四舍五入打架）
def _close(value: float, other: float, tolerance: float | None = None) -> bool:
    if tolerance is None:
        tolerance = max(0.011, abs(other) * 0.0005)
    return abs(value - other) <= tolerance


@dataclass(slots=True)
class PricePoint:
    date: str
    close: float


# ----------------------------------------------------------------------
# 确定性计算：不交给模型
# ----------------------------------------------------------------------
def range_return(points: list[PricePoint]) -> float | None:
    """区间收益率（%）。给定首尾收盘价，代码算，不让模型编。"""

    if len(points) < 2:
        return None
    first, last = points[0], points[-1]
    if first.close <= 0:
        return None
    return round((last.close / first.close - 1.0) * 100.0, 2)


def change_pct(prev_close: float, close: float) -> float | None:
    if prev_close <= 0:
        return None
    return round((close / prev_close - 1.0) * 100.0, 2)


# ----------------------------------------------------------------------
# 从工具轨迹里抽证据
# ----------------------------------------------------------------------
def evidence_from_trace(trace: list[dict[str, Any]]) -> list[EvidenceRecord]:
    """把工具返回的数字摊平成证据池。

    这里**不解析语义**，只做「这个数字来自哪个工具的哪个字段」的登记。
    真正判断「结论里的数字能不能对上」在 :func:`validate`。
    """

    records: list[EvidenceRecord] = []
    for item in trace or []:
        if not item.get("ok"):
            continue
        tool = str(item.get("tool") or "")
        preview = str(item.get("preview") or "")
        call_id = str(item.get("call_id") or "")
        source = _source_of(tool, preview)
        as_of = _as_of(preview)
        retrieved = datetime.now().isoformat(timespec="seconds")

        for index, match in enumerate(_NUMBER_RE.finditer(preview)):
            raw = match.group(0).replace(",", "")
            # 8 位日期（20261001）也是数字，但它不是数据——混进证据池会把
            # 「区间涨幅」算成 -100%（拿日期当价格）。
            if "." not in raw and len(raw) == 8 and raw[:2] in ("19", "20"):
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            records.append(
                EvidenceRecord(
                    claim_id=f"{tool}-{call_id or index}-{index}",
                    claim_type=_claim_type_of(tool),
                    value=value,
                    source=source,
                    source_field=tool,
                    as_of_date=as_of,
                    retrieved_at=retrieved,
                    status=VerificationStatus.VERIFIED,
                    source_call_ids=[call_id] if call_id else [],
                )
            )
    return records


def _source_of(tool: str, preview: str) -> str:
    lowered = preview.lower()
    for name in ("eastmoney", "sina", "tushare", "offline"):
        if name in lowered:
            return name
    if tool.startswith("mx_"):
        return "mx"
    return "tool"


def _claim_type_of(tool: str) -> ClaimType:
    if "realtime_price" in tool or "daily_price" in tool:
        return ClaimType.MARKET_PRICE
    if "daily_basic" in tool:
        return ClaimType.VALUATION
    if "indicator" in tool or "pattern" in tool:
        return ClaimType.INDICATOR
    if "income" in tool or "financial" in tool:
        return ClaimType.FUNDAMENTAL
    return ClaimType.OTHER


def _as_of(preview: str) -> str:
    match = _DATE_RE.search(preview)
    return f"{match.group(1)}{match.group(2)}{match.group(3)}" if match else ""


# ----------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------
def validate(
    text: str,
    *,
    evidence: list[EvidenceRecord],
    as_of: str = "",
    now: str = "",
    max_age_days: int = 7,
) -> ValidationReport:
    """校验结论里的数字声明。

    三类检查：
    1. 每个数字能不能在证据池里找到（找不到 = UNVERIFIED）
    2. 声称的涨跌幅能不能用证据里的价格重算出来（对不上 = CONFLICTED）
    3. 证据时点是否满足时效（过旧 = STALE）
    """

    report = ValidationReport()
    pool = [float(record.value) for record in evidence if _is_number(record.value)]

    # --- 1) 数字可追溯 ---
    claims = _numbers_in(text)
    for index, value in enumerate(claims):
        record = EvidenceRecord(
            claim_id=f"claim-{index}",
            claim_type=ClaimType.OTHER,
            value=value,
        )
        hits = [candidate for candidate in pool if _close(value, candidate)]
        if hits:
            record.status = VerificationStatus.VERIFIED
            record.source = "evidence_pool"
        else:
            record.status = VerificationStatus.UNVERIFIED
        report.records.append(record)

    # --- 2) 涨跌幅重算 ---
    for claimed in _pct_claims(text):
        prices = [
            float(record.value)
            for record in evidence
            if record.claim_type is ClaimType.MARKET_PRICE
            and _is_number(record.value)
            and 0 < float(record.value) < 1_000_000
        ]
        recomputed = None
        if len(prices) >= 2:
            recomputed = round((prices[-1] / prices[0] - 1.0) * 100.0, 2)
        if recomputed is None:
            report.records.append(
                EvidenceRecord(
                    claim_id=f"pct-{claimed}",
                    claim_type=ClaimType.RANGE_RETURN,
                    value=claimed,
                    unit="%",
                    status=VerificationStatus.PARTIALLY_VERIFIED,
                    note="缺少足够的价格序列，无法重算区间涨幅",
                )
            )
            report.warnings.append(f"区间涨幅 {claimed}% 无法重算（价格序列不足）")
            continue

        # 只和「百分比类证据」或重算值比。拿 30% 去匹配池子里的 30.0 元价格
        # 会误判为已验证——单位和量纲都不是一回事。
        pct_pool = [
            float(record.value)
            for record in evidence
            if record.claim_type in (ClaimType.CHANGE_PCT, ClaimType.RANGE_RETURN)
            and _is_number(record.value)
        ]
        matched = any(_close(claimed, candidate) for candidate in pct_pool) or _close(
            claimed, recomputed
        )
        report.records.append(
            EvidenceRecord(
                claim_id=f"pct-{claimed}",
                claim_type=ClaimType.RANGE_RETURN,
                value=claimed,
                unit="%",
                status=(VerificationStatus.VERIFIED if matched else VerificationStatus.CONFLICTED),
                note=f"按证据价格重算为 {recomputed}%",
            )
        )
        if not matched:
            report.blockers.append(
                f"结论称区间涨幅 {claimed}%，但按工具返回的价格重算为 {recomputed}%"
            )

    # --- 3) 时效 ---
    if as_of and now:
        age = _age_days(as_of, now)
        if age is not None and age > max_age_days:
            report.blockers.append(
                f"数据时点 {as_of} 距查询日 {now} 已 {age} 天，超出 {max_age_days} 天时效"
            )
            for record in report.records:
                if record.status is VerificationStatus.VERIFIED:
                    record.status = VerificationStatus.STALE

    # --- 汇总：未核实的数字占到一半就阻断 ---
    # 单条编造的数字也必须拦住：一条假价格足以让整篇结论不可信。
    unverified = report.by_status(VerificationStatus.UNVERIFIED)
    total = len(report.records)
    if unverified and total and len(unverified) / total >= 0.5:
        report.blockers.append(
            f"{len(unverified)}/{total} 个数字在工具返回里找不到出处（如 {unverified[0].value}）"
        )

    log.debug("证据校验：%s", report.summary())
    return report


# ----------------------------------------------------------------------
def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _numbers_in(text: str) -> list[float]:
    out: list[float] = []
    for match in _NUMBER_RE.finditer(text or ""):
        raw = match.group(0).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        # 一位数多是序号；四位数且落在年份区间的是年份，都不作为论据
        if abs(value) < 10 and "." not in raw:
            continue
        if "." not in raw and 1990 <= abs(value) <= 2100:
            continue
        out.append(value)
    return out


def _pct_claims(text: str) -> list[float]:
    out: list[float] = []
    for match in _PCT_RE.finditer(text or ""):
        try:
            out.append(float(match.group(1).replace(",", "")))
        except ValueError:
            continue
    return out


def _age_days(as_of: str, now: str) -> int | None:
    def parse(value: str) -> datetime | None:
        text = str(value or "").strip()
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    start, end = parse(as_of), parse(now)
    if start is None or end is None:
        return None
    return (end - start).days
