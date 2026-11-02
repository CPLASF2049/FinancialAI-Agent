"""复现「停不下来」：假 LLM 永远要求返工，看一轮会跑多少步。

用完即删。
"""

from __future__ import annotations

import asyncio
import time

from finagent.graph import open_app
from finagent.graph.experts import ExpertOutcome
from finagent.settings import reset_settings_cache

CALLS: list[str] = []


async def always_bad(agent, question, **kwargs):
    """专家永远给出没有数据支撑的数字 → 审查者永远想返工。"""
    CALLS.append(agent)
    return ExpertOutcome(
        agent=agent,
        answer="茅台现价 1234 元，必涨。",
        trace=[],
        tool_calls=0,
        mode="deterministic",
    )


async def main() -> None:
    import finagent.graph.nodes as nodes_mod

    nodes_mod.run_expert = always_bad  # type: ignore[assignment]

    started = time.time()
    async with open_app() as app:
        try:
            result = await asyncio.wait_for(
                app.arun("茅台现在多少钱", user_id="loop", thread_id="loop1"),
                timeout=60,
            )
            elapsed = time.time() - started
            print(f"结束：{len(CALLS)} 次专家调用，{elapsed:.1f}s")
            print("agents:", result.agents)
            print("errors:", result.errors[:2])
        except TimeoutError:
            print(f"60 秒未结束！已调用专家 {len(CALLS)} 次")


reset_settings_cache()
asyncio.run(main())
