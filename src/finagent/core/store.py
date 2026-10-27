"""持仓、预警、画像三个 store 共用的 JSON 持久化。

三个 MCP 服务器跑在**独立进程**里，所以每个 store 都必须在并发读改写下安全。因此
写入是：

1. 用跨进程锁文件（``<name>.lock``，10 秒后视为残留）串行化；
2. 先写临时文件，再用 :func:`os.replace` 原子替换。

读者要么看到旧的文档，要么看到新的，绝不会读到写了一半的文件。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from finagent.logging import get_logger

log = get_logger("store")

_LOCK_TIMEOUT = 10.0
_LOCK_STALE_AFTER = 10.0


class StoreError(RuntimeError):
    """store 读写失败时抛出。"""


@contextmanager
def _file_lock(lock_path: Path, timeout: float = _LOCK_TIMEOUT) -> Iterator[None]:
    """基于原子文件创建的、可移植的独占锁。"""

    deadline = time.monotonic() + timeout
    handle: int | None = None
    while True:
        try:
            handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0
            if age > _LOCK_STALE_AFTER:
                log.warning("清理残留锁文件 %s（已存在 %.1fs）", lock_path, age)
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() > deadline:
                raise StoreError(f"等待锁 {lock_path} 超时") from None
            time.sleep(0.02)

    try:
        yield
    finally:
        if handle is not None:
            os.close(handle)
        try:
            lock_path.unlink()
        except OSError:
            pass


class JsonStore:
    """小型的原子 JSON 文档存储。"""

    def __init__(self, path: Path | str, default: dict[str, Any] | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._default = default or {}
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    # ------------------------------------------------------------------
    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return json.loads(json.dumps(self._default))
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Cannot read %s (%s); falling back to defaults", self.path, exc)
            return json.loads(json.dumps(self._default))
        if not isinstance(data, dict):
            return json.loads(json.dumps(self._default))
        return data

    def write(self, data: dict[str, Any]) -> None:
        temp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        try:
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        except OSError as exc:
            raise StoreError(f"无法写入 {self.path}：{exc}") from exc
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass

    @contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        """在独占锁内完成「读—改—写」。"""

        with _file_lock(self._lock_path):
            data = self.read()
            yield data
            self.write(data)

    def update(self, mutator) -> dict[str, Any]:
        with self.transaction() as data:
            result = mutator(data)
            if isinstance(result, dict) and result is not data:
                data.clear()
                data.update(result)
        return self.read()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000):x}{os.getpid() % 1000:03d}"
