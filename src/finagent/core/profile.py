"""长期用户画像（投资风格、偏好板块、其他偏好）。

画像在本项目里有三个角色：

* 一份 :class:`JsonStore` 文档，被所有 MCP 服务器和图共享；
* 每轮注入 LangGraph 状态（``user_profile``），专家不必额外调工具就能看到；
* 通过 MCP 暴露（``get_user_profile`` / ``update_user_profile``），任何 MCP 客户端
  都能读写和细化它。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from finagent.core.store import JsonStore, now_iso

_RISK_LEVELS = ("Conservative", "Balanced", "Aggressive")
_HORIZONS = ("Short-term", "Medium-term", "Long-term")

DEFAULT_PROFILE: dict[str, Any] = {
    "risk_tolerance": "Unknown",
    "investment_horizon": "Unknown",
    "favorite_sectors": [],
    "avoid_sectors": [],
    "investment_style": "",
    "custom_preferences": {},
    "updated_at": None,
}


def _canonical_risk(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    mapping = {
        "conservative": "Conservative",
        "保守": "Conservative",
        "保守型": "Conservative",
        "低风险": "Conservative",
        "balanced": "Balanced",
        "稳健": "Balanced",
        "稳健型": "Balanced",
        "平衡": "Balanced",
        "aggressive": "Aggressive",
        "激进": "Aggressive",
        "激进型": "Aggressive",
        "高风险": "Aggressive",
    }
    return mapping.get(text.lower(), mapping.get(text, text if text in _RISK_LEVELS else None))


def _canonical_horizon(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    mapping = {
        "short": "Short-term",
        "short-term": "Short-term",
        "短线": "Short-term",
        "短期": "Short-term",
        "超短线": "Short-term",
        "medium": "Medium-term",
        "medium-term": "Medium-term",
        "中线": "Medium-term",
        "中期": "Medium-term",
        "波段": "Medium-term",
        "long": "Long-term",
        "long-term": "Long-term",
        "长线": "Long-term",
        "长期": "Long-term",
        "价值投资": "Long-term",
    }
    return mapping.get(text.lower(), mapping.get(text, text if text in _HORIZONS else None))


class ProfileStore:
    """读写持久化的用户画像。"""

    def __init__(self, path: Path | str) -> None:
        self._store = JsonStore(path, DEFAULT_PROFILE)

    # ------------------------------------------------------------------
    def get(self) -> dict[str, Any]:
        data = {**DEFAULT_PROFILE, **self._store.read()}
        data.setdefault("custom_preferences", {})
        return data

    def update(
        self,
        *,
        risk_tolerance: str | None = None,
        investment_horizon: str | None = None,
        favorite_sectors: list[str] | None = None,
        avoid_sectors: list[str] | None = None,
        investment_style: str | None = None,
        custom_preferences: dict[str, Any] | None = None,
        merge_sectors: bool = True,
    ) -> dict[str, Any]:
        """增量更新画像。板块列表默认合并（并去重）。"""

        def mutate(data: dict[str, Any]) -> None:
            current = {**DEFAULT_PROFILE, **data}
            current.setdefault("custom_preferences", {})

            risk = _canonical_risk(risk_tolerance)
            if risk:
                current["risk_tolerance"] = risk
            horizon = _canonical_horizon(investment_horizon)
            if horizon:
                current["investment_horizon"] = horizon
            if investment_style and str(investment_style).strip():
                current["investment_style"] = str(investment_style).strip()

            for field_name, incoming in (
                ("favorite_sectors", favorite_sectors),
                ("avoid_sectors", avoid_sectors),
            ):
                if incoming is None:
                    continue
                cleaned = [str(item).strip() for item in incoming if str(item).strip()]
                if merge_sectors:
                    merged = list(dict.fromkeys([*current.get(field_name, []), *cleaned]))
                else:
                    merged = list(dict.fromkeys(cleaned))
                current[field_name] = merged

            if custom_preferences:
                current["custom_preferences"] = {
                    **current.get("custom_preferences", {}),
                    **{str(k): v for k, v in custom_preferences.items()},
                }

            current["updated_at"] = now_iso()
            data.clear()
            data.update(current)

        return self._apply(mutate)

    def _apply(self, mutate) -> dict[str, Any]:
        with self._store.transaction() as data:
            mutate(data)
        return self.get()

    # ------------------------------------------------------------------
    def summary(self) -> str:
        """紧凑的、可直接放进提示词的摘要。"""

        profile = self.get()
        favorites = "、".join(profile.get("favorite_sectors") or []) or "未指定"
        avoid = "、".join(profile.get("avoid_sectors") or []) or "未指定"
        lines = [
            f"- 风险偏好: {profile.get('risk_tolerance') or 'Unknown'}",
            f"- 投资周期: {profile.get('investment_horizon') or 'Unknown'}",
            f"- 偏好板块: {favorites}",
            f"- 回避板块: {avoid}",
            f"- 投资风格: {profile.get('investment_style') or '未指定'}",
        ]
        custom = profile.get("custom_preferences") or {}
        if custom:
            lines.append("- 其他偏好: " + "; ".join(f"{k}={v}" for k, v in custom.items()))
        return "\n".join(lines)

    def is_empty(self) -> bool:
        profile = self.get()
        return (
            (profile.get("risk_tolerance") or "Unknown") == "Unknown"
            and (profile.get("investment_horizon") or "Unknown") == "Unknown"
            and not profile.get("favorite_sectors")
            and not profile.get("avoid_sectors")
            and not profile.get("investment_style")
            and not profile.get("custom_preferences")
        )

    def clear(self) -> None:
        self._store.write(dict(DEFAULT_PROFILE))
