"""结论审查者（Critic）。

多空辩论的降维版本：不引入第二个「会写文章」的 Agent，而是在专家产出结论后插一道
**结构化审查**，只回答一个问题——这份结论能不能直接交给用户。

三项检查固定不变（有 LLM 就让 LLM 做，没 LLM 就用规则做）：

1. **论据支撑**：结论里的数字，能不能在工具返回里找到出处？
2. **内部矛盾**：前后是否打架？与其他专家是否打架？（后者不是错误，是「分歧信号」）
3. **谄媚倾向**：结论是不是只是顺着用户提问里的预设往下说？

审查结论只有 ``pass`` / ``revise`` 两种。``revise`` 会把意见原样打回专家返工一轮，
由 ``FINAGENT_MAX_REVISIONS`` 封顶（默认 1 轮）。

**副作用型专家不参与返工**：持仓与预警的写入一旦成功，重跑会重复下单/重复建预警。
这两个专家只审不返工。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from finagent.graph.prompts import build_critic_prompt
from finagent.logging import get_logger

log = get_logger("graph.critic")

#: 允许返工的专家。持仓/预警有副作用，绝不重跑。
REVISABLE_AGENTS: frozenset[str] = frozenset(
    {"stock_picker", "fundamental_analyst", "technical_analyst", "backtest"}
)

#: 高严重度问题会导致 ``revise``；中等的只记录（例如「分歧信号」）。
HIGH = "high"
MEDIUM = "medium"

_TYPE_LABELS = {
    "unsupported": "论据无数据支撑",
    "contradiction": "内部矛盾",
    "sycophancy": "谄媚倾向",
}


@dataclass(slots=True)
class ReviewResult:
    verdict: str = "pass"
    issues: list[dict[str, str]] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    source: str = "rules"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "issues": self.issues,
            "checks": self.checks,
            "source": self.source,
        }

    @property
    def has_blocking(self) -> bool:
        return any(issue.get("severity") == HIGH for issue in self.issues)


def _issue(kind: str, detail: str, severity: str = HIGH) -> dict[str, str]:
    return {
        "type": kind,
        "label": _TYPE_LABELS.get(kind, kind),
        "detail": detail,
        "severity": severity,
    }


# ----------------------------------------------------------------------
# 数字抽取与比对
# ----------------------------------------------------------------------
_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?%?")
#: 日期、代码、章节序号这类数字不算「结论里的数字」。
_IGNORABLE = re.compile(r"^(?:19|20)\d{6}$")


def _numbers_in(text: str) -> list[str]:
    """抽出文本里的数字，丢掉日期、代码、序号这类噪音。"""

    found: list[str] = []
    for match in _NUMBER_RE.finditer(text or ""):
        token = match.group(0)
        digits = re.sub(r"[^\d.]", "", token)
        if not digits or _IGNORABLE.match(digits.replace(".", "")):
            continue
        integer = digits.split(".")[0]
        # 一位数多半是列表序号或「3 项」这类计数，不当作论据
        if len(integer) < 2 and "." not in digits:
            continue
        if digits not in found:
            found.append(digits)
    return found


def _normalise(raw: str) -> str:
    return re.sub(r"[,\s]", "", str(raw))


def _floats(text: str) -> list[float]:
    """把文本里的数字解析成浮点数（用于带容差的比对）。"""

    values: list[float] = []
    for token in _NUMBER_RE.findall(text or ""):
        cleaned = re.sub(r"[,\s%]", "", token)
        try:
            values.append(float(cleaned))
        except ValueError:
            continue
    return values


def _supported_by(claim: float, pool: list[float]) -> bool:
    """数字是否能在证据里找到「同一个值」。

    结论里的数字通常被四舍五入过（1650.956 → 1650.96、0.475% → 0.48%），逐字符比对会
    制造大量假阳性。这里按相对容差比较：``max(0.011, |v| * 0.0005)``，足以覆盖两位小数，
    又不足以让瞎编的价格蒙混过关。
    """

    for value in pool:
        if abs(claim - value) <= max(0.011, abs(value) * 0.0005):
            return True
    return False


def _support_ratio(answer: str, evidence: str) -> tuple[int, int]:
    """返回 ``(有出处的数字个数, 数字总数)``。"""

    claims = _floats(answer)
    if not claims:
        return 0, 0
    pool = _floats(evidence)
    supported = sum(1 for value in claims if _supported_by(value, pool))
    return supported, len(claims)


def check_evidence(answer: str, *, tool_calls: int, evidence: str) -> list[dict[str, str]]:
    """论据支撑检查。"""

    supported, total = _support_ratio(answer, evidence)

    if total == 0:
        return []

    if tool_calls == 0:
        return [
            _issue(
                "unsupported",
                f"本轮没有成功调用任何工具，却给出了 {total} 处数字（如 {_sample(answer)}）；"
                "这些数字没有数据来源",
            )
        ]

    if supported == 0:
        return [
            _issue(
                "unsupported",
                f"结论里的 {total} 处数字在工具返回中一处也找不到（如 {_sample(answer)}），"
                "可能来自记忆或臆测",
            )
        ]

    ratio = supported / total
    if total >= 5 and ratio < 0.5:
        return [
            _issue(
                "unsupported",
                f"{total} 处数字里只有 {supported} 处能在工具返回中找到出处"
                f"（{ratio:.0%}），偏低的支撑率",
                MEDIUM,
            )
        ]
    return []


def _sample(answer: str, limit: int = 2) -> str:
    items = _numbers_in(answer)[:limit]
    return "、".join(items) if items else "—"


# ----------------------------------------------------------------------
# 矛盾检查
# ----------------------------------------------------------------------
_BULLISH = (
    "看多",
    "看涨",
    "偏多",
    "强势",
    "向上",
    "上行",
    "突破",
    "金叉",
    "买入信号",
    "增持",
    "多头",
    "反弹",
    "利好",
    "低估",
)
_BEARISH = (
    "看空",
    "看淡",
    "偏空",
    "弱势",
    "向下",
    "下行",
    "跌破",
    "死叉",
    "卖出信号",
    "减持",
    "空头",
    "超买",
    "利空",
    "承压",
    "高估",
    "泡沫",
)

_UNDERVALUED = ("估值偏低", "低估", "便宜", "被低估")
_OVERVALUED = ("估值偏高", "高估", "泡沫", "偏贵")

_PE_RE = re.compile(r"(?:PE|市盈率)[^\d]{0,8}(\d+(?:\.\d+)?)")


def check_contradictions(
    answer: str, *, agent: str, other_answers: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """前后矛盾 + 跨专家矛盾。跨专家矛盾记为「分歧信号」，不阻断交付。"""

    issues: list[dict[str, str]] = []
    text = answer or ""

    # 1) 自称低估，但 PE 数字偏高
    if any(word in text for word in _UNDERVALUED):
        for match in _PE_RE.finditer(text):
            try:
                pe = float(match.group(1))
            except ValueError:
                continue
            if pe >= 30:
                issues.append(
                    _issue(
                        "contradiction",
                        f"结论称「估值偏低」，但同一段里 PE 为 {pe:g}，两者相互矛盾",
                    )
                )
                break

    # 2) 方向词自相矛盾
    if any(word in text for word in _BULLISH) and any(word in text for word in _BEARISH):
        issues.append(
            _issue(
                "contradiction",
                "同一份结论里同时出现看多与看空表述，需明确哪一个是主结论",
                MEDIUM,
            )
        )

    # 3) 跨专家分歧：技术面看多、基本面看空（或反过来）
    for other in other_answers:
        other_agent = other.get("agent")
        other_text = other.get("answer") or ""
        if other_agent == agent or not other_text:
            continue
        if not _is_analyst_pair(agent, other_agent):
            continue

        this_bull = any(word in text for word in _BULLISH)
        this_bear = any(word in text for word in _BEARISH)
        that_bull = any(word in other_text for word in _BULLISH)
        that_bear = any(word in other_text for word in _BEARISH)

        if (this_bull and that_bear) or (this_bear and that_bull):
            from finagent.graph.state import AGENT_LABELS

            issues.append(
                _issue(
                    "contradiction",
                    f"分歧信号：{AGENT_LABELS.get(agent, agent)}与"
                    f"{AGENT_LABELS.get(other_agent, other_agent)}方向相反，"
                    "这本身是有价值的信息，已标注给用户",
                    MEDIUM,
                )
            )
            break

        this_under = any(word in text for word in _UNDERVALUED)
        that_over = any(word in other_text for word in _OVERVALUED)
        if (this_under and that_over) or (
            any(word in text for word in _OVERVALUED)
            and any(word in other_text for word in _UNDERVALUED)
        ):
            issues.append(
                _issue(
                    "contradiction",
                    "分歧信号：两个分析师对估值高低的判断相反，需要说明分歧来源",
                    MEDIUM,
                )
            )
            break

    return issues


def _is_analyst_pair(left: str, right: str) -> bool:
    from finagent.graph.state import ANALYST_AGENTS

    return left in ANALYST_AGENTS and right in ANALYST_AGENTS


# ----------------------------------------------------------------------
# 谄媚检查
# ----------------------------------------------------------------------
_LEADING_QUESTION = (
    "是不是要涨",
    "会涨吗",
    "能涨吗",
    "要涨了吧",
    "能不能买",
    "能买吗",
    "该买吗",
    "是不是该买",
    "可以抄底",
    "该抄底",
    "见底了吗",
    "是不是见底",
    "值得买入吗",
    "能回本吗",
    "应该加仓吗",
    "是不是利好",
)
_CONFIRMATION = (
    "是的",
    "会涨",
    "可以买",
    "建议买入",
    "值得买",
    "没问题",
    "肯定",
    "一定会",
    "必然",
    "稳赚",
    "抄底机会",
    "强烈建议",
)
_HEDGE = ("但", "不过", "然而", "风险", "不确定", "需注意", "反之", "若", "如果", "可能")


def check_sycophancy(question: str, answer: str) -> list[dict[str, str]]:
    """结论是否只是顺着提问的预设。"""

    lowered_q = (question or "").lower()
    if not any(probe in lowered_q for probe in _LEADING_QUESTION):
        return []

    text = answer or ""
    if not any(word in text for word in _CONFIRMATION):
        return []

    # 有反面证据就不算谄媚 —— 与用户预期一致本身不是问题
    if any(word in text for word in _HEDGE):
        return []

    return [
        _issue(
            "sycophancy",
            "用户提问带有预设（如「是不是要涨」），而结论直接顺着预设给出肯定判断，"
            "没有任何反面证据或不确定性说明",
        )
    ]


# ----------------------------------------------------------------------
def review(
    answer: str,
    *,
    agent: str,
    question: str,
    tool_calls: int,
    evidence: str,
    other_answers: list[dict[str, Any]] | None = None,
) -> ReviewResult:
    """纯规则审查（离线模式与 LLM 失败时的兜底）。"""

    issues: list[dict[str, str]] = []
    issues += check_evidence(answer, tool_calls=tool_calls, evidence=evidence)
    issues += check_contradictions(answer, agent=agent, other_answers=other_answers or [])
    issues += check_sycophancy(question, answer)

    blocking = any(issue.get("severity") == HIGH for issue in issues)
    return ReviewResult(
        verdict="revise" if blocking else "pass",
        issues=issues,
        checks={"evidence": True, "contradiction": True, "sycophancy": True},
        source="rules",
    )


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_critic_output(text: str) -> ReviewResult | None:
    """解析模型输出；解析不出来就返回 ``None``，由调用方回退到规则。"""

    match = _JSON_BLOCK.search(text or "")
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in ("pass", "revise"):
        return None

    raw_issues = payload.get("issues") or []
    issues: list[dict[str, str]] = []
    for item in raw_issues:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip().lower()
        if kind not in _TYPE_LABELS:
            kind = "unsupported"
        detail = str(item.get("detail") or "").strip()
        if not detail:
            continue
        severity = MEDIUM if kind == "contradiction" else HIGH
        issues.append(_issue(kind, detail, severity))

    # 模型说 revise 却没给理由 → 不采信，避免无意义的返工
    if verdict == "revise" and not issues:
        return None
    if verdict == "pass" and any(issue["severity"] == HIGH for issue in issues):
        verdict = "revise"

    return ReviewResult(verdict=verdict, issues=issues, source="llm")


async def review_with_llm(
    model: Any,
    answer: str,
    *,
    agent: str,
    question: str,
    tool_summary: str,
    other_answers: list[dict[str, Any]] | None = None,
    tool_calls: int = 0,
    evidence: str = "",
) -> ReviewResult:
    """让模型做审查；解析失败或调用出错时回退到规则。"""

    from langchain_core.messages import HumanMessage, SystemMessage

    payload = (
        f"用户提问：{question}\n\n"
        f"产出结论的专家：{agent}\n\n"
        f"该专家本轮的工具调用：\n{tool_summary or '（没有调用任何工具）'}\n\n"
        f"待审查的结论：\n{answer}"
    )
    try:
        response = await model.ainvoke(
            [SystemMessage(content=build_critic_prompt()), HumanMessage(content=payload)]
        )
        parsed = parse_critic_output(getattr(response, "content", ""))
    except Exception as exc:  # noqa: BLE001 - 审查失败不能拖垮整轮
        log.warning("审查者的模型调用失败（%s），回退到规则审查", exc)
        parsed = None

    if parsed is None:
        return review(
            answer,
            agent=agent,
            question=question,
            tool_calls=tool_calls,
            evidence=evidence,
            other_answers=other_answers,
        )
    return parsed


def summarise_tools(trace: list[dict[str, Any]], limit: int = 1200) -> str:
    """把工具调用压成审查者能读懂的简短清单。"""

    lines: list[str] = []
    for item in trace:
        mark = "OK" if item.get("ok") else "FAIL"
        args = item.get("args") or {}
        arg_text = ", ".join(f"{key}={value}" for key, value in list(args.items())[:3])
        preview = str(item.get("preview") or item.get("error") or "")[:220]
        lines.append(f"- [{mark}] {item.get('tool')}({arg_text}) -> {preview}")
    return "\n".join(lines)[:limit]
