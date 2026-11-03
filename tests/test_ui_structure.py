"""界面结构测试：id 拼错会让整个 JS 挂掉，值得钉住。"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "finagent" / "web" / "static"


def _html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


#: 运行期动态创建的节点，不要求出现在初始 HTML 里
DYNAMIC = {
    "statusPills-child",
    "toast",
    "tool-card",
}


def test_every_referenced_id_exists() -> None:
    """`$("xxx")` 里的每个 id 必须在 HTML 里存在，否则 JS 一加载就抛错。"""

    html = _html()
    present = set(re.findall(r'id="([^"]+)"', html))
    used = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', _js()))

    missing = sorted(used - present - DYNAMIC)
    assert not missing, f"这些 id 在 HTML 里找不到：{missing}"


def test_four_panels_are_wired() -> None:
    """设置 / 插件 / 接入大模型 / 关于 四个入口都要在。"""

    html = _html()
    js = _js()

    for tab in ("settings", "plugins", "llm", "about"):
        assert f'data-tab="{tab}"' in html, f"缺少 {tab} 标签"

    assert "function renderPanel" in js
    assert 'openPanel("settings")' in js
    assert "openPanel(\"llm\")" in js


def test_provider_labels_cover_all_sources() -> None:
    """数据源标签必须覆盖全部 provider——之前只有 tushare 分支，
    导致 eastmoney 被显示成「离线演示数据」。"""

    js = _js()
    block = js[js.index("PROVIDER_LABELS = {") : js.index("};", js.index("PROVIDER_LABELS = {"))]
    for provider in ("eastmoney", "tushare", "offline"):
        assert provider in block, f"缺少 {provider} 的显示名"

    # 不允许再出现「非 tushare 即离线演示」那种写法
    assert '=== "tushare" ?' not in js


def test_offline_is_never_shown_as_default_data_source() -> None:
    """只有真的 offline 才显示「离线演示」，其余按实际 provider 显示。"""

    js = _js()
    assert 'offline: "离线演示"' in js
    assert 'info.provider === "tushare" ? "Tushare 行情" : "离线演示数据"' not in js
