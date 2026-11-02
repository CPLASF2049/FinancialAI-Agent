"""回测结论的**代码级**复核。

审查者（critic）原先只能做文本层面的检查：数字在不在工具返回里、前后有没有矛盾。
但「工具返回里出现过 0.85」和「这个 0.85 真的是这笔回测的夏普」是两件事——一旦专家把
累计收益写成了夏普、或者拿上一轮的指标凑数，纯文本比对是看不出来的。

这里补上真正的执行验证：

1. 从专家这一轮的工具调用里取出 ``run_backtest`` 的**参数**（标的 / 策略 / 区间 / 参数）；
2. 在**独立子进程**里用同一套引擎**重新算一遍**（:mod:`finagent.verify_runner`）；
3. 把结论里声称的指标与复算结果逐项比对，超出容差就判为「未通过验证」。

为什么是子进程而不是 ``exec()``：校验跑的是我们自己的引擎，规格里只有 JSON；策略名必须
在注册表里。**永远不会执行 LLM 写出来的代码**——那等于给自己开一个远程代码执行漏洞。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

from finagent.logging import get_logger

log = get_logger("graph.verify")

#: 单项指标的容差。回测本身是确定性的，容差只用来吸收四舍五入。
TOLERANCES: dict[str, float] = {
    "total_return_pct": 0.5,
    "annualized_return_pct": 0.5,
    "max_drawdown_pct": 0.5,
    "sharpe": 0.05,
    "win_rate_pct": 1.0,
    "trades_count": 0.0,
}

METRIC_LABELS: dict[str, str] = {
    "total_return_pct": "累计收益",
    "annualized_return_pct": "年化收益",
    "max_drawdown_pct": "最大回撤",
    "sharpe": "夏普比率",
    "win_rate_pct": "胜率",
    "trades_count": "交易次数",
}


@dataclass(slots=True)
class VerificationResult:
    attempted: bool = False
    ok: bool = True
    issues: list[dict[str, str]] = field(default_factory=list)
    actual: dict[str, Any] = field(default_factory=dict)
    claimed: dict[str, float] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "ok": self.ok,
            "issues": self.issues,
            "claimed": self.claimed,
            "actual": self.actual,
            "error": self.error,
        }


# ----------------------------------------------------------------------
def spec_from_trace(trace: list[dict[str, Any]]) -> dict[str, Any] | None:
    """从工具调用记录里还原出 ``run_backtest`` 的参数。"""

    for item in reversed(trace or []):
        if item.get("tool") != "run_backtest" or not item.get("ok"):
            continue
        args = item.get("args") or {}
        if not args.get("ts_code") or not args.get("strategy"):
            continue
        return {
            "ts_code": args.get("ts_code"),
            "strategy": args.get("strategy"),
            "start_date": args.get("start_date"),
            "end_date": args.get("end_date"),
            "params": args.get("params") or {},
            "benchmark": args.get("benchmark"),
        }
    return None


def run_in_subprocess(spec: dict[str, Any], *, timeout: float = 60.0) -> dict[str, Any]:
    """在独立进程里复算一次回测。"""

    # 子进程固定用 UTF-8：中文 Windows 的默认编码是 GBK，直接把 stdout 读成 str 会炸
    # （UnicodeDecodeError），这是实测踩到的坑。
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        completed = subprocess.run(  # noqa: S603 - 固定命令，参数是 JSON，shell=False
            [sys.executable, "-m", "finagent.verify_runner"],
            input=json.dumps(spec, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"复算超时（>{int(timeout)}s）"}
    except OSError as exc:
        return {"ok": False, "error": f"无法启动复算进程：{exc}"}

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip().splitlines()
        return {
            "ok": False,
            "error": f"复算进程退出码 {completed.returncode}：{stderr[-1] if stderr else '无输出'}",
        }
    stdout = completed.stdout or ""
    if not stdout.strip():
        return {"ok": False, "error": "复算进程没有输出"}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "复算进程返回的不是 JSON"}


# ----------------------------------------------------------------------
_PATTERNS: dict[str, re.Pattern[str]] = {
    "total_return_pct": re.compile(r"累计收益[^\-\d]{0,8}(-?\d+(?:\.\d+)?)\s*%"),
    "annualized_return_pct": re.compile(r"年化收益[^\-\d]{0,8}(-?\d+(?:\.\d+)?)\s*%"),
    "max_drawdown_pct": re.compile(r"最大回撤[^\-\d]{0,8}(-?\d+(?:\.\d+)?)\s*%"),
    "sharpe": re.compile(r"夏普[^\-\d]{0,8}(-?\d+(?:\.\d+)?)"),
    "win_rate_pct": re.compile(r"胜率[^\-\d]{0,8}(-?\d+(?:\.\d+)?)\s*%"),
    "trades_count": re.compile(r"交易次数[^\-\d]{0,8}(\d+)"),
}


def claimed_metrics(answer: str) -> dict[str, float]:
    """从结论正文里抽出它**声称**的指标。"""

    found: dict[str, float] = {}
    for name, pattern in _PATTERNS.items():
        match = pattern.search(answer or "")
        if not match:
            continue
        try:
            found[name] = float(match.group(1))
        except ValueError:
            continue
    return found


def compare(claimed: dict[str, float], actual: dict[str, Any]) -> list[dict[str, str]]:
    """逐项比对，返回不一致的描述。"""

    issues: list[dict[str, str]] = []
    for name, value in claimed.items():
        real = actual.get(name)
        if real is None:
            continue
        tolerance = TOLERANCES.get(name, 0.5)
        try:
            difference = abs(float(value) - float(real))
        except (TypeError, ValueError):
            continue
        if difference > tolerance:
            label = METRIC_LABELS.get(name, name)
            issues.append(
                {
                    "type": "unverified",
                    "label": "回测数字与实际不符",
                    "detail": (
                        f"结论称{label}为 {value:g}，但用同一参数重新回测得到 {float(real):g}"
                        f"（相差 {difference:.2f}）"
                    ),
                    "severity": "high",
                }
            )
    return issues


def verify_backtest_answer(
    answer: str,
    trace: list[dict[str, Any]],
    *,
    timeout: float = 60.0,
) -> VerificationResult:
    """完整流程：还原规格 → 子进程复算 → 比对声称的数字。"""

    spec = spec_from_trace(trace)
    if spec is None:
        return VerificationResult(attempted=False)

    result = VerificationResult(attempted=True)
    payload = run_in_subprocess(spec, timeout=timeout)
    if not payload.get("ok"):
        result.ok = False
        result.error = str(payload.get("error") or "复算失败")
        result.issues.append(
            {
                "type": "unverified",
                "label": "回测无法复现",
                "detail": f"用同一参数复算失败：{result.error}",
                "severity": "high",
            }
        )
        return result

    metrics = payload.get("metrics") or {}
    result.actual = metrics
    claimed = claimed_metrics(answer)
    result.claimed = claimed
    issues = compare(claimed, metrics)
    result.issues = issues
    result.ok = not issues

    log.info(
        "回测复算：声称 %d 项，不符 %d 项（%s/%s）",
        len(claimed),
        len(issues),
        spec.get("ts_code"),
        spec.get("strategy"),
    )
    return result
