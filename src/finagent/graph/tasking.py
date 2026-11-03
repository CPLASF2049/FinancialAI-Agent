"""任务分解与部分成功（2.0 P1-5）。

问题
----
「中国太保现在多少钱？最近一周的走势和估值怎么样？」——这一问里有四件事：
现价（必须）、近五日走势（必须）、PE/PB（必须）、技术指标与资金流（可选）。

现在只要有一件失败，模型往往把整段结论都写成"数据不可用"，
用户连已经拿到的价格都看不到。这是**一刀切的失败**。

做法
----
把请求拆成子任务，标上「必须 / 可选」，再按实际工具轨迹逐条判定：

* 必做项失败 → 整体 `partial`，明确告诉用户缺了什么
* 可选项失败 → 不影响交付，只在末尾提一句
* 必做项全部满足 → `complete`

关键：**可选项失败不该把已成功的必做项一起拖下水。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Need(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class Status(StrEnum):
    SATISFIED = "satisfied"
    MISSING = "missing"
    UNSUPPORTED = "unsupported"


class Overall(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(slots=True)
class SubTask:
    name: str
    label: str
    need: Need
    #: 满足它需要出现其中任一工具
    tools: tuple[str, ...] = ()
    status: Status = Status.MISSING

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "need": str(self.need),
            "status": str(self.status),
        }


@dataclass(slots=True)
class TaskPlan:
    subtasks: list[SubTask] = field(default_factory=list)

    @property
    def required(self) -> list[SubTask]:
        return [item for item in self.subtasks if item.need is Need.REQUIRED]

    @property
    def optional(self) -> list[SubTask]:
        return [item for item in self.subtasks if item.need is Need.OPTIONAL]

    def to_dict(self) -> dict[str, Any]:
        return {
            "subtasks": [item.to_dict() for item in self.subtasks],
            "required": [item.name for item in self.required],
            "optional": [item.name for item in self.optional],
        }


@dataclass(slots=True)
class Assessment:
    overall: Overall
    plan: TaskPlan
    #: 必做项里没拿到的
    missing_required: list[SubTask] = field(default_factory=list)
    #: 可选项里没拿到的（不影响交付）
    missing_optional: list[SubTask] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": str(self.overall),
            "missing_required": [item.label for item in self.missing_required],
            "missing_optional": [item.label for item in self.missing_optional],
            "plan": self.plan.to_dict(),
        }

    def notice(self) -> str:
        """给用户看的一句话（没有缺失就返回空串）。"""

        parts: list[str] = []
        if self.missing_required:
            parts.append("缺少：" + "、".join(item.label for item in self.missing_required))
        if self.missing_optional:
            parts.append("未覆盖：" + "、".join(item.label for item in self.missing_optional))
        return "；".join(parts)


# ----------------------------------------------------------------------
# 计划
# ----------------------------------------------------------------------
_TREND = re.compile(r"走势|趋势|涨|跌|表现|行情|最近.{0,3}(天|日|周|月)")
_VALUATION = re.compile(r"估值|市盈率|市净率|PE|PB|贵不贵|便宜")
_FUNDAMENTAL = re.compile(r"财报|净利润|营收|基本面|业绩|毛利率")
_TECHNICAL = re.compile(r"技术面|指标|MACD|KDJ|RSI|布林|形态")
_MONEYFLOW = re.compile(r"资金流|主力|北向|流入|流出")
_PRICE = re.compile(r"多少钱|现价|价格|报价|股价|收盘")


def plan_for(question: str) -> TaskPlan:
    """把一句话的问题拆成子任务。

    只做「用户明确问到什么」，不猜意图——猜错会让「必须项」虚增，
    反而更容易判成 partial。
    """

    text = str(question or "")
    tasks: list[SubTask] = []

    if _PRICE.search(text):
        tasks.append(
            SubTask(
                "current_price",
                "最新价格",
                Need.REQUIRED,
                tools=("get_realtime_price", "mx_financial_data"),
            )
        )
    if _TREND.search(text):
        tasks.append(
            SubTask(
                "price_trend",
                "近期走势",
                Need.REQUIRED,
                # 走势必须有价格序列：单次报价满足不了它
                tools=("get_daily_price", "mx_financial_data"),
            )
        )
    if _VALUATION.search(text):
        tasks.append(
            SubTask(
                "valuation",
                "估值（PE/PB/市值）",
                Need.REQUIRED,
                tools=("get_daily_basic", "mx_financial_data"),
            )
        )
    if _FUNDAMENTAL.search(text):
        tasks.append(
            SubTask(
                "fundamental",
                "财务基本面",
                Need.REQUIRED,
                tools=("get_income_statement", "mx_financial_data"),
            )
        )
    if _TECHNICAL.search(text):
        tasks.append(
            SubTask(
                "technical",
                "技术指标",
                Need.OPTIONAL,
                tools=("compute_indicators", "detect_patterns"),
            )
        )
    if _MONEYFLOW.search(text):
        tasks.append(
            SubTask(
                "moneyflow",
                "资金流向",
                Need.OPTIONAL,
                tools=("get_moneyflow",),
            )
        )

    # 一句都没匹配上：这多半是「聊偏好」「问定义」这类不需要行情数据的请求。
    # 记成**可选**，不能因为「没有工具调用」就判成部分完成——
    # 那会把一句正常的对话变成「缺少：可核验的数据」。
    if not tasks:
        tasks.append(
            SubTask(
                "any_evidence",
                "可核验的数据",
                Need.OPTIONAL,
                tools=("get_realtime_price", "get_daily_price", "mx_financial_data"),
            )
        )
    return TaskPlan(subtasks=tasks)


# ----------------------------------------------------------------------
# 判定
# ----------------------------------------------------------------------
def assess(plan: TaskPlan, trace: list[dict[str, Any]]) -> Assessment:
    """按工具轨迹判定每个子任务是否满足。"""

    called = {str(item.get("tool") or "") for item in trace if item.get("ok")}
    unsupported = {
        str(item.get("tool") or "")
        for item in trace
        if not item.get("ok")
        and "unsupported" in str(item.get("status") or item.get("error") or "")
    }

    for task in plan.subtasks:
        if task.tools and any(tool in called for tool in task.tools):
            task.status = Status.SATISFIED
        elif task.tools and any(tool in unsupported for tool in task.tools):
            task.status = Status.UNSUPPORTED
        else:
            task.status = Status.MISSING

    missing_required = [task for task in plan.required if task.status is not Status.SATISFIED]
    missing_optional = [task for task in plan.optional if task.status is not Status.SATISFIED]

    if not plan.required:
        overall = Overall.COMPLETE
    elif not missing_required:
        overall = Overall.COMPLETE
    elif len(missing_required) < len(plan.required):
        overall = Overall.PARTIAL
    else:
        overall = Overall.FAILED

    return Assessment(
        overall=overall,
        plan=plan,
        missing_required=missing_required,
        missing_optional=missing_optional,
    )


def notice_for(assessment: Assessment) -> str:
    """交付时的补充说明。complete 时不打扰用户。"""

    # 没有任何必做项 = 这轮本来就没承诺给数据（聊偏好、问定义…），一句话都别说。
    # 否则会冒出「已完成，但未覆盖：可核验的数据」这种废话。
    if not assessment.plan.required:
        return ""
    if assessment.overall is Overall.COMPLETE and not assessment.missing_optional:
        return ""
    text = assessment.notice()
    if not text:
        return ""
    prefix = (
        "部分完成（必做项有缺失）"
        if assessment.overall is not Overall.COMPLETE
        else "已完成，但以下内容未覆盖"
    )
    return f"{prefix}：{text}。"
