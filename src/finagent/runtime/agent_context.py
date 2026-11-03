"""三层上下文（2.0 P1-4）。

为什么分层
----------
长任务的上下文不能一视同仁地压。金融场景里三样东西的价值差了几个量级：

* **核心上下文**：用户目标、约束、审查意见 —— 压掉它，后面所有推理都失去前提
* **研究摘要**：各专家结论、证据引用、未解决问题 —— 结构化，可以折叠但必须保真
* **历史细节**：工具返回的原始数据 —— 最先该压的，压成「引用 + 摘要」即可

Pi 的 transformContext 给了启发：**在调用模型之前**判断要不要压缩，
而不是等上下文溢出、请求报错再来补救。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: 4 字符 ≈ 1 token 的粗估，与 compaction 保持一致
CHARS_PER_TOKEN = 4


class Layer(StrEnum):
    CORE = "core"
    SUMMARY = "summary"
    DETAIL = "detail"


def estimate_tokens(text: str) -> int:
    return max(1, len(str(text or "")) // CHARS_PER_TOKEN)


@dataclass(slots=True)
class ContextItem:
    message: Any
    layer: Layer
    #: 详情层的压缩替身（引用 ID）。压掉原文后，靠它回溯。
    ref: str = ""
    tokens: int = 0

    def __post_init__(self) -> None:
        if not self.tokens:
            content = getattr(self.message, "content", self.message)
            self.tokens = estimate_tokens(content if isinstance(content, str) else str(content))


@dataclass(slots=True)
class AgentContext:
    """把消息按层管理，并提供「压缩到预算内」的能力。"""

    items: list[ContextItem] = field(default_factory=list)
    #: 详情层被压缩后留下的引用（call_id -> 摘要）
    references: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def add(self, message: Any, layer: Layer = Layer.DETAIL, *, ref: str = "") -> ContextItem:
        item = ContextItem(message=message, layer=layer, ref=ref)
        self.items.append(item)
        return item

    def tokens(self) -> int:
        return sum(item.tokens for item in self.items)

    def messages(self) -> list[Any]:
        return [item.message for item in self.items]

    # ------------------------------------------------------------------
    def compact(self, budget: int, *, keep_recent_details: int = 2) -> dict[str, int]:
        """压到预算内，返回压缩前后 token 数。

        压缩顺序（从最不影响结论的开始）：

        1. 详情层里**较早**的工具返回 → 换成 ``[已压缩 tool:xxx]`` 引用
        2. 详情层里较新的几条 → 保留（模型通常还要接着用）
        3. 摘要层折叠成一条
        4. 核心层**永不压缩**
        """

        before = self.tokens()
        if before <= budget:
            return {"before_tokens": before, "after_tokens": before}

        details = [item for item in self.items if item.layer is Layer.DETAIL]
        # 先压最旧的，保留最近 N 条
        victims = details[:-keep_recent_details] if len(details) > keep_recent_details else []
        for item in victims:
            if self.tokens() <= budget:
                break
            self._shrink(item)

        if self.tokens() > budget:
            self._fold_summaries()

        after = self.tokens()
        return {"before_tokens": before, "after_tokens": after}

    def _shrink(self, item: ContextItem) -> None:
        content = getattr(item.message, "content", None)
        text = content if isinstance(content, str) else str(content)
        if text.startswith("[已压缩"):
            return
        ref = item.ref or "tool"
        self.references[ref] = text[:200]
        stub = f"[已压缩 {ref}] 原始返回 {len(text)} 字符，摘要：{text[:120]}…"
        _set_content(item.message, stub)
        item.tokens = estimate_tokens(stub)

    def _fold_summaries(self) -> None:
        summaries = [item for item in self.items if item.layer is Layer.SUMMARY]
        if len(summaries) <= 1:
            return
        merged = " | ".join((getattr(item.message, "content", "") or "")[:80] for item in summaries)
        first = summaries[0]
        _set_content(first.message, f"[研究摘要] {merged}")
        first.tokens = estimate_tokens(merged)
        for item in summaries[1:]:
            self.items.remove(item)

    # ------------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        by_layer: dict[str, int] = {}
        for item in self.items:
            by_layer[str(item.layer)] = by_layer.get(str(item.layer), 0) + 1
        return {
            "tokens": self.tokens(),
            "items": len(self.items),
            "by_layer": by_layer,
            "references": len(self.references),
        }


def _set_content(message: Any, text: str) -> None:
    try:
        message.content = text
    except Exception:  # noqa: BLE001 - 只读消息就不改，压缩只影响计数
        return
