"""配置解析：.env 查找、LLM 开关、别名的生效条件。

这里覆盖的正是「我在哪配大模型」这个最常被问的问题——配置写在哪个文件、从哪个目录
运行才生效、只填 key 够不够。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from finagent.settings import Settings, env_file_candidates, get_settings, reset_settings_cache


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "FINAGENT_LLM_PROVIDER",
        "FINAGENT_LLM_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_settings_cache()


def test_llm_is_off_by_default() -> None:
    settings = Settings(_env_file=None)
    assert settings.llm_provider == "none"
    assert settings.llm_enabled is False


def test_key_alone_does_not_enable_the_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """只设 DEEPSEEK_API_KEY 不够——必须同时指定 FINAGENT_LLM_PROVIDER。"""

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    settings = Settings(_env_file=None)

    assert settings.llm_api_key == "sk-test"  # 别名被读到了
    assert settings.llm_provider == "none"  # 但厂商还是 none
    assert settings.llm_enabled is False


def test_provider_plus_key_enables_the_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINAGENT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("FINAGENT_LLM_API_KEY", "sk-test")
    settings = Settings(_env_file=None)

    assert settings.llm_enabled is True
    assert settings.llm_model == "deepseek-chat"
    assert settings.llm_base_url == "https://api.deepseek.com"


def test_require_llm_explains_the_missing_piece(monkeypatch: pytest.MonkeyPatch) -> None:
    from finagent.errors import ConfigurationError

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    with pytest.raises(ConfigurationError) as excinfo:
        Settings(_env_file=None).require_llm()

    assert "FINAGENT_LLM_PROVIDER=deepseek" in (excinfo.value.hint or "")


def test_require_llm_complains_about_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from finagent.errors import ConfigurationError

    monkeypatch.setenv("FINAGENT_LLM_PROVIDER", "deepseek")
    with pytest.raises(ConfigurationError) as excinfo:
        Settings(_env_file=None).require_llm()
    assert "API Key" in excinfo.value.message


def test_env_file_candidates_prefers_cwd(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(data_dir)
    candidates = env_file_candidates()

    # 任何目录下都必须包含 cwd，且 cwd 排在最后（pydantic-settings 后者覆盖前者）
    assert candidates[-1] == data_dir / ".env"
    assert len(set(candidates)) == len(candidates)


def test_settings_reads_a_given_env_file(data_dir: Path) -> None:
    env_file = data_dir / ".env"
    env_file.write_text(
        "FINAGENT_LLM_PROVIDER=deepseek\nFINAGENT_LLM_API_KEY=sk-from-file\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_file)

    assert settings.llm_provider == "deepseek"
    assert settings.llm_api_key == "sk-from-file"
    assert settings.llm_enabled is True


def test_environment_beats_env_file(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = data_dir / ".env"
    env_file.write_text("FINAGENT_LLM_PROVIDER=deepseek\nFINAGENT_LLM_API_KEY=sk-file\n", "utf-8")
    monkeypatch.setenv("FINAGENT_LLM_API_KEY", "sk-env")

    assert Settings(_env_file=env_file).llm_api_key == "sk-env"


def test_blank_data_dir_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINAGENT_DATA_DIR", "")
    assert Settings(_env_file=None).resolve_data_dir().name == ".finagent"


def test_get_settings_is_cached_but_resetable() -> None:
    first = get_settings()
    assert get_settings() is first
    reset_settings_cache()
    assert get_settings() is not first
