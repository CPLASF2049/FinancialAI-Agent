"""Agent 质量评测集运行器（P1-3）。

测的不是「工具能不能跑通」，而是**输出可不可信**：

* 工具调用准确性：该调的调了没、有没有调不该调的
* 轨迹效率：步数、返工次数
* 忠实度：结论里的数字能否在工具返回里找到
* 风控：该拦的拦没拦、不该拦的有没有误拦
* 成本与延迟：token 与墙钟（P95）

用法：

    python scripts/eval_suite.py --limit 5          # 冒烟
    python scripts/eval_suite.py --save baseline     # 存档基线
    python scripts/eval_suite.py --compare baseline  # 与基线对比

关键：**基线要现在存**。等改造完再跑，就没有「改造前」的对照组了。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO / "eval"


# ----------------------------------------------------------------------
def load_tasks(limit: int | None = None) -> list[dict[str, Any]]:
    payload = json.loads((EVAL_DIR / "tasks.json").read_text(encoding="utf-8"))
    tasks = payload["tasks"]
    return tasks[:limit] if limit else tasks


def _faithfulness(answer: str, evidence: str) -> dict[str, Any]:
    """结论里的数字有多少能在工具返回里找到。"""

    from finagent.graph.critic import _floats, _supported_by

    claims = _floats(answer)
    if not claims:
        return {"supported": 0, "total": 0, "ratio": 1.0}
    pool = _floats(evidence)
    supported = sum(1 for value in claims if _supported_by(value, pool))
    return {"supported": supported, "total": len(claims), "ratio": supported / len(claims)}


def _tokens_from_events(app: Any, task_id: str) -> dict[str, int]:
    store = getattr(app, "events", None)
    if store is None:
        return {"input": 0, "output": 0}
    total_in = total_out = 0
    for event in store.query(task_id, event_type="step:end"):
        usage = event.payload.get("token_usage") or {}
        total_in += int(usage.get("input_tokens") or 0)
        total_out += int(usage.get("output_tokens") or 0)
    return {"input": total_in, "output": total_out}


# ----------------------------------------------------------------------
async def run_task(app: Any, task: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    thread_id = f"eval-{task['id']}"
    result = await app.arun(task["query"], user_id="eval", thread_id=thread_id)
    wall_ms = int((time.monotonic() - started) * 1000)

    trace = list(result.trace or [])
    called = [str(item.get("tool") or "") for item in trace]
    answer = str(result.answer or "")
    evidence = " ".join(str(item.get("preview") or "") for item in trace)

    must = list(task.get("must_call") or [])
    forbid = list(task.get("forbid_call") or [])
    missing = [name for name in must if name not in called]
    violated = [name for name in forbid if name in called]

    decisions = list(result.decisions or [])
    revisions = sum(
        1 for item in decisions if item.get("step") == "critic" and item.get("verdict") == "revise"
    )
    # 只统计**实质性**风控命中：补来源标注、演示数据提示属于装饰性 flag，
    # 把它们算成「拦截」会让命中率/误拦率失真。
    decorative = {"missing_source", "demo_data"}
    risk_hits = sum(
        1
        for flag in (result.risk_flags or [])
        if str(flag.get("code") or flag.get("type") or "") not in decorative
    )

    return {
        "id": task["id"],
        "category": task["category"],
        "query": task["query"],
        "tool_accuracy": not missing and not violated,
        "missing_tools": missing,
        "forbidden_called": violated,
        "called_tools": sorted(set(called)),
        "tool_calls": len(trace),
        "revisions": revisions,
        "risk_hits": risk_hits,
        "risk_expected": task.get("risk_expected", "none"),
        "faithfulness": _faithfulness(answer, evidence),
        "tokens": _tokens_from_events(app, f"eval:{thread_id}") or _tokens_from_events(app, thread_id),
        "wall_ms": wall_ms,
        "stop_reason": result.stop_reason,
        "answer_chars": len(answer),
        "answered": bool(answer.strip()),
    }


def summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {}

    def p95(values: list[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
        return ordered[index]

    risk_records = [item for item in records if item["risk_expected"] == "any"]
    normal_records = [item for item in records if item["risk_expected"] == "none"]
    faith_ratios = [item["faithfulness"]["ratio"] for item in records if item["faithfulness"]["total"]]

    return {
        "tasks": len(records),
        "answered_rate": round(sum(1 for r in records if r["answered"]) / len(records), 3),
        "tool_accuracy": round(sum(1 for r in records if r["tool_accuracy"]) / len(records), 3),
        "faithfulness_mean": round(statistics.fmean(faith_ratios), 3) if faith_ratios else None,
        "hallucination_rate": round(1 - statistics.fmean(faith_ratios), 3) if faith_ratios else None,
        "avg_tool_calls": round(statistics.fmean([r["tool_calls"] for r in records]), 2),
        "avg_revisions": round(statistics.fmean([r["revisions"] for r in records]), 2),
        "risk_hit_rate": (
            round(sum(1 for r in risk_records if r["risk_hits"]) / len(risk_records), 3)
            if risk_records
            else None
        ),
        "risk_false_positive_rate": (
            round(sum(1 for r in normal_records if r["risk_hits"]) / len(normal_records), 3)
            if normal_records
            else None
        ),
        "tokens_total": sum(r["tokens"]["input"] + r["tokens"]["output"] for r in records),
        "avg_wall_ms": int(statistics.fmean([r["wall_ms"] for r in records])),
        "p95_wall_ms": int(p95([r["wall_ms"] for r in records])),
    }


# ----------------------------------------------------------------------
async def main_async(args: argparse.Namespace) -> int:
    from finagent.graph import open_app

    tasks = load_tasks(args.limit)
    records: list[dict[str, Any]] = []

    async with open_app() as app:
        for task in tasks:
            record = await run_task(app, task)
            records.append(record)
            status = "OK " if record["tool_accuracy"] else "MISS"
            print(
                f"{record['id']} {status} {record['category']:<11} "
                f"tools={record['tool_calls']} rev={record['revisions']} "
                f"faith={record['faithfulness']['ratio']:.2f} {record['wall_ms']}ms",
                flush=True,
            )

    report = {
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summarise(records),
        "records": records,
    }

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    (EVAL_DIR / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.save:
        (EVAL_DIR / f"{args.save}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"已存档基线：eval/{args.save}.json")

    if args.compare:
        baseline_path = EVAL_DIR / f"{args.compare}.json"
        if baseline_path.exists():
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))["summary"]
            print("\n相对基线：")
            for key in report["summary"]:
                now, then = report["summary"][key], baseline.get(key)
                if isinstance(now, (int, float)) and isinstance(then, (int, float)):
                    delta = now - then
                    print(f"  {key:<26} {then} -> {now}  ({delta:+.3f})")

    print("\n汇总：")
    for key, value in report["summary"].items():
        print(f"  {key:<26} {value}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Fin-Agent 质量评测集")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（冒烟）")
    parser.add_argument("--save", default="", help="把本次结果存档为 eval/<name>.json")
    parser.add_argument("--compare", default="", help="与 eval/<name>.json 对比")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
