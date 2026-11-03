"""证据模型（2.0 P0-3）。

为什么把「证据」单独建模
------------------------
Critic 是语言模型：它能判断「这段话顺不顺、前后矛不矛盾」，判断不了
「32.25 这个数字是不是今天的收盘价」。后者需要的是**来源、字段、时点**，
是结构化事实，不是语感。

所以把结论里的每个数字抽成一条 :class:`EvidenceRecord`，带它的来源与验证状态。
报告再根据状态决定「哪些数字可以当确定结论发布」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class VerificationStatus(StrEnum):
    """证据等级。终稿据此决定措辞的确定性。"""

    VERIFIED = "verified"
    #: 有来源，但口径/时点不完整（例如缺复权声明）
    PARTIALLY_VERIFIED = "partially_verified"
    #: 找不到任何来源
    UNVERIFIED = "unverified"
    #: 多个来源给出不同数值
    CONFLICTED = "conflicted"
    #: 数据时点不满足任务要求（例如要「最近一周」但数据是上个月的）
    STALE = "stale"


class ClaimType(StrEnum):
    MARKET_PRICE = "market_price"
    CHANGE_PCT = "change_pct"
    RANGE_RETURN = "range_return"
    VALUATION = "valuation"
    MARKET_CAP = "market_cap"
    VOLUME = "volume"
    INDICATOR = "indicator"
    FUNDAMENTAL = "fundamental"
    OTHER = "other"


#: 可以当「确定数字」发布的证据等级。其余必须在措辞上降级。
PUBLISHABLE = frozenset({VerificationStatus.VERIFIED, VerificationStatus.PARTIALLY_VERIFIED})


@dataclass(slots=True)
class EvidenceRecord:
    """一条可核验的数字声明。"""

    claim_id: str
    claim_type: ClaimType
    value: float | str
    #: 单位（元/股、亿元、%…）。缺失会让不同口径的数字混在一起。
    unit: str | None = None
    #: 数据来源（eastmoney / sina / mx / offline）
    source: str = ""
    #: 来源字段（close / pe / pct_chg …）
    source_field: str = ""
    #: 数据时点（YYYYMMDD 或 YYYY-MM-DD）
    as_of_date: str = ""
    #: 抓取时间（ISO）
    retrieved_at: str = ""
    status: VerificationStatus = VerificationStatus.UNVERIFIED
    #: 命中的工具调用 ID，便于回溯
    source_call_ids: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def publishable(self) -> bool:
        return self.status in PUBLISHABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "claim_type": str(self.claim_type),
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "source_field": self.source_field,
            "as_of_date": self.as_of_date,
            "retrieved_at": self.retrieved_at,
            "status": str(self.status),
            "source_call_ids": list(self.source_call_ids),
            "note": self.note,
        }


@dataclass(slots=True)
class ValidationReport:
    """一次校验的完整结论。"""

    records: list[EvidenceRecord] = field(default_factory=list)
    #: 阻断项：关键数字无法核实或互相冲突
    blockers: list[str] = field(default_factory=list)
    #: 提示项：可以发布，但措辞需要降级
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """没有阻断项就是通过。"""

        return not self.blockers

    def by_status(self, status: VerificationStatus) -> list[EvidenceRecord]:
        return [item for item in self.records if item.status is status]

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for record in self.records:
            counts[str(record.status)] = counts.get(str(record.status), 0) + 1
        return {
            "total": len(self.records),
            "by_status": counts,
            "blockers": len(self.blockers),
            "warnings": len(self.warnings),
            "ok": self.ok,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary(),
            "records": [record.to_dict() for record in self.records],
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }
