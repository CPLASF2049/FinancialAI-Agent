"""端到端演示：把几条请求跑过真实的图。

    python scripts/demo.py

不需要 API Key，也不需要 Tushare token（离线数据源 + 确定性规划器）。设置
FINAGENT_PROVIDER=tushare 与 FINAGENT_LLM_PROVIDER/API_KEY，即可看到同一套拓扑的
LLM 驱动版本。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

os.environ.setdefault("FINAGENT_PROVIDER", "offline")
os.environ.setdefault("FINAGENT_LLM_PROVIDER", "none")
os.environ.setdefault("FINAGENT_MCP_TRANSPORT", "inprocess")
os.environ.setdefault("FINAGENT_DATA_DIR", str(REPO_ROOT / "_scratch" / "demo-data"))
os.environ.setdefault("FINAGENT_LOG_LEVEL", "WARNING")

from finagent.graph import open_app  # noqa: E402

QUESTIONS: tuple[str, ...] = (
    "贵州茅台现在多少钱？最近一个月走势怎么样？",
    "帮我筛选市盈率小于 10、市值大于 500 亿的银行股",
    "用 MACD 策略回测中信证券近一年",
    "我以 10.5 元买入 1000 股平安银行",
    "我的持仓盈亏怎么样？",
    "茅台涨到 2000 元提醒我",
    "我是保守型投资者，偏好银行和白酒板块",
)


async def main() -> int:
    async with open_app() as app:
        print(f"MCP 工具：{len(app.toolkit.tools)} 个（{app.toolkit.transport}）")
        print(f"LLM 模式：{'专家 Agent' if app.model else '确定性规划器'}")

        for index, question in enumerate(QUESTIONS, start=1):
            result = await app.arun(question, user_id="demo", thread_id="demo")

            print("\n" + "=" * 78)
            print(f"[{index}/{len(QUESTIONS)}] {question}")
            print("-" * 78)
            print(
                f"路由：{' → '.join(result.agents) or '（仅记忆）'} | "
                f"工具调用：{len(result.trace)} | 错误：{result.errors or '无'}"
            )
            print("-" * 78)
            print(result.answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
