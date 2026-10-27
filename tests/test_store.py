"""JSON store 的锁与原子写。

重点覆盖 Windows 上真实踩过的坑：``O_CREAT|O_EXCL`` 在锁文件处于「只读」「删除挂起」
或「被独占打开」状态时返回的是 ``EACCES`` 而不是 ``EEXIST``。早期版本只捕获
``FileExistsError``，于是整个 Web 服务被一个删不掉的锁文件打成 500。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from finagent.core.store import JsonStore, StoreError, _file_lock


@pytest.fixture
def store(data_dir: Path) -> JsonStore:
    target = data_dir / "store" / "doc.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    return JsonStore(target, {"n": 0})


# ----------------------------------------------------------------------
def test_transaction_reads_and_writes(store: JsonStore) -> None:
    with store.transaction() as data:
        data["n"] = 1
    assert store.read()["n"] == 1


def test_missing_file_returns_default(store: JsonStore) -> None:
    assert store.read() == {"n": 0}


def test_corrupt_file_falls_back_to_default(store: JsonStore) -> None:
    store.path.write_text("{not json", encoding="utf-8")
    assert store.read() == {"n": 0}


def test_update_helper(store: JsonStore) -> None:
    store.update(lambda data: {"n": 42})
    assert store.read()["n"] == 42


# ----------------------------------------------------------------------
# 锁的健壮性
# ----------------------------------------------------------------------
def test_lock_is_released_after_transaction(store: JsonStore) -> None:
    with store.transaction() as data:
        data["n"] = 1
    assert not store._lock_path.exists()


def test_stale_readonly_lock_is_cleared(store: JsonStore) -> None:
    """锁文件是只读的（O_EXCL 会报 EACCES）也要能自动清理。"""

    lock = store._lock_path
    lock.write_text("stale", encoding="utf-8")
    os.chmod(lock, 0o444)
    old = time.time() - 60
    os.utime(lock, (old, old))

    with store.transaction() as data:
        data["n"] = 7

    assert store.read()["n"] == 7
    assert not lock.exists()


def test_lock_path_as_directory_degrades_instead_of_hanging(store: JsonStore) -> None:
    """锁路径是个目录时，不该空转 10 秒，也不该抛异常。"""

    lock = store._lock_path
    lock.mkdir()
    try:
        started = time.monotonic()
        with store.transaction() as data:
            data["n"] = 3
        elapsed = time.monotonic() - started
    finally:
        lock.rmdir()

    assert store.read()["n"] == 3
    assert elapsed < 2.0, "遇到目录锁时应当立刻降级，而不是等到超时"


def test_lock_yields_false_when_degraded(store: JsonStore) -> None:
    lock = store._lock_path
    lock.mkdir()
    try:
        with _file_lock(lock, timeout=0.05) as acquired:
            assert acquired is False
    finally:
        lock.rmdir()


def test_lock_yields_true_in_the_normal_case(store: JsonStore) -> None:
    with _file_lock(store._lock_path, timeout=1.0) as acquired:
        assert acquired is True
    assert not store._lock_path.exists()


def test_concurrent_writes_do_not_lose_updates(store: JsonStore) -> None:
    """进程内有线程锁，跨进程有文件锁；20 个线程各加一次不能丢。"""

    def bump() -> None:
        with store.transaction() as data:
            data["n"] = int(data.get("n") or 0) + 1

    threads = [threading.Thread(target=bump) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert store.read()["n"] == 20


# ----------------------------------------------------------------------
def test_write_error_is_reported_as_store_error(store: JsonStore) -> None:
    """写入真的失败时抛 StoreError（供上层转成可读的错误）。"""

    with pytest.raises(StoreError):
        store.write({"n": object()})  # 不可 JSON 序列化
