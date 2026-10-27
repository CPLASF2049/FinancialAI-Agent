"""集中配置。

一切都由环境变量驱动，于是同一个 wheel 既能当 MCP 服务器、当 LangGraph worker，也能
当交互式 CLI。除 ``FINAGENT_*`` 主名外，也接受若干简短别名（``TUSHARE_TOKEN``、
``DEEPSEEK_API_KEY``、``OPENAI_BASE_URL``、``DATA_SOURCE`` 等），方便在已有的 shell
或 CI 环境里直接复用变量。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["offline", "tushare"]
LLMProviderName = Literal[
    "deepseek",
    "openai",
    "kimi",
    "glm",
    "qwen",
    "spark",
    "maas",
    "siliconflow",
    "local",
    "custom",
    "none",
]

#: 没有显式配置时数据目录的落点。刻意不读环境变量：`FINAGENT_DATA_DIR` 由字段本身读取，
#: 留空时应稳定地回到这里。
_DEFAULT_DATA_DIR = Path.home() / ".finagent"

_ENV_FILENAME = ".env"


def env_file_candidates() -> tuple[Path, ...]:
    """``.env`` 的查找顺序（顺序即优先级，**排在后面的覆盖前面的**）。

    1. 项目根目录（从本文件位置推回去；可编辑安装时就是仓库根）
    2. 用户数据目录（默认 ``~/.finagent``）

    当前工作目录放在最后，因此它优先级最高——在哪个目录下运行，就以那里的 ``.env``
    为准。这样既支持「在项目里跑」，也支持「在任意目录跑但配置集中在数据目录」。
    """

    candidates = [
        Path(__file__).resolve().parents[2] / _ENV_FILENAME,
        Path.home() / ".finagent" / _ENV_FILENAME,
        Path.cwd() / _ENV_FILENAME,
    ]
    ordered: list[Path] = []
    for candidate in candidates:
        if candidate not in ordered:
            ordered.append(candidate)
    return tuple(ordered)


class Settings(BaseSettings):
    """不可变的运行时配置。"""

    model_config = SettingsConfigDict(
        env_prefix="FINAGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # ---------------- 数据源 ----------------
    provider: ProviderName = Field(
        default="offline",
        validation_alias=AliasChoices("FINAGENT_PROVIDER", "FIN_AGENT_PROVIDER", "DATA_SOURCE"),
        description=(
            "'offline' serves deterministic synthetic fixtures (no network, ideal for tests "
            "and demos); 'tushare' calls the Tushare Pro API."
        ),
    )
    tushare_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("FINAGENT_TUSHARE_TOKEN", "TUSHARE_TOKEN"),
    )
    tushare_rps: float = Field(
        default=4.0,
        validation_alias=AliasChoices("FINAGENT_TUSHARE_RPS", "TUSHARE_RPS"),
        description="Client-side rate limit for provider calls.",
    )

    # ---------------- LLM ----------------
    llm_provider: LLMProviderName = Field(
        default="none",
        validation_alias=AliasChoices("FINAGENT_LLM_PROVIDER", "LLM_PROVIDER"),
        description="'none' selects the deterministic rule-based planner (offline mode).",
    )
    llm_model: str = Field(
        default="deepseek-chat",
        validation_alias=AliasChoices("FINAGENT_LLM_MODEL", "DEEPSEEK_MODEL", "OPENAI_MODEL"),
    )
    llm_base_url: str | None = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices(
            "FINAGENT_LLM_BASE_URL", "DEEPSEEK_BASE_URL", "OPENAI_BASE_URL"
        ),
    )
    llm_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("FINAGENT_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"),
    )
    llm_temperature: float = Field(default=0.2, validation_alias="FINAGENT_LLM_TEMPERATURE")
    llm_timeout_seconds: float = Field(default=90.0, validation_alias="FINAGENT_LLM_TIMEOUT")
    max_tool_iterations: int = Field(default=8, validation_alias="FINAGENT_MAX_TOOL_ITERATIONS")
    max_agent_hops: int = Field(
        default=4,
        validation_alias="FINAGENT_MAX_AGENT_HOPS",
        description="Upper bound on supervisor -> expert hand-offs per user turn.",
    )
    #: 每个专家最多被审查者打回返工几次。0 表示只审不返工。
    max_revisions: int = Field(
        default=1,
        validation_alias="FINAGENT_MAX_REVISIONS",
        description="每个专家被 critic 打回返工的最大次数。",
    )
    #: 交付前的确定性风控护栏开关。
    risk_guard_enabled: bool = Field(
        default=True,
        validation_alias="FINAGENT_RISK_GUARD",
        description="是否启用 risk_guard（确定性规则护栏）。",
    )

    # ---------------- 存储 ----------------
    #: 用普通字符串存放：``Path("")`` 会解析成当前目录，所以 .env 里留空的
    #: ``FINAGENT_DATA_DIR=`` 必须表示「用默认值」。
    data_dir: str | None = Field(
        default=None,
        validation_alias=AliasChoices("FINAGENT_DATA_DIR", "FIN_AGENT_DATA_DIR"),
    )
    checkpoint_db: str = Field(
        default="checkpoints.sqlite",
        validation_alias="FINAGENT_CHECKPOINT_DB",
        description="SQLite file (relative to data_dir) holding LangGraph checkpoints.",
    )

    # ---------------- 预警 / worker ----------------
    alert_poll_seconds: int = Field(default=60, validation_alias="FINAGENT_ALERT_POLL_SECONDS")
    alert_trading_hours_only: bool = Field(
        default=True, validation_alias="FINAGENT_ALERT_TRADING_HOURS_ONLY"
    )

    # ---------------- 邮件投递（可选） ----------------
    email_smtp_server: str | None = Field(
        default=None, validation_alias=AliasChoices("FINAGENT_SMTP_SERVER", "EMAIL_SMTP_SERVER")
    )
    email_smtp_port: int = Field(
        default=465, validation_alias=AliasChoices("FINAGENT_SMTP_PORT", "EMAIL_SMTP_PORT")
    )
    email_sender: str | None = Field(
        default=None, validation_alias=AliasChoices("FINAGENT_EMAIL_SENDER", "EMAIL_SENDER")
    )
    email_password: str | None = Field(
        default=None, validation_alias=AliasChoices("FINAGENT_EMAIL_PASSWORD", "EMAIL_PASSWORD")
    )
    email_receiver: str | None = Field(
        default=None, validation_alias=AliasChoices("FINAGENT_EMAIL_RECEIVER", "EMAIL_RECEIVER")
    )

    # ---------------- 可观测性 ----------------
    log_level: str = Field(default="INFO", validation_alias="FINAGENT_LOG_LEVEL")

    # ------------------------------------------------------------------
    def resolve_data_dir(self) -> Path:
        raw = (self.data_dir or "").strip()
        path = Path(raw).expanduser() if raw else _DEFAULT_DATA_DIR
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve_checkpoint_path(self) -> Path:
        raw = (self.checkpoint_db or "").strip() or "checkpoints.sqlite"
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            candidate.parent.mkdir(parents=True, exist_ok=True)
            return candidate
        return self.resolve_data_dir() / candidate

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider != "none" and bool(self.llm_api_key)

    def require_llm(self) -> None:
        from finagent.errors import ConfigurationError

        if self.llm_provider == "none":
            hint = "设置 FINAGENT_LLM_PROVIDER=deepseek 与 FINAGENT_LLM_API_KEY=<你的 key> 即可启用 Agent。"
            if self.llm_api_key:
                hint = (
                    "已经检测到 API Key，只差指定厂商：在配置里加一行 "
                    "FINAGENT_LLM_PROVIDER=deepseek（或 openai/kimi/glm/qwen/siliconflow/local）。"
                )
            raise ConfigurationError("LLM 提供方被设为 'none'（当前走确定性规划器）。", hint=hint)
        if not self.llm_api_key:
            raise ConfigurationError(
                f"LLM 提供方 '{self.llm_provider}' 未配置 API Key。",
                hint="设置 FINAGENT_LLM_API_KEY（或 DEEPSEEK_API_KEY / OPENAI_API_KEY）。",
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级配置单例。

    ``.env`` 的查找顺序见 :func:`env_file_candidates`，所以在项目目录之外的任何位置运行
    也能读到配置。
    """

    return Settings(_env_file=env_file_candidates())


def reset_settings_cache() -> None:
    """测试钩子：丢弃缓存的配置，让环境变量改动生效。"""

    get_settings.cache_clear()
