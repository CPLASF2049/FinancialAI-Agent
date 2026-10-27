"""基于 JSON 文件的 LangGraph 长期 store。

``langgraph.store.memory.InMemoryStore`` 一退出就全忘了，而 SQLite checkpointer 存的是
*会话*状态，不是关于用户的持久事实。这个 store 在两个抽象 ``BaseStore`` 方法之上实现
一个 JSON 文档，于是 ``store.get(("users", user_id), "profile")`` 能跨重启存活，
并与 MCP 画像服务器共享同一份文件。

磁盘布局::

    {"users": {"<user_id>": {"profile": {"value": {...}, "updated_at": "..."}}}}
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    Op,
    PutOp,
    SearchItem,
    SearchOp,
)

from finagent.core.store import JsonStore


class JsonFileStore(BaseStore):
    """最小可用的持久键值 store（无向量检索、无 TTL）。"""

    def __init__(self, path: Path | str) -> None:
        super().__init__()
        self._doc = JsonStore(path, {"namespaces": {}})

    # ------------------------------------------------------------------
    @staticmethod
    def _key(namespace: tuple[str, ...], name: str) -> str:
        return "::".join([*namespace, name])

    def _snapshot(self) -> dict[str, Any]:
        return self._doc.read().get("namespaces", {})

    # ------------------------------------------------------------------
    def batch(self, ops: Iterable[Op]) -> list[Any]:
        data = self._doc.read()
        namespaces: dict[str, Any] = data.setdefault("namespaces", {})
        results: list[Any] = []
        dirty = False

        for op in ops:
            if isinstance(op, GetOp):
                record = namespaces.get(self._key(op.namespace, op.key))
                if record is None:
                    results.append(None)
                else:
                    results.append(
                        Item(
                            namespace=tuple(op.namespace),
                            key=op.key,
                            value=record.get("value", {}),
                            created_at=record.get("created_at"),
                            updated_at=record.get("updated_at"),
                        )
                    )
            elif isinstance(op, PutOp):
                if op.value is None:
                    if self._key(op.namespace, op.key) in namespaces:
                        namespaces.pop(self._key(op.namespace, op.key))
                        dirty = True
                else:
                    namespaces[self._key(op.namespace, op.key)] = {
                        "namespace": list(op.namespace),
                        "key": op.key,
                        "value": op.value,
                        "updated_at": _now(),
                    }
                    dirty = True
                results.append(None)
            elif isinstance(op, SearchOp):
                prefix = tuple(op.namespace_prefix or ())
                matches: list[SearchItem] = []
                for record in namespaces.values():
                    namespace = tuple(record.get("namespace", ()))
                    if prefix and namespace[: len(prefix)] != prefix:
                        continue
                    matches.append(
                        SearchItem(
                            namespace=namespace,
                            key=record["key"],
                            value=record.get("value", {}),
                            created_at=record.get("created_at"),
                            updated_at=record.get("updated_at"),
                            score=None,
                        )
                    )
                results.append(matches[: op.limit])
            elif isinstance(op, ListNamespacesOp):
                seen = {tuple(record.get("namespace", ())) for record in namespaces.values()}
                results.append(sorted(seen)[: op.limit])
            else:  # pragma: no cover - unknown op type
                results.append(None)

        if dirty:
            self._doc.write(data)
        return results

    async def abatch(self, ops: Iterable[Op]) -> list[Any]:
        return self.batch(ops)

    # ------------------------------------------------------------------
    def dump(self) -> dict[str, Any]:
        """整个 store 的内容，用于诊断和导出。"""

        return {key: record.get("value") for key, record in self._snapshot().items()}


def _now() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC).isoformat()
