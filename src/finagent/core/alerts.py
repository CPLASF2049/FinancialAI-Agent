"""价格预警，投递渠道可插拔。

两个设计要点：

* **百分比预警由服务端换算**。调用方只给 ``pct_change=5``，服务端读取实时报价算出
  绝对阈值，并把基准价记下来便于审计——调用方（模型或脚本）不必自己做这道算术。
* **投递是一个协议**。``ConsoleNotifier``、``JsonlNotifier``、``SmtpNotifier``
  可以自由组合，于是 MCP 服务器、worker 和测试共用同一套求值逻辑。
"""

from __future__ import annotations

import json
import smtplib
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol

from finagent.core import calendar as cal
from finagent.core.store import JsonStore, new_id, now_iso
from finagent.core.symbols import resolve_strict
from finagent.errors import InvalidArgument, NotFound
from finagent.logging import get_logger
from finagent.providers.base import MarketDataProvider

log = get_logger("alerts")

OPERATORS = (">", ">=", "<", "<=", "cross_up", "cross_down")

_OPERATOR_LABELS = {
    ">": "高于",
    ">=": "不低于",
    "<": "低于",
    "<=": "不高于",
    "cross_up": "上穿",
    "cross_down": "下穿",
}


def _compare(operator: str, price: float, threshold: float) -> bool:
    if operator == ">":
        return price > threshold
    if operator == ">=":
        return price >= threshold
    if operator == "<":
        return price < threshold
    if operator == "<=":
        return price <= threshold
    if operator == "cross_up":
        return price >= threshold
    return price <= threshold


def _validate_operator(operator: str) -> str:
    cleaned = str(operator).strip()
    aliases = {
        "above": ">",
        "上穿": ">",
        "突破": ">",
        "超过": ">",
        "涨到": ">",
        "below": "<",
        "跌破": "<",
        "低于": "<",
        "跌到": "<",
        ">=": ">=",
        "<=": "<=",
    }
    cleaned = aliases.get(cleaned, cleaned)
    if cleaned not in OPERATORS:
        raise InvalidArgument(
            f"未知比较符 '{operator}'。",
            hint=f"可用：{', '.join(OPERATORS)}",
        )
    return cleaned


@dataclass(slots=True)
class Alert:
    id: str
    ts_code: str
    name: str
    operator: str
    threshold: float
    note: str = ""
    enabled: bool = True
    created_at: str = ""
    triggered_at: str | None = None
    last_price: float | None = None
    base_price: float | None = None
    pct_change: float | None = None

    def label(self) -> str:
        return (
            f"{self.name}（{self.ts_code}）价格{_OPERATOR_LABELS.get(self.operator, self.operator)}"
            f" {self.threshold:g}"
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["label"] = self.label()
        return payload


@dataclass(slots=True)
class TriggerEvent:
    alert: Alert
    price: float
    triggered_at: str
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert": self.alert.to_dict(),
            "price": round(self.price, 3),
            "triggered_at": self.triggered_at,
            "message": self.message or f"{self.alert.label()} 已触发，当前价 {self.price:g}",
        }


# ----------------------------------------------------------------------
# 投递
# ----------------------------------------------------------------------
class Notifier(Protocol):
    def send(self, event: TriggerEvent) -> None: ...


class ConsoleNotifier:
    """往日志（stderr）写一行人类可读的提醒。"""

    def send(self, event: TriggerEvent) -> None:
        log.warning("价格预警 %s", event.to_dict()["message"])


class JsonlNotifier:
    """把每次触发追加到 JSONL 审计文件。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, event: TriggerEvent) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")


class SmtpNotifier:
    """通过 SMTP 发送预警；未配置时静默跳过。"""

    def __init__(
        self,
        *,
        server: str | None,
        port: int = 465,
        sender: str | None,
        password: str | None,
        receiver: str | None = None,
    ) -> None:
        self.server = server
        self.port = port
        self.sender = sender
        self.password = password
        self.receiver = receiver or sender

    @property
    def configured(self) -> bool:
        return bool(self.server and self.sender and self.password and self.receiver)

    def send(self, event: TriggerEvent) -> None:
        if not self.configured:
            log.debug("未配置 SMTP，跳过邮件投递")
            return
        message = EmailMessage()
        payload = event.to_dict()
        message["Subject"] = f"[Fin-Agent] {event.alert.name} 价格提醒"
        message["From"] = self.sender or ""
        message["To"] = self.receiver or ""
        message.set_content(payload["message"])
        try:
            with smtplib.SMTP_SSL(self.server or "", self.port, timeout=15) as client:
                client.login(self.sender or "", self.password or "")
                client.send_message(message)
        except Exception as exc:  # noqa: BLE001 - delivery must never break evaluation
            log.error("SMTP 投递失败：%s", exc)


class CompositeNotifier:
    def __init__(self, notifiers: Iterable[Notifier]) -> None:
        self._notifiers = list(notifiers)

    def send(self, event: TriggerEvent) -> None:
        for notifier in self._notifiers:
            try:
                notifier.send(event)
            except Exception as exc:  # noqa: BLE001
                log.error("通知器 %s 失败：%s", type(notifier).__name__, exc)


# ----------------------------------------------------------------------
class AlertStore:
    """持久化的预警账本 + 求值循环。"""

    def __init__(self, path: Path | str, *, notifier: Notifier | None = None) -> None:
        self._store = JsonStore(path, {"alerts": [], "history": []})
        self.notifier: Notifier = notifier or ConsoleNotifier()

    # ------------------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        data = self._store.read()
        data.setdefault("alerts", [])
        data.setdefault("history", [])
        return data

    def _mutate(self, mutator) -> dict[str, Any]:
        with self._store.transaction() as data:
            data.setdefault("alerts", [])
            data.setdefault("history", [])
            mutator(data)
        return self._read()

    # ------------------------------------------------------------------
    def add(
        self,
        ts_code: str,
        operator: str | None = None,
        threshold: float | None = None,
        *,
        pct_change: float | None = None,
        note: str = "",
        provider: MarketDataProvider | None = None,
        base_price: float | None = None,
    ) -> Alert:
        """创建一条预警。

        ``threshold``（配 ``operator``）与 ``pct_change`` 二选一。百分比预警的方向由
        ``pct_change`` 的正负决定，传入的 ``operator`` 会被忽略——调用方完全不必自己做
        这道算术。
        """

        code, name = resolve_strict(ts_code)

        if (threshold is None) == (pct_change is None):
            raise InvalidArgument(
                "threshold 与 pct_change 必须且只能提供一个。",
                hint="绝对价格用 threshold=12.5，涨 5% 用 pct_change=5。",
            )

        resolved_base = base_price
        if pct_change is not None:
            if pct_change <= -100:
                raise InvalidArgument("pct_change 必须大于 -100。")
            op = ">" if float(pct_change) > 0 else "<"
            if provider is None and resolved_base is None:
                raise InvalidArgument("百分比预警需要数据源来读取基准价。")
            if resolved_base is None:
                assert provider is not None
                resolved_base = float(provider.realtime_price(code)["price"])
            threshold = round(float(resolved_base) * (1.0 + float(pct_change) / 100.0), 3)
        else:
            if not operator:
                raise InvalidArgument("传入 threshold 时必须同时提供 operator。")
            op = _validate_operator(operator)

        assert threshold is not None  # narrowed by the validation above
        if float(threshold) <= 0:
            raise InvalidArgument("threshold 必须为正数。")

        alert = Alert(
            id=new_id("alert"),
            ts_code=code,
            name=name,
            operator=op,
            threshold=round(float(threshold), 3),
            note=note or "",
            enabled=True,
            created_at=now_iso(),
            base_price=round(float(resolved_base), 3) if resolved_base is not None else None,
            pct_change=float(pct_change) if pct_change is not None else None,
        )

        self._mutate(lambda data: data["alerts"].append(alert.to_dict()))
        log.info("已添加预警 %s（%s %s %s）", alert.id, code, op, alert.threshold)
        return alert

    def list(self, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        data = self._read()
        alerts = data["alerts"]
        if not include_disabled:
            alerts = [alert for alert in alerts if alert.get("enabled", True)]
        return alerts

    def get(self, alert_id: str) -> dict[str, Any]:
        for alert in self.list():
            if alert["id"] == alert_id:
                return alert
        raise NotFound(
            f"未找到预警 '{alert_id}'。",
            hint="调用 list_alerts 查看现有 id。",
        )

    def update(
        self,
        alert_id: str,
        *,
        operator: str | None = None,
        threshold: float | None = None,
        note: str | None = None,
        enabled: bool | None = None,
    ) -> Alert:
        updated: dict[str, Any] = {}

        def mutate(data: dict[str, Any]) -> None:
            for entry in data["alerts"]:
                if entry["id"] != alert_id:
                    continue
                if operator is not None:
                    entry["operator"] = _validate_operator(operator)
                if threshold is not None:
                    if float(threshold) <= 0:
                        raise InvalidArgument("threshold 必须为正数。")
                    entry["threshold"] = round(float(threshold), 3)
                if note is not None:
                    entry["note"] = note
                if enabled is not None:
                    entry["enabled"] = bool(enabled)
                if (operator is not None or threshold is not None) and entry.get("triggered_at"):
                    entry["triggered_at"] = None
                    entry["enabled"] = True
                updated.update(entry)
                return
            raise NotFound(
                f"未找到预警 '{alert_id}'。",
                hint="调用 list_alerts 查看现有 id。",
            )

        self._mutate(mutate)
        return Alert(**{k: updated[k] for k in Alert.__dataclass_fields__})

    def remove(self, alert_id: str) -> dict[str, Any]:
        def mutate(data: dict[str, Any]) -> None:
            before = len(data["alerts"])
            data["alerts"] = [a for a in data["alerts"] if a["id"] != alert_id]
            if len(data["alerts"]) == before:
                raise NotFound(f"未找到预警 '{alert_id}'。")

        self._mutate(mutate)
        return {"removed": alert_id}

    # ------------------------------------------------------------------
    def check(
        self,
        provider: MarketDataProvider,
        *,
        notify: bool = True,
        trading_hours_only: bool = True,
        now: str | None = None,
    ) -> dict[str, Any]:
        """评估所有启用的预警，触发命中的那些。"""

        if trading_hours_only and not cal.is_trading_now():
            return {
                "evaluated": 0,
                "triggered": [],
                "skipped": "outside trading hours",
                "checked_at": now or now_iso(),
            }

        data = self._read()
        enabled = [alert for alert in data["alerts"] if alert.get("enabled", True)]
        triggered: list[dict[str, Any]] = []
        triggered_ids: set[str] = set()
        prices: dict[str, float] = {}
        checked_at = now or now_iso()

        for alert in enabled:
            code = alert["ts_code"]
            if code not in prices:
                try:
                    prices[code] = float(provider.realtime_price(code)["price"])
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s 没有报价：%s", code, exc)
                    prices[code] = -1.0
            price = prices[code]
            if price < 0:
                continue
            if not _compare(alert["operator"], price, float(alert["threshold"])):
                continue

            event = TriggerEvent(
                alert=Alert(**{k: alert[k] for k in Alert.__dataclass_fields__}),
                price=price,
                triggered_at=checked_at,
            )
            triggered.append(event.to_dict())
            triggered_ids.add(alert["id"])
            if notify:
                self.notifier.send(event)

        def mutate(document: dict[str, Any]) -> None:
            for entry in document["alerts"]:
                if entry["id"] in triggered_ids:
                    entry["enabled"] = False
                    entry["triggered_at"] = checked_at
                    entry["last_price"] = prices.get(entry["ts_code"])
            if triggered_ids:
                document["history"] = (document["history"] + triggered)[-200:]

        if triggered_ids:
            self._mutate(mutate)

        return {
            "evaluated": len(enabled),
            "triggered": triggered,
            "checked_at": checked_at,
        }

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        data = self._read()
        return list(reversed(data["history"]))[:limit]


def build_notifier(
    *,
    data_dir: Path | str,
    smtp: dict[str, Any] | None = None,
    jsonl: bool = True,
) -> Notifier:
    """组装 worker 与 MCP 服务器默认使用的通知链。"""

    notifiers: list[Notifier] = [ConsoleNotifier()]
    if jsonl:
        notifiers.append(JsonlNotifier(Path(data_dir) / "alerts_log.jsonl"))
    if smtp:
        notifier = SmtpNotifier(
            server=smtp.get("server"),
            port=int(smtp.get("port") or 465),
            sender=smtp.get("sender"),
            password=smtp.get("password"),
            receiver=smtp.get("receiver"),
        )
        if notifier.configured:
            notifiers.append(notifier)
    return CompositeNotifier(notifiers)
