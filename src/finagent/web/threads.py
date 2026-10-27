"""Web 界面的会话与消息存储。

界面需要两样东西：会话列表（标题、时间、预览）和每个会话的完整对话记录。这两样都以
一个 JSON 文档存在数据目录里，写入走 :class:`JsonStore` 的「锁 + 原子替换」，所以同时
开着多个浏览器标签页也不会写坏。

图的 checkpoint 仍然独立存在——那是给 LangGraph 用的对话状态；这里是给界面用的展示记录
（含工具调用轨迹），两者按 ``thread_id`` 对齐。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from finagent.core.store import JsonStore, new_id, now_iso

#: 每个会话最多保留多少条消息，防止单个 JSON 无限膨胀。
MAX_MESSAGES = 200


def _empty() -> dict[str, Any]:
    return {"threads": {}, "order": []}


class ThreadStore:
    """会话列表 + 逐条消息记录。"""

    def __init__(self, path: Path | str) -> None:
        self._store = JsonStore(path, _empty())

    # ------------------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        data = self._store.read()
        data.setdefault("threads", {})
        data.setdefault("order", [])
        # 补齐 order：手改过文件或早期版本可能没有
        for thread_id in data["threads"]:
            if thread_id not in data["order"]:
                data["order"].append(thread_id)
        return data

    def _mutate(self, mutator) -> None:
        with self._store.transaction() as data:
            data.setdefault("threads", {})
            data.setdefault("order", [])
            mutator(data)

    # ------------------------------------------------------------------
    def list_threads(self) -> list[dict[str, Any]]:
        """按最近活跃排序的会话摘要。"""

        data = self._read()
        items = [data["threads"][thread_id] for thread_id in data["order"]]
        items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return [
            {
                "id": item["id"],
                "title": item.get("title") or "新对话",
                "updated_at": item.get("updated_at"),
                "message_count": len(item.get("messages") or []),
                "preview": _preview(item.get("messages") or []),
            }
            for item in items
        ]

    def create(self, title: str = "") -> dict[str, Any]:
        thread_id = new_id("t")
        record = {
            "id": thread_id,
            "title": title or "新对话",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "messages": [],
        }

        def mutate(data: dict[str, Any]) -> None:
            data["threads"][thread_id] = record
            data["order"].insert(0, thread_id)

        self._mutate(mutate)
        return {"id": thread_id, "title": record["title"]}

    def get(self, thread_id: str) -> dict[str, Any] | None:
        return self._read()["threads"].get(thread_id)

    def messages(self, thread_id: str) -> list[dict[str, Any]]:
        record = self.get(thread_id)
        return list(record.get("messages") or []) if record else []

    def delete(self, thread_id: str) -> bool:
        removed = {"value": False}

        def mutate(data: dict[str, Any]) -> None:
            if thread_id in data["threads"]:
                data["threads"].pop(thread_id, None)
                removed["value"] = True
            if thread_id in data["order"]:
                data["order"].remove(thread_id)

        self._mutate(mutate)
        return removed["value"]

    # ------------------------------------------------------------------
    def append(self, thread_id: str, message: dict[str, Any]) -> None:
        """追加一条消息；会话不存在时顺带创建。"""

        payload = {"ts": now_iso(), **message}

        def mutate(data: dict[str, Any]) -> None:
            record = data["threads"].get(thread_id)
            if record is None:
                record = {
                    "id": thread_id,
                    "title": "新对话",
                    "created_at": now_iso(),
                    "messages": [],
                }
                data["threads"][thread_id] = record
                data["order"].insert(0, thread_id)

            record.setdefault("messages", []).append(payload)
            record["messages"] = record["messages"][-MAX_MESSAGES:]
            record["updated_at"] = now_iso()

            # 第一条用户消息自动成为标题
            if payload.get("role") == "user" and record.get("title") in ("", "新对话"):
                record["title"] = _title_of(str(payload.get("text") or ""))

        self._mutate(mutate)

    def rename(self, thread_id: str, title: str) -> bool:
        cleaned = str(title).strip()
        if not cleaned:
            return False
        renamed = {"value": False}

        def mutate(data: dict[str, Any]) -> None:
            record = data["threads"].get(thread_id)
            if record is not None:
                record["title"] = cleaned[:60]
                record["updated_at"] = now_iso()
                renamed["value"] = True

        self._mutate(mutate)
        return renamed["value"]


# ----------------------------------------------------------------------
def _title_of(text: str) -> str:
    clean = " ".join(text.split())
    return clean[:24] + ("…" if len(clean) > 24 else "") if clean else "新对话"


def _preview(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("text"):
            return " ".join(str(message["text"]).split())[:60]
        if message.get("role") == "user" and message.get("text"):
            return " ".join(str(message["text"]).split())[:60]
    return ""


def new_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
