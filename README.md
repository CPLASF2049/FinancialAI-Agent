# Fin-Agent

面向中国 A 股的智能投研助手：**打开浏览器就能对话**，背后是三个 MCP 工具服务器 +
一套 LangGraph 多 Agent 调度。

敲一句 `finagent`，浏览器会自动打开聊天界面：左侧会话列表、右侧对话流，Agent 每挑一个
工具、每查一次数据都实时可见，回答是逐字长出来的。

能力被拆成可独立复用的 MCP 服务器（41 个工具），Agent 侧用 LangGraph 的 Supervisor +
**3 个核心 Agent** 做路由，用户画像与会话历史由图状态持久化。

**结论不是一次生成的**：每个 Agent 产出后都过一道审查者（critic），有问题就打回返工；
跑过回测的结论还会被**子进程重新算一遍**，比对结论里声称的数字；交付前再过一个确定性
风控（risk_guard）。用户能在界面上看到这条链路被审查、被拦截的全过程。

装完即可离线运行——内置确定性的行情数据源与确定性规划器，不需要 Tushare token、不需要
LLM API Key、不需要联网，就能把界面和 367 个测试跑起来。

```
      你 ──► 浏览器聊天界面（默认端口 8000，会自动开浏览器）
                  │ SSE 流式（含「决策轨迹」）
                  ▼
   ┌─────────────────────────── LangGraph ────────────────────────────┐
   │  START → load_memory → supervisor ──┬→ researcher ─┐             │
   │           ▲                         ├→ quant      ├→ critic      │
   │           │   Command(goto)         └→ alert      ┘    │         │
   │           │                                            │         │
   │           │        pass ◄──────────────────────────────┘         │
   │           │        revise ──► 打回该 Agent 返工（有上限）         │
   │  supervisor ─finish→ save_memory → risk_guard → finalize → END   │
   │                                      ▲                           │
   │                    确定性规则护栏（不走 LLM）                     │
   └──────────────────────────┬───────────────────────┬───────────────┘
                              │  MCP 协议             │  LangGraph Store
              ┌───────────────▼───────────────┐       ▼
              │  finagent-market   19 个工具  │   用户画像 + 会话历史
              │  finagent-quant     6 个工具  │   （JSON + SQLite checkpoint）
              │  finagent-portfolio 16 个工具 │
              └───────────────┬───────────────┘
                              ▼
                          核心领域逻辑
        providers（Tushare | 离线 fixture）· 技术指标 · 选股器
        回测引擎（23 个策略）· 投资组合 · 价格预警
```

三个 Agent 各自再按**子职责**分头取数（白名单仍然互不重叠）：

| Agent | 子职责 | 说明 |
| --- | --- | --- |
| `researcher` | screening · fundamental · technical | 选股 / 估值财报 / 量价指标 |
| `quant` | backtest · portfolio | 策略回测 / 持仓盈亏 |
| `alert` | alert | 价格预警的增删改查 |


---

## 三分钟快速开始

已经装好 Python 3.11 的话，复制粘贴下面几行就能跑起来：

```bat
git clone https://github.com/CPLASF2049/FinancialAI-Agent.git
cd FinancialAI-Agent
py -3.11 -m venv .venv
.venv\Scripts\activate.bat
pip install -e ".[dev]"
finagent
```

最后一条 `finagent` 会启动本地服务并**自动打开浏览器**，这就是聊天界面，可以直接提问了。

macOS / Linux 把中间两行换成：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

> **默认连的是真实行情**：`.env` 里 `FINAGENT_PROVIDER=eastmoney`，走东方财富公开接口，
> 免费、不需要注册。没有任何 API Key 也能跑——只是不接大模型时，Agent 走确定性规划器，
> 但数据是真的。想要财报/宏观这类深度数据再配 Tushare。

遇到问题先跑自检，它会把每一项状态列出来：

```bat
finagent doctor
```

---

## 目录

- [三分钟快速开始](#三分钟快速开始)
- [能做什么](#能做什么)
- [环境要求](#环境要求)
- [安装](#安装)
- [第一次运行](#第一次运行)
- [界面说明](#界面说明)
- [配置](#配置)
- [命令行](#命令行)
- [MCP 服务器](#mcp-服务器)
- [架构](#架构)
- [开发与测试](#开发与测试)
- [常见问题](#常见问题)
- [许可证](#许可证)

---

## 能做什么

| 能力 | 说明 |
| --- | --- |
| **行情与基本面** | 实时报价、日线、PE/PB/市值、利润表、指数、宏观（GDP/CPI/M2/利率）、港股/美股/ETF/可转债/期货 |
| **技术分析** | MACD、RSI、KDJ、BOLL、ATR、ADX、CCI、Williams %R、随机指标、OBV、VWAP，以及金叉死叉/超买超卖/布林突破等形态 |
| **自然语言选股** | 把「PE 小于 15、市值大于 500 亿的银行股」翻译成筛选条件；另有「长尾挖掘」（低估 + 低关注，可选横盘/放量） |
| **策略回测** | 23 个内置策略，输出累计/年化收益、最大回撤、夏普、胜率、交易次数，可对比基准指数 |
| **模拟持仓** | 多组合、成本摊薄、减仓结算已实现盈亏、按实时价格估值 |
| **价格预警** | 绝对价格或百分比触发；百分比阈值由服务端换算。另有 `finagent alerts --watch` 独立 worker |
| **用户画像** | 风险偏好、投资周期、偏好/回避板块、投资风格，跨会话保留并注入每个专家的提示词 |
| **结论自审** | 每个专家产出后过一道审查者：查论据有没有数据出处、前后有没有矛盾、有没有顺着用户预设说话。不合格就打回返工（可配置轮数） |
| **确定性风控** | 交付前跑规则护栏：保守画像却建议加杠杆 → 改写；对未持有标的给「立即买入」→ 降级；有数字没标来源 → 自动补注 |
| **决策可追溯** | 路由、审查、风控全部作为结构化事件推到界面，收在「决策轨迹」面板里 |
| **前视偏差审计** | `finagent audit` 用截断检验逐策略验证「策略没有看到未来」，并对泄漏给出证据 |
| **代码级复核** | 回测结论会被子进程重算一遍，声称的夏普/回撤/胜率与实际不符就打回 |

---

## 环境要求

| 项 | 要求 |
| --- | --- |
| Python | **3.11 – 3.13**（推荐 3.11，已完整验证） |
| 操作系统 | Windows 10+、macOS、Linux |
| 磁盘 | 约 500 MB（含虚拟环境与依赖） |
| 网络 | **安装时需要**（下载依赖）；日常运行可完全离线 |
| 凭据 | **默认不需要**。要真实行情才需 Tushare token，要 LLM 推理才需 API Key |

> ⚠️ **不要用 Python 3.14**。它的扩展模块 ABI 与 3.11 不兼容，而不少依赖还没有 cp314
> 的预编译包；一旦环境变成「3.14 的解释器 + 3.11 的依赖」，就会出现
> `ImportError: cannot import name 'ArgsKwargs' from 'pydantic_core._pydantic_core'`
> 这类看不懂的错。项目已用 `requires-python` 挡掉 3.14。

确认版本，**并确认它是 3.11**：

```bat
python --version
:: 如果这里是 3.14，别用 `python -m venv`，改用下面指定版本的方式
```

建虚拟环境时请**显式指定版本**，不要依赖 `python` 这个名字（它可能指向高版本）：

```bat
:: 方式一：py launcher（Windows，最方便）
py -3.11 -m venv .venv

:: 方式二：uv（会自动下载所需版本）
uv venv --python 3.11 .venv

:: 方式三：确认 python 已经是 3.11 时
python -m venv .venv
```

低于 3.11 请先装新版 Python（<https://www.python.org/downloads/>）。

---

## 安装

下面五步都在**项目根目录**（`README.md` 所在目录）里执行。

### 第 1 步：拿到代码

```bat
git clone https://github.com/CPLASF2049/FinancialAI-Agent.git
cd FinancialAI-Agent
```

没有 git 就去 <https://github.com/CPLASF2049/FinancialAI-Agent> 点 `Code → Download ZIP`，
解压后 `cd` 进解压出来的目录即可。

### 第 2 步：建虚拟环境

**必须用 Python 3.11 – 3.13**，不要用 3.14（见[环境要求](#环境要求)）。

```bat
:: 你的 python 可能不是 3.11，所以用 py launcher 显式指定版本
py -3.11 -m venv .venv
```

没有 `py` 命令的话用 `uv`，它会自动下载 3.11：

```bat
uv venv --python 3.11 .venv
```

### 第 3 步：激活虚拟环境

| 终端 | 命令 |
| --- | --- |
| Windows cmd | `.venv\Scripts\activate.bat` |
| Windows PowerShell | `.\.venv\Scripts\Activate.ps1` |
| macOS / Linux | `source .venv/bin/activate` |

激活成功后命令行提示符前面会出现 `(.venv)`。

> PowerShell 若报「禁止运行脚本」，先执行一次：
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`

### 第 4 步：安装依赖

```bat
pip install -e ".[dev]"
```

`-e` 是可编辑安装，之后改代码不用重装。这一步要联网下载依赖，约 1–3 分钟。

### 第 5 步：验证

```bat
finagent doctor
```

看到 `MCP 正常：41 个工具` 就装好了，接着看[第一次运行](#第一次运行)。

> 之后每次打开新窗口，只要 `cd` 到项目目录、激活虚拟环境（第 3 步）就能直接用
> `finagent`。嫌麻烦可以把 `.venv\Scripts` 加进 PATH，见[常见问题](#常见问题)。

### 用 Docker 跑

不想在宿主机装 Python 的话：

`at
docker build -t fin-agent .
docker run --rm -p 8000:8000 -v %USERPROFILE%\.finagent:/data fin-agent
`

打开 <http://127.0.0.1:8000> 即可。要接真实行情/大模型，用环境变量传入（不要写进镜像）：

`at
docker run --rm -p 8000:8000 ^
  -e FINAGENT_PROVIDER=tushare -e FINAGENT_TUSHARE_TOKEN=xxxx ^
  -e FINAGENT_LLM_PROVIDER=deepseek -e FINAGENT_LLM_API_KEY=sk-xxxx ^
  fin-agent
`

镜像以非 root 用户运行，/data 是挂载出来的数据目录（checkpoint、持仓、预警、会话）。

### 可选依赖

| 想要什么 | 装什么命令 |
| --- | --- |
| 真实行情（Tushare） | `pip install -e ".[dev,tushare]"` |
| 只跑运行、不要测试工具 | `pip install -e .` |
| 更快的安装（已装 uv） | `uv pip install -e ".[dev]"` |

> `doctor` 里 `tushare extra` 显示「未安装」是正常的，只要不用 `FINAGENT_PROVIDER=tushare`。

### 关于命令名

装好后会得到两个等价命令，任选其一：

| 命令 | 说明 |
| --- | --- |
| `finagent` | 主命令 |
| `fin-agent` | 别名，指向同一个入口 |

另外还有三个 MCP 服务器入口：`finagent-mcp-market`、`finagent-mcp-quant`、
`finagent-mcp-portfolio`。

它们都安装在虚拟环境的 `Scripts\`（Windows）或 `bin\`（macOS/Linux）里，
**必须先激活虚拟环境**才能在任意目录使用。

---

## 第一次运行

```bat
:: 仍在虚拟环境里（命令行提示符前面有 (.venv)）
finagent doctor
```

正常输出应类似：

```
                                Fin-Agent 自检
┌─────────────────────────────┬───────────────────────────────────────────────┐
│ 项目                        │ 取值                                          │
├─────────────────────────────┼───────────────────────────────────────────────┤
│ 版本                        │ 1.0.0                                         │
│ Python                      │ 3.11.16                                       │
│ 行情数据源                  │ offline                                       │
│ LLM                         │ none / deepseek-chat (确定性)                 │
│ ...                         │ ...                                           │
│ 依赖:mcp                    │ 正常                                          │
└─────────────────────────────┴───────────────────────────────────────────────┘
MCP 正常：41 个工具，transport=inprocess
```

看到 `MCP 正常：41 个工具` 就装好了。然后：

```bat
finagent
```

浏览器会自动打开 `http://127.0.0.1:8000`，里面就是聊天界面，可以直接开始问。

### 界面打不开的话

```bat
:: 只起服务、自己手动打开浏览器（也会把地址打出来）
finagent web --no-browser
```

窗口里会显示实际地址（端口被占用会自动顺延到 8001、8002…），复制到浏览器即可。
按 `Ctrl+C` 停止服务。

### 除了界面，还有三种用法

```bat
:: A. 一次性提问（适合脚本 / 定时任务）
finagent run "茅台现在多少钱"

:: B. 终端里的交互式对话（适合 SSH、无浏览器环境）
finagent chat

:: C. 离线演示：7 条请求跑一遍，看各专家如何被调度
python scripts\demo.py
```

`finagent chat` 里可用的指令：

| 指令 | 作用 |
| --- | --- |
| 直接输入问题 | 交给 Supervisor 调度 |
| `/new` | 开一个新会话 |
| `/tools` | 列出 41 个 MCP 工具 |
| `/graph` | 查看拓扑与工具归属 |
| `/exit` | 退出 |

---

## 界面说明

打开 `finagent` 后就是一个完整的聊天应用，不需要记任何命令。整体是浅色主题，白底。

| 区域 | 说明 |
| --- | --- |
| **左侧栏** | 会话列表。点一下切换，悬停出现 `×` 删除；顶部「新建对话」开新会话。会话标题自动取第一条提问 |
| **顶部** | 数据源（离线演示数据 / Tushare 行情）、模型状态；右侧是「接入大模型」按钮 |
| **对话区** | 用户消息在右、回答在左。每条回答上方的小徽章是「由哪个专家处理」 |
| **决策轨迹** | 回答上方的折叠块：`→ 交给谁`、`✓ 审查通过 / ↺ 退回返工`、`🛡 风控命中`。回答是怎么被逼出来的，一目了然 |
| **执行过程** | 另一个折叠块，逐条列出这次调用了哪些工具、参数是什么、成功与否 |
| **输入框** | `Enter` 发送，`Shift+Enter` 换行，自动跟随内容变高；回答生成中发送按钮会变成「停止」 |
| **欢迎页** | 六个示例问题，点一下直接提问，不用想怎么开口 |

「决策轨迹」长这样：

```
决策轨迹 · 4 步
  →  交给 基本面分析专家          深度分析请求：逐个分析师给意见，再交叉检查
  ✓  审查通过 · 基本面分析专家     未发现问题
  →  交给 技术面分析专家          深度分析请求：逐个分析师给意见，再交叉检查
  ↺  退回返工 · 技术面分析专家     结论里的 2 处数字在工具返回中一处也找不到
```

几个使用要点：

1. **流式输出**。接了 DeepSeek 时是模型逐字吐出的；离线确定性模式下由前端做逐字动画，
   观感一致。
2. **多轮上下文**。同一个会话里的追问会延续上下文（「那它的市盈率呢」也能接上）。
3. **记忆是跨会话的**。说过「我是保守型投资者，偏好银行和白酒」，换个会话再问推荐，
   它仍然记得——这条会显示为「已记住偏好」徽章。
4. **会话会保存**。关掉浏览器再打开，左侧列表还在，点回去能接着聊（存在数据目录的
   `web_threads.json` 里）。
5. **只监听本机**。默认 `127.0.0.1`，局域网其他机器访问不到。想让同事用就
   `finagent web --host 0.0.0.0`（自己注意内网安全）。
6. **结论会被打回**。看到「退回返工」不是出错，是审查者发现论据站不住，专家正在重做；
   界面上最后只会留下修订后的那一版。

### 在界面里接入大模型（不用改文件、不用重启）

界面右上角有「**接入大模型**」按钮。点开填三样东西就行：

| 字段 | 怎么填 |
| --- | --- |
| **厂商** | 下拉选，默认 DeepSeek |
| **API Key** | 粘贴 DeepSeek 的 `sk-...`；输入框右侧「显示」可以核对有没有粘错 |
| **模型 / 接口地址** | **留空即可**，自动用该厂商的默认值（DeepSeek 是 `deepseek-chat` + `https://api.deepseek.com`）|

点「**保存并测试**」，它会：

1. 把配置写进项目根目录的 `.env`（已有的其他配置项原样保留）；
2. 立刻热替换模型，**当前页面直接生效**，不需要重启、不需要刷新；
3. 发一个极小的请求做连通性自检，把结果显示在弹窗里。

保存成功的提示长这样：

```
配置已保存。连接正常，模型已响应：你好！
```

Key 填错时会直接告诉你错在哪，而不是丢一串 JSON：

```
API Key 无效或已过期（401），请重新生成后粘贴。原始错误：Error code: 401 - ...
```

其他细节：

- 顶部状态条会从「未接大模型 · 点这里接入」变成 `deepseek · deepseek-chat`，绿色高亮。
- 已经保存过 Key 时，输入框会显示 `已保存：sk-abc…1234`；**留空即表示不修改**。
- 用本地模型（Ollama / LM Studio）时选「本地模型」，Key 可以不填。
- 想知道配置写到哪个文件，弹窗底部「配置写到哪里」会显示完整路径。
- 命令行也在跑着对话时不能保存，会提示「还有对话正在进行，等它结束再保存」。

---

## 配置

所有配置都走环境变量，零配置即可离线运行。

配置文件是一个 `.env`，放在**项目根目录**（README.md 所在目录）。没有就从模板复制：

```bat
copy .env.example .env
```

优先级从高到低：**当前 shell 的环境变量 → 项目根目录 `.env` → `~/.finagent/.env` → 内置默认值**。

> 只接大模型的话，**推荐直接在界面里填**（右上角「接入大模型」），它会自动写好这个
> `.env` 并立刻生效，见[在界面里接入大模型](#在界面里接入大模型不用改文件不用重启)。
> 手动改文件适合批量部署或 CI。

### 数据源：默认就是真实行情

| 数据源 | `FINAGENT_PROVIDER` | 需要 token | 覆盖范围 |
| --- | --- | --- | --- |
| **东方财富**（默认） | `eastmoney` | **不需要** | 日线、实时报价、PE/PB/市值、指数、全市场选股、交易日历 |
| Tushare Pro | `tushare` | 需要 | 上面全部 + 财报、宏观、资金流、龙虎榜、涨跌停 |
| 离线 fixture | `offline` | 不需要 | 合成数据，**只用于测试与演示** |

东方财富这条是**免费**的：直接调公开行情接口，连注册都不用。第一次选股会拉全市场
（约 5900 只，分页 60 次，约 15 秒），之后 10 分钟内走内存/磁盘缓存。

**免费源会被限流甚至封 IP，所以做了双活**：东财不可用时自动切到新浪（报价与全市场列表都切，
字段口径已对齐）。代价是新浪列表**不带行业分类**——这时按行业筛选会明确报错，而不是静默
返回「0 只」：

```
InvalidArgument: 当前数据源没有提供行业分类，无法按行业筛选。
hint: 稍后重试（东财恢复后会自动带上行业），或改用 FINAGENT_PROVIDER=tushare。
```

另外，全市场抓取可能只拿到一部分（限流时），返回结果里会带 `universe_complete: false`
与一句 `warning`——「命中 N 只」偏低时必须让调用方知道，而不是当成全量。

```dotenv
FINAGENT_PROVIDER=eastmoney
# 明确禁止「拿演示数据当真实行情」；打开后 provider=offline 会直接报错
FINAGENT_REQUIRE_REAL_DATA=true
```

确认配置真的生效（不要猜）：

```bat
finagent doctor
:: 行情数据源 │ eastmoney

python -c "from finagent.settings import get_settings; s=get_settings(); print(s.provider); assert s.provider != 'offline'"
```

环境变量与 `.env` 都设了值时，**环境变量优先**。排查「改了没生效」先看这一条。

### 妙想 Skills（可选，官方技能包）

东方财富官方提供的一组金融技能包（数据 / 资讯 / 选股 / 自选 / 模拟盘 / 社区），
走官方接口并带**每日调用限额**，适合查财报、股东、公司简介这类结构化数据。

已安装到 `%USERPROFILE%\skills\`，共 6 个：

```
mx-data    金融数据（行情/财务/股东/关联关系）
mx-search  资讯搜索
mx-xuangu  智能选股
mx-zixuan  自选股管理
mx-moni    模拟组合管理
mx-poster  AI 社区
```

API Key 已写入用户级环境变量 `MX_APIKEY`（永久生效，新开的终端才读得到）：

```bat
echo %MX_APIKEY%
python "%USERPROFILE%\skills\mx-data\mx_data.py" "贵州茅台近三年净利润 营业收入" "%CD%\out"
```

依赖提醒：`mx-data` 写 Excel 需要 `openpyxl`，已装进 `.venv`。

#### Agent 怎么调用它们

已经包成 MCP 服务器 `finagent-mx`，配置好 `MX_APIKEY` 后自动挂进工具面（**41 → 45 个工具**）：

| 工具 | 用途 | 挂在哪 |
| --- | --- | --- |
| `mx_financial_data` | 财报科目、股东、高管、公司简介、行情快照 | 基本面子职责 |
| `mx_news_search` | 公告、研报、行业资讯 | 选股 / 基本面子职责 |
| `mx_stock_screen` | 自然语言选股 | 选股子职责 |
| `mx_quota_status` | 查看今日额度（不消耗额度） | 基本面子职责 |

没有 `MX_APIKEY` 或没装技能包时，这 4 个工具**不会出现**在工具面里——避免模型对着一堆
注定报错的工具空转。

**额度是怎么管的**（接口按天限次，这点必须当心）：

```bat
finagent doctor        :: 看工具数是否变成 45
:: 或者在对话里问「妙想今天还剩多少次额度」
```

* 同一个问句当天重复调用会**命中缓存**，不再扣次数；
* 每日上限 `FINAGENT_MX_DAILY_LIMIT`（默认 30）；
* 额度用尽直接报错，不重试、不排队；
* 配额记在 `数据目录/mx_usage.json`，跨天自动重置。写不进去时会退化成本进程计数并打警告
  （`mx_quota_status` 的 `persisted=false` 就是这个意思）。

> 为什么只包三个：`mx-zixuan`（自选股）、`mx-moni`（模拟组合）、`mx-poster`（社区发帖）
> 都是**写操作**，会改你账户里的真实数据，且在限额下不可重放。它们保持命令行形态，需要时手动跑。

> ⚠️ 有每日额度，别拿它跑回归测试——测试里用假的技能客户端，完全不联网。

### 接入 DeepSeek（大模型）

这是最常问的一项，三步搞定。

**第 1 步**：在项目根目录建 `.env`（已存在就直接编辑），写入：

```dotenv
FINAGENT_LLM_PROVIDER=deepseek
FINAGENT_LLM_API_KEY=sk-你的deepseek密钥
```

> `FINAGENT_LLM_MODEL` 默认就是 `deepseek-chat`，`FINAGENT_LLM_BASE_URL` 默认就是
> `https://api.deepseek.com`，**这两项不用填**。想用推理模型再写
> `FINAGENT_LLM_MODEL=deepseek-reasoner`。
>
> 手动改完 `.env` 后需要重启 `finagent` 才生效；界面里填则会自动热生效。

**第 2 步**：验证是否生效。

```bat
finagent doctor
```

输出最后一行应该是：

```
LLM 已启用：deepseek / deepseek-chat（https://api.deepseek.com）
```

如果看到的是「未启用」或「只差一步」，说明 `.env` 没被读到，看下面的排错。

**第 3 步**：跑一个真问题，加 `--verbose` 能看到 Supervisor 的语言模型决策。

```bat
finagent run "帮我筛选 PE 小于 15 的银行股" --verbose
```

#### 三个必须知道的点

1. **只设 `DEEPSEEK_API_KEY` 不够**。项目还要知道「用哪家」：必须同时有
   `FINAGENT_LLM_PROVIDER=deepseek`。只给 key 的话仍是确定性模式（`finagent doctor`
   会提示「只差一步」）。
2. **`.env` 只写项目根目录那一份就够了**。程序会按
   `项目根 → ~/.finagent → 当前目录` 的顺序查找，所以在哪个目录下敲命令都能读到。
3. **不想写文件**也可以只用当前窗口的临时变量：

```bat
set FINAGENT_LLM_PROVIDER=deepseek
set FINAGENT_LLM_API_KEY=sk-你的deepseek密钥
finagent doctor
```

想永久生效就写进用户环境变量，然后**重开窗口**：

```bat
setx FINAGENT_LLM_PROVIDER deepseek
setx FINAGENT_LLM_API_KEY "sk-你的deepseek密钥"
```

#### 换成别的厂商

改 `FINAGENT_LLM_PROVIDER` 与 base URL 即可，key 变量名也可以沿用各家的习惯名：

| 厂商 | `FINAGENT_LLM_PROVIDER` | 默认 base URL | 默认模型 | 可直接用的 key 变量 |
| --- | --- | --- | --- | --- |
| DeepSeek | `deepseek` | `https://api.deepseek.com` | `deepseek-chat` | `DEEPSEEK_API_KEY` |
| OpenAI | `openai` | `https://api.openai.com/v1` | `gpt-4o-mini` | `OPENAI_API_KEY` |
| Kimi | `kimi` | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` | `OPENAI_API_KEY` |
| 智谱 GLM | `glm` | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` | `OPENAI_API_KEY` |
| 通义千问 | `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | `OPENAI_API_KEY` |
| **讯飞星火** | `spark` | `https://spark-api-open.xf-yun.com/v1` | `generalv3.5` | `OPENAI_API_KEY` |
| **讯飞星辰 MaaS** | `maas` | `https://maas-api.cn-huabei-1.xf-yun.com/v2` | 看服务列表 | `OPENAI_API_KEY` |
| SiliconFlow | `siliconflow` | `https://api.siliconflow.cn/v1` | — | `OPENAI_API_KEY` |
| Ollama / LM Studio | `local` | `http://localhost:11434/v1` | `qwen2.5:7b` | 随便填非空即可 |
| 其它 OpenAI 兼容接口 | `custom` | 自己填 | 自己填 | `OPENAI_API_KEY` |

#### 讯飞星火（注意 Key 不是 APIKey）

星火的 OpenAI 兼容地址是 `https://spark-api-open.xf-yun.com/v1`，项目已经内置这个预设，
界面里选「讯飞星火」即可。**唯一的坑是 Key**：

1. 打开 [讯飞开放平台控制台](https://console.xfyun.cn/services/bmx1)；
2. 找到「**http服务接口认证信息**」，抄 **`APIPassword`** 那一栏（一串字符）；
3. **不要**用 `APIKey` 或 `APISecret` —— 那两个是 WebSocket 用的，填进来会报 401。

模型名对应关系：

| 填这里的值 | 实际是 |
| --- | --- |
| `4.0Ultra` | Ultra（最强，非思考） |
| `generalv3.5` | Max（**默认**，旗舰） |
| `max-32k` | Max 32K 上下文 |
| `generalv3` | Pro |
| `pro-128k` | Pro 128K 上下文 |
| `lite` | Lite（**免费**，先用它试通最省） |

命令行方式同样可用：

```dotenv
FINAGENT_LLM_PROVIDER=spark
FINAGENT_LLM_API_KEY=你的APIPassword
FINAGENT_LLM_MODEL=lite
```

> 建议先用 `lite` 试通（免费），确认能连上再换成 `generalv3.5` 或 `4.0Ultra`。
> 星火有秒级/并发限流，报 11202/11203 就是撞到额度了。

#### 讯飞星辰 MaaS（`ak-` 开头的 Key）

星火大模型（上一节）和**星辰 MaaS** 是两个不同的平台，地址、Key 形态、模型名都不一样，
别混用：

| | 星火大模型 | 星辰 MaaS |
| --- | --- | --- |
| 控制台 | console.xfyun.cn | [maas.xfyun.cn](https://maas.xfyun.cn) |
| `FINAGENT_LLM_PROVIDER` | `spark` | `maas` |
| 接口地址 | `https://spark-api-open.xf-yun.com/v1` | `https://maas-api.cn-huabei-1.xf-yun.com/v2` |
| Key 形态 | `APIPassword` 一串字符 | **`ak-` 开头** |
| 模型名 | `generalv3.5` / `lite` … | 服务卡片上的 `modelId`，如 `spark-x2.6-4b` |

配置写法：

```dotenv
FINAGENT_LLM_PROVIDER=maas
FINAGENT_LLM_API_KEY=ak-你的key
FINAGENT_LLM_MODEL=spark-x2.6-4b
FINAGENT_LLM_BASE_URL=https://maas-api.cn-huabei-1.xf-yun.com/v2
```

**不知道自己的 modelId 是什么？** 直接问接口就行（返回当前 Key 可用的模型）：

```bat
curl -s https://maas-api.cn-huabei-1.xf-yun.com/v2/models ^
  -H "Authorization: Bearer ak-你的key"
```

```
{"object":"list","data":[{"id":"spark-x2.6-4b","object":"model",...}]}
```

也可以看 MaaS 控制台：「服务管控」→「模型服务列表」→ 模型卡片右侧的调用信息。
注意 `modelId` 不等于控制台网址里那串数字（`modelSquare/base/2629...`），那个是页面 id。

两个常见报错：

| 返回 | 含义 |
| --- | --- |
| `403` + `11200` `no valid authorization` | 这个模型**没开通**或额度用尽。`/models` 里没列出来的就是没开通 |
| `400` + `10404` `no category route found` | 模型名写错了，用 `/models` 返回的 `id` |

> MaaS 上的 `spark-x2.6` 系列是**推理模型**：会先输出一段思考内容再给答案，所以首字
> 延迟比普通模型长几秒，回答也更慢。这是正常的，不是卡住。

#### 接入列表里没有的厂商

选「**自定义（任意 OpenAI 兼容接口）**」，自己填接口地址和模型名即可。只要是
OpenAI 兼容协议就能用，例如百度千帆、MiniMax、one-api / new-api 网关、vLLM、Xinference。
命令行等价写法：

```dotenv
FINAGENT_LLM_PROVIDER=custom
FINAGENT_LLM_BASE_URL=https://你的网关/v1
FINAGENT_LLM_MODEL=你的模型名
FINAGENT_LLM_API_KEY=你的Key
```


除 DeepSeek 用 `DEEPSEEK_API_KEY` 外，其余都认 `OPENAI_API_KEY`；也可以统一用
`FINAGENT_LLM_API_KEY`。base URL 与模型名可用 `FINAGENT_LLM_BASE_URL`、
`FINAGENT_LLM_MODEL` 覆盖。

### 接入真实行情（Tushare）

```dotenv
FINAGENT_PROVIDER=tushare
FINAGENT_TUSHARE_TOKEN=你的token
```

需要先装可选依赖：`pip install -e ".[dev,tushare]"`（`finagent doctor` 会显示
`tushare extra` 是否已安装）。

### 全部配置项

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `FINAGENT_PROVIDER` | `offline` | `offline` 用内置 fixture；`tushare` 用真实行情 |
| `FINAGENT_TUSHARE_TOKEN` | 空 | Tushare Pro 的 token |
| `FINAGENT_TUSHARE_RPS` | `4` | 数据源调用限速（次/秒） |
| `FINAGENT_LLM_PROVIDER` | `none` | `none` 用确定性规划器；也可填 `deepseek`/`openai`/`kimi`/`glm`/`qwen`/`siliconflow`/`local` |
| `FINAGENT_LLM_API_KEY` | 空 | LLM 的 key |
| `FINAGENT_LLM_MODEL` | `deepseek-chat` | 模型名 |
| `FINAGENT_LLM_BASE_URL` | `https://api.deepseek.com` | 接口地址 |
| `FINAGENT_LLM_TEMPERATURE` | `0.2` | 采样温度 |
| `FINAGENT_LLM_TIMEOUT` | `90` | 单次请求超时（秒） |
| `FINAGENT_MAX_AGENT_HOPS` | `4` | 单轮最多跳转几个专家 |
| `FINAGENT_MAX_TOOL_ITERATIONS` | `8` | 单个专家最多迭代几次工具调用 |
| `FINAGENT_MAX_REVISIONS` | `1` | 每个专家被审查者打回返工几次；`0` 表示只审不返工 |
| `FINAGENT_MAX_TOTAL_REVISIONS` | `2` | 一轮里所有 Agent 合计的返工上限（跨 Agent）|
| `FINAGENT_MAX_TURN_SECONDS` | `180` | 一轮的墙钟预算，超时即用已有结论收尾 |
| `FINAGENT_MAX_STEPS` | `24` | 一轮最多执行多少个图节点 |
| `FINAGENT_RISK_GUARD` | `true` | 是否启用交付前的确定性风控护栏 |
| `FINAGENT_VERIFY_BACKTESTS` | `true` | 是否用子进程复算回测结论（代码级校验）|
| `FINAGENT_REQUIRE_REAL_DATA` | `false` | 为 `true` 时禁止 offline 数据源作答 |
| `FINAGENT_MCP_TRANSPORT` | `inprocess` | `inprocess` 进程内挂载；`stdio` 拉起真实子进程 |
| `FINAGENT_DATA_DIR` | `~/.finagent` | checkpoint、持仓、预警、画像、记忆的存放目录 |
| `FINAGENT_CHECKPOINT_DB` | `checkpoints.sqlite` | 会话 checkpoint 文件名（相对数据目录） |
| `FINAGENT_LOG_LEVEL` | `INFO` | 日志级别 |
| `FINAGENT_ALERT_POLL_SECONDS` | `60` | 预警 worker 轮询间隔 |
| `FINAGENT_ALERT_TRADING_HOURS_ONLY` | `true` | 是否只在交易时段评估预警 |
| `EMAIL_SMTP_SERVER` / `EMAIL_SMTP_PORT` | 空 / `465` | 预警邮件投递（可选） |
| `EMAIL_SENDER` / `EMAIL_PASSWORD` / `EMAIL_RECEIVER` | 空 | 同上 |

也接受这些简短别名：`TUSHARE_TOKEN`、`DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、
`OPENAI_MODEL`、`OPENAI_BASE_URL`、`DATA_SOURCE`。

---

## 命令行

```bat
finagent                                       打开浏览器界面（默认行为）
finagent web [--host H] [--port N] [--no-browser]
finagent run "<问题>" [--json] [--verbose] [--user U] [--thread T]
finagent chat [--user U] [--thread T]
finagent tools [--transport stdio|inprocess]
finagent graph
finagent mcp {market|quant|portfolio} [--transport ...]
finagent alerts [--watch] [--interval N] [--trading-hours]
finagent doctor
```

几个实际用法：

```bat
:: 换个端口（默认 8000，占用了会自动顺延）
finagent web --port 8100

:: 只起服务，不自动开浏览器
finagent web --no-browser

:: 让局域网内其他机器也能访问（注意内网安全）
finagent web --host 0.0.0.0

:: 看路由与工具调用过程
finagent run "用 MACD 策略回测中信证券近一年" --verbose

:: 机器可读输出（便于接入脚本）
finagent run "茅台现在多少钱" --json

:: 复合请求：Supervisor 会连发两个专家
finagent run "回测一下茅台的双均线策略，再看看我的持仓盈亏" --verbose

:: 后台预警 worker（需另开一个窗口，不要关）
finagent alerts --watch --interval 60
```

`--user` 用来隔离不同人的持仓与画像，`--thread` 用来开不同会话：

```bat
finagent chat --user alice --thread research
```

---

## MCP 服务器

三个服务器都是标准 stdio MCP 服务器，Claude Desktop、Cursor 等任何 MCP 宿主都能直接用。

| 服务器 | 入口 | 工具数 | 职责 |
| --- | --- | --- | --- |
| `finagent-market` | `finagent-mcp-market` | 19 | 行情、估值、财务、指数、宏观、跨市场 |
| `finagent-quant` | `finagent-mcp-quant` | 6 | 技术指标、形态识别、选股、长尾挖掘、回测 |
| `finagent-portfolio` | `finagent-mcp-portfolio` | 16 | 持仓、价格预警、用户画像 |

单独启动一个（把自己当成 MCP 客户端来调试）：

```bat
finagent mcp quant --transport stdio
:: 等价写法
finagent-mcp-quant
python -m finagent.mcp_servers quant
```

在 MCP 宿主里注册（**把路径换成你自己机器上的绝对路径**，Windows 反斜杠要写成 `\\`）：

```json
{
  "mcpServers": {
    "finagent-market": {
      "command": "C:\\你的路径\\FinancialAI-Agent\\.venv\\Scripts\\finagent-mcp-market.exe"
    },
    "finagent-quant": {
      "command": "C:\\你的路径\\FinancialAI-Agent\\.venv\\Scripts\\finagent-mcp-quant.exe"
    },
    "finagent-portfolio": {
      "command": "C:\\你的路径\\FinancialAI-Agent\\.venv\\Scripts\\finagent-mcp-portfolio.exe"
    }
  }
}
```

> 不知道自己该填什么？在项目目录里激活虚拟环境后执行
> `where finagent-mcp-market`（macOS/Linux 用 `which`），把它打印出来的路径填进去。
>
> 用完整路径最省事，避免宿主进程找不到 PATH 里的虚拟环境命令。
> 若宿主需要环境变量，把 `FINAGENT_PROVIDER`、`FINAGENT_TUSHARE_TOKEN` 等一并写进它的 `env` 配置。

### 返回格式

所有工具都只返回一个 JSON 字符串：

```json
{"ok": true,  "data": { ... }}
{"ok": false, "error": {"code": "unknown_symbol", "message": "…", "hint": "…"}}
```

完整的 41 个工具（含每个参数的说明）见 **[docs/TOOLS.md](docs/TOOLS.md)**，
该文件由 `scripts/gen_tool_reference.py` 从线上定义自动生成。

---

## 架构

```
cli.py                        命令行入口（finagent 默认启动下面的 web）
worker.py                     后台预警 worker
web/                          浏览器聊天界面：Starlette + SSE + 静态资源
graph/                        LangGraph 编排：Supervisor + 3 个 Agent + critic + risk_guard
  ├ experts.py                3 个 Agent 与其子职责的工具白名单
  ├ verify.py                 回测结论的子进程复算与比对
  ├ critic.py                 审查者：论据支撑 / 内部矛盾 / 谄媚倾向
  ├ risk.py                   风控护栏：确定性规则，不走 LLM
  └ agent_loop.py             专家的 ReAct 循环与流式输出
mcp_servers/                  三个 MCP 服务器（41 个工具）
core/                         纯领域逻辑：指标、选股、回测、持仓、预警、画像
providers/                    数据访问：Tushare 实盘 / 离线 fixture
```

依赖只向下：`core` 不 import `mcp_servers` 或 `graph`，`mcp_servers` 不 import `graph`。
所以领域逻辑可以当普通 Python 库用，MCP 服务器只是它的序列化外壳，图只是服务器之上的
编排外壳，而 Web 界面只是图之上的一层事件消费者——它通过 `POST /api/chat` 拿到同一条
SSE 事件流，和 CLI 看到的东西完全一致。

### 回测可靠性：三道硬检查

「回测跑出 30% 年化」这件事本身没有意义——先要证明它不是假的。项目里做了三件事，
都不依赖人眼审查：

**① 前视偏差审计（`finagent audit`）**

用**截断检验**：把数据截断到某一天重跑一遍，如果截止日之前（含当日）的成交与权益曲线
和完整数据跑出来的不一致，说明策略看到了未来。

```bat
finagent audit --ts-code 600519.SH
```

```
│ 策略      │ 结果 │ 截断日   │ 比对成交 │ 比对权益点 │
│ ma_cross  │ 通过 │ 20250912 │ 12       │ 175        │
│ macd      │ 通过 │ 20250912 │ 19       │ 175        │
...
22/22 个策略通过（共 22 个，跳过 0 个）
```

这个检验是**方法级**保证，对离线 fixture 和真实行情同样有效。`tests/test_audit.py` 里
还有一个故意用「全样本分位数」当阈值的**泄漏策略**作为负向对照——它必须被判为泄漏，
否则「全部通过」只能说明检验是瞎的。

**② 代码级复核（子进程复算）**

只要这一轮真的跑过回测，审查者就会把 `run_backtest` 的参数取出来，在**独立子进程**里
用同一套引擎重新算一遍，再和结论里声称的数字逐项比对（容差仅用于吸收四舍五入）：

```
结论称夏普比率为 9.99，但用同一参数重新回测得到 -0.434（相差 10.42）
```

**为什么不用 `exec()` 跑 LLM 生成的代码**：那等于在服务进程里开一个远程代码执行口子。
这里策略名必须在注册表里、参数是 JSON、执行在子进程并带超时——同样是"真跑一遍"，
但攻击面完全不同。

**③ 时间戳与数据来源**

每条回测结果都带 `data: {provider, asof, first_bar, bars}`；离线 fixture 的回答会被强制
加上顶部横幅，避免把样例数字当成市场事实：

```
> ⚠️ **离线演示数据**：以下数字来自内置 fixture，**不是真实行情**，仅用于演示与测试。
```

想彻底禁止演示数据作答，设置 `FINAGENT_REQUIRE_REAL_DATA=true`——此时
`provider=offline` 会直接报错并提示配置 Tushare。

### 回测绩效归因（示例输出）

下表是 `600519.SH`、2025 全年、初始资金 100 万的输出**格式**示例。数据来自**离线
fixture（合成数据）**，**不代表任何真实策略的历史业绩**，只看列结构与口径：

| 策略 | 累计收益 | 年化 | 最大回撤 | 夏普 | 交易次数 | 超额(vs 沪深300) |
| --- | --- | --- | --- | --- | --- | --- |
| `rsi` | +26.04% | +26.76% | 9.56% | 1.902 | 4 | +46.15% |
| `williams_r` | +35.01% | +36.00% | 18.76% | 1.539 | 18 | +55.11% |
| `kdj` | +28.51% | +29.30% | 10.83% | 1.444 | 29 | +48.61% |
| `boll_reversion` | +11.80% | +12.10% | 6.24% | 1.250 | 6 | +31.90% |
| `volume_breakout` | +16.74% | +17.18% | 15.59% | 1.153 | 1 | +36.84% |

22 个单标的策略的夏普范围 **-1.43 ~ 1.90**，中位数 **0.09**——这个分布本身比排名更有信息量：
绝大多数技术策略在扣掉摩擦成本后没有稳定优势，和真实市场的经验一致。

换成真实行情后重跑同样的命令即可（需要 Tushare token）：

```bat
set FINAGENT_PROVIDER=tushare
set FINAGENT_TUSHARE_TOKEN=你的token
finagent run "用 RSI 策略回测贵州茅台 2025 全年" --verbose
finagent audit --ts-code 600519.SH
```

**口径**：日线收盘成交、100 股整手、双边佣金 0.03%、卖出印花税 0.05%，未建模滑点与
停牌，基准默认沪深 300（000001.SH）。

### 为什么它不会卡死

多 Agent 系统最常见的故障不是答错，而是**停不下来**：Supervisor 觉得没做完、反复派活，
Critic 觉得不对、反复打回，最后烧光 token 还是没结果。这里用四个互相独立的刹车：

| 刹车 | 默认值 | 作用 |
| --- | --- | --- |
| `FINAGENT_MAX_AGENT_HOPS` | 4 | 一轮最多派几次活 |
| `FINAGENT_MAX_STEPS` | 24 | 一轮最多执行多少个图节点 |
| `FINAGENT_MAX_TURN_SECONDS` | 180 | 墙钟上限：模型再慢也会停 |
| `FINAGENT_MAX_TOTAL_REVISIONS` | 2 | 跨 Agent 的返工总额度 |

前两个是「做了多少事」，第三个是「花了多久」——**只有跳数限制是不够的**：模型慢的时候，
4 次跳转照样能跑好几分钟。任何一个刹车先到，本轮就停止派发新工作、用已有结论收尾。

还有两个防「打乒乓」的细节：

- **返工无变化**：返工后的结论与上一版完全相同 → 不再重审（否则两个节点会来回转）。
- **副作用工具**：这一轮只要调用过 `add_position` / `add_price_alert` 这类写操作，
  就一律不允许返工——重跑等于重复下单。

被刹车截断时，回答照常给出，同时明确告知原因（CLI 打黄字，界面显示黄色提示条）：

```
本轮因步数预算提前收尾，结论可能不完整。
```

程序侧对应 `TurnResult.stop_reason`（`time` / `steps` / `hops`），也写进「决策轨迹」。

### 结论是怎么被审查的

三个环节，职责严格分开：

| 环节 | 谁来做 | 能做什么 | 不能做什么 |
| --- | --- | --- | --- |
| **critic**（审查者） | 有 LLM 就用 LLM，没有就用规则 | 判定 `pass` / `revise`，列出问题 | 不改写正文，只提意见 |
| **返工** | 原专家重跑一次 | 按意见补数据或删论断 | 最多 `max_revisions` 次（默认 1） |
| **risk_guard**（风控） | **纯规则，不走 LLM** | 改写正文、标记拦截 | 不做「推理」，只做硬检查 |

审查者只看三件事：

1. **论据有没有数据支撑** —— 结论里的每个数字，都要能在工具返回里找到出处。
   四舍五入不算问题（`1650.956` → `1650.96` 可以通过），瞎编的价格会被抓出来。
2. **有没有内部矛盾** —— 同一段里说「估值偏低」但 PE 35 倍；或者技术面看多、基本面看空。
   后者**不阻断交付**，而是标成「分歧信号」——两个分析师看法相反，本身就是有用的信息。
3. **有没有谄媚倾向** —— 用户问「是不是要涨」，结论就顺着说「会涨」，且不给任何反面证据。

**为什么持仓和预警专家不返工**：它们有副作用。买入记录写进去了再重跑一遍，就变成买两次。
所以这两个专家只审查、不返工（`REVISABLE_AGENTS` 之外）。

风控的三条规则：

| 规则 | 触发条件 | 处理 |
| --- | --- | --- |
| 风险偏好冲突 | 画像保守/稳健，结论出现「加杠杆/满仓/梭哈」 | 改写为「控制仓位」 |
| 绝对化表述 | 「立即买入」「必涨」「保证收益」 | 降级为「可关注」「存在上涨可能」 |
| 未持有却给强指令 | 对没有持仓的标的说「建议买入」 | 加注提示，需自行确认 |
| 缺少来源标注 | 有数字但没写数据来源 | 自动补一条脚注 |

设计取舍、状态通道、记忆写入路径等细节见 **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**。

---

## 开发与测试

```bat
:: 全量测试（389 个，约 90 秒；默认不联网）
pytest -q

:: 只跑某一层
pytest tests\test_mcp_servers.py -q
pytest tests\test_graph.py -q
pytest tests\test_web.py -q
pytest tests\test_debate.py tests\test_critic.py tests\test_risk.py -q

:: 代码风格
ruff check src tests scripts
ruff format --check src tests
```

| 测试文件 | 覆盖范围 |
| --- | --- |
| `test_symbols.py` | 代码/别名解析、交易日历 |
| `test_indicators.py` | MACD/RSI/KDJ/BOLL/ATR/ADX/OBV 计算、形态识别 |
| `test_backtest.py` | 全部 23 个策略、整手约束、指标边界、确定性、多标的拦截 |
| `test_screener.py` | 筛选条件、亿元单位、排序、数据源 fixture |
| `test_portfolio_alerts.py` | 成本摊薄、已实现盈亏、预警触发、画像归一化 |
| `test_mcp_servers.py` | MCP 线上契约：schema、信封、错误码 |
| `test_intents.py` | 路由关键词、实体/日期/阈值抽取 |
| `test_graph.py` | 路由、多专家跳转、记忆、轮次隔离、持久化、流式事件 |
| `test_llm_mode.py` | ReAct 循环、Supervisor JSON 解析、LLM 模式下的整图 |
| `test_settings.py` | `.env` 查找顺序、LLM 开关条件、别名生效条件 |
| `test_cli.py` | 命令行入口：默认打开界面、各子命令、退出码 |
| `test_web.py` | 端口顺延、会话存储、HTTP 接口、SSE 事件顺序、决策轨迹落盘、界面接入大模型 |
| `test_critic.py` | 审查者三项检查的正反例（含四舍五入容忍、分歧信号不阻断） |
| `test_risk.py` | 风控四条规则、幂等性、持仓未知时跳过 |
| `test_debate.py` | 返工链路：替换旧答案、返工次数封顶、副作用专家不重跑 |
| `test_store.py` | JSON 存储的锁：只读残留锁清理、锁路径异常时降级、并发不丢更新 |
| `test_termination.py` | 终止性：三重预算各自生效、返工不乒乓、截断后仍有回答 |
| `test_eastmoney.py` | 东方财富数据源：字段缩放、万元口径、分页与完整性标记（含 1 条需联网的 live 用例）|

改了 MCP 工具之后记得重新生成文档：

```bat
python scripts\gen_tool_reference.py
```

---

## 常见问题

### `ImportError: cannot import name 'ArgsKwargs' from 'pydantic_core._pydantic_core'`

**虚拟环境的 Python 版本和已装依赖对不上**（最常见：venv 被用 Python 3.14 重建过，但
依赖是给 3.11 装的）。先确认：

```bat
.venv\Scripts\python.exe --version
:: 如果显示 3.14.x，就是这个原因
type .venv\pyvenv.cfg
:: 看 version = 与 home = 是否指向 3.14
```

对照一下 site-packages 里的编译扩展：如果看到 `*.cp311-win_amd64.pyd`，而解释器是 3.14，
就证实了。**重装依赖是没用的**，必须重建虚拟环境：

```bat
cd /d "你的路径\FinancialAI-Agent"

:: 1. 退出虚拟环境（提示符前面的 (.venv) 要消失）
deactivate

:: 2. 删掉坏掉的 .venv
rd /s /q .venv

:: 3. 用 3.11 重建（别用 python -m venv，你的 python 可能是 3.14）
py -3.11 -m venv .venv
:: 或者：uv venv --python 3.11 .venv

:: 4. 重新安装
.venv\Scripts\activate.bat
pip install -e ".[dev]"

:: 5. 验证
finagent doctor
```

### `requires-python` 报错，说需要 <3.14

同上：`python` 指向了 3.14。用 `py -3.11 -m venv .venv` 指定版本即可。

### 界面还是旧样子（背景没变、按钮没出现）

浏览器缓存了旧的 JS/CSS。现在的版本已经处理了这件事——资源 URL 自带
`?v=<文件指纹>`，页面本身也强制回源校验，正常情况下改完刷新就能看到。

如果**升级前**就已经开着这个页面，第一次需要手动强刷一次：

| 系统 | 快捷键 |
| --- | --- |
| Windows / Linux | `Ctrl + F5` 或 `Ctrl + Shift + R` |
| macOS | `Cmd + Shift + R` |

之后就不用再管了。还不行的话关掉标签页重新打开，或换个浏览器确认一下。

### `git push` 卡住或 `Connection was reset`

日常使用不需要 git，这一条只在你改完代码想推送时有用。国内网络下往 GitHub 推送经常连不上，
但 Python 的 HTTP 往往是通的。仓库自带一个小中继，让 git 借 Python 的连接走：

```bat
:: 终端 A：起中继（只监听 127.0.0.1，只放行 github.com:443）
python scripts\git_relay.py 8899

:: 终端 B：经中继推送
git -c http.proxy=http://127.0.0.1:8899 push origin main
```

推完 Ctrl+C 关掉终端 A。

### `PermissionError: ... web_threads.json.lock`

会话读写失败、界面点「新建对话」没反应，日志里是这个：

```
PermissionError: [Errno 13] Permission denied: 'C:\\Users\\你\\.finagent\\web_threads.json.lock'
```

**已经修好了**（残留锁自动清理 + 拿不到锁时降级写入）。如果还遇到，多半是旧版本留下的锁文件：

```bat
:: 1. 先关掉正在跑的 finagent（Ctrl+C）

:: 2. 删掉锁文件
del "%USERPROFILE%\.finagent\*.lock"

:: 3. 重开
finagent
```

原理：写入前用一个 `.lock` 文件做跨进程互斥，正常情况下用完就删。但进程被强杀、或杀毒
软件正在扫这个文件时，锁会留成「只读」或「删除挂起」状态。旧版本只处理了「文件已存在」
一种情况，于是后续所有写入都失败。现在会：残留超过 10 秒就强删（只读的也能删）→
仍然拿不到就**降级为无锁写入**并记一条告警，界面不会再被一个锁文件卡死。

### 敲 `finagent` 没反应 / 浏览器没自动打开

敲 `finagent` 后命令行会停住并打印地址——**这是正常的**，服务正在运行，别关窗口。

```
  Fin-Agent 界面已启动：http://127.0.0.1:8000
  数据源：offline   LLM：确定性
  按 Ctrl+C 停止
```

浏览器没自动弹出来，就把那个地址复制到浏览器打开；或者用
`finagent web --no-browser` 自己控制。

### 端口 8000 被占用

会自动顺延到 8001、8002…，看窗口里打印的实际地址即可。也可以直接指定：
`finagent web --port 8100`。

### 界面能打开但发消息报错 / 一直转圈

按顺序看这三点：

1. **命令行窗口里有没有报错**。服务端的异常会打在那里。
2. **`finagent doctor` 是否正常**，特别是 `MCP 正常：41 个工具` 这一行。
3. **换个端口重开**（`--port 8100`），排除旧进程残留。

界面会在消息下方用红框显示具体错误，把那段文字发出来最快。

### 界面「点不动」/ 侧边栏冒出一堆「新对话」

**已修**，但值得知道原因——两个独立的问题叠在一起：

1. 「新对话」按钮每点一次就在服务端建一个会话。界面卡住时人会连点，于是建了一堆空会话，
   刷新后全冒出来。
2. SSE 流卡住时前端没有超时机制，`state.streaming` 一直是 true，于是「发送」永远被禁用
   ——这就是「按钮点不动」。

现在的行为：

* 「新对话」只清空视图，**发送时才创建会话**；空会话在侧边栏不显示
* 2 分钟收不到任何事件就自动中止并提示「后台 2 分钟没有响应，已自动停止，可直接重发」
* 生成中再点发送会弹提示，不是毫无反应
* 「停止」按钮立即恢复界面，不必等后台收尾

如果还是卡住，说明后台确实慢（推理模型单次 20–30 秒很常见）：看「决策轨迹」里有没有在动，
或调大 `FINAGENT_MAX_TURN_SECONDS`。

### 界面显示「确定性模式（未接大模型）」

表示还没配 LLM，能用但回答是模板化的。按[接入 DeepSeek](#接入-deepseek大模型)那节填两行
`.env`，重启 `finagent` 即可，顶部状态会变成 `deepseek · deepseek-chat`。

### 想让局域网内别人也能用

```bat
finagent web --host 0.0.0.0 --port 8000
```

然后在对方浏览器打开 `http://你的内网IP:8000`。注意：本项目没有登录鉴权，
只适合在可信内网临时使用，用完记得 `Ctrl+C` 关掉。

### `'finagent' 不是内部或外部命令`

虚拟环境没激活，或者不在项目目录。两个办法：

```bat
:: 办法一：进项目目录并激活
cd /d "你的路径\FinancialAI-Agent"
.venv\Scripts\activate.bat
finagent doctor

:: 办法二：不激活，直接用完整路径
.venv\Scripts\finagent.exe doctor
```

想让它在任意目录都能用，把虚拟环境的 Scripts 目录加进用户 PATH：

```bat
setx PATH "%PATH%;你的路径\FinancialAI-Agent\.venv\Scripts"
```

**关掉当前窗口重开**才生效。

### 敲 `finagent` 命令直接报错退出

先看版本能不能打印出来：

```bat
finagent --version
```

- 这里就报「不是内部或外部命令」→ 按上一条处理（虚拟环境没激活）。
- 能打印版本，但直接敲 `finagent` 立刻退出 → 用当前代码重装一次：

```bat
cd /d "你的路径\FinancialAI-Agent"
.venv\Scripts\activate.bat
pip install -e . --force-reinstall --no-deps
```

正常情况应该看到 ASCII 横幅和一个显示 `provider=... llm=...` 的框，然后进入对话提示。

### `ImportError: cannot import name 'FastMCP'`

环境里装的是 `mcp` 2.x（FastMCP 被改名）。本项目要求 `mcp>=1.9,<2`：

```bat
pip install "mcp>=1.9,<2"
```

### 配了 DeepSeek 但 `doctor` 还说 LLM 未启用

按这四条依次排查：

1. **是不是只填了 key，没填厂商？** 必须同时有 `FINAGENT_LLM_PROVIDER=deepseek`。
   只设 `DEEPSEEK_API_KEY` 时，`doctor` 会明确提示「只差一步」。
2. **`.env` 放对位置了吗？** 放在项目根目录（和 `README.md` 同一层），文件名就是
   `.env`，不要叫 `.env.txt`。Windows 资源管理器默认隐藏扩展名，容易命名成 `.env.txt`。
3. **变量名拼对了吗？** 是 `FINAGENT_LLM_PROVIDER`（有三个 `FINAGENT_` 前缀里的下划线），
   不是 `FINAGENT_PROVIDER`——后者是行情数据源，两者不同。
4. **改完重新跑一次 `finagent doctor`**。配置在进程启动时读取，改完 `.env` 不需要重装，
   但当前已经打开的对话需要退出重进。

确认配置到底被读到了什么：

```bat
finagent doctor
:: 关注 LLM 那一行：
::   deepseek / deepseek-chat (llm)      -> 已生效
::   none / deepseek-chat (确定性)        -> 没生效
```

### 报 401 / `AuthenticationError`

key 无效或已过期。去 <https://platform.deepseek.com/api_keys> 重新生成一个，注意
DeepSeek 的 key 以 `sk-` 开头，复制时不要带空格或引号。

### 报 `Connection error` / 超时

`FINAGENT_LLM_BASE_URL` 不可达。默认是 `https://api.deepseek.com`；若走了代理或私有网关，
把它改成实际地址，并可用 `FINAGENT_LLM_TIMEOUT=180` 放宽超时。

### `doctor` 里依赖显示红色

依赖没装全，重装（**不要**加 `--no-deps`）：

```bat
pip install -e ".[dev]"
```

### `No module named finagent`

不在项目目录，或者装到了别的解释器。确认三件事：

```bat
where python
where finagent
pip show finagent-mcp
```

### PowerShell 报「禁止运行脚本」

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

### 数据存在哪里，怎么清空

默认 `~/.finagent`（Windows 上是 `C:\Users\<你>\.finagent`），或 `FINAGENT_DATA_DIR` 指定的目录：

| 文件 | 内容 |
| --- | --- |
| `checkpoints.sqlite` | 会话历史 |
| `portfolios.json` | 模拟持仓 |
| `alerts.json` | 价格预警 |
| `alerts_log.jsonl` | 预警触发记录 |
| `profile.json` | 用户画像 |
| `memory.json` | 长期记忆（LangGraph Store） |

把整个目录删掉就是恢复出厂状态。

### 只想离线试一下，什么都不配

默认就是离线的，直接：

```bat
python scripts\demo.py
```

---

## 已知局限性

写在这里的东西都比「功能列表」重要——它们决定了这个项目**能用来做什么、不能用来做什么**。

**数据**

- 默认的 offline 数据源是**内置合成 fixture**，只用于演示与测试。所有回答都会带顶部横幅，
  数字**不是真实行情**。
- Tushare 免费额度有限（分钟级频率限制），批量扫描或长时间批量回测会触发限流；
  目前只有客户端侧的简单限速（FINAGENT_TUSHARE_RPS），**没有指数退避重试**。
- 没有实时盘中推送：日线来自日终数据，盘中价是快照，不适合做分钟级策略。

**回测**

- 日线收盘成交、100 股整手、双边佣金 + 卖出印花税；**未建模滑点、涨跌停无法成交、
  停牌、分红送转**。真实换手成本会显著拉低高频策略的收益。
- 前视偏差审计（inagent audit）只能证明**策略代码**没有用未来数据；它不能证明
  数据本身没有幸存者偏差（退市股缺失）、也不能证明参数没有过拟合。
- 23 个内置策略是教学/演示用的经典技术指标，**没有一个是经过统计显著性检验的**。
  上面绩效表里中位数夏普 0.09 就是这个意思。

**Agent**

- 审查者（critic）在无 LLM 时是**规则匹配**：它能抓「数字没有出处」「自相矛盾」「顺着用户
  说话」，但抓不住「推理逻辑微妙地错了」。
- 返工上限默认 1 轮（FINAGENT_MAX_REVISIONS），调高会显著增加 token 消耗与延迟。
- 子进程复算只覆盖 
un_backtest 这一类有确定性答案的结论；对纯文字分析无法复算。
- 3 个 Agent 的路由在无 LLM 时靠关键词，长尾表达会落到默认的 researcher。

**工程**

- 单机、单进程设计：会话与画像存本地 JSON/SQLite，没有多租户隔离，也不适合多人同时用。
- 没有鉴权。inagent web --host 0.0.0.0 只在可信内网里用。
- 依赖钉死版本（见 
equirements.txt）：MCP 生态变化快，升级需要重新验证工具契约。

## 许可证

MIT，见 [LICENSE](LICENSE)。
