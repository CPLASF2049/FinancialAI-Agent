/* Fin-Agent 聊天界面前端。
 *
 * 没有构建步骤、不依赖 CDN：一个 markdown 渲染器 + 一个 SSE 解析器，就能把
 * LangGraph 的事件流变成会"逐字长出来"的回答。离线也能用。
 */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // 欢迎页的模板：resetView 会清空消息区，所以必须在启动时留一份克隆源，
  // 否则第二次「新建对话」时会因为找不到 #welcome 而崩。
  const welcomeTemplate = document.getElementById("welcome").cloneNode(true);

  const state = {
    threadId: null,
    threads: [],
    streaming: false,
    controller: null,
    tokens: "",
    seenToken: false,
    agents: [],
    tools: [],
    llmEnabled: false,
    llmHasKey: false,
    decisions: [],
  };

  // ------------------------------------------------------------------
  // 极简 Markdown 渲染
  // 先整体转义 HTML，再只注入我们自己生成的标签 —— 模型输出永远不会被当 HTML 执行。
  // ------------------------------------------------------------------
  const escapeHtml = (text) =>
    String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");

  function inline(text) {
    let out = escapeHtml(text);
    out = out.replace(/`([^`]+)`/g, (_, code) => `<code>${code}</code>`);
    out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, label, url) => `<a href="${url}" target="_blank" rel="noreferrer">${label}</a>`);
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
    out = out.replace(/(^|[^_])_([^_\n]+)_(?!_)/g, "$1<em>$2</em>");
    return out;
  }

  function isTableRow(line) {
    return /^\s*\|.*\|\s*$/.test(line);
  }
  function isTableDivider(line) {
    return /^\s*\|?(\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$/.test(line);
  }
  function splitRow(line) {
    return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  }

  function renderMarkdown(source) {
    const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
    const html = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];

      // 围栏代码块
      const fence = line.match(/^\s*```(\w*)\s*$/);
      if (fence) {
        const lang = fence[1];
        const buf = [];
        i += 1;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
          buf.push(lines[i]);
          i += 1;
        }
        i += 1;
        html.push(`<pre><code data-lang="${escapeHtml(lang)}">${escapeHtml(buf.join("\n"))}</code></pre>`);
        continue;
      }

      // 表格
      if (isTableRow(line) && i + 1 < lines.length && isTableDivider(lines[i + 1])) {
        const head = splitRow(line);
        i += 2;
        const rows = [];
        while (i < lines.length && isTableRow(lines[i])) {
          rows.push(splitRow(lines[i]));
          i += 1;
        }
        const thead = head.map((c) => `<th>${inline(c)}</th>`).join("");
        const tbody = rows
          .map((row) => `<tr>${row.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`)
          .join("");
        html.push(`<table><thead><tr>${thead}</tr></thead><tbody>${tbody}</tbody></table>`);
        continue;
      }

      // 标题
      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        const level = Math.min(heading[1].length, 4);
        html.push(`<h${level}>${inline(heading[2])}</h${level}>`);
        i += 1;
        continue;
      }

      // 分隔线
      if (/^\s*(---+|\*\*\*+|___+)\s*$/.test(line)) {
        html.push("<hr />");
        i += 1;
        continue;
      }

      // 引用
      if (/^\s*>\s?/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          buf.push(lines[i].replace(/^\s*>\s?/, ""));
          i += 1;
        }
        html.push(`<blockquote>${inline(buf.join(" "))}</blockquote>`);
        continue;
      }

      // 列表（有序 / 无序，支持一层缩进）
      if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
        const ordered = /^\s*\d+\.\s+/.test(line);
        const items = [];
        while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, ""));
          i += 1;
        }
        const tag = ordered ? "ol" : "ul";
        html.push(`<${tag}>${items.map((item) => `<li>${inline(item)}</li>`).join("")}</${tag}>`);
        continue;
      }

      // 空行
      if (!line.trim()) {
        i += 1;
        continue;
      }

      // 段落（吃到空行为止）
      const buf = [];
      while (
        i < lines.length &&
        lines[i].trim() &&
        !/^\s*```/.test(lines[i]) &&
        !/^(#{1,6})\s+/.test(lines[i]) &&
        !/^\s*([-*+]|\d+\.)\s+/.test(lines[i]) &&
        !/^\s*>\s?/.test(lines[i]) &&
        !isTableRow(lines[i])
      ) {
        buf.push(lines[i]);
        i += 1;
      }
      if (buf.length) html.push(`<p>${inline(buf.join(" "))}</p>`);
      else i += 1;
    }
    return html.join("\n");
  }

  // ------------------------------------------------------------------
  // 会话列表
  // ------------------------------------------------------------------
  async function loadThreads() {
    try {
      const res = await fetch("/api/threads");
      const data = await res.json();
      state.threads = data.threads || [];
      renderThreads();
    } catch (err) {
      console.warn("加载会话失败", err);
    }
  }

  function renderThreads() {
    const list = $("threadList");
    list.innerHTML = "";
    state.threads.forEach((thread) => {
      const row = document.createElement("div");
      row.className = "thread" + (thread.id === state.threadId ? " active" : "");
      row.title = thread.preview || thread.title;

      const label = document.createElement("div");
      label.className = "thread-title";
      label.textContent = thread.title || "新对话";
      row.appendChild(label);

      const del = document.createElement("button");
      del.className = "thread-del";
      del.textContent = "×";
      del.title = "删除";
      del.addEventListener("click", async (event) => {
        event.stopPropagation();
        await deleteThread(thread.id);
      });
      row.appendChild(del);

      row.addEventListener("click", () => openThread(thread.id));
      list.appendChild(row);
    });
  }

  async function newThread() {
    const res = await fetch("/api/threads", { method: "POST" });
    const created = await res.json();
    state.threadId = created.id;
    localStorage.setItem("finagent.thread", created.id);
    resetView();
    await loadThreads();
    $("input").focus();
  }

  async function deleteThread(id) {
    await fetch(`/api/threads/${id}`, { method: "DELETE" });
    if (state.threadId === id) {
      state.threadId = null;
      localStorage.removeItem("finagent.thread");
      resetView();
    }
    await loadThreads();
  }

  async function openThread(id) {
    state.threadId = id;
    localStorage.setItem("finagent.thread", id);
    try {
      const res = await fetch(`/api/threads/${id}`);
      if (!res.ok) throw new Error("会话不存在");
      const data = await res.json();
      resetView();
      $("threadTitle").textContent = data.title || "新对话";
      (data.messages || []).forEach((message) => {
        appendMessage(message.role, message, { animate: false });
      });
      scrollToBottom();
    } catch (err) {
      console.warn(err);
      resetView();
    }
    await loadThreads();
  }

  // ------------------------------------------------------------------
  // 视图
  // ------------------------------------------------------------------
  function resetView() {
    const container = $("messages");
    container.innerHTML = "";
    const fresh = welcomeTemplate.cloneNode(true);
    container.appendChild(fresh);
    fresh.querySelectorAll("[data-q]").forEach((button) => {
      button.addEventListener("click", () => send(button.dataset.q));
    });
    $("threadTitle").textContent = "新对话";
  }

  function ensureConversation() {
    const container = $("messages");
    if (!$("threadTitle") || !container.querySelector(".messages-inner")) {
      container.innerHTML = '<div class="messages-inner"></div>';
    }
    return container.querySelector(".messages-inner");
  }

  function appendMessage(role, payload, options = {}) {
    const animate = options.animate !== false;
    const container = $("messages");
    const welcome = container.querySelector(".welcome");
    if (welcome) welcome.remove();
    const inner = ensureConversation();

    if (role === "user") {
      const wrap = document.createElement("div");
      wrap.className = "msg user";
      const bubble = document.createElement("div");
      bubble.className = "bubble";
      bubble.textContent = payload.text || "";
      wrap.appendChild(bubble);
      inner.appendChild(wrap);
      scrollToBottom();
      return { wrap, bubble };
    }

    const wrap = document.createElement("div");
    wrap.className = "msg ai";

    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.textContent = "FA";
    wrap.appendChild(avatar);

    const bubble = document.createElement("div");
    bubble.className = "bubble";

    const meta = document.createElement("div");
    meta.className = "meta";
    bubble.appendChild(meta);

    // 决策轨迹：路由 / 审查 / 风控 —— 结论是怎么被逼出来的
    const decision = document.createElement("details");
    decision.className = "trace decision hidden";
    const decisionSummary = document.createElement("summary");
    const decisionBody = document.createElement("div");
    decisionBody.className = "trace-body";
    decision.appendChild(decisionSummary);
    decision.appendChild(decisionBody);
    bubble.appendChild(decision);

    // 执行过程：工具调用明细
    const trace = document.createElement("details");
    trace.className = "trace hidden";
    const summary = document.createElement("summary");
    const traceBody = document.createElement("div");
    traceBody.className = "trace-body";
    trace.appendChild(summary);
    trace.appendChild(traceBody);
    bubble.appendChild(trace);

    const text = document.createElement("div");
    text.className = "body-text";
    bubble.appendChild(text);

    wrap.appendChild(bubble);
    inner.appendChild(wrap);

    // 已有历史（重新打开会话时）直接渲染成完整形态
    if (!animate) {
      (payload.agents || []).forEach((agent) => {
        meta.appendChild(badge(agent));
      });
      (payload.tools || []).forEach((tool) => addToolLine(trace, summary, traceBody, tool));
      (payload.decisions || []).forEach((item) =>
        addDecisionLine(decision, decisionSummary, decisionBody, item)
      );
      text.innerHTML = renderMarkdown(payload.text || "");
      if (!(payload.agents || []).length && !(payload.tools || []).length) {
        meta.remove();
      }
      if (!(payload.tools || []).length) trace.remove();
      if (!(payload.decisions || []).length) decision.remove();
    }

    scrollToBottom();
    return {
      wrap,
      bubble,
      meta,
      trace,
      summary,
      traceBody,
      decision,
      decisionSummary,
      decisionBody,
      text,
    };
  }

  function badge(label) {
    const span = document.createElement("span");
    span.className = "badge";
    span.textContent = label;
    return span;
  }

  function addToolLine(trace, summary, traceBody, payload) {
    trace.classList.remove("hidden");
    const line = document.createElement("div");
    line.className = "trace-line";

    const mark = document.createElement("span");
    mark.className = payload.ok === false ? "err" : "ok";
    mark.textContent = payload.ok === false ? "✗" : "✓";
    line.appendChild(mark);

    const name = document.createElement("span");
    name.className = "tool";
    name.textContent = payload.tool || "";
    line.appendChild(name);

    const args = document.createElement("span");
    args.className = "args";
    args.textContent = summariseArgs(payload.args);
    line.appendChild(args);

    traceBody.appendChild(line);
    const total = traceBody.querySelectorAll(".trace-line").length;
    summary.textContent = `执行过程 · ${total} 次工具调用`;
  }

  /* 一条决策记录：路由 / 审查 / 风控。比工具调用更「语义」，所以单独一块。 */
  function addDecisionLine(container, summary, body, item) {
    const step = item.step || "route";

    // 同一路由重复出现（supervisor 可能多轮）时只保留有理由的那条
    if (step === "route") {
      const dup = [...body.children].some(
        (node) => node.dataset.step === "route" && node.dataset.agent === item.agent
      );
      if (dup) return;
    }

    container.classList.remove("hidden");
    const line = document.createElement("div");
    line.className = "trace-line";
    line.dataset.step = step;
    if (item.agent) line.dataset.agent = item.agent;

    const mark = document.createElement("span");
    mark.className = "dec-mark";
    const head = document.createElement("span");
    head.className = "dec-head";
    const detail = document.createElement("span");
    detail.className = "args";

    if (step === "route") {
      mark.textContent = "→";
      mark.classList.add("dec-route");
      head.textContent = `交给 ${item.label || item.agent || ""}`;
      detail.textContent = item.reason || "";
    } else if (step === "critic") {
      const passed = item.verdict !== "revise";
      mark.textContent = passed ? "✓" : "↺";
      mark.classList.add(passed ? "ok" : "dec-revise");
      const issues = item.issues || [];
      head.textContent = passed
        ? `审查通过 · ${item.label || ""}`
        : `退回返工 · ${item.label || ""}`;
      detail.textContent = issues.length
        ? issues.map((issue) => issue.detail || issue.label).join("；")
        : "未发现问题";
    } else if (step === "risk") {
      mark.textContent = "🛡";
      mark.classList.add("dec-risk");
      const flags = item.flags || [];
      head.textContent = `风控命中 ${flags.length} 条`;
      detail.textContent = flags.map((flag) => flag.label || flag.code).join("、");
    } else {
      mark.textContent = "·";
      head.textContent = item.label || step;
      detail.textContent = "";
    }

    line.appendChild(mark);
    line.appendChild(head);
    line.appendChild(detail);
    body.appendChild(line);

    const total = body.querySelectorAll(".trace-line").length;
    summary.textContent = `决策轨迹 · ${total} 步`;
  }

  function summariseArgs(args) {
    if (!args || typeof args !== "object") return "";
    const entries = Object.entries(args).slice(0, 3);
    if (!entries.length) return "";
    return entries
      .map(([key, value]) => `${key}=${truncate(typeof value === "object" ? JSON.stringify(value) : value, 40)}`)
      .join("  ");
  }

  function truncate(text, limit) {
    const str = String(text);
    return str.length > limit ? str.slice(0, limit) + "…" : str;
  }

  function scrollToBottom() {
    const container = $("messages");
    requestAnimationFrame(() => {
      container.scrollTop = container.scrollHeight;
    });
  }

  // ------------------------------------------------------------------
  // 发送与流式接收
  // ------------------------------------------------------------------
  async function send(text) {
    const message = (text || $("input").value || "").trim();
    if (!message || state.streaming) return;

    $("input").value = "";
    autoGrow();
    setStreaming(true);

    appendMessage("user", { text: message });

    if (!state.threadId) {
      const res = await fetch("/api/threads", { method: "POST" });
      state.threadId = (await res.json()).id;
      localStorage.setItem("finagent.thread", state.threadId);
    }

    const view = appendMessage("assistant", { text: "" });
    const thinking = document.createElement("div");
    thinking.className = "thinking";
    thinking.innerHTML = '<span class="dots"><i></i><i></i><i></i></span><span>正在理解你的问题…</span>';
    view.bubble.insertBefore(thinking, view.text);

    state.tokens = "";
    state.seenToken = false;
    state.agents = [];
    state.tools = [];
    state.decisions = [];
    state.controller = new AbortController();

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          thread_id: state.threadId,
          user_id: localStorage.getItem("finagent.user") || "web",
        }),
        signal: state.controller.signal,
      });

      if (!response.ok || !response.body) {
        throw new Error(`服务返回 ${response.status}`);
      }

      await consumeStream(response.body, { view, thinking });
    } catch (err) {
      if (err.name === "AbortError") {
        view.text.classList.remove("streaming");
        view.text.innerHTML = renderMarkdown(state.tokens || "_（已停止）_");
      } else {
        thinking.remove();
        const box = document.createElement("div");
        box.className = "error-box";
        box.textContent = `出了点问题：${err.message}`;
        view.bubble.appendChild(box);
      }
    } finally {
      setStreaming(false);
      state.controller = null;
      state.threadId && localStorage.setItem("finagent.thread", state.threadId);
      await loadThreads();
    }
  }

  async function consumeStream(body, ctx) {
    const reader = body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let finished = false;

    while (!finished) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let split;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const raw = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        const parsed = parseEvent(raw);
        if (!parsed) continue;
        handleEvent(parsed.event, parsed.data, ctx);
        if (parsed.event === "done" || parsed.event === "error") finished = true;
      }
    }
  }

  function parseEvent(raw) {
    let event = "message";
    const dataLines = [];
    raw.split("\n").forEach((line) => {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    });
    if (!dataLines.length) return null;
    try {
      return { event, data: JSON.parse(dataLines.join("\n")) };
    } catch {
      return null;
    }
  }

  function handleEvent(event, data, ctx) {
    const { view, thinking } = ctx;

    switch (event) {
      case "route": {
        if (data.label) {
          addBadgeOnce(view.meta, data.label);
          addDecisionLine(view.decision, view.decisionSummary, view.decisionBody, {
            step: "route",
            agent: data.agent,
            label: data.label,
            reason: data.reason,
          });
          state.decisions.push({ step: "route", ...data });
          const hint = thinking.querySelector("span:last-child");
          if (hint) hint.textContent = `交给「${data.label}」处理…`;
        }
        break;
      }
      case "critic": {
        addDecisionLine(view.decision, view.decisionSummary, view.decisionBody, {
          step: "critic",
          agent: data.agent,
          label: data.label,
          verdict: data.verdict,
          issues: data.issues,
        });
        state.decisions.push({ step: "critic", ...data });
        break;
      }
      case "risk": {
        addDecisionLine(view.decision, view.decisionSummary, view.decisionBody, {
          step: "risk",
          flags: data.flags,
          actions: data.actions,
        });
        state.decisions.push({ step: "risk", ...data });
        break;
      }
      case "agent": {
        const hint = thinking.querySelector("span:last-child");
        if (hint && data.label) hint.textContent = `「${data.label}」正在查数据…`;
        break;
      }
      case "tool": {
        thinking.remove();
        if (!view.text.textContent) {
          const hint = document.createElement("div");
          hint.className = "thinking";
          hint.dataset.role = "tool-hint";
          hint.innerHTML = '<span class="dots"><i></i><i></i><i></i></span><span>正在调用工具…</span>';
          view.bubble.insertBefore(hint, view.text);
        }
        break;
      }
      case "tool_result": {
        addToolLine(view.trace, view.summary, view.traceBody, data);
        state.tools.push(data);
        const hint = view.bubble.querySelector('[data-role="tool-hint"]');
        if (hint) hint.querySelector("span:last-child").textContent = "正在整理结果…";
        break;
      }
      case "token": {
        thinking.remove();
        const hint = view.bubble.querySelector('[data-role="tool-hint"]');
        if (hint) hint.remove();
        state.tokens += data.text || "";
        state.seenToken = true;
        view.text.classList.add("streaming");
        view.text.textContent = state.tokens;
        view.text.appendChild(cursor());
        scrollToBottom();
        break;
      }
      case "memory": {
        const updates = data.updates || {};
        if (Object.keys(updates).length) {
          addBadgeOnce(view.meta, "已记住偏好");
        }
        break;
      }
      case "done": {
        thinking.remove();
        if (!view.decisionBody.children.length && (data.decisions || []).length) {
          data.decisions.forEach((item) =>
            addDecisionLine(view.decision, view.decisionSummary, view.decisionBody, item)
          );
        }
        view.bubble.querySelectorAll('[data-role="tool-hint"]').forEach((n) => n.remove());
        (data.labels || []).forEach((label) => addBadgeOnce(view.meta, label));
        if (!view.meta.children.length) view.meta.remove();
        view.text.classList.remove("streaming");
        if (!state.seenToken && data.answer) {
          revealText(view.text, data.answer);
        } else {
          view.text.innerHTML = renderMarkdown(data.answer || state.tokens);
        }
        if (data.errors && data.errors.length) {
          const box = document.createElement("div");
          box.className = "error-box";
          box.textContent = data.errors.join("；");
          view.bubble.appendChild(box);
        }
        scrollToBottom();
        break;
      }
      case "error": {
        thinking.remove();
        const box = document.createElement("div");
        box.className = "error-box";
        box.textContent = data.message || "未知错误";
        view.bubble.appendChild(box);
        break;
      }
      default:
        break;
    }
  }

  function addBadgeOnce(meta, label) {
    const exists = [...meta.children].some((child) => child.textContent === label);
    if (!exists) meta.appendChild(badge(label));
  }

  /* 确定性模式下服务端一次给全文，这里在前端做「逐字出现」。比服务端 sleep 可靠：
     网络与代理缓冲会把带间隔的多个 chunk 合并成一个，观感就没了。 */
  function revealText(element, text) {
    const total = text.length;
    const step = Math.max(2, Math.ceil(total / 90)); // 大约 90 帧画完
    let index = 0;
    const tick = () => {
      index = Math.min(total, index + step);
      element.textContent = text.slice(0, index);
      element.appendChild(cursor());
      scrollToBottom();
      if (index < total) {
        requestAnimationFrame(tick);
      } else {
        element.innerHTML = renderMarkdown(text);
        scrollToBottom();
      }
    };
    requestAnimationFrame(tick);
  }

  function cursor() {
    const span = document.createElement("span");
    span.className = "cursor";
    span.dataset.cursor = "1";
    return span;
  }

  function setStreaming(value) {
    state.streaming = value;
    $("send").classList.toggle("hidden", value);
    $("stop").classList.toggle("hidden", !value);
    $("send").disabled = value;
  }

  // ------------------------------------------------------------------
  // 顶部状态
  // ------------------------------------------------------------------
  async function loadInfo() {
    try {
      const info = await (await fetch("/api/info")).json();
      const llmOn = info.llm && info.llm.enabled;
      $("providerPill").textContent =
        info.provider === "tushare" ? "Tushare 行情" : "离线演示数据";
      $("llmPill").textContent = llmOn
        ? `${info.llm.provider} · ${info.llm.model}`
        : "未接大模型 · 点这里接入";
      $("llmPill").classList.toggle("on", !!llmOn);
      $("openLlm").classList.toggle("attention", !llmOn);

      const pills = $("statusPills");
      pills.innerHTML = "";
      pills.appendChild(pill(`${info.tool_count} 个工具`));
      pills.appendChild(pill(`${(info.experts || []).length} 个专家`));
      if (!llmOn) {
        const tip = pill("点这里接入大模型");
        tip.classList.add("action");
        tip.title = "填入 API Key 即可启用真实推理";
        tip.addEventListener("click", openLlmModal);
        pills.appendChild(tip);
      }
      state.llmEnabled = !!llmOn;
    } catch (err) {
      console.warn("加载状态失败", err);
    }
  }

  function pill(text) {
    const span = document.createElement("span");
    span.className = "pill";
    span.textContent = text;
    return span;
  }

  // ------------------------------------------------------------------
  // 接入大模型
  // ------------------------------------------------------------------
  let llmPresets = [];

  function presetOf(id) {
    return llmPresets.find((item) => item.id === id) || null;
  }

  function applyPreset(id, { keepFields = false } = {}) {
    const preset = presetOf(id);
    if (!preset) return;
    if (!keepFields) {
      $("llmModel").value = "";
      $("llmBaseUrl").value = "";
    }
    const isCustom = id === "custom";
    $("llmModel").placeholder = preset.model || "模型名，例如 generalv3.5";
    $("llmBaseUrl").placeholder = preset.base_url || "https://你的接口地址/v1";
    $("llmKeyTip").textContent = preset.hint || "";
    $("llmModelTip").textContent = preset.models
      ? `可选：${preset.models}`
      : isCustom
        ? "必填。填服务商文档里的 model 名称"
        : "留空则用该厂商的默认模型";
    // Key 框始终可编辑：本地模型和自建网关有时也要填一个（哪怕是占位值）
    $("llmKey").disabled = false;
    if (isCustom) {
      $("llmKey").placeholder = "需要就填，不需要可以留空";
    } else if (!preset.needs_key) {
      $("llmKey").placeholder = "本地模型通常不需要 Key";
    } else {
      $("llmKey").placeholder = "sk-...";
    }
  }

  function setStatus(kind, message) {
    const box = $("llmStatus");
    box.className = `modal-status show ${kind}`;
    box.textContent = message;
  }

  function clearStatus() {
    const box = $("llmStatus");
    box.className = "modal-status";
    box.textContent = "";
  }

  async function openLlmModal() {
    clearStatus();
    $("llmModal").classList.remove("hidden");
    $("llmSave").disabled = true;

    try {
      const state_ = await (await fetch("/api/llm")).json();
      llmPresets = state_.presets || [];

      const select = $("llmProvider");
      select.innerHTML = "";
      llmPresets.forEach((preset) => {
        const option = document.createElement("option");
        option.value = preset.id;
        option.textContent = preset.label;
        select.appendChild(option);
      });

      const current = state_.provider && state_.provider !== "none" ? state_.provider : "deepseek";
      select.value = current;
      applyPreset(current, { keepFields: true });

      if (state_.model) $("llmModel").value = state_.model;
      if (state_.base_url) $("llmBaseUrl").value = state_.base_url;

      $("llmKey").value = "";
      $("llmKey").placeholder = state_.has_key
        ? `已保存：${state_.key_masked}（留空表示不修改）`
        : "sk-...";
      state.llmHasKey = !!state_.has_key;
      state.llmEnabled = !!state_.enabled;

      $("llmEnvPath").textContent = state_.env_file || "（未知）";

      if (state_.env_overrides && state_.env_overrides.length) {
        setStatus(
          "info",
          `注意：进程环境变量里已有 ${state_.env_overrides.join("、")}，` +
            "它的优先级高于配置文件，保存后仍以环境变量为准。"
        );
      }
    } catch (err) {
      setStatus("err", `读取配置失败：${err.message}`);
    } finally {
      $("llmSave").disabled = false;
      $("llmKey").focus();
    }
  }

  function closeLlmModal() {
    $("llmModal").classList.add("hidden");
  }

  async function saveLlm() {
    const provider = $("llmProvider").value;
    const apiKey = $("llmKey").value.trim();
    const preset = presetOf(provider);

    if (preset && preset.needs_key && !preset.key_optional && !apiKey && !state.llmHasKey) {
      setStatus("err", "请填写 API Key");
      $("llmKey").focus();
      return;
    }
    if (provider === "custom" && !$("llmBaseUrl").value.trim()) {
      setStatus("err", "「自定义」需要填接口地址");
      $("llmBaseUrl").focus();
      return;
    }
    if (provider === "custom" && !$("llmModel").value.trim()) {
      setStatus("err", "「自定义」需要填模型名");
      $("llmModel").focus();
      return;
    }

    $("llmSave").disabled = true;
    setStatus("info", "正在保存并测试连接…");

    try {
      const response = await fetch("/api/llm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider,
          api_key: apiKey || undefined,
          model: $("llmModel").value.trim() || undefined,
          base_url: $("llmBaseUrl").value.trim() || undefined,
          test: true,
        }),
      });
      const data = await response.json();

      if (!response.ok || !data.ok) {
        setStatus("err", data.message || `保存失败（${response.status}）`);
        return;
      }

      const test = data.test;
      if (test && test.ok) {
        setStatus("ok", `${data.message}。${test.message}`);
      } else if (test) {
        setStatus("err", `${data.message}，但连接测试没通过：${test.message}`);
      } else {
        setStatus("ok", data.message);
      }

      state.llmHasKey = !!(data.state && data.state.has_key);
      await loadInfo();
      setTimeout(closeLlmModal, test && test.ok ? 1200 : 2600);
    } catch (err) {
      setStatus("err", `保存失败：${err.message}`);
    } finally {
      $("llmSave").disabled = false;
    }
  }

  // ------------------------------------------------------------------
  // 输入框
  // ------------------------------------------------------------------
  function autoGrow() {
    const input = $("input");
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 180) + "px";
  }

  function bind() {
    $("composer").addEventListener("submit", (event) => {
      event.preventDefault();
      send();
    });

    $("input").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        send();
      }
    });
    $("input").addEventListener("input", autoGrow);

    $("newChat").addEventListener("click", newThread);

    $("stop").addEventListener("click", () => {
      if (state.controller) state.controller.abort();
    });

    $("toggleSidebar").addEventListener("click", () => {
      $("sidebar").classList.toggle("open");
    });

    // ---- 接入大模型 ----
    $("openLlm").addEventListener("click", openLlmModal);
    $("llmPill").addEventListener("click", openLlmModal);
    $("llmClose").addEventListener("click", closeLlmModal);
    $("llmCancel").addEventListener("click", closeLlmModal);
    $("llmSave").addEventListener("click", saveLlm);

    $("llmModal").addEventListener("click", (event) => {
      if (event.target === $("llmModal")) closeLlmModal(); // 点遮罩关闭
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !$("llmModal").classList.contains("hidden")) {
        closeLlmModal();
      }
    });

    $("llmToggleKey").addEventListener("click", () => {
      const input = $("llmKey");
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      $("llmToggleKey").textContent = show ? "隐藏" : "显示";
    });

    $("llmProvider").addEventListener("change", (event) => {
      clearStatus();
      applyPreset(event.target.value);
      $("llmKey").value = "";
      $("llmKey").focus();
    });

    document.querySelectorAll("#welcome [data-q]").forEach((button) => {
      button.addEventListener("click", () => send(button.dataset.q));
    });
  }

  // ------------------------------------------------------------------
  async function boot() {
    bind();
    await loadInfo();
    await loadThreads();

    const last = localStorage.getItem("finagent.thread");
    if (last && state.threads.some((thread) => thread.id === last)) {
      await openThread(last);
    }
    $("input").focus();
  }

  boot();
})();
