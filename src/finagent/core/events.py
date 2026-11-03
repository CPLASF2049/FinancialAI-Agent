"""append-only 事件表：一次执行的可回放轨迹（P1-1）。

为什么要单独一张表
------------------
SSE 现在混着两种东西：**可回放的事实**（谁在什么时候调了什么工具、审了多久）
和**实时协调信号**（token 增量、进度提示）。前端既要渲染又要靠它们推断状态，
于是「这个 turn 到底走到哪一步」谁也说不清。

这里把**事实**落到一张 append-only 表里，``task_id`` 做索引。有了它：

* 「耗时最长的节点」= 对 ``step:end`` 的 ``wall_time_ms`` 排序
* 「失败的工具有哪些」= 过滤 ``tool:result`` 里 ``status != ok``
* 「为什么返工」= ``review:verdict`` 里 ``verdict=revise`` 之后紧跟的 ``step:start``

不需要额外追踪系统，SQLite 就够。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from finagent.logging import get_logger

log = get_logger("core.events")

#: 事件类型（durable replay facts）。顺序即语义上的先后。
EVENT_TYPES = (
    "turn:start",
    "route:decision",
    "step:start",
    "tool:call",
    "tool:result",
    "assistant:message",
    "step:end",
    "review:verdict",
    "risk:triggered",
    "turn:end",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    task_id   TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    ts        REAL NOT NULL,
    type      TEXT NOT NULL,
    payload   TEXT NOT NULL,
    PRIMARY KEY (task_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
"""


@dataclass(slots=True)
class Event:
    task_id: str
    seq: int
    ts: float
    type: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "payload": self.payload,
        }


class EventStore:
    """SQLite 支撑的 append-only 事件表。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ------------------------------------------------------------------
    def append(self, task_id: str, event_type: str, **payload: Any) -> Event:
        """追加一条事件。``seq`` 在事务里取最大值 +1，保证同一 task 内有序。"""

        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events WHERE task_id = ?", (task_id,)
            ).fetchone()
            seq = int(row[0]) + 1
            stamp = time.time()
            conn.execute(
                "INSERT INTO events (task_id, seq, ts, type, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    task_id,
                    seq,
                    stamp,
                    event_type,
                    json.dumps(payload, ensure_ascii=False, default=str),
                ),
            )
        return Event(task_id=task_id, seq=seq, ts=stamp, type=event_type, payload=payload)

    def query(self, task_id: str, *, event_type: str | None = None) -> list[Event]:
        sql = "SELECT task_id, seq, ts, type, payload FROM events WHERE task_id = ?"
        params: list[Any] = [task_id]
        if event_type:
            sql += " AND type = ?"
            params.append(event_type)
        sql += " ORDER BY seq"
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            Event(
                task_id=row[0],
                seq=row[1],
                ts=row[2],
                type=row[3],
                payload=json.loads(row[4]),
            )
            for row in rows
        ]

    def replay(self, task_id: str) -> list[dict[str, Any]]:
        """按时间线还原一次执行。"""

        return [event.to_dict() for event in self.query(task_id)]

    def tasks(self, limit: int = 50) -> list[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id, MAX(ts) AS last FROM events GROUP BY task_id "
                "ORDER BY last DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [row[0] for row in rows]

    # ------------------------------------------------------------------
    # 三个开箱即用的查询
    # ------------------------------------------------------------------
    def slowest_steps(self, task_id: str, limit: int = 5) -> list[dict[str, Any]]:
        """耗时最长的节点。"""

        steps = [
            {
                "agent": event.payload.get("agent"),
                "wall_time_ms": int(event.payload.get("wall_time_ms") or 0),
                "termination_reason": event.payload.get("termination_reason"),
            }
            for event in self.query(task_id, event_type="step:end")
        ]
        return sorted(steps, key=lambda item: item["wall_time_ms"], reverse=True)[:limit]

    def failed_tools(self, task_id: str) -> list[dict[str, Any]]:
        """失败的（或慢的）工具调用。"""

        return [
            event.payload
            for event in self.query(task_id, event_type="tool:result")
            if str(event.payload.get("status")) != "ok"
        ]

    def revisions(self, task_id: str) -> list[dict[str, Any]]:
        """返工记录：谁被判 revise，以及紧接着跑了什么。"""

        events = self.query(task_id)
        out: list[dict[str, Any]] = []
        for index, event in enumerate(events):
            if event.type != "review:verdict":
                continue
            if str(event.payload.get("verdict")) != "revise":
                continue
            following = next(
                (item for item in events[index + 1 :] if item.type == "step:start"), None
            )
            out.append(
                {
                    "agent": event.payload.get("agent"),
                    "issues": event.payload.get("issues") or [],
                    "next_agent": following.payload.get("agent") if following else None,
                }
            )
        return out


class TurnRecorder:
    """把一次 turn 的事件写进 :class:`EventStore`。

    刻意很薄：调用点只负责「说发生了什么」，落盘与顺序由这里保证。
    落盘失败绝不打断主流程——审计能力不该成为可用性的风险。
    """

    def __init__(self, store: EventStore | None, task_id: str) -> None:
        self.store = store
        self.task_id = task_id
        self.started = time.monotonic()

    def record(self, event_type: str, **payload: Any) -> None:
        if self.store is None:
            return
        # task_id 由 recorder 自己带着，调用方重复传会撞参数
        payload.pop("task_id", None)
        try:
            self.store.append(self.task_id, event_type, **payload)
        except Exception as exc:  # noqa: BLE001 - 审计失败不影响回答
            log.debug("事件落盘失败（%s）：%s", event_type, exc)

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)
