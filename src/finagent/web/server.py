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

from finagent.core.phase import resume_hint
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
        #: 后台执行与断流重连。断开投递不等于停止执行。
        from finagent.web.streams import StreamHub

        self.hub = StreamHub()
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


async def api_steer(request: Request) -> Response:
    """往正在跑的 turn 里塞一条指令。

    指令在**下一个 step 边界**生效，不会打断正在执行的工具——
    这样不会留下半个副作用。
    """

    from finagent.core.steering import QUEUE

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)

    task_id = str(payload.get("task_id") or "")
    directive = str(payload.get("directive") or "")
    if not task_id or not directive:
        return JSONResponse({"ok": False, "error": "需要 task_id 与 directive"}, status_code=400)

    try:
        item = QUEUE.push(
            task_id, directive, text=payload.get("text", ""), target=payload.get("target", "")
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    return JSONResponse({"ok": True, "queued": item.to_dict()})


async def api_trace(request: Request) -> Response:
    """按 task_id 还原一次执行链路。

    返回事件时间线 + 三个开箱查询（最慢节点 / 失败工具 / 返工原因），
    这样排查「为什么慢、为什么返工」不用去翻日志。
    """

    web = state_of(request)
    task_id = (request.path_params or {}).get("task_id") or ""
    store = getattr(web.app, "events", None) if web.app is not None else None
    if store is None:
        return JSONResponse({"ok": False, "error": "事件表未启用"}, status_code=503)

    events = store.query(task_id)
    if not events:
        return JSONResponse({"ok": False, "error": f"找不到 task {task_id}"}, status_code=404)

    return JSONResponse(
        {
            "ok": True,
            "task_id": task_id,
            "timeline": [event.to_dict() for event in events],
            "slowest_steps": store.slowest_steps(task_id),
            "failed_tools": store.failed_tools(task_id),
            "revisions": store.revisions(task_id),
            "resume": resume_hint(events),
        }
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
            # 排查「改了没生效」：这里给出本进程加载的代码版本与启动时刻。
            # 前端/CLI 都能据此确认跑的是不是最新代码。
            "build": _build_stamp(),
        }
    )


def _build_stamp() -> dict[str, str]:
    """本进程加载的代码版本与启动时间。"""

    import os
    import subprocess
    import time

    sha = os.environ.get("FINAGENT_BUILD_SHA", "")
    if not sha:
        try:
            sha = (
                subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                ).stdout.strip()
                or "unknown"
            )
        except Exception:  # noqa: BLE001 - 拿不到版本不是错误
            sha = "unknown"

    from finagent.graph.builder import APP_STARTED_AT

    return {
        "sha": sha,
        "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(APP_STARTED_AT)),
    }


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
            # ?if_empty=1：只有「没有任何消息」时才删。
            # 这是「点了新建对话但没说话就切走」的清理路径——有内容的一律不动。
            only_empty = str(request.query_params.get("if_empty") or "") in ("1", "true", "yes")
            if only_empty:
                removed = web.threads.delete_if_empty(thread_id)
            else:
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

    import time as _time

    run_id = f"run_{int(_time.time() * 1000)}_{thread_id[-6:]}"

    async def producer(run: object) -> None:
        # 执行与投递解耦：客户端断开只影响下面的订阅，不影响这里继续跑。
        async for chunk in _stream_turn(web, thread_id, user_id, message):
            web.hub.emit(run, "sse", {"raw": chunk.decode("utf-8", "replace")})  # type: ignore[arg-type]

    web.hub.start(run_id, producer)

    return StreamingResponse(
        _tail_run(web, run_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 关掉代理缓冲，否则流式会被攒着
            "Connection": "keep-alive",
            # 客户端可用它断线重连：GET /api/stream/{run_id}?after=<seq>
            "X-Run-Id": run_id,
        },
    )


async def _tail_run(web: WebState, run_id: str, after: int = 0):
    """订阅后台执行的事件。

    客户端断开时这个生成器会被取消，但 ``producer`` 是独立的 asyncio 任务，
    会继续跑完——这正是「切走就白跑」的修复点。
    """

    from finagent.web.streams import StreamHub

    hub: StreamHub = web.hub
    try:
        async for item in hub.subscribe(run_id, after=after):
            if item["event"] == "sse":
                yield item["data"]["raw"].encode("utf-8")
            elif item["event"] == "ping":
                yield b": ping\n\n"
    except Exception as exc:  # noqa: BLE001 - 投递失败不代表执行失败
        log.warning("SSE 投递中断（%s），后台执行继续", exc)


async def api_cancel_run(request: Request) -> Response:
    """显式取消一次后台执行。

    断开投递**不等于**取消执行——想真停就用这个（或 Steering 的 cancel 指令）。
    """

    web = state_of(request)
    run_id = (request.path_params or {}).get("run_id") or ""
    stopped = web.hub.cancel(run_id)
    return JSONResponse({"ok": stopped, "run_id": run_id, "status": web.hub.status(run_id)})


async def api_run_status(request: Request) -> Response:
    """查询后台执行进度（不订阅、不重连）。"""

    web = state_of(request)
    run_id = (request.path_params or {}).get("run_id") or ""
    return JSONResponse(web.hub.status(run_id))


async def api_stream(request: Request) -> Response:
    """断线重连：从 ``after`` 续播，不重跑任务。"""

    web = state_of(request)
    run_id = (request.path_params or {}).get("run_id") or ""
    try:
        after = int(request.query_params.get("after") or 0)
    except ValueError:
        after = 0

    status = web.hub.status(run_id)
    if not status.get("exists"):
        return JSONResponse({"ok": False, "error": f"未知 run {run_id}"}, status_code=404)

    return StreamingResponse(
        _tail_run(web, run_id, after=after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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
    _stop_reason = ""
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
                    _stop_reason = str(getattr(result, "stop_reason", "") or "")
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
            "stop_reason": _stop_reason,
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
                # 第几稿：前端据此清空上一稿的流式文本，避免新旧两版拼在一起
                "revision": int(event.get("revision") or 0),
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
        Route("/api/steer", api_steer, methods=["POST"]),
        Route("/api/stream/{run_id}", api_stream),
        Route("/api/runs/{run_id}", api_run_status),
        Route("/api/runs/{run_id}/cancel", api_cancel_run, methods=["POST"]),
        Route("/api/trace/{task_id}", api_trace),
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
