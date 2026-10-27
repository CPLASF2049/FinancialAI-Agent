"""行情数据源的抽象接口。

所有上层代码只认 :class:`MarketDataProvider`，它有两个实现：

* :class:`finagent.providers.offline.OfflineProvider` —— 确定性的合成 fixture，不联网、
  不需要 token。测试套件和演示都用它，因此整套工具面在离线状态下依然可被完整验证。
* :class:`finagent.providers.tushare.TushareProvider` —— 生产环境用的真实 Tushare Pro
  后端。

所有 DataFrame 都使用小写、与数据源无关的列名（``ts_code``、``trade_date``、``open``、
``high``、``low``、``close``、``vol``、``amount``）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any, Literal

import pandas as pd

from finagent.errors import ProviderUnavailable

AssetClass = Literal["hk", "us", "etf", "cb", "futures"]
MacroIndicator = Literal["gdp", "cpi", "m2", "interest_rate"]

PRICE_COLUMNS = ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"]


class MarketDataProvider(ABC):
    """只读行情数据门面。"""

    name: str = "base"

    #: 能力标签，工具据此快速失败并给出有用提示。
    CAPABILITIES: frozenset[str] = frozenset()

    def supports(self, capability: str) -> bool:
        return capability in self.CAPABILITIES

    def require(self, capability: str) -> None:
        if not self.supports(capability):
            raise ProviderUnavailable(
                f"数据源 '{self.name}' 不支持 '{capability}'。",
                hint="切换 FINAGENT_PROVIDER，或改用其他能力。",
            )

    # ------------------------------------------------------------------
    # 基础资料
    # ------------------------------------------------------------------
    @abstractmethod
    def stock_basic(self, ts_code: str | None = None, name: str | None = None) -> pd.DataFrame:
        """标的参考信息：ts_code、symbol、name、area、industry、market、list_date。"""

    @abstractmethod
    def trade_calendar(self, start_date: str, end_date: str) -> pd.DataFrame:
        """行包含 (exchange, cal_date, is_open, pretrade_date)。"""

    # ------------------------------------------------------------------
    # 行情与估值
    # ------------------------------------------------------------------
    @abstractmethod
    def daily_price(
        self,
        ts_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adj: str | None = None,
    ) -> pd.DataFrame:
        """日线 OHLCV，按 trade_date 升序。"""

    @abstractmethod
    def realtime_price(self, ts_code: str) -> dict[str, Any]:
        """最新快照；必须包含 price、pre_close、pct_chg、name。"""

    @abstractmethod
    def daily_basic(
        self, ts_code: str, start_date: str | None = None, end_date: str | None = None
    ) -> pd.DataFrame:
        """估值行：pe、pb、ps、total_mv、circ_mv、turnover_rate。"""

    @abstractmethod
    def income_statement(
        self, ts_code: str, start_date: str | None = None, end_date: str | None = None
    ) -> pd.DataFrame:
        """利润表行：end_date、revenue、net_profit 等。"""

    @abstractmethod
    def index_daily(
        self, ts_code: str, start_date: str | None = None, end_date: str | None = None
    ) -> pd.DataFrame:
        """指数 OHLCV。"""

    # ------------------------------------------------------------------
    # 跨市场
    # ------------------------------------------------------------------
    def asset_basic(self, asset: AssetClass, keyword: str | None = None) -> pd.DataFrame:
        raise ProviderUnavailable(f"数据源 '{self.name}' 不提供跨市场标的资料。")

    def asset_daily(
        self,
        asset: AssetClass,
        ts_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        raise ProviderUnavailable(f"数据源 '{self.name}' 不提供跨市场行情数据。")

    # ------------------------------------------------------------------
    # 资金与事件
    # ------------------------------------------------------------------
    def moneyflow(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        raise ProviderUnavailable(f"数据源 '{self.name}' 不提供资金流数据。")

    def limit_list(self, trade_date: str) -> pd.DataFrame:
        raise ProviderUnavailable(f"数据源 '{self.name}' 不提供涨跌停数据。")

    def top_list(self, trade_date: str) -> pd.DataFrame:
        raise ProviderUnavailable(f"数据源 '{self.name}' 不提供龙虎榜数据。")

    def forecast(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        raise ProviderUnavailable(f"数据源 '{self.name}' 不提供业绩预告数据。")

    def concept_detail(self, concept_name: str | None, ts_code: str | None) -> pd.DataFrame:
        raise ProviderUnavailable(f"数据源 '{self.name}' 不提供概念成分数据。")

    # ------------------------------------------------------------------
    # 宏观
    # ------------------------------------------------------------------
    def macro(self, indicator: MacroIndicator) -> pd.DataFrame:
        raise ProviderUnavailable(f"数据源 '{self.name}' 不提供宏观数据。")

    # ------------------------------------------------------------------
    # 选股池
    # ------------------------------------------------------------------
    def screening_universe(self) -> pd.DataFrame:
        """把基本面与每只标的的最新估值拼在一起宽表。

        列：ts_code、name、industry、close、pe、pb、total_mv（万元）、turnover_rate、pct_chg。
        """

        raise ProviderUnavailable(f"数据源 '{self.name}' 无法提供选股数据。")

    # ------------------------------------------------------------------
    # 辅助函数
    # ------------------------------------------------------------------
    @staticmethod
    def empty_with(columns: Iterable[str]) -> pd.DataFrame:
        return pd.DataFrame(columns=list(columns))

    def close(self) -> None:
        """释放资源。provider 本身无状态，默认空操作。"""

        return None
