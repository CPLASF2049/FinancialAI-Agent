"""多子职责合并：套话剥离与单份收尾的回归测试。"""

from __future__ import annotations

from finagent.graph.experts import _strip_boilerplate, _with_heading


def test_markdown_bold_risk_section_is_stripped() -> None:
    """模型常写 `**风险提示**`，不能因为多了两个星号就漏掉。"""

    body = "现价 1476 元。\n\n**风险提示**\n白酒有波动。"
    assert "风险提示" not in _strip_boilerplate(body)
    assert "1476" in _strip_boilerplate(body)


def test_heading_style_risk_section_is_stripped() -> None:
    assert "免责" not in _strip_boilerplate("事实。\n\n## 免责声明\n仅供研究。")


def test_inline_mention_of_risk_is_kept() -> None:
    """正文里顺带提一句「风险提示功能」不算套话，不能误删。"""

    body = "这个页面提供风险提示功能，点击可查看。"
    assert "风险提示功能" in _strip_boilerplate(body)


def test_leading_conclusion_heading_is_dropped() -> None:
    out = _with_heading("technical", "结论\n现价 1476")
    assert out.startswith("## 技术面")
    assert "结论\n" not in out


def test_source_line_is_stripped() -> None:
    body = "PE 21.5。\n\n数据来源：东方财富行情，截至 20261103。"
    assert "数据来源" not in _strip_boilerplate(body)


def test_multiple_passes_heading_once_each() -> None:
    a = _with_heading("fundamental", "PE 21.5")
    b = _with_heading("technical", "近一月涨 13%")
    joined = a + "\n\n" + b
    assert joined.count("## ") == 2
    assert "风险提示" not in joined
