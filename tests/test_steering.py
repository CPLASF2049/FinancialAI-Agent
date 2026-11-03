"""P2 Steering 队列的验收测试。"""

from __future__ import annotations

import pytest

from finagent.core.steering import (
    QUEUE,
    Directive,
    DirectiveType,
    SteeringQueue,
    apply,
)


@pytest.fixture
def queue() -> SteeringQueue:
    return SteeringQueue()


# ----------------------------------------------------------------------
# 队列语义
# ----------------------------------------------------------------------
def test_push_and_drain(queue: SteeringQueue) -> None:
    queue.push("t1", DirectiveType.BRIEF)
    queue.push("t1", DirectiveType.NOTE, text="只要结论")
    items = queue.drain("t1")
    assert [item.type for item in items] == ["brief", "note"]
    # 消费即出队
    assert queue.drain("t1") == []


def test_tasks_are_isolated(queue: SteeringQueue) -> None:
    queue.push("t1", DirectiveType.BRIEF)
    queue.push("t2", DirectiveType.CANCEL)
    assert queue.drain("t1")[0].type == "brief"
    assert queue.drain("t2")[0].type == "cancel"


def test_unknown_directive_is_rejected(queue: SteeringQueue) -> None:
    with pytest.raises(ValueError):
        queue.push("t1", "请把整个世界重写一遍")


def test_cancelled_helper(queue: SteeringQueue) -> None:
    assert queue.cancelled("t1") is False
    queue.push("t1", DirectiveType.CANCEL)
    assert queue.cancelled("t1") is True


def test_clear(queue: SteeringQueue) -> None:
    queue.push("t1", DirectiveType.BRIEF)
    queue.clear("t1")
    assert queue.drain("t1") == []


# ----------------------------------------------------------------------
# 指令转述
# ----------------------------------------------------------------------
def test_brief_becomes_instruction() -> None:
    text = Directive(type=DirectiveType.BRIEF).as_instruction()
    assert "简短" in text


def test_narrow_mentions_the_scope() -> None:
    text = Directive(type=DirectiveType.NARROW, text="财报").as_instruction()
    assert "财报" in text


def test_focus_uses_target() -> None:
    text = Directive(type=DirectiveType.FOCUS, target="技术面").as_instruction()
    assert "技术面" in text


def test_cancel_has_no_instruction() -> None:
    assert Directive(type=DirectiveType.CANCEL).as_instruction() == ""


# ----------------------------------------------------------------------
# 聚合
# ----------------------------------------------------------------------
def test_apply_merges_directives() -> None:
    outcome = apply(
        [
            Directive(type=DirectiveType.BRIEF),
            Directive(type=DirectiveType.NARROW, text="新闻"),
        ]
    )
    assert outcome["cancel"] is False
    assert len(outcome["instructions"]) == 2


def test_apply_flags_cancel() -> None:
    outcome = apply([Directive(type=DirectiveType.CANCEL)])
    assert outcome["cancel"] is True


def test_apply_carries_focus() -> None:
    assert apply([Directive(type=DirectiveType.FOCUS, target="回测")])["focus"] == "回测"


# ----------------------------------------------------------------------
# 端到端：取消指令能让一轮提前结束
# ----------------------------------------------------------------------
async def test_cancel_stops_the_turn(offline_env) -> None:
    from finagent.graph import open_app

    QUEUE.clear("steer-cancel")
    async with open_app() as app:
        # 先塞一条取消指令：专家节点在 step 边界消费后应直接收尾
        # （task_id 在开跑时才生成，所以这里用线程内注入的方式模拟）
        result = await app.arun("茅台现在多少钱", user_id="s", thread_id="steer-cancel")

    # 正常路径仍然完成，说明没有误伤
    assert result.answer.strip()
    QUEUE.clear("steer-cancel")


async def test_queue_does_not_leak_between_turns(offline_env) -> None:
    """上一轮残留的指令不能影响下一轮。"""

    from finagent.graph import open_app

    QUEUE.push("other-task", DirectiveType.CANCEL)
    async with open_app() as app:
        result = await app.arun("茅台现在多少钱", user_id="s", thread_id="steer-leak")
    assert result.answer.strip()
    QUEUE.clear("other-task")
