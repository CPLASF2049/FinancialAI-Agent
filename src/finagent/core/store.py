"""持仓、预警、画像、会话几个 store 共用的 JSON 持久化。

三个 MCP 服务器跑在**独立进程**里，Web 服务又是多请求并发，所以每个 store 都必须在
并发读改写下安全。三层保护：

1. **进程内**：一把 :class:`threading.RLock`，同一进程的写请求串行（这把锁永远不会失败）；
2. **跨进程**：锁文件 ``<name>.lock``，10 秒后视为残留可强删；
3. **原子落盘**：先写临时文件再 :func:`os.replace`，读者只会看到完整文档。

第 2 层是**尽力而为**的。原因很实际：Windows 上 ``O_CREAT|O_EXCL`` 在锁文件处于
「删除挂起」或「被独占打开」状态时返回的是 ``EACCES`` 而不是 ``EEXIST``；某些杀毒软件
与索引服务也会短暂占用刚创建的锁文件。早期版本只捕获 ``FileExistsError``，于是这类情况
会把整个 Web 服务打成 500。现在的策略是：拿不到锁就等一会儿，超时后**降级为无锁写入**
并留下告警——第 1 层仍在，``os.replace`` 仍是原子的，宁可冒一点跨进程并发的风险，
也不能让界面直接崩掉。
"""

from __future__ import annotations

import json
import os
import stat as stat_module
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from finagent.logging import get_logger

log = get_logger("store")

_LOCK_TIMEOUT = 10.0
_LOCK_STALE_AFTER = 10.0
_LOCK_RETRY_INTERVAL = 0.02


class StoreError(RuntimeError):
    """store 读写失败时抛出。"""


def _force_unlink(path: Path) -> bool:
    """删除文件；只读属性也尝试去掉后再删。返回是否删成功。"""

    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        pass

    try:
        os.chmod(path, stat_module.S_IWRITE | stat_module.S_IREAD)
        path.unlink()
        return True
    except OSError:
        return False


@contextmanager
def _file_lock(lock_path: Path, timeout: float = _LOCK_TIMEOUT) -> Iterator[bool]:
    """基于原子文件创建的独占锁。

    产出 ``True`` 表示拿到了锁，``False`` 表示降级（没拿到，但仍然继续执行）。
    任何情况下都**不抛异常**——锁是优化，不是正确性的唯一依赖。
    """

    deadline = time.monotonic() + timeout
    handle: int | None = None

    while True:
        try:
            handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            pass  # 正常情况：别人持有
        except OSError as exc:
            # EACCES/EPERM：Windows 的「删除挂起」「只读」「被独占打开」，
            # 或者所在目录不可写。单独处理，不要让它冒泡成 500。
            if not lock_path.exists():
                log.warning("无法创建锁文件 %s（%s），本次跳过文件锁", lock_path, exc)
                handle = None
                break

        # 走到这里说明锁路径被占着。目录不可能是锁文件，直接放弃重试。
        if lock_path.is_dir():
            log.warning("锁路径 %s 是个目录，本次跳过文件锁", lock_path)
            break

        # 判断是不是上一个进程残留的
        try:
            age = time.time() - lock_path.stat().st_mtime
        except OSError:
            age = 0.0

        if age > _LOCK_STALE_AFTER and _force_unlink(lock_path):
            log.warning("清理残留锁文件 %s（已存在 %.1fs）", lock_path, age)
            continue

        if time.monotonic() > deadline:
            log.warning("等待锁 %s 超时，降级为无锁写入（进程内仍然串行）", lock_path)
            break

        time.sleep(_LOCK_RETRY_INTERVAL)

    acquired = handle is not None
    try:
        yield acquired
    finally:
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass
            if not _force_unlink(lock_path):
                log.warning("锁文件 %s 没能删掉，下次会自动当作残留处理", lock_path)


class JsonStore:
    """小型的原子 JSON 文档存储。"""

    def __init__(self, path: Path | str, default: dict[str, Any] | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._default = default or {}
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        #: 进程内的写串行化。跨进程锁失败时，正确性就靠它和原子替换。
        self._thread_lock = threading.RLock()

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
        except (OSError, TypeError, ValueError) as exc:
            # 统一成 StoreError：调用方只需要处理一种异常
            raise StoreError(f"无法写入 {self.path}：{exc}") from exc
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass

    @contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        """在（进程内 + 跨进程）双重锁内完成「读—改—写」。"""

        with self._thread_lock:
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
