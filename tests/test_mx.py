"""妙想 Skills 桥接与 MCP 工具测试。

全部用假的技能模块，不联网、不消耗真实额度。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finagent.core import mx
from finagent.errors import ConfigurationError, ProviderUnavailable


class FakeDataClient:
    """模拟 mx-data 的客户端：query + parse_result + format_terminal。"""

    calls: list[str] = []

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key

    def query(self, question: str) -> dict:
        type(self).calls.append(question)
        return {
            "status": 0,
            "data": {"rows": [{"date": "2025年报", "净利润": "853.1亿元"}]},
        }

    def parse_result(self, result: dict):
        rows = result["data"]["rows"]
        return (
            [{"sheet_name": "财务", "rows": rows, "fieldnames": list(rows[0])}],
            ["贵州茅台"],
            len(rows),
            None,
        )

    def format_terminal(self, result: dict, tables: list, total: int) -> str:
        return f"**查询结果**: {total} 行"


class FakeSearchClient:
    calls: list[str] = []

    def __init__(self, api_key: str = "") -> None:
        pass

    def search(self, question: str) -> dict:
        type(self).calls.append(question)
        return {"status": 0, "data": "研报：维持买入评级"}

    def format_pretty(self, result: dict) -> str:
        return result["data"]


class FakeScreenClient:
    calls: list[str] = []

    def __init__(self, api_key: str = "") -> None:
        pass

    def search(self, question: str) -> dict:
        type(self).calls.append(question)
        return {"status": 0}

    def extract_data(self, result: dict):
        return (
            [
                {"代码": "601398.SH", "名称": "工商银行", "市盈率": "8.69"},
                {"代码": "601939.SH", "名称": "建设银行", "市盈率": "9.28"},
            ],
            "allResults",
            None,
        )


FAKE_CLASSES = {
    "data": ("MXData", FakeDataClient),
    "search": ("MXSearch", FakeSearchClient),
    "screen": ("MXSelectStock", FakeScreenClient),
}


def _skills_dir(base) -> Path:
    """在仓库内造一个假技能目录（沙箱不允许写系统临时目录，所以不用 tmp_path）。"""

    root = Path(base) / "fake_mx_skills"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def mx_env(monkeypatch, data_dir):
    """给一个装了假技能、配了 Key、数据目录独立的运行环境。"""

    from finagent.settings import get_settings, reset_settings_cache

    base = _skills_dir(data_dir)
    monkeypatch.setenv("MX_APIKEY", "mkt_test_key")
    monkeypatch.setenv("FINAGENT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("FINAGENT_MX_DAILY_LIMIT", "3")
    monkeypatch.setenv("FINAGENT_PROVIDER", "offline")
    reset_settings_cache()

    for client in (FakeDataClient, FakeSearchClient, FakeScreenClient):
        client.calls = []

    def fake_load(kind: str, settings) -> object:
        return FAKE_CLASSES[kind][1](api_key=settings.mx_api_key)

    monkeypatch.setattr(mx, "_load_client", fake_load)
    monkeypatch.setattr(mx, "skill_script", lambda kind, settings=None: base / kind)
    yield get_settings()
    reset_settings_cache()


# ----------------------------------------------------------------------
def test_financial_data_returns_tables(mx_env) -> None:
    payload = mx.run("data", "贵州茅台近三年净利润")

    assert payload["skill"] == mx.SKILL_LABELS["data"]
    assert payload["tables"][0]["rows"][0]["date"] == "2025年报"
    assert payload["row_count"] == 1
    assert "查询结果" in payload["text"]
    assert payload["cached"] is False
    assert payload["quota"]["used"] == 1


def test_same_query_hits_cache_and_spares_quota(mx_env) -> None:
    """同一问句第二次必须走缓存：接口按天限次，不能白扣。"""

    first = mx.run("data", "茅台净利润")
    second = mx.run("data", "茅台净利润")

    assert first["cached"] is False
    assert second["cached"] is True
    assert len(FakeDataClient.calls) == 1, "第二次不该再打接口"
    assert second["quota"]["used"] == 1


def test_daily_limit_blocks_further_calls(mx_env) -> None:
    for index in range(3):
        mx.run("data", f"问题 {index}")

    with pytest.raises(ProviderUnavailable) as excinfo:
        mx.run("data", "第四个问题")
    assert "额度" in str(excinfo.value)
    assert "明天" in (getattr(excinfo.value, "hint", "") or "")


def test_quota_exhaustion_does_not_call_api(mx_env) -> None:
    for index in range(3):
        mx.run("data", f"问 {index}")
    before = len(FakeDataClient.calls)

    with pytest.raises(ProviderUnavailable):
        mx.run("data", "超出")
    assert len(FakeDataClient.calls) == before


def test_missing_key_is_configuration_error(data_dir) -> None:
    """没配 Key 时要给出可操作的错误，而不是让技能脚本抛 ValueError。

    注意不能改环境变量来测：.env 里现在就有 MX_APIKEY，monkeypatch 抹不掉。
    直接构造一个空 Key 的 Settings 最可靠。
    """

    from finagent.settings import Settings

    settings = Settings(mx_api_key="", data_dir=str(data_dir))
    with pytest.raises(ConfigurationError) as excinfo:
        mx.run("data", "茅台", settings=settings)

    message = str(excinfo.value)
    hint = getattr(excinfo.value, "hint", "") or ""
    assert "MX_APIKEY" in message or "API Key" in message
    assert "dl.dfcfs.com" in hint or "MX_APIKEY" in hint


def test_news_search_text(mx_env) -> None:
    payload = mx.run("search", "茅台 研报")
    assert "买入评级" in payload["text"]
    assert payload["tables"] == []


def test_screen_rows(mx_env) -> None:
    payload = mx.run("screen", "PE 小于 15 的银行")
    assert payload["row_count"] == 2
    assert payload["tables"][0]["rows"][0]["名称"] == "工商银行"


def test_quota_status_shape(mx_env) -> None:
    mx.run("data", "茅台")
    state = mx.quota()
    assert state["used"] == 1
    assert state["limit"] == 3
    assert state["remaining"] == 2


def test_installed_detects_missing_skill(monkeypatch, data_dir) -> None:
    from finagent.settings import reset_settings_cache

    monkeypatch.setenv("FINAGENT_MX_SKILLS_DIR", str(_skills_dir(data_dir) / "nowhere"))
    reset_settings_cache()
    flags = mx.installed()
    assert set(flags) == set(mx.SKILLS)
    assert not any(flags.values())
    reset_settings_cache()


def test_is_available_needs_key_and_skill(monkeypatch, data_dir) -> None:
    from finagent.settings import get_settings, reset_settings_cache

    monkeypatch.setenv("FINAGENT_MX_SKILLS_DIR", str(_skills_dir(data_dir) / "nowhere"))
    monkeypatch.setenv("MX_APIKEY", "mkt_x")
    reset_settings_cache()
    assert mx.is_available(get_settings()) is False
    reset_settings_cache()


# ----------------------------------------------------------------------
def test_mcp_server_exposes_mx_tools(mx_env) -> None:
    from finagent.mcp_servers.mx import build_server

    server = build_server()
    tools = server._tool_manager.list_tools()
    names = {tool.name for tool in tools}
    assert {"mx_financial_data", "mx_news_search", "mx_stock_screen", "mx_quota_status"} <= names


def test_mcp_tool_returns_envelope(mx_env) -> None:
    from finagent.mcp_servers.mx import build_server

    server = build_server()
    tool = server._tool_manager.get_tool("mx_financial_data")
    raw = tool.fn(query="贵州茅台近三年净利润")
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["data"]["tables"][0]["rows"]


def test_mcp_tool_reports_quota_error_as_envelope(mx_env) -> None:
    from finagent.mcp_servers.mx import build_server

    for index in range(3):
        mx.run("data", f"占额度 {index}")

    server = build_server()
    tool = server._tool_manager.get_tool("mx_financial_data")
    payload = json.loads(tool.fn(query="再来一个"))
    assert payload["ok"] is False
    assert "额度" in payload["error"]["message"]


def test_skill_module_loading_uses_api_key(monkeypatch, data_dir) -> None:
    """加载器要把 settings 里的 key 传给技能客户端。"""

    script = _skills_dir(data_dir) / "mx-data" / "mx_data.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "class MXData:\n    def __init__(self, api_key=''):\n        self.api_key = api_key\n",
        encoding="utf-8",
    )

    from finagent.settings import get_settings, reset_settings_cache

    monkeypatch.setenv("MX_APIKEY", "mkt_from_env")
    monkeypatch.setenv("FINAGENT_MX_SKILLS_DIR", str(_skills_dir(data_dir)))
    reset_settings_cache()

    client = mx._load_client("data", get_settings())
    assert isinstance(client, object)
    assert client.api_key == "mkt_from_env"
    reset_settings_cache()


def test_module_import_is_isolated(monkeypatch, data_dir) -> None:
    """加载技能不应污染 sys.modules 里的公共名字。"""

    import sys

    script = _skills_dir(data_dir) / "mx-search" / "mx_search.py"
    script.parent.mkdir(parents=True)
    script.write_text("class MXSearch:\n    pass\n", encoding="utf-8")

    from finagent.settings import get_settings, reset_settings_cache

    monkeypatch.setenv("FINAGENT_MX_SKILLS_DIR", str(_skills_dir(data_dir)))
    monkeypatch.setenv("MX_APIKEY", "k")
    reset_settings_cache()
    mx._load_client("search", get_settings())
    assert "mx_search" not in sys.modules
    reset_settings_cache()
