"""回测校验执行器（在**独立子进程**里跑一遍回测）。

由 :mod:`finagent.graph.verify` 以 ``python -m finagent.verify_runner`` 方式拉起：
从 stdin 读一个 JSON 规格，把结果以 JSON 写回 stdout。

为什么要单开一个进程：

* **隔离**。校验跑的是真实回测引擎，吃 CPU、可能抛异常；放在子进程里不会拖垮主服务，
  也能用超时硬杀掉（``subprocess`` + ``timeout``）。
* **独立复算**。子进程重新加载配置与数据源，等于「换一个人重新算一遍」，能发现同进程内
  被缓存掩盖的不一致。

**这里不接受任意代码。** 规格里的 ``strategy`` 只能是注册表里已有的名字，参数是 JSON；
没有任何 ``exec`` / ``eval``。把 LLM 生成的 Python 直接跑进服务进程，等于开一个远程代码
执行漏洞——那不是一个可交付的量化系统该有的东西。
"""

from __future__ import annotations

import json
import sys
from typing import Any


def run(spec: dict[str, Any]) -> dict[str, Any]:
    """执行一次回测，返回指标字典；异常一律转成 ``{"ok": false, "error": ...}``。"""

    try:
        from finagent.core.backtest.engine import BacktestEngine
        from finagent.core.backtest.registry import get_strategy_class, list_registered_strategies
        from finagent.providers.registry import get_provider

        strategy = str(spec.get("strategy") or "").strip()
        if strategy not in list_registered_strategies():
            return {
                "ok": False,
                "error": f"未知策略 '{strategy}'",
                "hint": "策略只能取自 list_strategies()，不接受任意代码",
            }

        strategy_cls = get_strategy_class(strategy)
        if strategy_cls.requires_multi_asset:
            return {"ok": False, "error": f"策略 '{strategy}' 需要多标的截面数据"}

        engine = BacktestEngine(
            get_provider(), initial_capital=float(spec.get("initial_capital") or 1_000_000)
        )
        result = engine.run(
            str(spec.get("ts_code") or ""),
            strategy,
            start_date=spec.get("start_date"),
            end_date=spec.get("end_date"),
            params=spec.get("params") or {},
            benchmark=spec.get("benchmark"),
            curve_points=0,
            include_trades=False,
        )
        return {
            "ok": True,
            "metrics": {
                "total_return_pct": result.total_return_pct,
                "annualized_return_pct": result.annualized_return_pct,
                "max_drawdown_pct": result.max_drawdown_pct,
                "sharpe": result.sharpe,
                "win_rate_pct": result.win_rate_pct,
                "trades_count": result.trades_count,
                "exposure_pct": result.exposure_pct,
            },
            "period": {"start": result.start_date, "end": result.end_date},
            "data": {
                "provider": result.provider,
                "asof": result.data_asof,
                "bars": result.bars,
            },
        }
    except Exception as exc:  # noqa: BLE001 - 子进程边界上不再上抛
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    raw = sys.stdin.read()
    try:
        spec = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        spec = {}
        payload = {"ok": False, "error": f"规格不是合法 JSON：{exc}"}
    else:
        payload = run(spec)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
