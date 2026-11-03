"""会话生命周期测试：空会话可删、有内容不可删、标题提炼。"""

from __future__ import annotations

import pytest

from finagent.web.threads import ThreadStore, _title_of


@pytest.fixture
def store(data_dir) -> ThreadStore:
    return ThreadStore(data_dir / "threads.json")


# ----------------------------------------------------------------------
# 标题提炼
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("帮我筛选市盈率小于15的银行股", "筛选市盈率小于15的银行股"),
        ("你好，茅台现在多少钱？", "茅台现在多少钱"),
        ("请回测平安银行双均线策略。", "回测平安银行双均线策略"),
        ("   ", "新对话"),
        ("", "新对话"),
    ],
)
def test_title_is_distilled(message: str, expected: str) -> None:
    assert _title_of(message) == expected


def test_long_title_is_truncated() -> None:
    title = _title_of("茅台" * 40)
    assert len(title) <= 25
    assert title.endswith("…")


# ----------------------------------------------------------------------
# 空会话清理
# ----------------------------------------------------------------------
def test_empty_thread_can_be_deleted(store: ThreadStore) -> None:
    created = store.create()
    assert store.delete_if_empty(created["id"]) is True
    assert store.get(created["id"]) is None


def test_thread_with_messages_is_kept(store: ThreadStore) -> None:
    """一旦说过话，这条会话就固定下来——切走也不该被删。"""

    created = store.create()
    store.append(created["id"], {"role": "user", "text": "茅台多少钱"})

    assert store.delete_if_empty(created["id"]) is False
    assert store.get(created["id"]) is not None


def test_unknown_thread_is_safe(store: ThreadStore) -> None:
    assert store.delete_if_empty("nope") is False


# ----------------------------------------------------------------------
# 标题在第一条消息后固定
# ----------------------------------------------------------------------
def test_title_derived_from_first_message(store: ThreadStore) -> None:
    created = store.create()
    store.append(created["id"], {"role": "user", "text": "帮我看看茅台最近的走势"})

    record = store.get(created["id"])
    assert record["title"] == "看看茅台最近的走势"


def test_title_not_overwritten_by_later_messages(store: ThreadStore) -> None:
    created = store.create()
    store.append(created["id"], {"role": "user", "text": "茅台多少钱"})
    store.append(created["id"], {"role": "user", "text": "那五粮液呢"})

    assert store.get(created["id"])["title"] == "茅台多少钱"


def test_empty_threads_are_listed_but_flagged(store: ThreadStore) -> None:
    """空会话要能出现在列表里（否则切走就找不回来），但 message_count 为 0。"""

    created = store.create()
    listed = {item["id"]: item for item in store.list_threads()}
    assert created["id"] in listed
    assert listed[created["id"]]["message_count"] == 0
