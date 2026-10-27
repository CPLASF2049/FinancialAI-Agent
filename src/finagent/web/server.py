"""Web 聊天服务。

用 Starlette + uvicorn 起一个本地 HTTP 服务，浏览器打开就是一个聊天窗口：
左侧会话列表、右侧对话流，Agent 每走一步都实时可见。

接口：

``GET  /``                        界面
``GET  /api/info``                数据源、模型、工具数、专家清单
``GET  /api/threads``             会话列表
``POST /api/threads``             新建会话
``GET  /api/threads/{id}``        某个会话的全部消息
``DELETE /api/threads/{id}``      删除会话
``POST /api/chat``                SSE 流式对话

为什么用 SSE 而不是 WebSocket：单向推送足够，浏览器原生 ``EventSource`` 支持自动重连，
服务端就是普通的 ``StreamingResponse``，不需要额外的依赖或握手逻辑。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import threading
import webbrowser
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from finagent.core.store import StoreError
from finagent.graph import FinAgentApp
from finagent.graph.experts import describe_experts
from finagent.graph.state import AGENT_LABELS
from finagent.llm import describe_llm
from finagent.logging import get_logger
from finagent.settings import Settings, get_settings
from finagent.web.threads import ThreadStore

log = get_logger("web")

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: 确定性模式下不会产生 token 事件；"逐字出现"的动画由前端负责（服务端 sleep 会被
#: 网络与代理缓冲合并，不可靠）。


# ----------------------------------------------------------------------
class WebState:
    """整个进程共享的运行时对象。"""

    def __init__(self, settings: Settings, transport: str | None = None) -> None:
        self.settings = settings
        self.transport = transport
        self.app: FinAgentApp | None = None
        self.threads = ThreadStore(settings.resolve_data_dir() / "web_threads.json")
        self._locks: dict[str, asyncio.Lock] = {}
        #: 正在进行的对话数。改配置时要原地重建图，此时不能有对话在跑。
        self.active_streams = 0
        self._reload_lock = asyncio.Lock()

    async def start(self) -> None:
        self.app = FinAgentApp(self.settings, transport=self.transport)
        await self.app.open()
        log.info("Web 服务已就绪：%d 个 MCP 工具", len(self.app.toolkit.tools))

    async def stop(self) -> None:
        if self.app is not None:
            await self.app.close()
            self.app = None

    def reconfigure(self, settings: Settings) -> None:
        """换一份 LLM 配置并让它立刻生效。

        只替换模型槽，**不重建图**：MCP 会话是在 lifespan 任务里打开的，从请求任务里拆掉
        会触发 anyio 的 cancel scope 守卫（"Attempted to exit cancel scope in a different
        task"）。数据源之类的非 LLM 配置仍需重启进程。
        """

        self.settings = settings
        if self.app is not None:
            self.app.set_llm(settings)

    def lock_for(self, thread_id: str) -> asyncio.Lock:
        if thread_id not in self._locks:
            self._locks[thread_id] = asyncio.Lock()
        return self._locks[thread_id]


def state_of(request: Request) -> WebState:
    return request.app.state.web


# ----------------------------------------------------------------------
def _sse(event: str, payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {body}\n\n".encode()


# ----------------------------------------------------------------------
def asset_version() -> str:
    """按静态文件的修改时间 + 大小算一个短哈希。

    它会被注入到 ``index.html`` 的 ``?v=`` 参数里：改完前端，URL 就变了，浏览器必须重新
    下载。这是解决「改了代码但页面还是旧的」最省事的办法——比让用户手动清缓存可靠。
    """

    parts: list[str] = []
    for name in ("app.js", "style.css", "index.html"):
        path = STATIC_DIR / name
        try:
            stat = path.stat()
            parts.append(f"{name}:{stat.st_mtime_ns}:{stat.st_size}")
        except OSError:  # pragma: no cover - 文件缺失时退化为名字
            parts.append(name)
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]


async def index(request: Request) -> Response:
    page = STATIC_DIR / "index.html"
    if not page.exists():  # pragma: no cover - 打包异常时给出可读信息
        return HTMLResponse("<h1>界面文件缺失</h1><p>请重新安装本项目。</p>", status_code=500)

    try:
        html = page.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover
        return FileResponse(page, media_type="text/html; charset=utf-8")

    body = html.replace("__ASSET_VERSION__", asset_version())
    return HTMLResponse(
        body,
        # 页面本身必须每次回源校验，否则浏览器会一直用旧的 HTML（也就一直引用旧资源）
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


async def api_info(request: Request) -> Response:
    web = state_of(request)
    settings = web.settings
    llm = describe_llm(settings)
    toolkit = web.app.toolkit if web.app else None

    return JSONResponse(
        {
            "version": __import__("finagent").__version__,
            "provider": settings.provider,
            "llm": llm,
            "transport": toolkit.transport if toolkit else web.transport or "inprocess",
            "tool_count": len(toolkit.tools) if toolkit else 0,
            "experts": [
                {
                    "agent": item["agent"],
                    "label": item["label"],
                    "tool_count": item["tool_count"],
                }
                for item in describe_experts()
            ],
            "max_hops": settings.max_agent_hops,
        }
    )


async def api_llm(request: Request) -> Response:
    """读取 / 保存大模型配置。

    ``GET``  返回当前状态、可选厂商与写入位置（Key 只露头尾）。
    ``POST`` 写入 ``.env``，热重建图，并做一次连通性自检。
    """

    from finagent.web import llm_setup

    web = state_of(request)

    if request.method == "GET":
        return JSONResponse(llm_setup.current_state(web.settings))

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"}, status_code=400)

    if web.active_streams > 0:
        return JSONResponse(
            {"ok": False, "message": "还有对话正在进行，等它结束再保存"},
            status_code=409,
        )

    provider = str(body.get("provider") or "").strip()
    if not provider:
        return JSONResponse({"ok": False, "message": "请选择厂商"}, status_code=400)

    try:
        settings = llm_setup.apply(
            provider=provider,
            api_key=body.get("api_key"),
            model=body.get("model"),
            base_url=body.get("base_url"),
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)

    # 热替换模型，让新配置立刻生效；失败也不回滚文件，用户可以在界面上继续改
    reload_error = ""
    try:
        web.reconfigure(settings)
    except Exception as exc:  # noqa: BLE001
        log.exception("切换大模型失败")
        reload_error = str(exc)

    result: dict[str, Any] = {
        "ok": True,
        "message": "配置已保存" + ("，但模型切换失败：" + reload_error if reload_error else ""),
        "state": llm_setup.current_state(web.settings),
        "test": None,
    }

    if body.get("test", True):
        result["test"] = await llm_setup.test_connection(web.settings)

    return JSONResponse(result)


async def api_threads(request: Request) -> Response:
    web = state_of(request)
    try:
        if request.method == "POST":
            created = web.threads.create()
            return JSONResponse(created, status_code=201)
        return JSONResponse({"threads": web.threads.list_threads()})
    except StoreError as exc:
        return _store_error(exc)


async def api_thread(request: Request) -> Response:
    web = state_of(request)
    thread_id = request.path_params["thread_id"]

    try:
        if request.method == "DELETE":
            removed = web.threads.delete(thread_id)
            return JSONResponse({"deleted": removed}, status_code=200 if removed else 404)

        record = web.threads.get(thread_id)
    except StoreError as exc:
        return _store_error(exc)

    if record is None:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    return JSONResponse(
        {
            "id": record["id"],
            "title": record.get("title"),
            "messages": record.get("messages") or [],
        }
    )


def _store_error(exc: Exception) -> Response:
    """把存储层异常翻成一条能给用户看的错误，而不是一页 traceback。"""

    log.error("会话存储不可用：%s", exc)
    return JSONResponse(
        {
            "error": "无法读写会话记录",
            "detail": str(exc),
            "hint": "检查数据目录的权限，或删掉残留的 *.lock 文件后重试",
        },
        status_code=503,
    )


# ----------------------------------------------------------------------
async def api_chat(request: Request) -> Response:
    web = state_of(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "请求体不是合法 JSON"}, status_code=400)

    message = str(body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "消息不能为空"}, status_code=400)

    thread_id = str(body.get("thread_id") or "").strip()
    if not thread_id:
        thread_id = web.threads.create()["id"]
    user_id = str(body.get("user_id") or "web").strip() or "web"

    if web.app is None:
        return JSONResponse({"error": "服务尚未就绪"}, status_code=503)

    web.threads.append(thread_id, {"role": "user", "text": message})

    return StreamingResponse(
        _stream_turn(web, thread_id, user_id, message),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 关掉代理缓冲，否则流式会被攒着
            "Connection": "keep-alive",
        },
    )


async def _stream_turn(
    web: WebState, thread_id: str, user_id: str, message: str
) -> AsyncIterator[bytes]:
    """把一轮对话翻译成 SSE 事件流。"""

    assert web.app is not None
    app = web.app

    answer = ""
    agents: list[str] = []
    tools: list[dict[str, Any]] = []
    errors: list[str] = []
    decisions: list[dict[str, Any]] = []
    routed: set[str] = set()
    seen_token = False

    yield _sse("start", {"thread_id": thread_id, "message": message})

    web.active_streams += 1
    try:
        async with web.lock_for(thread_id):
            async for event in app.astream(message, user_id=user_id, thread_id=thread_id):
                kind = event.get("type")

                if kind == "route":
                    agent = str(event.get("agent") or "")
                    label = AGENT_LABELS.get(agent, agent)
                    # supervisor 会从自定义事件和 updates 各发一次同一路由，去重后再推
                    if agent in routed:
                        continue
                    routed.add(agent)
                    yield _sse(
                        "route",
                        {"agent": agent, "label": label, "reason": event.get("reason") or ""},
                    )
                    decisions.append(
                        {
                            "step": "route",
                            "agent": agent,
                            "label": label,
                            "reason": event.get("reason") or "",
                        }
                    )
                elif kind == "agent":
                    agent = str(event.get("agent") or "")
                    yield _sse("agent", {"agent": agent, "label": AGENT_LABELS.get(agent, agent)})
                elif kind == "memory":
                    yield _sse("memory", {"updates": event.get("updates") or {}})
                elif kind == "event":
                    name = event.get("event")
                    if name == "route":
                        # 路由一律在这里统一处理：supervisor 会从自定义事件和 updates 各发
                        # 一次，只在第一次到达时推给前端。
                        agent = str(event.get("agent") or "")
                        if agent in routed:
                            continue
                        routed.add(agent)
                        label = AGENT_LABELS.get(agent, agent)
                        yield _sse(
                            "route",
                            {
                                "agent": agent,
                                "label": label,
                                "reason": event.get("reason") or "",
                            },
                        )
                        decisions.append(
                            {
                                "step": "route",
                                "agent": agent,
                                "label": label,
                                "reason": event.get("reason") or "",
                            }
                        )
                        continue
                    yield _translate_agent_event(event, tools, seen_token)
                    if name == "critic":
                        decisions.append(
                            {
                                "step": "critic",
                                "agent": event.get("agent"),
                                "label": event.get("label"),
                                "verdict": event.get("verdict"),
                                "issues": event.get("issues") or [],
                            }
                        )
                    elif name == "risk":
                        decisions.append(
                            {
                                "step": "risk",
                                "label": AGENT_LABELS["risk_guard"],
                                "flags": event.get("flags") or [],
                                "actions": event.get("actions") or [],
                            }
                        )
                    if name == "token":
                        seen_token = True
                elif kind == "final":
                    result = event["result"]
                    answer = result.answer or ""
                    agents = list(result.agents or [])
                    errors = [error for error in (result.errors or []) if error]
                    if not tools:
                        tools = [
                            {
                                "tool": item.get("tool"),
                                "args": item.get("args") or {},
                                "ok": item.get("ok", True),
                                "preview": (item.get("preview") or "")[:400],
                            }
                            for item in (result.trace or [])
                        ]
    except asyncio.CancelledError:  # 浏览器关标签页
        log.info("客户端断开，停止本轮")
        raise
    except Exception as exc:  # noqa: BLE001 - 任何异常都要告诉前端
        from finagent.web.llm_setup import friendly_error

        log.exception("对话失败")
        yield _sse("error", {"message": friendly_error(str(exc))})
        return
    finally:
        web.active_streams = max(0, web.active_streams - 1)

    web.threads.append(
        thread_id,
        {
            "role": "assistant",
            "text": answer,
            "agents": agents,
            "tools": tools,
            "errors": errors,
            "decisions": decisions,
        },
    )

    yield _sse(
        "done",
        {
            "answer": answer,
            "agents": agents,
            "labels": [AGENT_LABELS.get(agent, agent) for agent in agents],
            "tool_calls": len(tools),
            "errors": errors,
            "decisions": decisions,
        },
    )


def _translate_agent_event(
    event: dict[str, Any], tools: list[dict[str, Any]], seen_token: bool
) -> bytes:
    name = event.get("event")
    if name == "tool_call":
        return _sse("tool", {"tool": event.get("tool"), "args": event.get("args") or {}})
    if name == "tool_result":
        record = {
            "tool": event.get("tool"),
            "ok": bool(event.get("ok")),
            "preview": str(event.get("preview") or "")[:400],
        }
        tools.append(record)
        return _sse("tool_result", record)
    if name == "agent_end":
        return _sse(
            "agent_end",
            {"agent": event.get("agent"), "tool_calls": event.get("tool_calls", 0)},
        )
    if name == "token":
        return _sse("token", {"text": event.get("text") or "", "agent": event.get("agent")})
    if name == "agent_start":
        return _sse("agent", {"agent": event.get("agent"), "label": ""})
    if name == "critic":
        return _sse(
            "critic",
            {
                "agent": event.get("agent"),
                "label": event.get("label") or AGENT_LABELS.get(str(event.get("agent")), ""),
                "verdict": event.get("verdict") or "pass",
                "issues": event.get("issues") or [],
            },
        )
    if name == "risk":
        return _sse(
            "risk",
            {
                "flags": event.get("flags") or [],
                "actions": event.get("actions") or [],
                "blocked": bool(event.get("blocked")),
            },
        )
    if name == "route":
        return _sse(
            "route",
            {
                "agent": event.get("agent"),
                "label": event.get("label") or AGENT_LABELS.get(str(event.get("agent")), ""),
                "reason": event.get("reason") or "",
            },
        )
    return _sse("log", {"event": name, "seen_token": seen_token})


# ----------------------------------------------------------------------
class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    """让静态资源每次回源校验。

    ``StaticFiles`` 只发 ``ETag``/``Last-Modified``，不发 ``Cache-Control``，浏览器于是按
    启发式规则自行决定缓存多久——改完前端刷新页面看到的还是旧界面。加上 ``no-cache``
    后每次都带 ``If-None-Match`` 校验，没变就是 304（几乎零开销），变了立刻生效。
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


def build_app(settings: Settings | None = None, *, transport: str | None = None) -> Starlette:
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        web = WebState(resolved, transport=transport)
        await web.start()
        app.state.web = web
        try:
            yield
        finally:
            await web.stop()

    routes = [
        Route("/", index),
        Route("/api/info", api_info),
        Route("/api/llm", api_llm, methods=["GET", "POST"]),
        Route("/api/threads", api_threads, methods=["GET", "POST"]),
        Route("/api/threads/{thread_id}", api_thread, methods=["GET", "DELETE"]),
        Route("/api/chat", api_chat, methods=["POST"]),
        Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(NoCacheStaticMiddleware)
    return app


# ----------------------------------------------------------------------
def pick_port(host: str, preferred: int, *, tries: int = 20) -> int:
    """端口被占用时顺延，避免"启动失败"这种无谓的挫败感。"""

    for offset in range(tries):
        candidate = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            # 刻意不设 SO_REUSEADDR：在 Windows 上它允许绑定到已被占用的端口，
            # 会让这个探测永远"看起来"成功。
            try:
                probe.bind((host, candidate))
            except OSError:
                continue
            return candidate
    return preferred


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    open_browser: bool = True,
    transport: str | None = None,
    settings: Settings | None = None,
) -> int:
    """启动 Web 服务（阻塞直到 Ctrl+C）。"""

    import uvicorn

    resolved = settings or get_settings()
    actual = pick_port(host, port)
    url = f"http://{host}:{actual}"

    print()
    print(f"  Fin-Agent 界面已启动：{url}")
    print(f"  数据源：{resolved.provider}   LLM：{describe_llm(resolved)['mode']}")
    print("  按 Ctrl+C 停止")
    print()

    if open_browser:
        threading.Timer(1.2, lambda: _open(url)).start()

    uvicorn.run(
        build_app(resolved, transport=transport),
        host=host,
        port=actual,
        log_level="warning",
    )
    return 0


def _open(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001
        log.warning("无法自动打开浏览器：%s", exc)
