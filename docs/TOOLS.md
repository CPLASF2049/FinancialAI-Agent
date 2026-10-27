# MCP 工具参考

> 由 `scripts/gen_tool_reference.py` 从线上 FastMCP 服务器定义生成。
> 不要手工编辑；改完工具后重新运行生成脚本。

3 个 MCP 服务器，共 41 个工具。

## 返回信封

每个工具都只返回**一个 JSON 字符串**：

```json
{"ok": true,  "data": { ... }}
{"ok": false, "error": {"code": "unknown_symbol", "message": "...", "hint": "..."}}
```

已知错误码：`configuration_error`、`provider_error`、`provider_unavailable`、
`unknown_symbol`、`invalid_argument`、`insufficient_data`、`strategy_not_found`、
`not_found`、`internal_error`。

## 目录

- [`finagent-market`](#finagent-market) —— 19 个工具
- [`finagent-quant`](#finagent-quant) —— 6 个工具
- [`finagent-portfolio`](#finagent-portfolio) —— 16 个工具

## finagent-market

**行情数据** · 入口 `finagent-mcp-market` · `python -m finagent.mcp_servers market`

| 工具 | 说明 |
| --- | --- |
| [`get_asset_basic`](#get_asset_basic) | 某一类非 A 股资产的标的目录。 |
| [`get_asset_daily`](#get_asset_daily) | 港股/美股/ETF/可转债/期货的日线行情。 |
| [`get_concept_detail`](#get_concept_detail) | 概念/题材成分：某个概念包含哪些股票。 |
| [`get_current_time`](#get_current_time) | 获取当前日期时间与交易时段状态。 |
| [`get_daily_basic`](#get_daily_basic) | 估值指标：PE、PE(TTM)、PB、PS、总市值/流通市值、换手率。 |
| [`get_daily_price`](#get_daily_price) | 日线 OHLCV 历史，按时间从新到旧返回。 |
| [`get_forecast`](#get_forecast) | 业绩预告：类型、预计同比变动区间、摘要。 |
| [`get_global_index_comparison`](#get_global_index_comparison) | 跨市场指数表现快照（近 1 日 / 5 日 / 20 日 / 年初至今）。 |
| [`get_income_statement`](#get_income_statement) | 季度利润表：营业收入、营业利润、净利润、每股收益。 |
| [`get_index_daily`](#get_index_daily) | 指数 OHLCV（上证指数、沪深300、创业板指等）。 |
| [`get_limit_list`](#get_limit_list) | 某个交易日的涨跌停板清单。 |
| [`get_macro`](#get_macro) | 宏观序列：GDP、CPI、M2 货币供应量，或利率（Shibor/LPR）。 |
| [`get_moneyflow`](#get_moneyflow) | 按单笔规模划分的资金流向：大单/特大单买卖金额、净流入。 |
| [`get_realtime_price`](#get_realtime_price) | 最新报价快照（价格、涨跌额、涨跌幅、最高/最低、成交量、交易日）。 |
| [`get_stock_basic`](#get_stock_basic) | 个股基础资料：名称、行业、板块、交易所、上市日期。 |
| [`get_top_list`](#get_top_list) | 某个交易日的龙虎榜排行。 |
| [`get_trade_calendar`](#get_trade_calendar) | 交易所交易日历：哪些日期开市，以及它们各自的上一个交易日。 |
| [`list_supported_symbols`](#list_supported_symbols) | 列出内置参考标的库（离线/演示的覆盖范围）。 |
| [`resolve_symbol`](#resolve_symbol) | 把用户输入解析成规范的 ts_code。 |

### get_asset_basic

某一类非 A 股资产的标的目录。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `asset` | `enum(hk, us, etf, cb, futures)` | 是 | hk=港股，us=美股，etf=基金，cb=可转债，futures=商品/股指期货。 |
| `keyword` | `str | None` | 否 | 可选的代码/名称过滤。 |
| `limit` | `int` | 否 | 最多返回多少行。（默认 `20`） |

### get_asset_daily

港股/美股/ETF/可转债/期货的日线行情。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `asset` | `enum(hk, us, etf, cb, futures)` | 是 | 与 ts_code 对应的资产类别。 |
| `ts_code` | `str` | 是 | 标的代码，例如 '00700.HK'、'AAPL.O'、'510300.SH'。 |
| `start_date` | `str | None` | 否 | 起始日期 'YYYYMMDD'。 |
| `end_date` | `str | None` | 否 | 结束日期 'YYYYMMDD'。 |
| `limit` | `int` | 否 | 最多返回多少行（从新到旧）。（默认 `30`） |

### get_concept_detail

概念/题材成分：某个概念包含哪些股票。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `concept_name` | `str | None` | 否 | 概念名，例如 '白酒'、'人工智能'。 |
| `ts_code` | `str | None` | 否 | 另一种用法：查某只股票属于哪些概念。 |
| `limit` | `int` | 否 | 最多返回多少行。（默认 `30`） |

### get_current_time

获取当前日期时间与交易时段状态。

        任何涉及「今天」「最新」「本周」等时间敏感的问题，都必须先调用它，
        以便正确锚定日期区间。

_无参数。_

### get_daily_basic

估值指标：PE、PE(TTM)、PB、PS、总市值/流通市值、换手率。

        回答「贵不贵 / 便宜吗」这类问题用它。`total_mv` 单位是万元，
        同时给出的 `summary.total_mv_yi` 已换算成亿元。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ts_code` | `str` | 是 | 标的或名称。 |
| `start_date` | `str | None` | 否 | 起始日期 'YYYYMMDD'。 |
| `end_date` | `str | None` | 否 | 结束日期 'YYYYMMDD'。 |
| `limit` | `int` | 否 | 最多返回多少行（从新到旧）。（默认 `20`） |

### get_daily_price

日线 OHLCV 历史，按时间从新到旧返回。

        走势、波动率，以及用户提出的任何区间统计都用它（如「最近一周」「过去一个月」）。
        画图或算收益通常用 `adj='qfq'`（前复权）。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ts_code` | `str` | 是 | 标的或名称。 |
| `start_date` | `str | None` | 否 | 起始日期 'YYYYMMDD'，默认 90 天前。 |
| `end_date` | `str | None` | 否 | 结束日期 'YYYYMMDD'，默认最新交易日。 |
| `adj` | `str | None` | 否 | 复权方式：null/不复权、'qfq'/前复权、'hfq'/后复权。 |
| `limit` | `int` | 否 | 最多返回多少行（从新到旧）。（默认 `60`） |

### get_forecast

业绩预告：类型、预计同比变动区间、摘要。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ts_code` | `str` | 是 | 标的或名称。 |
| `start_date` | `str | None` | 否 | 公告起始日期 'YYYYMMDD'。 |
| `end_date` | `str | None` | 否 | 公告结束日期 'YYYYMMDD'。 |
| `limit` | `int` | 否 | 最多返回多少行。（默认 `10`） |

### get_global_index_comparison

跨市场指数表现快照（近 1 日 / 5 日 / 20 日 / 年初至今）。

_无参数。_

### get_income_statement

季度利润表：营业收入、营业利润、净利润、每股收益。

        金额单位见返回里的 `unit_note`：离线模式下是亿元，Tushare 模式下沿用原始单位。
        引用数字前先确认单位。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ts_code` | `str` | 是 | 标的或名称。 |
| `start_date` | `str | None` | 否 | 报告期起始 'YYYYMMDD'。 |
| `end_date` | `str | None` | 否 | 报告期结束 'YYYYMMDD'。 |
| `limit` | `int` | 否 | 最多返回几个报告期（从新到旧）。（默认 `8`） |

### get_index_daily

指数 OHLCV（上证指数、沪深300、创业板指等）。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ts_code` | `str` | 是 | 指数代码：'000001.SH'、'399001.SZ'、'000300.SH' 等。 |
| `start_date` | `str | None` | 否 | 起始日期 'YYYYMMDD'。 |
| `end_date` | `str | None` | 否 | 结束日期 'YYYYMMDD'。 |
| `limit` | `int` | 否 | 最多返回多少行（从新到旧）。（默认 `60`） |

### get_limit_list

某个交易日的涨跌停板清单。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `trade_date` | `str | None` | 否 | 'YYYYMMDD'，默认最新交易日。 |
| `limit` | `int` | 否 | 最多返回多少行。（默认 `40`） |

### get_macro

宏观序列：GDP、CPI、M2 货币供应量，或利率（Shibor/LPR）。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `indicator` | `enum(gdp, cpi, m2, interest_rate)` | 是 | 要取哪条宏观序列。 |
| `limit` | `int` | 否 | 最多返回多少个观测值（从新到旧）。（默认 `12`） |

### get_moneyflow

按单笔规模划分的资金流向：大单/特大单买卖金额、净流入。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ts_code` | `str` | 是 | 标的或名称。 |
| `start_date` | `str | None` | 否 | 起始日期 'YYYYMMDD'，默认 30 天前。 |
| `end_date` | `str | None` | 否 | 结束日期 'YYYYMMDD'。 |
| `limit` | `int` | 否 | 最多返回多少行（从新到旧）。（默认 `20`） |

### get_realtime_price

最新报价快照（价格、涨跌额、涨跌幅、最高/最低、成交量、交易日）。

        在 `tushare` 模式下取最新日线；在 `offline` 模式下由最后一根日线派生出
        确定性的一个 tick。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ts_code` | `str` | 是 | 标的或名称，例如 '600519.SH' 或 '茅台'。 |

### get_stock_basic

个股基础资料：名称、行业、板块、交易所、上市日期。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ts_code` | `str | None` | 否 | ts_code，例如 '600519.SH'。 |
| `name` | `str | None` | 否 | 名称包含的子串，例如 '银行'。 |
| `limit` | `int` | 否 | 最多返回多少行。（默认 `20`） |

### get_top_list

某个交易日的龙虎榜排行。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `trade_date` | `str | None` | 否 | 'YYYYMMDD'，默认最新交易日。 |
| `limit` | `int` | 否 | 最多返回多少行。（默认 `20`） |

### get_trade_calendar

交易所交易日历：哪些日期开市，以及它们各自的上一个交易日。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `start_date` | `str | None` | 否 | 起始日期 'YYYYMMDD'。 |
| `end_date` | `str | None` | 否 | 结束日期 'YYYYMMDD'。 |
| `limit` | `int` | 否 | 最多返回多少行。（默认 `60`） |

### list_supported_symbols

列出内置参考标的库（离线/演示的覆盖范围）。

        没有 Tushare token 时，用它来发现可用的示例标的。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `industry` | `str | None` | 否 | 可选的行业过滤，例如 '银行'。 |

### resolve_symbol

把用户输入解析成规范的 ts_code。

        用户提到公司名而不是代码时就调用它，然后把返回的 `ts_code` 传给其他工具。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `query` | `str` | 是 | 可以是中文名（'贵州茅台'、'茅台'、'招行'）、6 位裸代码（'600519'）或完整 ts_code（'600519.SH'）。 |

## finagent-quant

**量化分析** · 入口 `finagent-mcp-quant` · `python -m finagent.mcp_servers quant`

| 工具 | 说明 |
| --- | --- |
| [`compute_indicators`](#compute_indicators) | 在日线上计算技术指标。 |
| [`detect_patterns`](#detect_patterns) | 识别最新一根 K 线上的技术形态。 |
| [`get_long_tail_stocks`](#get_long_tail_stocks) | 挖掘被忽视的中小市值标的：便宜、市值不大、换手低，可选「横盘」或「开始放量」。 |
| [`list_strategies`](#list_strategies) | 列出全部已注册的回测策略及一句话说明。 |
| [`run_backtest`](#run_backtest) | 在指定区间内对单个标的回测一个策略。 |
| [`screen_stocks`](#screen_stocks) | 按基本面与价格表现筛选股票。 |

### compute_indicators

在日线上计算技术指标。

        可用指标：macd、rsi、kdj、boll、atr、adx、cci、williams_r、stochastic、
        obv、vwap。返回最近的若干根 K 线，按时间从新到旧。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ts_code` | `str` | 是 | 标的或名称，例如 '600519.SH' 或 '茅台'。 |
| `indicators` | `list | None` | 否 | 要计算哪些指标；省略则默认 MACD/RSI/KDJ/BOLL。 |
| `end_date` | `str | None` | 否 | 锚定日期 'YYYYMMDD'，默认最新交易日。 |
| `rows` | `int` | 否 | 返回最近多少根 K 线（最多 60）。（默认 `5`） |
| `params` | `dict | None` | 否 | 按指标覆盖参数，例如 {"macd": {"fast_period": 8}}。 |

### detect_patterns

识别最新一根 K 线上的技术形态。

        报告 MACD/KDJ 金叉死叉、RSI/KDJ 超买超卖、布林带上下轨突破，
        并给出每个指标的多空倾向。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ts_code` | `str` | 是 | 标的或名称。 |
| `end_date` | `str | None` | 否 | 锚定日期 'YYYYMMDD'。 |

### get_long_tail_stocks

挖掘被忽视的中小市值标的：便宜、市值不大、换手低，可选「横盘」或「开始放量」。

        这是本项目招牌的「长尾」挖掘：找市场关注度低但估值合理的品种，
        可叠加横盘整理或成交量异动过滤。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `mv_min` | `float` | 否 | 总市值下限，单位亿元。（默认 `10.0`） |
| `mv_max` | `float` | 否 | 总市值上限，单位亿元。（默认 `200.0`） |
| `pe_max` | `float` | 否 | 市盈率上限。（默认 `40.0`） |
| `pb_max` | `float` | 否 | 市净率上限。（默认 `5.0`） |
| `turnover_max` | `float` | 否 | 换手率上限（%）——「被忽视」意味着换手低。（默认 `3.0`） |
| `check_consolidation` | `bool` | 否 | 要求近 60 日价格变异系数 < 15%（长期横盘）。（默认 `False`） |
| `check_volume_spike` | `bool` | 否 | 要求最新成交量 ≥ 20 日均量的 2 倍（异动放量）。（默认 `False`） |
| `limit` | `int` | 否 | 最多返回多少行。（默认 `20`） |

### list_strategies

列出全部已注册的回测策略及一句话说明。

_无参数。_

### run_backtest

在指定区间内对单个标的回测一个策略。

        返回累计/年化收益、最大回撤、夏普、胜率、交易次数、降采样后的权益曲线，
        以及（可选）相对基准的超额收益。

        撮合假设：日线、按信号当日收盘成交、100 股整手、双边佣金 + 卖出 0.05%
        印花税，不建模滑点。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ts_code` | `str` | 是 | 待回测的标的或名称。 |
| `strategy` | `str` | 是 | 策略名，取自 list_strategies，例如 'ma_cross'。 |
| `start_date` | `str | None` | 否 | 起始日期 'YYYYMMDD'，默认一年前。 |
| `end_date` | `str | None` | 否 | 结束日期 'YYYYMMDD'，默认今天。 |
| `params` | `dict | None` | 否 | 策略参数，例如 {"short_window": 5, "long_window": 20}。 |
| `initial_capital` | `float` | 否 | 初始资金，单位元。（默认 `100000.0`） |
| `benchmark` | `str | None` | 否 | 用于计算超额收益的基准指数代码，例如 '000001.SH'。 |
| `curve_points` | `int` | 否 | 权益曲线最多返回多少个点。（默认 `120`） |

### screen_stocks

按基本面与价格表现筛选股票。

        把自然语言条件翻译成过滤参数：「PE 小于 15、市值大于 500 亿的银行股」
        对应 pe_max=15、mv_min=500、industries=['银行']。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `pe_min` | `float | None` | 否 | 市盈率（TTM）下限。 |
| `pe_max` | `float | None` | 否 | 市盈率（TTM）上限。 |
| `pb_min` | `float | None` | 否 | 市净率下限。 |
| `pb_max` | `float | None` | 否 | 市净率上限。 |
| `mv_min` | `float | None` | 否 | 总市值下限，单位亿元。 |
| `mv_max` | `float | None` | 否 | 总市值上限，单位亿元。 |
| `turnover_min` | `float | None` | 否 | 换手率下限（%）。 |
| `turnover_max` | `float | None` | 否 | 换手率上限（%）。 |
| `pct_chg_min` | `float | None` | 否 | 当日涨跌幅下限（%）。 |
| `pct_chg_max` | `float | None` | 否 | 当日涨跌幅上限（%）。 |
| `industries` | `list | None` | 否 | 包含的行业，例如 ['银行','白酒']。 |
| `exclude_industries` | `list | None` | 否 | 要排除的行业。 |
| `require_profit` | `bool` | 否 | 只保留盈利标的（PE > 0）。（默认 `True`） |
| `sort_by` | `str` | 否 | 排序字段：total_mv、pe、pb、pct_chg、turnover_rate、amount、close，或 none。（默认 `total_mv`） |
| `ascending` | `bool | None` | 否 | 排序方向；省略则用该字段的默认方向。 |
| `limit` | `int` | 否 | 最多返回多少行（上限 100）。（默认 `20`） |

## finagent-portfolio

**持仓、预警与用户画像** · 入口 `finagent-mcp-portfolio` · `python -m finagent.mcp_servers portfolio`

| 工具 | 说明 |
| --- | --- |
| [`add_position`](#add_position) | 记录一笔买入。重复添加同一标的会摊薄成本。 |
| [`add_price_alert`](#add_price_alert) | 创建一条价格预警。 |
| [`check_alerts`](#check_alerts) | 立即评估所有启用的预警，触发命中项。 |
| [`clear_portfolio`](#clear_portfolio) | 清空某个组合的全部持仓。 |
| [`create_portfolio`](#create_portfolio) | 新建一个空组合。 |
| [`get_alert_history`](#get_alert_history) | 最近已触发的预警。 |
| [`get_held_symbols`](#get_held_symbols) | 当前持有的标的列表，便于做组合视角的分析或批量设置预警。 |
| [`get_portfolio_status`](#get_portfolio_status) | 按实时价格给组合估值：逐仓盈亏与合计。 |
| [`get_user_profile`](#get_user_profile) | 读取持久化的投资者画像（风险偏好、投资周期、板块偏好、风格）。 |
| [`list_alerts`](#list_alerts) | 列出价格预警：阈值、状态与触发记录。 |
| [`list_portfolios`](#list_portfolios) | 列出所有组合，并标出当前生效的那一个。 |
| [`remove_alert`](#remove_alert) | 删除一条预警。 |
| [`remove_position`](#remove_position) | 卖出（或清仓）并报告已实现盈亏。 |
| [`update_alert`](#update_alert) | 修改一条预警。改动阈值会让已触发过的预警重新生效。 |
| [`update_position`](#update_position) | 更正某个持仓的股数、成本或备注。 |
| [`update_user_profile`](#update_user_profile) | 持久化投资者偏好。用户明确表达或隐含表达偏好时调用。 |

### add_position

记录一笔买入。重复添加同一标的会摊薄成本。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ts_code` | `str` | 是 | 标的或名称，例如 '000001.SZ' 或 '平安银行'。 |
| `amount` | `float` | 是 | 买入股数。 |
| `price` | `float` | 是 | 每股买入价格。 |
| `portfolio` | `str | None` | 否 | 组合 id 或名称；省略则用当前组合。 |
| `note` | `str` | 否 | 备注。（默认 ``） |

### add_price_alert

创建一条价格预警。

        `threshold` + `operator` 与 `pct_change` 二者择一。用户说「涨 5% 提醒我」时
        直接传 pct_change=5：服务端会读取现价并算出目标价，不需要模型做任何算术。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ts_code` | `str` | 是 | 要监控的标的或名称。 |
| `operator` | `enum(>, >=, <, <=, cross_up, cross_down) | None` | 否 | 绝对价格阈值用的比较符；传 pct_change 时会被忽略。 |
| `threshold` | `float | None` | 否 | 绝对价格阈值。 |
| `pct_change` | `float | None` | 否 | 以百分比表示的相对触发条件（+5 表示上涨 5%，-5 表示下跌 5%），由服务端换算。 |
| `note` | `str` | 否 | 这条预警的原因/备注。（默认 ``） |

### check_alerts

立即评估所有启用的预警，触发命中项。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `trading_hours_only` | `bool` | 否 | 非 A 股交易时段是否跳过检查。（默认 `False`） |
| `notify` | `bool` | 否 | 是否对触发的预警发送通知。（默认 `True`） |

### clear_portfolio

清空某个组合的全部持仓。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `portfolio` | `str | None` | 否 | 组合 id 或名称。 |

### create_portfolio

新建一个空组合。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | `str` | 是 | 新组合的显示名称。 |

### get_alert_history

最近已触发的预警。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `limit` | `int` | 否 | 返回最近多少条触发记录。（默认 `20`） |

### get_held_symbols

当前持有的标的列表，便于做组合视角的分析或批量设置预警。

_无参数。_

### get_portfolio_status

按实时价格给组合估值：逐仓盈亏与合计。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `portfolio` | `str | None` | 否 | 组合 id 或名称；省略则用当前组合。 |

### get_user_profile

读取持久化的投资者画像（风险偏好、投资周期、板块偏好、风格）。

_无参数。_

### list_alerts

列出价格预警：阈值、状态与触发记录。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `include_disabled` | `bool` | 否 | 是否包含已经触发过的预警。（默认 `True`） |

### list_portfolios

列出所有组合，并标出当前生效的那一个。

_无参数。_

### remove_alert

删除一条预警。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `alert_id` | `str` | 是 | 要删除的预警 id。 |

### remove_position

卖出（或清仓）并报告已实现盈亏。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ts_code` | `str` | 是 | 要卖出的标的或名称。 |
| `amount` | `float | None` | 否 | 卖出股数；省略表示清仓。 |
| `price` | `float | None` | 否 | 卖出价格，用于计算已实现盈亏。 |
| `portfolio` | `str | None` | 否 | 组合 id 或名称。 |

### update_alert

修改一条预警。改动阈值会让已触发过的预警重新生效。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `alert_id` | `str` | 是 | 预警 id，来自 list_alerts。 |
| `operator` | `str | None` | 否 | 新的比较符。 |
| `threshold` | `float | None` | 否 | 新的绝对价格阈值。 |
| `note` | `str | None` | 否 | 新的备注。 |
| `enabled` | `bool | None` | 否 | 重新启用或暂停该预警。 |

### update_position

更正某个持仓的股数、成本或备注。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ts_code` | `str` | 是 | 已持有的标的或名称。 |
| `amount` | `float | None` | 否 | 新的股数。 |
| `cost` | `float | None` | 否 | 新的每股平均成本。 |
| `note` | `str | None` | 否 | 新的备注。 |
| `portfolio` | `str | None` | 否 | 组合 id 或名称。 |

### update_user_profile

持久化投资者偏好。用户明确表达或隐含表达偏好时调用。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `risk_tolerance` | `str | None` | 否 | 'Conservative' \| 'Balanced' \| 'Aggressive'（也接受「保守/稳健/激进」等中文输入）。 |
| `investment_horizon` | `str | None` | 否 | 'Short-term' \| 'Medium-term' \| 'Long-term'（也接受短期/中期/长期）。 |
| `favorite_sectors` | `list | None` | 否 | 投资者偏好的板块。 |
| `avoid_sectors` | `list | None` | 否 | 投资者回避的板块。 |
| `investment_style` | `str | None` | 否 | 自由文本风格描述，例如「只买高股息」。 |
| `custom_preferences` | `dict | None` | 否 | 其他任意键值偏好。 |
| `merge_sectors` | `bool` | 否 | 与已有板块列表合并，而不是整体替换。（默认 `True`） |
