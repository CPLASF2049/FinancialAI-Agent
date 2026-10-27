"""截面策略占位。

保持注册是为了让 ``list_strategies`` 的目录覆盖截面流派。单标的引擎会识别
``requires_multi_asset`` 并返回结构化错误，而不是悄悄给出没有意义的数字。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from finagent.core.backtest.base import BacktestStrategy
from finagent.core.backtest.registry import register_strategy


@register_strategy
class CrossSectionMomentumStrategy(BacktestStrategy):
    """截面动量（尚未实现；需要多标的）。"""

    name = "cross_section_momentum"
    description = "截面动量（需多标的，单标的引擎会返回错误）"
    requires_multi_asset = True

    def enrich(self, frame: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        return frame

    def signal(self, row, prev_row, params, context=None) -> int:
        return 0
