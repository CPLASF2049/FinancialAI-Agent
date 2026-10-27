"""共享的 pytest fixture 与一个脚本化聊天模型。

所有测试都**完全离线**运行：``FINAGENT_PROVIDER=offline`` 提供确定性的 fixture，
数据目录建在仓库内部（沙箱不允许写系统临时目录）。
"""

from __future__ import annotations

import itertools
import os
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTEST_DATA = REPO_ROOT / "_pytest"

_counter = itertools.count()


@pytest.fixture(scope="session", autouse=True)
def _cleanup_runtime_dirs() -> Any:
    yield
    shutil.rmtree(PYTEST_DATA / "data", ignore_errors=True)


@pytest.fixture
def data_dir() -> Path:
    """A fresh, writable data directory inside the workspace."""

    path = PYTEST_DATA / "data" / f"t{next(_counter)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def offline_env(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> Path:
    """Point the whole stack at the offline provider and a scratch data dir."""

    monkeypatch.setenv("FINAGENT_PROVIDER", "offline")
    monkeypatch.setenv("FINAGENT_TUSHARE_TOKEN", "")
    monkeypatch.setenv("FINAGENT_LLM_PROVIDER", "none")
    monkeypatch.setenv("FINAGENT_LLM_API_KEY", "")
    monkeypatch.setenv("FINAGENT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("FINAGENT_MCP_TRANSPORT", "inprocess")

    from finagent.settings import reset_settings_cache

    reset_settings_cache()

    from finagent.providers import registry

    registry._cached.cache_clear()  # noqa: SLF001 - test isolation

    yield data_dir

    reset_settings_cache()
    registry._cached.cache_clear()  # noqa: SLF001
    os.environ.pop("FINAGENT_DATA_DIR", None)


@pytest.fixture
def provider(offline_env: Path):
    from finagent.providers.offline import OfflineProvider

    return OfflineProvider()


# ----------------------------------------------------------------------
# 脚本化的聊天模型，在没有网络的情况下也能测 LLM 相关路径
# ----------------------------------------------------------------------
class ScriptedChatModel:
    """Minimal stand-in for ``ChatOpenAI``.

    Returns the queued messages in order; ``bind_tools`` is a no-op so the ReAct
    loop under test behaves exactly as it would with a real tool-calling model.
    """

    def __init__(self, responses: Sequence[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[Any] = []
        self.bound_tools: list[str] = []

    # LangChain 兼容层 --------------------------------------------------
    def bind_tools(self, tools: Sequence[Any], **_kwargs: Any) -> ScriptedChatModel:
        self.bound_tools = [getattr(tool, "name", str(tool)) for tool in tools]
        return self

    async def ainvoke(self, messages: Any, **_kwargs: Any) -> Any:
        self.calls.append(messages)
        if not self.responses:
            raise AssertionError("ScriptedChatModel ran out of responses")
        return self.responses.pop(0)


def ai_text(text: str) -> Any:
    from langchain_core.messages import AIMessage

    return AIMessage(content=text)


def ai_tool_call(name: str, args: dict[str, Any], call_id: str = "call-1") -> Any:
    from langchain_core.messages import AIMessage

    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


@pytest.fixture
def scripted_model() -> Callable[..., ScriptedChatModel]:
    return ScriptedChatModel
