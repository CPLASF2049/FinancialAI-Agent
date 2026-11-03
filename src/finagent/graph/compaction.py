"""上下文压缩（P1-2）。

在**每次模型调用之前**估算 token，超阈值就修剪，而不是等上下文溢出再被动处理。
压缩策略是金融长任务特有的：

* **保留**审查意见与风控命中——它们是审计证据，删了就说不清「为什么这么答」
* **优先修剪**中间工具返回的原始数据，只留摘要与引用 ID（call_id）
"""

from __future__ import annotations

from typing import Any

#: 粗估：4 个字符 ≈ 1 token。够用且不需要 tokenizer 依赖。
CHARS_PER_TOKEN = 4

#: 这些内容属于审计证据，永远不压缩。
PROTECTED_MARKERS = ("审查", "verdict", "风险提示", "风控", "review")


def estimate_tokens(text: str) -> int:
    return max(1, len(str(text or "")) // CHARS_PER_TOKEN)


def context_tokens(messages: list[Any]) -> int:
    total = 0
    for message in messages:
        content = getattr(message, "content", message)
        total += estimate_tokens(content if isinstance(content, str) else str(content))
    return total


def _is_protected(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in PROTECTED_MARKERS)


def compact(messages: list[Any], *, budget: int, keep_recent: int = 6) -> list[Any]:
    """超出预算时修剪中间的工具返回。

    保留：系统消息、第一条用户消息、最近 ``keep_recent`` 条、以及任何带审查/风控证据的消息。
    修剪：中间的工具结果，替换成「前 160 字 + 已压缩」的占位。
    """

    if budget <= 0 or context_tokens(messages) <= budget:
        return messages

    total = len(messages)
    head = messages[:2]  # system + 第一条 user
    recent = messages[-keep_recent:] if total > keep_recent + 2 else []
    middle = messages[2 : total - keep_recent] if recent else []

    trimmed: list[Any] = []
    for message in middle:
        content = getattr(message, "content", None)
        text = content if isinstance(content, str) else str(content)
        if _is_protected(text):
            trimmed.append(message)
            continue
        # 工具结果是压缩的主要对象
        shortened = text[:160] + "\n…（中间结果已压缩，完整数据见 tool:result 事件）"
        trimmed.append(_replace_content(message, shortened))

    return [*head, *trimmed, *recent]


def _replace_content(message: Any, text: str) -> Any:
    """尽量原地替换内容；改不了就转成同类的简化对象。"""

    try:
        message.content = text
        return message
    except Exception:  # noqa: BLE001 - 某些消息对象是只读的
        try:
            from langchain_core.messages import HumanMessage

            return HumanMessage(content=text)
        except Exception:  # noqa: BLE001
            return text
