# webui — Claude Code 风格的终端工作台

`ClaudeCodeUI.jsx` 是一个**单文件** React 组件，实现「可观察的自主性」：
Agent 的每一次思考、工具调用、审查结论都在时间线上可见，而不是一个
「正在思考…」的模态遮罩。

## 依赖

- React 18+
- Tailwind CSS（只用基础工具类：flex / border / text / px 等，无自定义插件）
- 后端接口：`/api/info`、`/api/threads`、`/api/chat`(SSE)、`/api/runs/{id}`、
  `/api/stream/{id}`、`/api/steer`、`/api/llm`

## 接入方式

Vite（推荐）：

```bash
npm create vite@latest webui -- --template react
# 把 ClaudeCodeUI.jsx 放进 src/，App.jsx 里 return <ClaudeCodeUI apiBase="http://127.0.0.1:8000" />
npm i -D tailwindcss postcss autoprefixer && npx tailwindcss init -p
```

然后 `npm run build`，把产物放到 `src/finagent/web/static/ui/`，
由 FastAPI 静态路由挂到 `/ui`。

## 关键设计取舍

1. **不做「正在思考」遮罩**：用时间线的圆点（橙=执行中 / 绿=成功 / 红=失败）
   和逐字流式文本替代。用户看到的是过程，不是一个转圈。
2. **工具调用是折叠面板**：默认一行 `tool args`，点开才看原始输出。
   信息密度优先，避免一屏全是日志。
3. **权限审批 200ms 防误触**：`[允许]` 在 200ms 内不可点，并提示 `y`/`n` 快捷键。
   敏感操作误点一次代价太大。
4. **Ctrl+C 全局中断**：同时取消前端流与后端 `run`（`POST /api/runs/{id}/cancel`），
   已经完成的部分保留。
5. **会话生命周期**：新建即建空会话；切走时若没说过话就删除（`?if_empty=1`）；
   一旦发出消息就固定，标题由后端从首条内容提炼。

## 多区域并发更新与流式渲染

见项目 README 的「前端：多区域并发更新」一节。
