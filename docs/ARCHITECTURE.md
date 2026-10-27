# 架构说明

这份文档解释系统**为什么**长成这样。用法见 [README](../README.md)，工具清单见
[TOOLS.md](TOOLS.md)。

---

## 1. 分层

```
┌──────────────────────────────────────────────────────────────────────────┐
│ cli.py / worker.py            运维入口                                   │
├──────────────────────────────────────────────────────────────────────────┤
│ web/                          浏览器界面（Starlette + SSE + 静态资源）   │
├──────────────────────────────────────────────────────────────────────────┤
│ graph/                        LangGraph 编排层                           │
│   builder.py  state.py  nodes.py  experts.py  agent_loop.py  offline.py   │
│   intents.py  prompts.py  store.py  mcp_client.py  tooling.py             │
├──────────────────────────────────────────────────────────────────────────┤
│ mcp_servers/                  对外的、可复用的契约                        │
│   market.py (19)  quant.py (6)  portfolio.py (16)  _common.py            │
├──────────────────────────────────────────────────────────────────────────┤
│ core/                         纯领域逻辑，除 provider 外不做 I/O          │
│   indicators  screener  backtest/  portfolio  alerts  profile  symbols    │
│   calendar  store                                                         │
├──────────────────────────────────────────────────────────────────────────┤
│ providers/                    数据访问                                    │
│   base.py  offline.py  tushare.py  registry.py                            │
└──────────────────────────────────────────────────────────────────────────┘
```

**依赖只向下**。`core` 从不 import `mcp_servers` 或 `graph`；`mcp_servers` 从不 import
`graph`。层间共享的只有 `errors.py`、`settings.py`、`logging.py` 这套词汇表。

为什么重要：回测引擎、选股器、指标库都可以当作普通 Python 代码直接使用和测试
（`tests/test_backtest.py` 就是这么做的）；MCP 服务器只是包在上面的一层序列化外壳，
图又只是包在服务器之上的一层编排外壳。想换掉其中一层，另一层不需要知情。

### Provider 这道缝

```python
class MarketDataProvider(ABC):
    CAPABILITIES: frozenset[str]
    def stock_basic(...)      -> pd.DataFrame
    def daily_price(...)      -> pd.DataFrame
    def realtime_price(...)   -> dict
    def screening_universe()  -> pd.DataFrame
    ...
```

两个实现：

| | `OfflineProvider` | `TushareProvider` |
| --- | --- | --- |
| 网络 | 不需要 | Tushare Pro |
| 凭据 | 不需要 | token |
| 确定性 | 逐位可复现 | 实时 |
| 用途 | 测试、演示、CI | 生产 |

`CAPABILITIES` + `require()` 让不支持的能力在**调用时立刻**带着提示失败，而不是返回一个
`None` 然后一路流进模型的上下文里。

离线 provider 是刻意的工程决策，不是占位实现：没有它，CI 里一个测试都跑不起来。它按标的
的 CRC32 做种子生成数据，带行业合理的 PE/PB 区间、由目标亿元市值反推股本、并把价格路径
整体缩放确保**今天的**收盘价落在预定估值上（所以「PE 小于 10」的筛选会返回像样的银行股，
而不是噪声）。

---

## 2. MCP 层

### 为什么是三个服务器

按**领域与生命周期**切，而不是按文件切：

| 服务器 | 状态 | 何时变更 |
| --- | --- | --- |
| `market` | 无状态只读 | 新增数据源/接口 |
| `quant` | 无状态计算 | 新增策略或指标 |
| `portfolio` | 有状态、按用户隔离 | 账户/预警/画像模型变化 |

只需要行情的客户端可以只启 `finagent-market`；回测 worker 永远碰不到持仓存储；改一个策略
不可能弄坏预警投递。

### 信封

```json
{"ok": true,  "data": {...}}
{"ok": false, "error": {"code": "unknown_symbol", "message": "...", "hint": "..."}}
```

三件事比看上去更重要：

1. **失败与空结果可区分**。`{"rows": []}`（没有数据）和 `{"error": ...}`（查询失败）是
   两个不同的事实；否则模型只能靠猜字符串。
2. **错误是可行动的**。`hint` 直接告诉模型怎么改调用（比如 `可用指标：macd, rsi, kdj, ...`），
   实测能显著减少重试空转。
3. **可测**。`tests/test_mcp_servers.py` 断言的是 `ok`/`code`，不是散文。

`_common.error_boundary` 包住每个工具，所以意料之外的异常会变成 `internal_error` 加一条
服务端日志，绝不会把堆栈丢进模型上下文。

### schema 从类型注解生成

```python
@mcp.tool()
@error_boundary
def run_backtest(
    ts_code: Annotated[str, Field(description="待回测的标的，如 '600519.SH' 或 '茅台'。")],
    strategy: Annotated[str, Field(description="策略名，取自 list_strategies。")],
    params: Annotated[dict[str, Any] | None, Field(description="策略参数。")] = None,
) -> str: ...
```

JSON Schema、必填列表、每个参数的说明都由 FastMCP 生成。手写 schema 字典与实现之间天然会
漂移；这里从构造上就不可能漂移。`scripts/gen_tool_reference.py` 把同一份 schema 渲染成
[TOOLS.md](TOOLS.md)。

### 两种传输

`graph/mcp_client.py` 用一个 `Toolkit` 抽象两种传输：

* **inprocess**（默认）——`create_connected_server_and_client_session` 把每个 FastMCP 服务器
  挂在当前解释器里，走内存流，但说的是真实 MCP 协议。没有子进程，0.3 秒拿到 41 个工具。
* **stdio**——`MultiServerMCPClient` 拉起 `python -m finagent.mcp_servers <name>`，和外部
  宿主的行为一致。

Agent 和测试都不知道当前是哪一种。

---

## 3. 图

### 节点与控制流

用 LangGraph 的 `Command(goto=...)` 而不是条件边，因为 Supervisor 是**动态**路由：下一步
去哪取决于已经查到的东西，不是某个静态谓词。

```
supervisor --Command(goto="backtest")--> backtest --Command(goto="supervisor")--> supervisor
```

两道闸保证它会停：

* 状态里的 `agent_hops` 对比 `settings.max_agent_hops`；
* `_may_continue()`——次优意图得分必须 ≥ `max(2.5, 0.7 × 最高分)`。没有这个比例约束时，
  「用 MACD 策略回测 X」会在回测之后又跑一遍分析师，因为 `macd` 同时也是分析师的关键词
  （`test_compound_request_runs_two_experts` 与 `test_single_expert_routing` 锁住了这个行为）。

### 状态通道与「重置问题」

`FinState` 混合了两种生命周期：

| 持久（跨轮保留在 checkpoint 里） | 本轮临时（每轮必须清空） |
| --- | --- |
| `messages`、`user_profile`、`profile_summary` | `answers`、`trace`、`errors`、`completed_agents`、`artifacts`、`agent_hops` |

checkpointer 会把**所有**东西都存下来，于是第二轮的 `completed_agents` 里还留着上一轮的
专家，Supervisor 就会拒绝再路由过去。所以 `load_memory` 被包了一层，专门负责开轮清场：

```python
{
  "completed_agents": Overwrite([]),   # 带 reducer 的通道
  "answers":  Overwrite([]),
  "trace":    Overwrite([]),
  "errors":   Overwrite([]),
  "artifacts": {},                    # 普通 LastValue 通道
  "agent_hops": 0,
}
```

`Overwrite` 绕过通道的 reducer；普通通道（`dict`/`int`）直接返回新值就会被替换。
`test_per_turn_channels_reset` 锁住了这一点。

### 记忆

两条 LangGraph 机制，刻意分开：

**会话状态**——`AsyncSqliteSaver` 落在 `<数据目录>/checkpoints.sqlite`，线程键
`f"{user_id}:{thread_id}"`，`messages` 通过 `add_messages` 累积。

**长期画像**——`graph/store.py::JsonFileStore` 在原子 JSON 文档上实现了 `BaseStore` 的两个
抽象方法（`batch`/`abatch`）：

```python
store.put(("users", user_id), "profile", {...})
item = store.get(("users", user_id), "profile")
```

`InMemoryStore` 一退出就全没了；SQLite checkpointer 存的是**会话**，不是关于用户的持久事实。
文件型 store 是既真正持久、又随时可肉眼检查的最小方案。

写入路径：

```
用户说「我是保守型投资者，偏好银行和白酒」
  → load_memory 把现有画像注入状态
  → 专家作答（如果只是陈述偏好，可能没有专家被路由）
  → save_memory：intents.extract_profile_hints() 得到确定性字典
  → 合并进 state，写入 Store，并通过 MCP 的 update_user_profile 工具镜像一份
    （这样外部 MCP 客户端看到的偏好是一致的）
  → finalize 给出确认
```

抽取刻意做成规则驱动：让 LLM 悄悄决定「记什么」，是不可审计的。
`test_profile_is_remembered_and_hydrated`、`test_memory_is_mirrored_into_the_mcp_profile_store`、
`test_non_preference_turn_does_not_write` 把这套行为钉住了。

### 两种专家模式

`run_expert()` 按 `model is None` 分叉：

| | LLM 模式 | 确定性模式 |
| --- | --- | --- |
| 推理 | `run_tool_loop`（显式 ReAct） | `graph/offline.py` 里的处理函数 |
| 工具 | 专家自己的 MCP 白名单，绑定给模型 | 同一份 MCP 白名单，直接调用 |
| 路由 | Supervisor 向模型要 JSON | `intents.classify_next` 关键词打分 |
| 输出 | 自由格式 Markdown | 模板化 Markdown 表格 |

两种模式产出同样形状的 `trace` 记录和同样的流式事件，所以 CLI、测试、记忆回写完全共用一套
逻辑。正是这种对称性，让 `test_graph.py`（确定性）和 `test_llm_mode.py`（脚本化模型）能
断言同一批形状。

### 为什么自己写 ReAct 循环

LangGraph 1.x 里 `create_react_agent` 已标记废弃，官方建议改用 `langchain.agents`，为了一个
约 40 行的循环再加一个依赖并不划算。自写循环换来：

* 每次工具调用都有结构化记录（`ok`、`error`、`preview`）喂给 `trace` 通道——CLI 用它展示，
  测试用它断言；
* 信封感知的失败判定（`_parse_envelope`），工具级错误会被标成失败，而不是当成数据又塞回模型；
* 一个模型再啰嗦也绕不过去的迭代上限。

废弃函数唯一带来的额外能力是跨厂商的动态模型选择，这里用不到。

---

## 4. 横切关注点

### 并发与持久化

三个 MCP 服务器可能跑在不同进程里，所以所有 JSON 状态都走 `core/store.py::JsonStore`：

1. 独占锁文件（`O_CREAT|O_EXCL`，10 秒后视为残留）跨进程串行化「读—改—写」；
2. 写入先落地临时文件，再 `os.replace` 原子替换。

于是读者要么看到旧文档，要么看到新文档，永远看不到写了一半的内容。

### 流式

节点通过 `langgraph.config.get_stream_writer()` 发出自定义事件（`route`、`agent_start`、
`tool_call`、`tool_result`、`token`、`agent_end`）。`FinAgentApp.astream()` 把
`stream_mode=["updates","custom"]` 交织成统一事件流（`start`、`route`、`agent`、`event`、
`memory`、`final`）。CLI 与 Web 界面消费的是同一条流。

### Web 层为什么这么薄

界面是**纯事件消费者**，不含任何业务判断：

```
浏览器 ──POST /api/chat──► Starlette ──astream()──► LangGraph
       ◄──── SSE ─────────         ◄── 事件流 ──
```

服务端只做三件事：把图的事件翻成 SSE、把一轮对话存进 `web_threads.json`、把静态资源发出去。
因此 `finagent run --verbose` 打印的东西，和界面里「执行过程」折叠块里的内容一模一样。

几个刻意的选择：

* **SSE 而不是 WebSocket**。单向推送足够，浏览器原生 `EventSource` 语义清晰，服务端就是普通
  的 `StreamingResponse`，不引入额外依赖（Starlette + uvicorn 本来就在依赖树里）。
* **逐字动画放在前端**。服务端曾用 `asyncio.sleep` 切片推送做打字效果，但网络与代理缓冲会把
  带间隔的多个 chunk 合并成一个，观感直接消失。现在 LLM 模式是真的流式 token，确定性模式
  由前端 `requestAnimationFrame` 做揭示动画。
* **`route` 事件由 Supervisor 主动发出**。若只靠 `Command.update` 进 updates 流，要等节点
  返回才可见，而那时专家已经开始发工具事件，界面会「先看到工具、后看到交给谁」。
* **记忆注入不露脸**。`load_memory` / `save_memory` 调用 MCP 工具时设 `emit_events=False`，
  用户只看到真正的业务工具，不看到内部管线动作。
* **会话与图状态分开存**。`web_threads.json` 是给界面看的展示记录（含工具轨迹），
  `checkpoints.sqlite` 是给 LangGraph 看的对话状态，按同一个 `thread_id` 对齐。

### 大模型配置的热替换

界面上的「接入大模型」保存后**立刻生效，不重启、不刷新**。实现上只做了一件事：给模型换一个值。

`python
class ModelSlot:          # graph/builder.py
    model: Any = None
    def get(self) -> Any: return self.model
`

节点拿到的不是模型实例，而是 get_model 这个可调用对象，每次调用时现取。于是
pp.set_llm(settings) 只给槽赋新值，图和 MCP 会话都保持原样。

为什么不能「重建整张图」？因为 MCP 会话（in-process 传输）是在 uvicorn 的 lifespan 任务里
用 AsyncExitStack 打开的，而从 HTTP 请求任务里关掉它们会触发 anyio 的守卫：

`
RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
`

同一个模型槽也顺手解决了「CLI 里换配置要重启」的问题——只要进程还在，换 LLM 就只是
换一个对象。数据源、MCP 传输这类配置仍然需要重启进程，因为它们决定了启动时建立的连接。

保存流程还要处理三件小事：

* **合并写入**。只替换 FINAGENT_LLM_* 这四行，.env 里其他配置原样保留（重复键会被
  清掉，避免「改了却没生效」）。
* **环境变量优先**。进程环境变量优先级高于 .env，所以保存时同步写一份到 os.environ；
  如果检测到原本就存在同名环境变量，响应里会告诉界面「以环境变量为准」。
* **端口占用与并发**。改配置时若有对话在跑，返回 409 让用户等一会儿，避免换到一半的流。

### 自我纠错：critic 与 risk_guard

多 Agent 系统最容易犯的错是「看起来很忙，结论不可信」。这一层加了两道关卡，
都刻意做成**单一职责**：

```
专家产出 ──► critic ──pass──► supervisor（继续/收尾）
                │
                └──revise──► 打回该专家返工 ──► critic（再审一次）
```

**critic 只判能不能交付，不写正文。** 三项检查固定：

| 检查 | 手段（无 LLM 时） | 严重度 |
| --- | --- | --- |
| 论据支撑 | 结论里的数字与工具返回做**带容差**比对 | high → 返工 |
| 内部矛盾 | 关键词方向冲突 + PE 与「低估」的自相矛盾 | 跨专家为 medium（分歧信号，不阻断）|
| 谄媚倾向 | 提问含预设 + 结论含肯定 + 无反面证据 | high → 返工 |

容差是踩过坑才加的：结论把 `1650.956` 写成 `1650.96` 属于正常四舍五入，逐字符比对会把
**正确的**结论判成「没有出处」。现在按 `max(0.011, |v| × 0.0005)` 比较，既能容忍舍入，
又抓得住瞎编的价格。

**返工会替换旧答案，而不是追加。** `answers` 是追加通道，所以修订时用 `Overwrite`
整体替换该专家的那一条——否则最终正文里会同时出现「看多」和「看空」两段。

**副作用型专家不返工。** `portfolio` / `alert` 的写入是真实状态变更，重跑等于重复下单、
重复建预警。它们只审查、不返工，问题以提示形式保留（`REVISABLE_AGENTS` 白名单之外）。

**risk_guard 不推理，只跑规则。** 「画像保守却建议加杠杆」是规则判断，不是推理问题；
让概率模型决定要不要拦自己，既不可靠也不可审计。四条规则全部是纯函数，可单测、可复现：

```
risk_preference   保守画像 + 加杠杆/满仓/梭哈  → 改写为「控制仓位」
strong_directive  立即买入/必涨/保证收益      → 降级为可检验说法
unheld_buy        对未持仓标的给强买指令      → 加注提示
missing_source    有数字却没标来源            → 自动补脚注
```

风控位于 `save_memory → risk_guard → finalize`，所以它改写的是**最终正文**，而
`finalize` 只是把改写后的文本写进消息历史。这样「用户看到的」与「存进会话的」永远一致。

### 决策轨迹

路由、审查、风控都以自定义事件的形式经 `stream_writer()` 推到 SSE：

```
route   {agent, label, reason}
critic  {agent, label, verdict, issues:[{type, detail, severity}]}
risk    {flags:[{code, label, detail}], actions:[...], blocked}
```

前端把它们渲染成可折叠的「决策轨迹」面板；服务端同时把这条轨迹**落盘**在
`web_threads.json` 的消息里，所以重新打开旧会话仍然能看到当时的审查结论。

路由事件有个重复问题需要处理：supervisor 既发自定义事件（为了排在工具调用之前），
返回后 `updates` 又带一次。服务端按 agent 去重，只推第一次。

### 配置

`pydantic-settings` 负载配置，并用 `AliasChoices` 接受同义变量名：

```python
tushare_token: str | None = Field(validation_alias=AliasChoices(
    "FINAGENT_TUSHARE_TOKEN", "TUSHARE_TOKEN"))
```

这样 CI 里已有的 `TUSHARE_TOKEN`、`DEEPSEEK_API_KEY` 之类的变量可以直接复用，不必改名。

### 日志

MCP stdio 服务器**绝不能**往 stdout 写东西（那是传输通道本身）。所有日志经
`finagent/logging.py` 走 stderr，FastMCP 构造时也传了 `log_level="WARNING"`，让通道保持干净。

---

## 5. 扩展点

| 想做的事 | 改哪里 |
| --- | --- |
| 加一个指标 | `core/indicators.py`，注册进 `INDICATORS` |
| 加一个策略 | 继承 `BacktestStrategy`，加 `@register_strategy` |
| 加一个数据源 | 实现 `MarketDataProvider`，在 `providers/registry.py` 加分支 |
| 加一个 MCP 工具 | 在对应服务器里写 `@mcp.tool()` 函数，补一条契约测试，重新生成 `docs/TOOLS.md` |
| 加一个专家 | `EXPERT_TOOLS` + `EXPERT_PROMPTS` + 一个 `HANDLERS` 条目（或仅 LLM）+ `EXPERT_AGENTS` |
| 改路由 | `graph/intents.py`（规则）或 `SUPERVISOR_PROMPT`（LLM） |
| 加一种投递渠道 | 在 `core/alerts.py` 实现 `Notifier` |

加一个专家刻意设计成四文件改动，没有需要手工维护的中心注册表——工具白名单**本身就是**注册。

---

## 6. 已知限制

* **回测只支持单标的**。引擎一次处理一个代码；`cross_section_momentum` 为目录完整性注册，
  调用时会返回结构化错误，而不是产出一堆没有意义的数字。
* **没有滑点/冲击成本模型**。成交价取信号当日收盘。用来比较规则行为够用，做容量分析不够。
* **确定性规划器不是通用 NLU**。它覆盖文档里列出的说法，超出的会落到分析师，再要求用户补充
  标的。要通用能力请用 LLM 模式。
* **预警需要有人跑**。图能创建和检查预警，但没法自己触发；需要在某处跑着
  `finagent alerts --watch`。
* **离线数据就是 fixture**。确定性、结构合理，但不是行情模拟器，不适合做研究。
