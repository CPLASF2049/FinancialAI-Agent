"""东方财富妙想 Skills 的桥接层。

背景
----
官方把「妙想」这组能力做成了独立的脚本包（`~/skills/mx-*`），每个脚本自带一个小的
客户端类，直连 `mkapi2.dfcfs.com` 并用 `apikey` 头鉴权。本项目要把它们接进 MCP 工具面，
所以这里做三件事：

1. **加载**：按路径 import 技能脚本，取出它的客户端类（不执行 `main()`，因此不会写 Excel）；
2. **配额**：官方接口**按天限次**。这里用「当日计数 + 当日缓存」把额度花在真正新的查询上——
   同样的问句重复调用直接命中缓存，不消耗次数；
3. **归一化**：把各技能不同的返回结构压成同一形状（``text`` + ``tables``），
   让 Agent 不必区分是哪个技能给的。

配额用尽、未装技能、没配 Key，都会抛带 hint 的领域异常，最终变成 ``{"ok": false}`` 信封，
而不是让模型对着一堆 500 猜。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

from finagent.errors import ConfigurationError, ProviderUnavailable
from finagent.logging import get_logger
from finagent.settings import Settings, get_settings

log = get_logger("core.mx")

#: 技能标识 -> (目录名, 脚本名, 客户端类名)
SKILLS: dict[str, tuple[str, str, str]] = {
    "data": ("mx-data", "mx_data.py", "MXData"),
    "search": ("mx-search", "mx_search.py", "MXSearch"),
    "screen": ("mx-xuangu", "mx_xuangu.py", "MXSelectStock"),
}

#: 给用户看的技能名
SKILL_LABELS = {"data": "妙想金融数据", "search": "妙想资讯搜索", "screen": "妙想智能选股"}

_LOCK = threading.RLock()

#: 配额文件写不进去时的进程内回退（跨调用仍能计数，重启归零）。
_memory_usage: dict[str, Any] | None = None


# ----------------------------------------------------------------------
# 路径与可用性
# ----------------------------------------------------------------------
def skills_root(settings: Settings | None = None) -> Path:
    resolved = settings or get_settings()
    if resolved.mx_skills_dir:
        return Path(resolved.mx_skills_dir).expanduser()
    return Path.home() / "skills"


def skill_script(kind: str, settings: Settings | None = None) -> Path:
    directory, script, _ = SKILLS[kind]
    return skills_root(settings) / directory / script


def installed(settings: Settings | None = None) -> dict[str, bool]:
    return {kind: skill_script(kind, settings).exists() for kind in SKILLS}


def is_available(settings: Settings | None = None) -> bool:
    """装了技能 + 配了 Key，才算可用。用来决定要不要把 mx 服务器挂进工具面。"""

    resolved = settings or get_settings()
    if not resolved.mx_api_key:
        return False
    return any(installed(resolved).values())


# ----------------------------------------------------------------------
# 配额与缓存
# ----------------------------------------------------------------------
def _usage_path(settings: Settings) -> Path:
    return settings.resolve_data_dir() / "mx_usage.json"


def _read_usage(settings: Settings) -> dict[str, Any]:
    today = date.today().strftime("%Y%m%d")
    if _memory_usage is not None:
        if _memory_usage.get("date") != today:
            return {"date": today, "calls": 0, "cache": {}}
        return json.loads(json.dumps(_memory_usage))

    path = _usage_path(settings)
    if not path.exists():
        return {"date": today, "calls": 0, "cache": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"date": today, "calls": 0, "cache": {}}
    if payload.get("date") != today:
        # 跨天：配额与缓存一起作废
        return {"date": today, "calls": 0, "cache": {}}
    payload.setdefault("calls", 0)
    payload.setdefault("cache", {})
    return payload


def _write_usage(settings: Settings, payload: dict[str, Any]) -> bool:
    """写配额文件；成功返回 True。

    写不进去时**不能静默**：那意味着「当日额度计数失效」，用户会以为还有额度、
    实际可能已经在超支边缘。这里退化成进程内计数并打一次警告。
    """

    global _memory_usage

    path = _usage_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        _memory_usage = None
        return True
    except OSError as exc:  # noqa: BLE001 - 记不上账不该让查询失败
        _memory_usage = payload
        log.warning(
            "妙想配额文件写不进去（%s）：%s。本次会话内改用内存计数，重启会归零。",
            path,
            exc,
        )
        return False


def _usage_writable(settings: Settings | None = None) -> bool:
    resolved = settings or get_settings()
    path = _usage_path(resolved)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return os.access(path.parent, os.W_OK)
    except OSError:
        return False


def quota(settings: Settings | None = None) -> dict[str, Any]:
    """今日用量与剩余额度。"""

    resolved = settings or get_settings()
    with _LOCK:
        payload = _read_usage(resolved)
    used = int(payload["calls"])
    limit = resolved.mx_daily_limit
    return {
        "date": payload["date"],
        "used": used,
        "limit": limit,
        "remaining": max(limit - used, 0),
        "cached_queries": len(payload.get("cache") or {}),
        #: False 说明计数只存在内存里，重启会归零——调用方应当据此提醒用户
        "persisted": _memory_usage is None and _usage_writable(resolved),
    }


def _cache_key(kind: str, query: str) -> str:
    return hashlib.sha1(f"{kind}:{query.strip()}".encode()).hexdigest()[:16]


# ----------------------------------------------------------------------
# 加载技能
# ----------------------------------------------------------------------
def _load_client(kind: str, settings: Settings) -> Any:
    script = skill_script(kind, settings)
    if not script.exists():
        raise ConfigurationError(
            f"未安装{SKILL_LABELS[kind]}技能包。",
            hint=(
                "按官方文档安装妙想 Skills：把 mx-* 压缩包解压到 "
                f"{skills_root(settings)} 下；或用 FINAGENT_MX_SKILLS_DIR 指定目录。"
            ),
        )

    module_name = f"finagent_mx_{kind}"
    spec = importlib.util.spec_from_file_location(module_name, script)
    if spec is None or spec.loader is None:  # pragma: no cover - 极少数加载失败
        raise ConfigurationError(f"无法加载技能脚本：{script}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class_name = SKILLS[kind][2]
    client_class = getattr(module, class_name, None)
    if client_class is None:  # pragma: no cover - 技能包版本变化时才触发
        raise ConfigurationError(
            f"技能脚本里找不到 {class_name}。",
            hint="技能包可能升级后改了接口，重新下载官方最新版本。",
        )
    try:
        return client_class(api_key=settings.mx_api_key)
    except TypeError:  # 老版本构造函数不接受参数
        return client_class()


# ----------------------------------------------------------------------
# 调用
# ----------------------------------------------------------------------
def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "\n…(已截断)"


def _normalise_data(client: Any, raw: dict[str, Any], limit: int) -> dict[str, Any]:
    """用技能自带的解析器把结果摊平成表格 + 文本。

    不自己解析那个嵌套很深的信封：技能包里已经有 ``parse_result`` / ``format_terminal``，
    复用它们比自己猜结构稳得多（接口一改，跟着技能包升级即可）。
    """

    tables: list[dict[str, Any]] = []
    conditions: list[str] = []
    total_rows = 0
    error: str | None = None

    parse = getattr(client, "parse_result", None)
    if parse is not None:
        parsed = parse(raw)
        if isinstance(parsed, tuple) and len(parsed) >= 4:
            tables = list(parsed[0] or [])
            conditions = list(parsed[1] or [])
            total_rows = int(parsed[2] or 0)
            error = parsed[3]

    if error:
        raise ProviderUnavailable(f"妙想数据返回错误：{error}")

    payload_tables: list[dict[str, Any]] = []
    for entry in tables:
        rows = list(entry.get("rows") or [])
        payload_tables.append(
            {
                "name": entry.get("sheet_name") or entry.get("name") or "数据",
                "columns": list(entry.get("fieldnames") or (rows[0].keys() if rows else [])),
                "rows": rows[:limit],
                "total_rows": len(rows),
            }
        )

    text = ""
    formatter = getattr(client, "format_terminal", None)
    if formatter is not None:
        try:
            text = formatter(raw, tables, total_rows)
        except Exception:  # noqa: BLE001 - 文本只是锦上添花，拿不到就算了
            text = ""

    return {
        "text": _clip(text, 6000),
        "tables": payload_tables,
        "conditions": conditions,
        "row_count": total_rows,
        "truncated": any(len(entry["rows"]) < entry["total_rows"] for entry in payload_tables),
        "status": raw.get("status"),
    }


def run(
    kind: str,
    query: str,
    *,
    settings: Settings | None = None,
    max_rows: int = 30,
) -> dict[str, Any]:
    """执行一次技能查询（带缓存与配额）。

    :raises ConfigurationError: 没配 Key / 没装技能
    :raises ProviderUnavailable: 配额用尽 / 接口报错
    """

    resolved = settings or get_settings()
    question = (query or "").strip()
    if not question:
        raise ConfigurationError("查询问句不能为空。")
    if kind not in SKILLS:
        raise ConfigurationError(f"未知技能 '{kind}'。")
    if not resolved.mx_api_key:
        raise ConfigurationError(
            "未配置妙想 API Key（MX_APIKEY）。",
            hint=(
                "在 https://dl.dfcfs.com/m/itc4 获取 Key，然后设置环境变量 MX_APIKEY "
                "并重启终端；也可以在 .env 里写 MX_APIKEY=..."
            ),
        )

    key = _cache_key(kind, question)
    with _LOCK:
        usage = _read_usage(resolved)
        cached = (usage.get("cache") or {}).get(key)
        if cached is not None:
            log.debug("妙想缓存命中：%s / %s", kind, question)
            return {**cached, "cached": True, "quota": quota(resolved)}

        used = int(usage.get("calls") or 0)
        if used >= resolved.mx_daily_limit:
            raise ProviderUnavailable(
                f"妙想接口今日额度已用尽（{used}/{resolved.mx_daily_limit}）。",
                hint=(
                    "妙想按天限次，明天自动恢复；也可以调大 FINAGENT_MX_DAILY_LIMIT，"
                    "或改用本地行情数据源（get_* 系列工具）。"
                ),
            )
        # 先记账再调用：宁可多扣一次，也不能因为异常把额度漏掉
        usage["calls"] = used + 1
        _write_usage(resolved, usage)

    started = time.monotonic()
    client = _load_client(kind, resolved)
    try:
        if kind == "data":
            raw = client.query(question)
            payload = _normalise_data(client, raw, max_rows)
        elif kind == "search":
            raw = client.search(question)
            payload = _normalise_search(client, raw, max_rows)
        else:
            raw = client.search(question)
            payload = _normalise_screen(client, raw, max_rows)
    except Exception as exc:  # noqa: BLE001 - 网络/鉴权/解析都可能失败
        raise ProviderUnavailable(
            f"{SKILL_LABELS[kind]}调用失败：{str(exc)[:160]}",
            hint="检查网络与 MX_APIKEY 是否有效；额度用尽时明天再试。",
        ) from exc

    elapsed = round(time.monotonic() - started, 2)
    payload = {
        **payload,
        "skill": SKILL_LABELS[kind],
        "query": question,
        "elapsed_seconds": elapsed,
        "cached": False,
    }

    with _LOCK:
        usage = _read_usage(resolved)
        usage.setdefault("cache", {})[key] = payload
        _write_usage(resolved, usage)

    return {**payload, "quota": quota(resolved)}


def _normalise_search(client: Any, raw: dict[str, Any], limit: int) -> dict[str, Any]:
    text = ""
    for method in ("format_pretty", "extract_content"):
        handler = getattr(client, method, None)
        if handler is None:
            continue
        try:
            candidate = handler(raw)
        except Exception:  # noqa: BLE001 - 换个格式化方法继续
            continue
        if isinstance(candidate, str) and candidate.strip():
            text = candidate
            break
    return {
        "text": _clip(text, 6000),
        "tables": [],
        "row_count": 0,
        "status": raw.get("status"),
    }


def _normalise_screen(client: Any, raw: dict[str, Any], limit: int) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    source = ""
    error = None
    extract = getattr(client, "extract_data", None)
    if extract is not None:
        extracted = extract(raw)
        if isinstance(extracted, tuple) and len(extracted) >= 2:
            rows = list(extracted[0] or [])
            source = str(extracted[1] or "")
            error = extracted[2] if len(extracted) > 2 else None
    if error:
        raise ProviderUnavailable(f"妙想选股返回错误：{error}")
    return {
        "text": "",
        "tables": [
            {
                "name": "选股结果",
                "columns": list(rows[0].keys()) if rows else [],
                "rows": rows[:limit],
                "total_rows": len(rows),
            }
        ]
        if rows
        else [],
        "row_count": len(rows),
        "truncated": len(rows) > limit,
        "source": source,
        "status": raw.get("status"),
    }
