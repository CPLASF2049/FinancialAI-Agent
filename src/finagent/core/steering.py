"""Steering 队列（P2）：边跑边调整，而不是硬刹车。

为什么不是又一个预算
--------------------
预算（轮数/时间/token）解决的是「停下来」，它救不了「跑偏了」。用户在 Agent 干活
途中往往知道自己想要什么：

* 「别查财报了，直接给结论」   —— 收窄
* 「简短点」                   —— 压缩
* 「改看技术面」               —— 转向
* 「算了，停」                 —— 取消

硬刹车只能全部取消；Steering 是**协作式控制**：把指令排进队列，Agent 在下一个
可安全接管的边界（每次工具循环开始前）读取并应用，然后继续跑——已经取到的数据
不浪费。

安全边界
--------
只在 **step 边界**消费指令，不在工具执行中间打断：工具要么跑完，要么不跑。
这样不会留下「半个副作用」——那正是 P3 要解决的问题，这里先用边界规避它。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


class DirectiveType:
    """支持的指令类型。少而明确，避免变成「任意提示词注入」。"""

    CANCEL = "cancel"
    NARROW = "narrow"  # 收窄范围：别再查某一类
    BRIEF = "brief"  # 简短点
    FOCUS = "focus"  # 转向某个 Agent / 子职责
    NOTE = "note"  # 附加一句要求


VALID_TYPES = {
    DirectiveType.CANCEL,
    DirectiveType.NARROW,
    DirectiveType.BRIEF,
    DirectiveType.FOCUS,
    DirectiveType.NOTE,
}


@dataclass(slots=True)
class Directive:
    type: str
    text: str = ""
    target: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "text": self.text,
            "target": self.target,
            "created_at": self.created_at,
        }

    def as_instruction(self) -> str:
        """转成给模型看的一句话。"""

        if self.type == DirectiveType.BRIEF:
            return "用户要求：回答更简短，只保留结论与关键数据。"
        if self.type == DirectiveType.NARROW:
            return f"用户要求：不要再查「{self.text}」这类内容，基于已有信息给结论。"
        if self.type == DirectiveType.FOCUS:
            return f"用户要求：重点看「{self.target or self.text}」这个维度。"
        if self.type == DirectiveType.NOTE:
            return f"用户补充要求：{self.text}"
        return ""


class SteeringQueue:
    """按 task_id 隔离的指令队列。线程安全，消费即出队。"""

    def __init__(self) -> None:
        self._queues: dict[str, deque[Directive]] = {}
        self._lock = threading.RLock()

    def push(self, task_id: str, directive: str, *, text: str = "", target: str = "") -> Directive:
        kind = str(directive or "").strip().lower()
        if kind not in VALID_TYPES:
            raise ValueError(f"不支持的指令 '{directive}'；可用：{', '.join(sorted(VALID_TYPES))}")
        item = Directive(type=kind, text=str(text or ""), target=str(target or ""))
        with self._lock:
            self._queues.setdefault(task_id, deque()).append(item)
        return item

    def drain(self, task_id: str) -> list[Directive]:
        """取出并清空该 task 的全部待处理指令。"""

        with self._lock:
            queue = self._queues.get(task_id)
            if not queue:
                return []
            items = list(queue)
            queue.clear()
            return items

    def peek(self, task_id: str) -> list[Directive]:
        with self._lock:
            return list(self._queues.get(task_id, ()))

    def cancelled(self, task_id: str) -> bool:
        return any(item.type == DirectiveType.CANCEL for item in self.peek(task_id))

    def clear(self, task_id: str) -> None:
        with self._lock:
            self._queues.pop(task_id, None)


# ----------------------------------------------------------------------
def apply(directives: list[Directive]) -> dict[str, Any]:
    """把一批指令归一成「这一轮该怎么办」。

    返回：``cancel`` 是否取消、``instructions`` 要附加给模型的句子、
    ``focus`` 是否有指定方向。
    """

    result: dict[str, Any] = {"cancel": False, "instructions": [], "focus": ""}
    for item in directives:
        if item.type == DirectiveType.CANCEL:
            result["cancel"] = True
            continue
        if item.type == DirectiveType.FOCUS:
            result["focus"] = item.target or item.text
        sentence = item.as_instruction()
        if sentence:
            result["instructions"].append(sentence)
    return result


#: 进程级单例。Web 端通过 API 推指令，图节点从同一个对象取。
QUEUE = SteeringQueue()
