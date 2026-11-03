"""断点恢复诊断（P1-2）。

LangGraph 的 checkpoint 保存的是「图的状态」，但中断时真正要知道的是
**执行阶段**：卡在收件箱、卡在模型请求、还是卡在某个工具的「已发出未返回」。

这里用事件表最后一条事件来精确判断——不猜：

    没有 turn:start              -> inbox     （还没开始，或在唤醒阶段）
    有 step:start 没有 assistant -> provider  （模型请求中）
    有 tool:call 没有 tool:result -> tool      （工具执行中，可凭 call_id 幂等跳过）
    有 turn:end                  -> done
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class Phase:
    name: str
    #: 恢复时该怎么做（给人和给程序都看得懂）
    action: str
    #: 已发出但没返回的工具 call_id，恢复时应**跳过**而不是重放
    pending_calls: list[str]
    last_event: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.name,
            "action": self.action,
            "pending_calls": list(self.pending_calls),
            "last_event": self.last_event,
        }


_PHASES = {
    "inbox": "从未开始：重新发起这一轮即可。",
    "provider": "模型请求中：重试该 step，不要重放已完成的工具。",
    "tool": "工具执行中：凭 call_id 判断该工具是否已有副作用，已执行的直接跳过。",
    "review": "审查环节：重新跑审查，结论不变则直接沿用。",
    "done": "已完成：无需恢复。",
}


def detect_phase(events: list[Any]) -> Phase:
    """从事件序列推断执行阶段。``events`` 可以是 Event 或 dict。"""

    if not events:
        return Phase("inbox", _PHASES["inbox"], [])

    def kind(item: Any) -> str:
        return str(getattr(item, "type", None) or (item.get("type") if isinstance(item, dict) else ""))

    types = [kind(item) for item in events]

    if "turn:end" in types:
        return Phase("done", _PHASES["done"], [], types[-1])
    if "turn:start" not in types:
        return Phase("inbox", _PHASES["inbox"], [], types[-1] if types else "")

    # 已发出但没返回的工具调用
    pending: list[str] = []
    for index, item in enumerate(events):
        if kind(item) != "tool:call":
            continue
        payload = getattr(item, "payload", None) or (item.get("payload") if isinstance(item, dict) else {}) or {}
        call_id = str(payload.get("call_id") or "")
        later = [
            getattr(other, "payload", None)
            or (other.get("payload") if isinstance(other, dict) else {})
            or {}
            for other in events[index + 1 :]
        ]
        if not any(str(item.get("call_id") or "") == call_id for item in later):
            pending.append(call_id)

    if pending:
        return Phase("tool", _PHASES["tool"], pending, types[-1])

    if "step:start" in types and types[-1] == "step:start":
        return Phase("provider", _PHASES["provider"], [], types[-1])

    if types[-1] == "review:verdict":
        return Phase("review", _PHASES["review"], [], types[-1])

    if types[-1] == "step:end":
        return Phase("review", _PHASES["review"], [], types[-1])

    return Phase("provider", _PHASES["provider"], [], types[-1] if types else "")


def resume_hint(events: list[Any]) -> dict[str, Any]:
    """给调用方的恢复建议（不自动执行，只给出判断依据）。"""

    return detect_phase(events).to_dict()
