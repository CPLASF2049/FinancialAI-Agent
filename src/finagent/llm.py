"""LLM 构造。

DeepSeek、OpenAI、Moonshot、Qwen、GLM、SiliconFlow、Ollama、LM Studio 之间只有三样
东西不同：base URL、key 和模型 id。它们都兼容 OpenAI 协议，所以一个
:class:`~langchain_openai.ChatOpenAI` 就覆盖了全部厂商。

当 ``FINAGENT_LLM_PROVIDER=none`` 时，Agent 回退到 :mod:`finagent.graph.offline` 里的
确定性规划器，整套系统在没有任何 API Key 的情况下依然能跑。
"""

from __future__ import annotations

from typing import Any

from finagent.errors import ConfigurationError
from finagent.logging import get_logger
from finagent.settings import Settings, get_settings

log = get_logger("llm")

#: 厂商预设：没显式配置时使用的 base URL / 模型。
#:
#: 这些都是 **OpenAI 兼容** 的接口，所以同一套 ``ChatOpenAI`` 客户端就能打通；差别只在
#: 地址、模型名和 Key 的形态。
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "kimi": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "glm": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    "qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    # 讯飞星火：控制台拿「APIPassword」当作 API Key；generalv3.5 就是 Max 版
    "spark": {"base_url": "https://spark-api-open.xf-yun.com/v1", "model": "generalv3.5"},
    # 讯飞星辰 MaaS：多模型平台，Key 是 ak- 开头；具体 modelId 看「模型服务列表」
    "maas": {
        "base_url": "https://maas-api.cn-huabei-1.xf-yun.com/v2",
        "model": "spark-x2.6-4b",
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-72B-Instruct",
    },
    "local": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b"},
    # 自定义：地址和模型全部由用户填，用来接千帆、MiniMax、one-api 网关、vLLM 等
    "custom": {},
}


def build_chat_model(settings: Settings | None = None, **overrides: Any) -> Any:
    """返回配置所指定的 LangChain 聊天模型。"""

    resolved = settings or get_settings()
    resolved.require_llm()

    preset = PROVIDER_PRESETS.get(resolved.llm_provider, {})
    base_url = resolved.llm_base_url or preset.get("base_url")
    model = resolved.llm_model or preset.get("model")

    # 「自定义」厂商没有预设，地址和模型必须由用户填，否则会悄悄连到 api.openai.com
    if resolved.llm_provider == "custom":
        if not base_url:
            raise ConfigurationError(
                "自定义厂商缺少接口地址。",
                hint="设置 FINAGENT_LLM_BASE_URL，例如 https://spark-api-open.xf-yun.com/v1",
            )
        if not model:
            raise ConfigurationError(
                "自定义厂商缺少模型名。",
                hint="设置 FINAGENT_LLM_MODEL，例如 generalv3.5",
            )

    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ConfigurationError(
            "未安装 langchain-openai。",
            hint="pip install langchain-openai",
        ) from exc

    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": resolved.llm_api_key,
        "base_url": base_url,
        "temperature": resolved.llm_temperature,
        "timeout": resolved.llm_timeout_seconds,
        "max_retries": 2,
    }
    kwargs.update(overrides)
    log.info("LLM：provider=%s model=%s base_url=%s", resolved.llm_provider, model, base_url)
    return ChatOpenAI(**kwargs)


def describe_llm(settings: Settings | None = None) -> dict[str, Any]:
    resolved = settings or get_settings()
    preset = PROVIDER_PRESETS.get(resolved.llm_provider, {})
    return {
        "provider": resolved.llm_provider,
        "enabled": resolved.llm_enabled,
        "model": resolved.llm_model or preset.get("model"),
        "base_url": resolved.llm_base_url or preset.get("base_url"),
        "mode": "llm" if resolved.llm_enabled else "确定性",
    }
