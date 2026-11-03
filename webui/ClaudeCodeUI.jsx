/**
 * ClaudeCodeUI — 终端原生的 Agent 工作台（单文件 React 组件）
 *
 * 设计哲学：可观察的自主性。界面不是聊天窗口，而是「玻璃箱」——
 * 用户随时能看到 Agent 在做什么、做到哪一步、花了多少 token。
 *
 * 依赖：React 18+、Tailwind CSS（仅用基础工具类）。无其它运行时依赖。
 * 后端契约：/api/info、/api/threads、/api/chat（SSE）、/api/steer、
 *          /api/stream/{run_id}、/api/runs/{run_id}、/api/trace/{task_id}
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

/* ------------------------------------------------------------------ */
/* 设计令牌                                                            */
/* ------------------------------------------------------------------ */
const SEMANTIC = {
  ok: "#74c991",
  err: "#c74e39",
  warn: "#e1c08d",
  info: "#4daafc",
  accent: "#d97757",
};

const cx = (...parts) => parts.filter(Boolean).join(" ");

/* ------------------------------------------------------------------ */
/* 图标（内联 SVG，stroke 1.5，尺寸 14）                                */
/* ------------------------------------------------------------------ */
const Icon = ({ d, size = 14, className = "" }) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.5"
    strokeLinecap="round"
    strokeLinejoin="round"
    className={className}
    aria-hidden="true"
  >
    {d}
  </svg>
);

const Chevron = (p) => (
  <Icon {...p}>
    <path d="M9 6l6 6-6 6" />
  </Icon>
);
const Check = (p) => (
  <Icon {...p}>
    <path d="M20 6L9 17l-5-5" />
  </Icon>
);
const Cross = (p) => (
  <Icon {...p}>
    <path d="M18 6L6 18M6 6l12 12" />
  </Icon>
);
const Copy = (p) => (
  <Icon {...p}>
    <rect x="9" y="9" width="11" height="11" rx="2" />
    <path d="M5 15V5a2 2 0 012-2h10" />
  </Icon>
);
const Plus = (p) => (
  <Icon {...p}>
    <path d="M12 5v14M5 12h14" />
  </Icon>
);
const Cog = (p) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="3" />
    <path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M19.1 4.9L17 7M7 17l-2.1 2.1" />
  </Icon>
);
const Plug = (p) => (
  <Icon {...p}>
    <path d="M9 3v6M15 3v6M6 9h12v3a6 6 0 01-6 6 6 6 0 01-6-6V9zM12 18v3" />
  </Icon>
);
const Bolt = (p) => (
  <Icon {...p}>
    <path d="M13 2L4 14h6l-1 8 9-12h-6l1-8z" />
  </Icon>
);
const Info = (p) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 11v5M12 8h.01" />
  </Icon>
);

/* ------------------------------------------------------------------ */
/* 流式 Markdown（无依赖，逐块渲染；代码块带 Copy）                      */
/* ------------------------------------------------------------------ */
function CodeBlock({ code, lang }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      /* 剪贴板不可用就静默失败，不打断阅读 */
    }
  };
  return (
    <div className="relative group my-2">
      <div className="flex items-center justify-between px-3 py-1 border border-white/10 border-b-0 rounded-t-md bg-black/20">
        <span className="text-[11px] text-white/40 font-mono">{lang || "text"}</span>
        <button
          onClick={copy}
          className="flex items-center gap-1 text-[11px] text-white/40 hover:text-white/80 transition-colors"
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre className="px-3 py-2 border border-white/10 rounded-b-md bg-black/30 overflow-x-auto">
        <code className="font-mono text-[12px] leading-[1.5] whitespace-pre">{code}</code>
      </pre>
    </div>
  );
}

function Markdown({ text, streaming }) {
  // 极简解析：代码块 / 标题 / 列表 / 段落。够用且没有依赖。
  const blocks = useMemo(() => splitBlocks(text || ""), [text]);
  return (
    <div className="text-[13px] leading-[1.55] text-white/85 break-words">
      {blocks.map((block, index) => {
        if (block.type === "code") {
          return <CodeBlock key={index} code={block.code} lang={block.lang} />;
        }
        if (block.type === "heading") {
          return (
            <div key={index} className="mt-3 mb-1 font-semibold text-white/90">
              {block.text}
            </div>
          );
        }
        if (block.type === "list") {
          return (
            <ul key={index} className="my-1 pl-4 list-disc marker:text-white/30">
              {block.items.map((item, i) => (
                <li key={i}>{item}</li>
              ))}
            </ul>
          );
        }
        return (
          <p key={index} className="my-1 whitespace-pre-wrap">
            {block.text}
          </p>
        );
      })}
      {streaming && (
        <span
          className="inline-block w-[7px] h-[13px] align-[-2px] ml-[2px] animate-pulse"
          style={{ background: SEMANTIC.accent }}
        />
      )}
    </div>
  );
}

function splitBlocks(text) {
  const out = [];
  const lines = text.split("\n");
  let buffer = [];
  let code = null;
  const flush = () => {
    if (buffer.length) {
      out.push({ type: "paragraph", text: buffer.join("\n") });
      buffer = [];
    }
  };
  for (const line of lines) {
    const fence = line.match(/^```(\w*)/);
    if (fence) {
      if (code === null) {
        flush();
        code = { type: "code", lang: fence[1], code: "" };
        out.push(code);
      } else {
        code = null;
      }
      continue;
    }
    if (code) {
      code.code += line + "\n";
      continue;
    }
    const heading = line.match(/^#{1,3}\s+(.*)/);
    if (heading) {
      flush();
      out.push({ type: "heading", text: heading[1] });
      continue;
    }
    const item = line.match(/^\s*[-*]\s+(.*)/);
    if (item) {
      const last = out[out.length - 1];
      if (last && last.type === "list") last.items.push(item[1]);
      else out.push({ type: "list", items: [item[1]] });
      continue;
    }
    buffer.push(line);
  }
  flush();
  return out.map((b) => (b.type === "code" ? { ...b, code: b.code.replace(/\n$/, "") } : b));
}

/* ------------------------------------------------------------------ */
/* 时间线：工具调用（折叠面板 + 状态圆点）                               */
/* ------------------------------------------------------------------ */
function ToolRow({ step }) {
  const [open, setOpen] = useState(false);
  const color =
    step.status === "running"
      ? SEMANTIC.accent
      : step.status === "ok"
        ? SEMANTIC.ok
        : SEMANTIC.err;

  return (
    <div className="relative pl-5">
      <span
        className={cx(
          "absolute left-0 top-[7px] w-[7px] h-[7px] rounded-full",
          step.status === "running" && "animate-pulse"
        )}
        style={{ background: color }}
      />
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 text-left py-[3px] text-[12px] font-mono text-white/60 hover:text-white/90 transition-colors"
      >
        <span
          className="text-[11px] transition-transform"
          style={{ transform: open ? "rotate(90deg)" : "none" }}
        >
          <Chevron size={12} />
        </span>
        <span style={{ color }}>{step.tool}</span>
        <span className="truncate text-white/40">{step.argsText}</span>
        {step.ms ? <span className="ml-auto text-[11px] text-white/30">{step.ms}ms</span> : null}
      </button>
      {open && (
        <pre className="mt-1 mb-2 ml-1 px-3 py-2 border border-white/10 rounded-md bg-black/30 font-mono text-[11.5px] leading-[1.5] text-white/50 whitespace-pre-wrap max-h-64 overflow-auto">
          {step.detail || "（无输出）"}
        </pre>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* 权限审批：默认 [允许] 有 200ms 防误触                                 */
/* ------------------------------------------------------------------ */
function PermissionPrompt({ request, onDecide }) {
  const [armed, setArmed] = useState(false);

  useEffect(() => {
    setArmed(false);
    const timer = setTimeout(() => setArmed(true), 200);
    return () => clearTimeout(timer);
  }, [request?.id]);

  useEffect(() => {
    const onKey = (event) => {
      if (event.key === "y") onDecide(true);
      if (event.key === "n") onDecide(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onDecide]);

  if (!request) return null;
  return (
    <div className="mx-3 mb-2 px-3 py-2 border border-white/10 rounded-md bg-[#1c1b19] flex items-center gap-3">
      <span style={{ color: SEMANTIC.warn }}>
        <Bolt size={13} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[12px] text-white/80 truncate">{request.title}</div>
        <div className="text-[11px] text-white/35 font-mono truncate">{request.detail}</div>
      </div>
      <button
        disabled={!armed}
        onClick={() => onDecide(true)}
        className={cx(
          "px-2.5 py-1 rounded text-[12px] border transition-colors",
          armed
            ? "border-white/15 text-white/85 hover:bg-white/10"
            : "border-white/5 text-white/25 cursor-not-allowed"
        )}
      >
        允许 <span className="text-white/30 ml-1">y</span>
      </button>
      <button
        onClick={() => onDecide(false)}
        className="px-2.5 py-1 rounded text-[12px] border border-white/10 text-white/60 hover:bg-white/10"
      >
        拒绝 <span className="text-white/30 ml-1">n</span>
      </button>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* 右侧面板：设置 / 插件 / 接入大模型 / 关于                             */
/* ------------------------------------------------------------------ */
function Panel({ tab, onClose, info, llm, onSaveLlm }) {
  const [key, setKey] = useState("");
  const [model, setModel] = useState(llm?.model || "");

  if (!tab) return null;
  return (
    <aside className="w-[340px] shrink-0 border-l border-white/10 flex flex-col">
      <header className="h-9 px-3 flex items-center justify-between border-b border-white/10">
        <span className="text-[12px] font-semibold text-white/80">
          {{ settings: "设置", plugins: "插件", llm: "接入大模型", about: "关于" }[tab]}
        </span>
        <button onClick={onClose} className="text-white/40 hover:text-white/80">
          <Cross size={13} />
        </button>
      </header>

      <div className="p-3 overflow-auto text-[12px] text-white/70 space-y-3">
        {tab === "settings" && (
          <>
            <Row label="数据源" value={info?.provider || "—"} />
            <Row label="工具数" value={String(info?.tool_count ?? "—")} />
            <Row label="专家数" value={String(info?.experts?.length ?? "—")} />
            <Row label="最大跳转" value={String(info?.max_hops ?? "—")} />
            <Row label="版本" value={`${info?.version || "—"} · ${info?.build?.sha || "dev"}`} />
            <Row label="启动于" value={info?.build?.started || "—"} />
          </>
        )}

        {tab === "plugins" && (
          <div className="space-y-2">
            {(info?.experts || []).map((item) => (
              <div key={item.agent} className="border border-white/10 rounded-md px-3 py-2">
                <div className="text-white/85">{item.label}</div>
                <div className="text-[11px] text-white/40 font-mono">
                  {item.agent} · {item.tool_count} tools
                </div>
              </div>
            ))}
          </div>
        )}

        {tab === "llm" && (
          <div className="space-y-2">
            <div className="text-[11px] text-white/40">当前：{llm?.provider || "未接入"}</div>
            <input
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder="API Key"
              className="w-full px-2 py-1.5 rounded border border-white/10 bg-black/20 font-mono text-[12px] text-white/85 placeholder:text-white/25 outline-none focus:border-white/25"
            />
            <input
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="模型名"
              className="w-full px-2 py-1.5 rounded border border-white/10 bg-black/20 font-mono text-[12px] text-white/85 placeholder:text-white/25 outline-none focus:border-white/25"
            />
            <button
              onClick={() => onSaveLlm({ apiKey: key, model })}
              className="px-3 py-1.5 rounded border border-white/15 text-[12px] text-white/85 hover:bg-white/10"
            >
              保存并生效
            </button>
          </div>
        )}

        {tab === "about" && (
          <div className="space-y-2 leading-[1.6]">
            <div className="text-white/85 font-semibold">FinancialAI-Agent 2.0</div>
            <p className="text-white/50">
              多 Agent 投研系统：研究 / 量化 / 预警三个 Agent，经审查者、证据校验与风控三道关后交付。
            </p>
            <p className="text-white/35 text-[11px]">
              数据来自公开行情接口，不构成投资建议。
            </p>
          </div>
        )}
      </div>
    </aside>
  );
}

const Row = ({ label, value }) => (
  <div className="flex items-center justify-between gap-3 py-1 border-b border-white/5 last:border-0">
    <span className="text-white/40">{label}</span>
    <span className="font-mono text-white/75 truncate">{value}</span>
  </div>
);

/* ------------------------------------------------------------------ */
/* 主组件                                                              */
/* ------------------------------------------------------------------ */
export default function ClaudeCodeUI({ apiBase = "" }) {
  const [info, setInfo] = useState(null);
  const [threads, setThreads] = useState([]);
  const [threadId, setThreadId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [steps, setSteps] = useState([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [panel, setPanel] = useState(null);
  const [permission, setPermission] = useState(null);
  const [usage, setUsage] = useState({ tokens: 0, startedAt: 0 });

  const abortRef = useRef(null);
  const draftRef = useRef(null); // 当前空会话 id：切走时若无内容就删掉
  const taRef = useRef(null);

  /* ---------------- 会话生命周期 ---------------- */
  const createThread = useCallback(async () => {
    const response = await fetch(`${apiBase}/api/threads`, { method: "POST" });
    const created = await response.json();
    setThreads((prev) => [{ id: created.id, title: "新对话", message_count: 0 }, ...prev]);
    return created.id;
  }, [apiBase]);

  const dropEmptyDraft = useCallback(async () => {
    const draft = draftRef.current;
    if (!draft) return;
    draftRef.current = null;
    // 只删「没说过话」的那条，有内容的一律保留
    await fetch(`${apiBase}/api/threads/${draft}?if_empty=1`, { method: "DELETE" });
    setThreads((prev) => prev.filter((item) => item.id !== draft));
  }, [apiBase]);

  const newChat = useCallback(async () => {
    await dropEmptyDraft();
    const id = await createThread();
    draftRef.current = id;
    setThreadId(id);
    setMessages([]);
    setSteps([]);
    taRef.current?.focus();
  }, [createThread, dropEmptyDraft]);

  const openThread = useCallback(
    async (id) => {
      if (id === threadId) return;
      await dropEmptyDraft();
      setThreadId(id);
      const data = await fetch(`${apiBase}/api/threads/${id}`).then((r) => r.json());
      setMessages(data.messages || []);
      setSteps([]);
      // 后台可能还在跑：接回来
      const runId = localStorage.getItem(`finagent.run.${id}`);
      if (runId) {
        const status = await fetch(`${apiBase}/api/runs/${runId}`).then((r) => r.json());
        if (status?.exists && !status.done) {
          resumeRun(runId);
        } else {
          localStorage.removeItem(`finagent.run.${id}`);
        }
      }
    },
    [apiBase, threadId, dropEmptyDraft]
  );

  /* ---------------- 流式接收 ---------------- */
  const handleEvent = useCallback((event, data, assistantId) => {
    if (event === "token") {
      setMessages((prev) =>
        prev.map((item) =>
          item.id === assistantId ? { ...item, text: (item.text || "") + (data.text || "") } : item
        )
      );
      setUsage((prev) => ({ ...prev, tokens: prev.tokens + Math.ceil((data.text || "").length / 4) }));
      return;
    }
    if (event === "tool") {
      setSteps((prev) => {
        const callId = data.call_id || `${data.tool}-${prev.length}`;
        const existing = prev.find((step) => step.id === callId);
        if (existing) {
          return prev.map((step) =>
            step.id === callId ? { ...step, status: "ok", ms: data.latency_ms } : step
          );
        }
        return [
          ...prev,
          {
            id: callId,
            tool: data.tool,
            argsText: compactArgs(data.args),
            status: "running",
            detail: data.preview || "",
          },
        ];
      });
      return;
    }
    if (event === "tool_result") {
      setSteps((prev) =>
        prev.map((step) =>
          step.tool === data.tool && step.status === "running"
            ? { ...step, status: data.ok ? "ok" : "err", detail: data.preview || step.detail }
            : step
        )
      );
      return;
    }
    if (event === "critic") {
      setSteps((prev) => [
        ...prev,
        {
          id: `critic-${prev.length}`,
          tool: `review:${data.label || data.agent}`,
          argsText: data.verdict === "revise" ? "打回返工" : "通过",
          status: data.verdict === "revise" ? "running" : "ok",
          detail: (data.issues || []).map((issue) => `- ${issue.detail || issue.label}`).join("\n"),
        },
      ]);
      return;
    }
    if (event === "done") {
      setMessages((prev) =>
        prev.map((item) =>
          item.id === assistantId
            ? { ...item, text: data.answer || item.text, streaming: false }
            : item
        )
      );
    }
  }, []);

  const consume = useCallback(
    async (body, assistantId) => {
      const reader = body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let split;
        while ((split = buffer.indexOf("\n\n")) !== -1) {
          const raw = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          let name = "message";
          const dataLines = [];
          raw.split("\n").forEach((line) => {
            if (line.startsWith("event:")) name = line.slice(6).trim();
            else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
          });
          if (!dataLines.length) continue;
          try {
            handleEvent(name, JSON.parse(dataLines.join("\n")), assistantId);
          } catch {
            /* 单条事件解析失败不应中断流 */
          }
        }
      }
    },
    [handleEvent]
  );

  const resumeRun = useCallback(
    async (runId) => {
      const assistantId = `assistant-${Date.now()}`;
      setMessages((prev) => [...prev, { id: assistantId, role: "assistant", text: "", streaming: true }]);
      const response = await fetch(`${apiBase}/api/stream/${runId}?after=0`);
      if (response.body) await consume(response.body, assistantId);
    },
    [apiBase, consume]
  );

  /* ---------------- 发送 ---------------- */
  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || streaming) return;

    let target = threadId;
    if (!target) target = await newChat();
    draftRef.current = null; // 一旦发出内容，这条会话就固定下来

    setInput("");
    const userMessage = { id: `user-${Date.now()}`, role: "user", text };
    const assistantId = `assistant-${Date.now()}`;
    setMessages((prev) => [
      ...prev,
      userMessage,
      { id: assistantId, role: "assistant", text: "", streaming: true },
    ]);
    setSteps([]);
    setStreaming(true);
    setUsage({ tokens: 0, startedAt: Date.now() });

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const response = await fetch(`${apiBase}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, thread_id: target }),
        signal: controller.signal,
      });
      const runId = response.headers.get("X-Run-Id");
      if (runId) localStorage.setItem(`finagent.run.${target}`, runId);
      if (response.body) await consume(response.body, assistantId);
    } catch (error) {
      if (error.name !== "AbortError") {
        setMessages((prev) =>
          prev.map((item) =>
            item.id === assistantId ? { ...item, text: `调用失败：${error.message}` } : item
          )
        );
      }
    } finally {
      abortRef.current = null;
      setStreaming(false);
      localStorage.removeItem(`finagent.run.${target}`);
      const list = await fetch(`${apiBase}/api/threads`).then((r) => r.json());
      setThreads(list.threads || []);
    }
  }, [apiBase, consume, input, newChat, streaming, threadId]);

  const interrupt = useCallback(async () => {
    abortRef.current?.abort();
    if (threadId) {
      const runId = localStorage.getItem(`finagent.run.${threadId}`);
      if (runId) await fetch(`${apiBase}/api/runs/${runId}/cancel`, { method: "POST" });
    }
    setSteps((prev) => prev.map((step) => (step.status === "running" ? { ...step, status: "err" } : step)));
  }, [apiBase, threadId]);

  /* ---------------- 键盘 ---------------- */
  useEffect(() => {
    const onKey = (event) => {
      if (event.ctrlKey && event.key.toLowerCase() === "c" && streaming) {
        event.preventDefault();
        interrupt();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [interrupt, streaming]);

  useEffect(() => {
    fetch(`${apiBase}/api/info`)
      .then((r) => r.json())
      .then(setInfo)
      .catch(() => {});
    fetch(`${apiBase}/api/threads`)
      .then((r) => r.json())
      .then((data) => setThreads(data.threads || []))
      .catch(() => {});
  }, [apiBase]);

  const elapsed = usage.startedAt ? Math.round((Date.now() - usage.startedAt) / 1000) : 0;

  /* ---------------- 渲染 ---------------- */
  return (
    <div
      className="h-screen w-screen flex flex-col font-sans text-[13px]"
      style={{ background: "#141413", color: "#faf9f5" }}
    >
      {/* 顶部状态栏 */}
      <header className="h-9 shrink-0 px-3 flex items-center gap-3 border-b border-white/10">
        <span className="text-[12px] font-semibold tracking-tight">FinancialAI</span>
        <span className="text-white/25">/</span>
        <span className="text-white/50 font-mono text-[11.5px]">
          {info?.provider || "—"} · {info?.tool_count ?? "—"} tools ·{" "}
          {info?.experts?.length ?? "—"} agents
        </span>
        <div className="ml-auto flex items-center gap-3 font-mono text-[11px] text-white/35">
          {streaming && <span style={{ color: SEMANTIC.accent }}>● running</span>}
          <span>{usage.tokens} tok</span>
          <span>{elapsed}s</span>
          {[["settings", Cog], ["plugins", Plug], ["llm", Bolt], ["about", Info]].map(
            ([name, Glyph]) => (
              <button
                key={name}
                onClick={() => setPanel(panel === name ? null : name)}
                className={cx("hover:text-white/80", panel === name && "text-white/90")}
                title={name}
              >
                <Glyph size={14} />
              </button>
            )
          )}
        </div>
      </header>

      <div className="flex-1 flex min-h-0">
        {/* 会话列表 */}
        <nav className="w-[200px] shrink-0 border-r border-white/10 flex flex-col">
          <button
            onClick={newChat}
            className="m-2 flex items-center gap-1.5 px-2 py-1.5 rounded border border-white/10 text-[12px] text-white/70 hover:bg-white/5"
          >
            <Plus size={13} /> 新建对话
          </button>
          <div className="flex-1 overflow-auto px-1">
            {threads.map((thread) => (
              <button
                key={thread.id}
                onClick={() => openThread(thread.id)}
                className={cx(
                  "w-full text-left px-2 py-1.5 rounded text-[12px] truncate transition-colors",
                  thread.id === threadId
                    ? "bg-white/10 text-white/90"
                    : "text-white/50 hover:bg-white/5 hover:text-white/75"
                )}
                title={thread.title}
              >
                {thread.title}
              </button>
            ))}
          </div>
        </nav>

        {/* 主区域 */}
        <main className="flex-1 min-w-0 flex flex-col">
          <div className="flex-1 overflow-auto px-4 py-3">
            {messages.length === 0 && (
              <div className="text-white/25 text-[12px] mt-8 text-center font-mono">
                输入问题开始。Ctrl+C 可中断。
              </div>
            )}

            {messages.map((message) =>
              message.role === "user" ? (
                <div key={message.id} className="my-3 flex gap-2">
                  <span className="text-white/25 font-mono text-[11.5px] mt-[2px]">›</span>
                  <div className="text-white/85 whitespace-pre-wrap">{message.text}</div>
                </div>
              ) : (
                <div key={message.id} className="my-3">
                  {steps.length > 0 && (
                    <div className="relative border-l border-white/10 ml-[3px] mb-2">
                      {steps.map((step) => (
                        <ToolRow key={step.id} step={step} />
                      ))}
                    </div>
                  )}
                  <Markdown text={message.text} streaming={message.streaming} />
                </div>
              )
            )}
          </div>

          {/* 权限审批 + 输入区 */}
          <PermissionPrompt request={permission} onDecide={() => setPermission(null)} />
          <div className="shrink-0 border-t border-white/10 px-3 py-2">
            <div className="flex items-end gap-2">
              <textarea
                ref={taRef}
                value={input}
                onChange={(event) => setInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    send();
                  }
                }}
                rows={2}
                placeholder="问点什么，Enter 发送，Shift+Enter 换行"
                className="flex-1 resize-none bg-transparent outline-none text-[13px] text-white/85 placeholder:text-white/25 font-sans"
              />
              <span className="font-mono text-[10.5px] text-white/25 whitespace-nowrap mb-1">
                Ctrl+C to interrupt
              </span>
            </div>
          </div>
        </main>

        <Panel
          tab={panel}
          onClose={() => setPanel(null)}
          info={info}
          llm={{ provider: info?.llm, model: "" }}
          onSaveLlm={async (payload) => {
            await fetch(`${apiBase}/api/llm`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(payload),
            });
            setPanel(null);
          }}
        />
      </div>
    </div>
  );
}

const compactArgs = (args) => {
  if (!args) return "";
  const entries = Object.entries(args);
  if (!entries.length) return "";
  return entries
    .slice(0, 3)
    .map(([key, value]) => `${key}=${typeof value === "string" ? value : JSON.stringify(value)}`)
    .join(" ");
};
