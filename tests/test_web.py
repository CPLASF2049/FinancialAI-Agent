"""Web 界面：端口选择、会话存储、HTTP 接口与 SSE 流。

用 Starlette 的 ``TestClient`` 驱动完整应用（含 lifespan），所以这些用例覆盖的是真实的
启动流程：MCP 工具加载、会话存档、事件流 —— 只是把浏览器换成了测试客户端。
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from finagent.llm import PROVIDER_PRESETS
from finagent.settings import get_settings
from finagent.web.server import asset_version, build_app, pick_port
from finagent.web.threads import ThreadStore


# ----------------------------------------------------------------------
def test_pick_port_skips_a_busy_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        taken = busy.getsockname()[1]
        busy.listen(1)

        picked = pick_port("127.0.0.1", taken)

    assert picked != taken, "端口被占用时应当顺延"


def test_thread_store_roundtrip(data_dir: Path) -> None:
    store = ThreadStore(data_dir / "threads.json")

    created = store.create()
    assert store.list_threads()[0]["id"] == created["id"]

    store.append(created["id"], {"role": "user", "text": "茅台现在多少钱"})
    store.append(created["id"], {"role": "assistant", "text": "…", "agents": ["analyst"]})

    messages = store.messages(created["id"])
    assert [m["role"] for m in messages] == ["user", "assistant"]

    # 第一条用户消息自动成为标题
    assert store.get(created["id"])["title"].startswith("茅台现在多少钱")

    assert store.delete(created["id"]) is True
    assert store.get(created["id"]) is None
    assert store.delete(created["id"]) is False


def test_thread_store_caps_history(data_dir: Path) -> None:
    store = ThreadStore(data_dir / "threads.json")
    thread_id = store.create()["id"]
    for index in range(230):
        store.append(thread_id, {"role": "user", "text": f"第 {index} 条"})

    assert len(store.messages(thread_id)) <= 200


# ----------------------------------------------------------------------
@pytest.fixture
def client(offline_env) -> TestClient:
    with TestClient(build_app(get_settings())) as test_client:
        yield test_client


def test_index_serves_the_ui(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Fin-Agent" in response.text
    assert "/static/app.js" in response.text
    # 界面上要有「接入大模型」入口
    assert "openLlm" in response.text
    assert "接入大模型" in response.text


def test_static_assets_are_served(client: TestClient) -> None:
    for path in ("/static/app.js", "/static/style.css"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.text


def test_assets_are_versioned_and_not_cached(client: TestClient) -> None:
    """改了前端必须能立刻看到，不能被浏览器缓存挡住。

    做法：页面 URL 带 ``?v=<静态文件指纹>``，并且 HTML 与静态资源都回源校验。
    """

    response = client.get("/")
    version = asset_version()
    assert f"/static/app.js?v={version}" in response.text
    assert f"/static/style.css?v={version}" in response.text
    assert "__ASSET_VERSION__" not in response.text
    assert "no-cache" in response.headers["cache-control"]

    asset = client.get("/static/app.js")
    assert "no-cache" in asset.headers["cache-control"]


def test_asset_version_changes_with_file_content(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """指纹来自文件内容（mtime + 大小），改文件就会变。"""

    import finagent.web.server as server

    fake_dir = data_dir / "static"
    fake_dir.mkdir()
    for name in ("app.js", "style.css", "index.html"):
        (fake_dir / name).write_text("v1", encoding="utf-8")

    monkeypatch.setattr(server, "STATIC_DIR", fake_dir)
    first = server.asset_version()

    (fake_dir / "app.js").write_text("v2-changed", encoding="utf-8")
    assert server.asset_version() != first


def test_info_endpoint(client: TestClient) -> None:
    data = client.get("/api/info").json()

    assert data["provider"] == "offline"
    assert data["tool_count"] == 41
    assert data["llm"]["enabled"] is False
    assert {expert["agent"] for expert in data["experts"]} == {
        "stock_picker",
        "analyst",
        "backtest",
        "portfolio",
        "alert",
    }


def test_thread_crud_over_http(client: TestClient) -> None:
    created = client.post("/api/threads").json()
    assert created["id"]

    listed = client.get("/api/threads").json()["threads"]
    assert any(item["id"] == created["id"] for item in listed)

    detail = client.get(f"/api/threads/{created['id']}").json()
    assert detail["messages"] == []

    assert client.delete(f"/api/threads/{created['id']}").status_code == 200
    assert client.get(f"/api/threads/{created['id']}").status_code == 404


def test_chat_streams_events_and_persists_the_turn(client: TestClient) -> None:
    thread_id = client.post("/api/threads").json()["id"]

    events: list[tuple[str, dict]] = []
    with client.stream(
        "POST",
        "/api/chat",
        json={"message": "茅台现在多少钱", "thread_id": thread_id},
    ) as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        name = ""
        for line in response.iter_lines():
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                events.append((name, json.loads(line[5:].strip())))

    kinds = [name for name, _ in events]
    assert "start" in kinds
    assert "route" in kinds
    assert "tool" in kinds and "tool_result" in kinds
    assert "done" in kinds
    assert "error" not in kinds

    # 路由必须排在工具调用之前，否则界面上会「先看到工具、后看到交给谁」
    assert kinds.index("route") < kinds.index("tool")

    done = next(payload for name, payload in events if name == "done")
    assert done["answer"]
    assert done["agents"] == ["analyst"]
    assert done["tool_calls"] >= 1

    # 落盘：一轮对话产生 user + assistant 两条
    messages = client.get(f"/api/threads/{thread_id}").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["agents"] == ["analyst"]
    assert messages[1]["tools"]

    # 会话标题来自第一条用户消息
    listed = client.get("/api/threads").json()["threads"]
    title = next(item["title"] for item in listed if item["id"] == thread_id)
    assert "茅台" in title


def test_chat_rejects_empty_message(client: TestClient) -> None:
    response = client.post("/api/chat", json={"message": "   "})
    assert response.status_code == 400


def test_chat_creates_a_thread_when_missing(client: TestClient) -> None:
    events: list[tuple[str, dict]] = []
    with client.stream("POST", "/api/chat", json={"message": "上证指数怎么样"}) as response:
        name = ""
        for line in response.iter_lines():
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                events.append((name, json.loads(line[5:].strip())))

    start = next(payload for name, payload in events if name == "start")
    assert start["thread_id"]
    assert any(name == "done" for name, _ in events)


# ----------------------------------------------------------------------
# 在界面里接入大模型
# ----------------------------------------------------------------------
@pytest.fixture
def llm_env(data_dir: Path, monkeypatch: pytest.MonkeyPatch, offline_env) -> Path:
    """把配置写到临时 .env，避免污染仓库；用完清理进程环境变量。"""

    target = data_dir / ".env"
    monkeypatch.setenv("FINAGENT_ENV_FILE", str(target))
    try:
        yield target
    finally:
        for key in (
            "FINAGENT_LLM_PROVIDER",
            "FINAGENT_LLM_API_KEY",
            "FINAGENT_LLM_MODEL",
            "FINAGENT_LLM_BASE_URL",
        ):
            monkeypatch.delenv(key, raising=False)
        from finagent.settings import reset_settings_cache

        reset_settings_cache()


def test_llm_state_endpoint(client: TestClient, llm_env: Path) -> None:
    data = client.get("/api/llm").json()

    assert data["provider"] == "none"
    assert data["enabled"] is False
    assert data["has_key"] is False
    assert data["env_file"] == str(llm_env)
    ids = {preset["id"] for preset in data["presets"]}
    assert ids == {
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
    }
    assert next(p for p in data["presets"] if p["id"] == "deepseek")["base_url"]

    # 讯飞星火：OpenAI 兼容地址 + APIPassword 当作 Key
    spark = next(p for p in data["presets"] if p["id"] == "spark")
    assert spark["base_url"] == "https://spark-api-open.xf-yun.com/v1"
    assert spark["model"] == "generalv3.5"
    assert spark["needs_key"] is True
    assert "APIPassword" in spark["hint"]

    # 讯飞星辰 MaaS：另一套地址，Key 是 ak- 开头
    maas = next(p for p in data["presets"] if p["id"] == "maas")
    assert maas["base_url"] == "https://maas-api.cn-huabei-1.xf-yun.com/v2"
    assert maas["model"] == "spark-x2.6-4b"
    assert "ak-" in maas["hint"]
    # 要明确和星火的 APIPassword 区分开，别让人拿错 Key
    assert "不是一回事" in maas["hint"]


def test_llm_save_spark(client: TestClient, llm_env: Path) -> None:
    """星火按 OpenAI 兼容协议接入：地址与模型都用预设，Key 是 APIPassword。"""

    response = client.post(
        "/api/llm", json={"provider": "spark", "api_key": "spark-pass-123", "test": False}
    )
    assert response.status_code == 200
    state = response.json()["state"]
    assert state["enabled"] is True
    assert state["provider"] == "spark"

    written = llm_env.read_text(encoding="utf-8")
    assert "FINAGENT_LLM_PROVIDER=spark" in written
    assert "FINAGENT_LLM_BASE_URL=https://spark-api-open.xf-yun.com/v1" in written
    assert "FINAGENT_LLM_MODEL=generalv3.5" in written


def test_llm_custom_requires_base_url_and_model(client: TestClient, llm_env: Path) -> None:
    """「自定义」没有预设垫底，必须自己填全，否则会悄悄连到 api.openai.com。"""

    missing_url = client.post("/api/llm", json={"provider": "custom", "test": False})
    assert missing_url.status_code == 400
    assert "接口地址" in missing_url.json()["message"]

    bad_url = client.post(
        "/api/llm",
        json={"provider": "custom", "base_url": "api.example.com/v1", "model": "m", "test": False},
    )
    assert bad_url.status_code == 400
    assert "http" in bad_url.json()["message"]

    missing_model = client.post(
        "/api/llm",
        json={"provider": "custom", "base_url": "https://api.example.com/v1", "test": False},
    )
    assert missing_model.status_code == 400
    assert "模型名" in missing_model.json()["message"]

    ok = client.post(
        "/api/llm",
        json={
            "provider": "custom",
            "base_url": "https://api.example.com/v1",
            "model": "my-model",
            "test": False,
        },
    )
    assert ok.status_code == 200
    assert ok.json()["state"]["enabled"] is True
    written = llm_env.read_text(encoding="utf-8")
    assert "FINAGENT_LLM_BASE_URL=https://api.example.com/v1" in written


def test_llm_save_rejects_unknown_provider(client: TestClient, llm_env: Path) -> None:
    response = client.post("/api/llm", json={"provider": "nope", "api_key": "x"})
    assert response.status_code == 400
    assert "不支持的厂商" in response.json()["message"]


def test_llm_save_requires_a_key(client: TestClient, llm_env: Path) -> None:
    response = client.post("/api/llm", json={"provider": "deepseek"})
    assert response.status_code == 400
    assert "API Key" in response.json()["message"]


def test_llm_save_writes_env_and_hot_swaps(client: TestClient, llm_env: Path) -> None:
    """保存后：文件落盘、配置生效、图里的模型被换掉（不重启）。"""

    response = client.post(
        "/api/llm",
        json={"provider": "deepseek", "api_key": "sk-test-abcdef", "test": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["state"]["enabled"] is True
    assert body["state"]["provider"] == "deepseek"
    assert body["test"] is None  # test=False 时不发网络请求

    written = llm_env.read_text(encoding="utf-8")
    assert "FINAGENT_LLM_PROVIDER=deepseek" in written
    assert "FINAGENT_LLM_API_KEY=sk-test-abcdef" in written
    assert "FINAGENT_LLM_MODEL=deepseek-chat" in written

    # 热生效：/api/info 与图里的模型槽都应反映新配置
    assert client.get("/api/info").json()["llm"]["enabled"] is True
    web = client.app.state.web
    assert web.app is not None
    assert web.app.model is not None

    # Key 在对外状态里只露头尾
    shown = client.get("/api/llm").json()
    assert shown["has_key"] is True
    assert "abcdef" not in shown["key_masked"]
    assert shown["key_masked"].startswith("sk-tes")


def test_llm_save_merges_existing_env_file(client: TestClient, llm_env: Path) -> None:
    """已有 .env 里的其他配置项必须原样保留。"""

    llm_env.write_text(
        "FINAGENT_PROVIDER=tushare\nFINAGENT_TUSHARE_TOKEN=keep-me\n", encoding="utf-8"
    )

    client.post("/api/llm", json={"provider": "deepseek", "api_key": "sk-x", "test": False})

    written = llm_env.read_text(encoding="utf-8")
    assert "FINAGENT_PROVIDER=tushare" in written
    assert "FINAGENT_TUSHARE_TOKEN=keep-me" in written
    assert "FINAGENT_LLM_PROVIDER=deepseek" in written


def test_llm_save_local_provider_needs_no_key(client: TestClient, llm_env: Path) -> None:
    response = client.post("/api/llm", json={"provider": "local", "test": False})
    assert response.status_code == 200
    assert response.json()["state"]["enabled"] is True

    written = llm_env.read_text(encoding="utf-8")
    assert "FINAGENT_LLM_PROVIDER=local" in written
    assert f"FINAGENT_LLM_BASE_URL={PROVIDER_PRESETS['local']['base_url']}" in written


def test_llm_save_is_rejected_while_a_chat_is_running(client: TestClient, llm_env: Path) -> None:
    client.app.state.web.active_streams = 1
    try:
        response = client.post(
            "/api/llm", json={"provider": "deepseek", "api_key": "sk-x", "test": False}
        )
    finally:
        client.app.state.web.active_streams = 0

    assert response.status_code == 409
    assert "对话正在进行" in response.json()["message"]


def test_friendly_error_and_masking() -> None:
    from finagent.web.llm_setup import friendly_error, mask_key

    message = friendly_error("Error code: 401 - {'error': {'message': 'Authentication Fails'}}")
    assert "API Key 无效" in message
    assert "401" in message
    assert friendly_error("Connection error: timed out").startswith("连接失败")
    assert mask_key("sk-1234567890abcdef") == "sk-123…cdef"
    assert mask_key("") == ""
