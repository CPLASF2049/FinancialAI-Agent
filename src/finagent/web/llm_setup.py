"""在界面里接入大模型：写配置、热生效、连通性自检。

界面上的「接入大模型」面板最终落到这里。它做四件事：

1. 把用户填的厂商 / Key / 模型 / 地址写进 ``.env``（合并已有内容，不覆盖其他配置项）；
2. 清掉配置缓存，让新设置立刻生效，并原地重建 Agent 图；
3. 通过一次极小的请求做连通性自检，把真实报错原样回给界面；
4. 报告「环境变量是否覆盖了文件」，因为环境变量优先级更高，不说清楚会让人以为保存失败。

写入目标是 ``env_file_candidates()`` 里第一个「看起来属于用户」的位置：项目根目录存在
``pyproject.toml`` 就写那里，否则写数据目录。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from finagent.llm import PROVIDER_PRESETS, describe_llm
from finagent.logging import get_logger
from finagent.settings import Settings, env_file_candidates, get_settings, reset_settings_cache

log = get_logger("web.llm")

#: 界面上「厂商」下拉框的内容。``needs_key=False`` 的是本地模型，不需要 Key。
PROVIDER_CHOICES: tuple[dict[str, Any], ...] = (
    {
        "id": "deepseek",
        "label": "DeepSeek（推荐）",
        "base_url": PROVIDER_PRESETS["deepseek"]["base_url"],
        "model": PROVIDER_PRESETS["deepseek"]["model"],
        "needs_key": True,
        "hint": "在 platform.deepseek.com 创建，形如 sk-xxxxxxxx",
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "base_url": PROVIDER_PRESETS["openai"]["base_url"],
        "model": PROVIDER_PRESETS["openai"]["model"],
        "needs_key": True,
        "hint": "platform.openai.com 的 API Key",
    },
    {
        "id": "kimi",
        "label": "Kimi / Moonshot",
        "base_url": PROVIDER_PRESETS["kimi"]["base_url"],
        "model": PROVIDER_PRESETS["kimi"]["model"],
        "needs_key": True,
        "hint": "platform.moonshot.cn 的 API Key",
    },
    {
        "id": "glm",
        "label": "智谱 GLM",
        "base_url": PROVIDER_PRESETS["glm"]["base_url"],
        "model": PROVIDER_PRESETS["glm"]["model"],
        "needs_key": True,
        "hint": "open.bigmodel.cn 的 API Key",
    },
    {
        "id": "qwen",
        "label": "通义千问",
        "base_url": PROVIDER_PRESETS["qwen"]["base_url"],
        "model": PROVIDER_PRESETS["qwen"]["model"],
        "needs_key": True,
        "hint": "阿里云百炼的 API Key",
    },
    {
        "id": "spark",
        "label": "讯飞星火（星火大模型）",
        "base_url": PROVIDER_PRESETS["spark"]["base_url"],
        "model": PROVIDER_PRESETS["spark"]["model"],
        "needs_key": True,
        "hint": "控制台「http服务接口认证信息」里的 APIPassword（不是 APIKey/APISecret）",
        "models": "generalv3.5（Max）· 4.0Ultra · max-32k · generalv3（Pro）· pro-128k · lite",
    },
    {
        "id": "maas",
        "label": "讯飞星辰 MaaS（多模型平台）",
        "base_url": PROVIDER_PRESETS["maas"]["base_url"],
        "model": PROVIDER_PRESETS["maas"]["model"],
        "needs_key": True,
        "hint": "MaaS 控制台的 API Key，ak- 开头（和星火的 APIPassword 不是一回事）",
        "models": "看「模型服务列表」里的 modelId，例如 spark-x2.6-4b、xdeepseekv32",
    },
    {
        "id": "siliconflow",
        "label": "SiliconFlow",
        "base_url": PROVIDER_PRESETS["siliconflow"]["base_url"],
        "model": PROVIDER_PRESETS["siliconflow"]["model"],
        "needs_key": True,
        "hint": "siliconflow.cn 的 API Key",
    },
    {
        "id": "local",
        "label": "本地模型（Ollama / LM Studio）",
        "base_url": PROVIDER_PRESETS["local"]["base_url"],
        "model": PROVIDER_PRESETS["local"]["model"],
        "needs_key": False,
        "hint": "本地服务通常不校验 Key，留空即可",
    },
    {
        "id": "custom",
        "label": "自定义（任意 OpenAI 兼容接口）",
        "base_url": "",
        "model": "",
        "needs_key": True,
        "key_optional": True,
        "hint": "填第 4 项的地址与模型名，例如百度千帆、MiniMax、one-api 网关、vLLM",
    },
)

#: 由这个面板管理的配置项。写入时只碰这几行，其余原样保留。
MANAGED_KEYS = (
    "FINAGENT_LLM_PROVIDER",
    "FINAGENT_LLM_API_KEY",
    "FINAGENT_LLM_MODEL",
    "FINAGENT_LLM_BASE_URL",
)

_ENV_WRITE_ORDER = (
    "# ===== 由界面「接入大模型」写入 =====",
    "FINAGENT_LLM_PROVIDER",
    "FINAGENT_LLM_API_KEY",
    "FINAGENT_LLM_MODEL",
    "FINAGENT_LLM_BASE_URL",
)

_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


# ----------------------------------------------------------------------
def mask_key(key: str | None) -> str:
    """只露头尾，界面里够用来确认「填的是哪个 Key」。"""

    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "…"
    return f"{key[:6]}…{key[-4:]}"


def env_target() -> Path:
    """选择写入位置：``FINAGENT_ENV_FILE`` 覆盖 > 项目根 > 数据目录 > 当前目录。"""

    override = os.environ.get("FINAGENT_ENV_FILE", "").strip()
    if override:
        return Path(override).expanduser()

    root = Path(__file__).resolve().parents[3]
    if (root / "pyproject.toml").exists():
        return root / ".env"
    for candidate in env_file_candidates():
        if candidate.parent.exists():
            return candidate
    return Path.cwd() / ".env"


def env_overrides() -> list[str]:
    """哪些受管配置被进程环境变量覆盖了（环境变量优先级更高）。"""

    return [key for key in MANAGED_KEYS if os.environ.get(key, "").strip()]


def read_env_file(path: Path) -> dict[str, str]:
    """解析一个 ``.env``，仅用于展示。"""

    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _LINE_RE.match(line)
        if match:
            _, _, raw = line.partition("=")
            values[match.group(1)] = raw.strip()
    return values


def write_env(path: Path, updates: dict[str, str]) -> Path:
    """把 ``updates`` 合并进 ``.env``：已有键就地替换，其余行原样保留。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    written: set[str] = set()
    out: list[str] = []
    for line in lines:
        match = _LINE_RE.match(line)
        if match and match.group(1) in updates:
            key = match.group(1)
            if key not in written:
                out.append(f"{key}={updates[key]}")
                written.add(key)
            continue  # 重复键丢掉，避免「改了但没生效」
        out.append(line)

    missing = {key: value for key, value in updates.items() if key not in written}
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.append(_ENV_WRITE_ORDER[0])
        for key in _ENV_WRITE_ORDER[1:]:
            if key in missing:
                out.append(f"{key}={missing[key]}")
        for key, value in missing.items():
            if key not in _ENV_WRITE_ORDER:
                out.append(f"{key}={value}")

    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return path


# ----------------------------------------------------------------------
def current_state(settings: Settings | None = None) -> dict[str, Any]:
    """给界面用的当前配置快照。"""

    resolved = settings or get_settings()
    llm = describe_llm(resolved)
    target = env_target()
    return {
        "provider": resolved.llm_provider,
        "model": resolved.llm_model,
        "base_url": resolved.llm_base_url,
        "enabled": resolved.llm_enabled,
        "has_key": bool(resolved.llm_api_key),
        "key_masked": mask_key(resolved.llm_api_key),
        "mode": llm["mode"],
        "presets": list(PROVIDER_CHOICES),
        "env_file": str(target),
        "env_file_exists": target.exists(),
        "env_overrides": env_overrides(),
    }


def apply(
    *,
    provider: str,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> Settings:
    """写入配置并让新设置生效，返回新的 :class:`Settings`。"""

    cleaned_provider = str(provider).strip().lower()
    valid = {choice["id"] for choice in PROVIDER_CHOICES} | {"none"}
    if cleaned_provider not in valid:
        raise ValueError(f"不支持的厂商 '{provider}'。可选：{', '.join(sorted(valid))}")

    preset = PROVIDER_PRESETS.get(cleaned_provider, {})
    updates: dict[str, str] = {"FINAGENT_LLM_PROVIDER": cleaned_provider}

    key = (api_key or "").strip()
    if key:
        updates["FINAGENT_LLM_API_KEY"] = key
    elif cleaned_provider in ("local", "custom"):
        # 本地模型与自建网关通常不校验 Key，但客户端要求非空，写个占位符保持一致
        updates["FINAGENT_LLM_API_KEY"] = cleaned_provider
    elif cleaned_provider == "none":
        pass
    else:
        raise ValueError("请填写 API Key")

    resolved_model = (model or "").strip() or preset.get("model", "")
    resolved_base = (base_url or "").strip() or preset.get("base_url", "")

    # 「自定义」没有预设垫底，必须自己填全，否则会悄悄连到 api.openai.com
    if cleaned_provider == "custom":
        if not resolved_base:
            raise ValueError("「自定义」需要填接口地址，例如 https://spark-api-open.xf-yun.com/v1")
        if not resolved_base.startswith(("http://", "https://")):
            raise ValueError("接口地址要以 http:// 或 https:// 开头")
        if not resolved_model:
            raise ValueError("「自定义」需要填模型名，例如 generalv3.5")

    updates["FINAGENT_LLM_MODEL"] = resolved_model
    updates["FINAGENT_LLM_BASE_URL"] = resolved_base

    path = write_env(env_target(), updates)
    log.info("已写入大模型配置 -> %s（provider=%s）", path, cleaned_provider)

    # 环境变量优先级高于文件：如果它已经设了，文件里的值不会生效，这里同步一下进程环境，
    # 否则「保存成功却仍然未启用」会让人一头雾水。
    for key_name, value in updates.items():
        if value:
            os.environ[key_name] = value

    reset_settings_cache()
    from finagent.providers import registry

    registry._cached.cache_clear()  # noqa: SLF001 - 数据源未变，仅为一致性

    return get_settings()


async def test_connection(
    settings: Settings | None = None, *, timeout: float = 25.0
) -> dict[str, Any]:
    """用一次极小请求验证 Key / 地址 / 模型是否可用。"""

    import asyncio

    resolved = settings or get_settings()
    if not resolved.llm_enabled:
        return {"ok": False, "message": "尚未配置大模型"}

    try:
        from langchain_core.messages import HumanMessage

        from finagent.llm import build_chat_model

        model = build_chat_model(resolved)
        bound = model.bind(max_tokens=8)
        reply = await asyncio.wait_for(bound.ainvoke([HumanMessage(content="hi")]), timeout=timeout)
        text = getattr(reply, "content", "") or ""
        if isinstance(text, list):
            text = " ".join(str(item) for item in text)
        return {
            "ok": True,
            "message": f"连接正常，模型已响应：{str(text).strip()[:40] or '（空）'}",
        }
    except TimeoutError:
        return {"ok": False, "message": f"连接超时（>{int(timeout)} 秒），检查网络或接口地址"}
    except Exception as exc:  # noqa: BLE001 - 原样把服务端的错误给用户
        return {"ok": False, "message": friendly_error(str(exc))}


def friendly_error(message: str) -> str:
    """把厂商返回的原始报错翻成一句人话，并保留一小段原文方便排查。"""

    short = " ".join(str(message).split())[:150]
    lowered = str(message).lower()
    if "401" in str(message) or "authentication" in lowered or "invalid api key" in lowered:
        return f"API Key 无效或已过期（401），请重新生成后粘贴。原始错误：{short}"
    if "403" in str(message):
        return f"这个 Key 没有该模型的权限（403），换个模型或开通权限。原始错误：{short}"
    if "404" in str(message):
        return f"接口地址或模型名不对（404），检查 base URL 与模型。原始错误：{short}"
    if "429" in str(message):
        return f"请求过于频繁或额度用尽（429），稍后再试或检查余额。原始错误：{short}"
    if "timeout" in lowered or "connect" in lowered or "timed out" in lowered:
        return f"连接失败，检查网络或接口地址。原始错误：{short}"
    return short[:300]
