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
    // 空会话不显示。历史上「新对话」每点一次就在服务端建一个空会话，点几次堆几个，
    // 刷新后满屏「新对话」。现在前端改成发送时才建会话，老的那批也在这里藏掉。
    const visible = state.threads.filter(
      (thread) => thread.message_count > 0 || thread.id === state.threadId
    );
    visible.forEach((thread) => {
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
    // 规则：
    // 1) 点「新建对话」-> 立刻建一条空会话，这样切走再切回也能看到它
    // 2) 如果上一条也是空的（没说过话），先删掉它，避免堆一排空对话
    // 3) 一旦发出第一条消息，这条会话就固定下来，标题由服务端从内容提炼
    if (state.streaming) stopStream();

    const previous = state.threadId;
    if (previous && state.threadEmpty) {
      await fetch(`/api/threads/${previous}?if_empty=1`, { method: "DELETE" });
    }

    const res = await fetch("/api/threads", { method: "POST" });
    const created = await res.json();
    state.threadId = created.id;
    state.threadEmpty = true;
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
    if (state.threadId && state.threadId !== id && state.threadEmpty) {
      await fetch(`/api/threads/${state.threadId}?if_empty=1`, { method: "DELETE" });
      state.threadEmpty = false;
    }
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
    // 这个会话可能还有后台在跑的任务：接回来，而不是让它白跑
    await resumeRun(id);
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
    if (!message) return;
    if (state.streaming) {
      // 以前这里直接 return：用户点「发送」毫无反应，就会以为界面卡死了
      showToast("正在生成中，先点「停止」再发送");
      return;
    }

    $("input").value = "";
    autoGrow();
    setStreaming(true);
    // 说过话就不再是空会话，切走也不会被删
    state.threadEmpty = false;

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

      // 记住这次的 run_id：切走 / 刷新后靠它重连，而不是重跑
      const runId = response.headers.get("X-Run-Id");
      if (runId) {
        state.runId = runId;
        state.lastSeq = 0;
        if (state.threadId) {
          localStorage.setItem(`finagent.run.${state.threadId}`, runId);
        }
      }

      armWatchdog({ view, thinking });
      await consumeStream(response.body, { view, thinking });
    } catch (err) {
      if (err.name === "AbortError") {
        view.text.classList.remove("streaming");
        thinking.remove();
        if (state.abortReason === "timeout") {
          // 后台 2 分钟没有任何输出：主动收尾，把界面还给用户，
          // 而不是让「发送」按钮永远禁用（这就是「点不动」的来源）
          view.text.innerHTML = renderMarkdown(
            (state.tokens || "") + "\n\n> ⚠️ 后台 2 分钟没有响应，已自动停止。可直接重发。"
          );
          state.abortReason = "";
        } else {
          view.text.innerHTML = renderMarkdown(state.tokens || "_（已停止）_");
        }
      } else {
        thinking.remove();
        const box = document.createElement("div");
        box.className = "error-box";
        box.textContent = `出了点问题：${err.message}`;
        view.bubble.appendChild(box);
      }
      // 正常收尾：这条 thread 不再有待恢复的 run
      if (state.threadId) {
        localStorage.removeItem(`finagent.run.${state.threadId}`);
      }
    } finally {
      clearWatchdog();
      setStreaming(false);
      state.controller = null;
      state.threadId && localStorage.setItem("finagent.thread", state.threadId);
      await loadThreads();
    }
  }

  // ------------------------------------------------------------------
  // 看门狗：SSE 长时间没有任何事件 = 后台卡住了，必须自己收尾
  // ------------------------------------------------------------------
  //: 多久没收到事件就认为后台失联（毫秒）。单次 LLM 调用最长 90 秒，留一点余量。
  const WATCHDOG_MS = 120000;
  let watchdogTimer = null;
  let watchdogStart = 0;

  function armWatchdog(ctx) {
    watchdogStart = Date.now();
    state.lastEventAt = Date.now();
    clearInterval(watchdogTimer);
    watchdogTimer = setInterval(() => {
      const silent = Date.now() - state.lastEventAt;
      const total = Date.now() - watchdogStart;
      // 把「已经跑了多久」显示出来，用户能分清「在跑」和「卡死」
      const hint = ctx.thinking.querySelector("span:last-child");
      if (hint && hint.textContent && !hint.textContent.includes("×")) {
        const seconds = Math.round(total / 1000);
        hint.textContent = hint.textContent.replace(/（\d+ 秒）$/, "") + `（${seconds} 秒）`;
      }
      if (silent > WATCHDOG_MS) {
        clearWatchdog();
        state.abortReason = "timeout";
        if (state.controller) state.controller.abort();
      }
    }, 1000);
  }

  function clearWatchdog() {
    clearInterval(watchdogTimer);
    watchdogTimer = null;
  }

  function showToast(text) {
    let box = $("toast");
    if (!box) {
      box = document.createElement("div");
      box.id = "toast";
      box.className = "toast";
      document.body.appendChild(box);
    }
    box.textContent = text;
    box.classList.add("show");
    clearTimeout(box._timer);
    box._timer = setTimeout(() => box.classList.remove("show"), 2600);
  }

  // ------------------------------------------------------------------
  // 断线重连：后端在跑，前端回来接着看
  // ------------------------------------------------------------------
  async function resumeRun(threadId) {
    const runId = localStorage.getItem(`finagent.run.${threadId}`);
    if (!runId) return false;

    try {
      const status = await fetch(`/api/runs/${runId}`).then((r) => r.json());
      if (!status || status.exists === false || status.done) {
        localStorage.removeItem(`finagent.run.${threadId}`);
        return false;
      }
    } catch {
      return false;
    }

    // 后台还在跑：开一个气泡接上，不重新发请求
    const view = appendMessage("assistant", { text: "" });
    const thinking = document.createElement("div");
    thinking.className = "thinking";
    thinking.innerHTML =
      '<span class="dots"><i></i><i></i><i></i></span><span>后台仍在处理，正在接回结果…</span>';
    view.bubble.insertBefore(thinking, view.text);

    state.tokens = "";
    state.streaming = true;
    setStreaming(true);
    try {
      const response = await fetch(`/api/stream/${runId}?after=0`);
      if (response.body) {
        armWatchdog({ view, thinking });
        await consumeStream(response.body, { view, thinking });
      }
    } catch (err) {
      thinking.remove();
      const box = document.createElement("div");
      box.className = "error-box";
      box.textContent = `接回失败：${err.message}`;
      view.bubble.appendChild(box);
    } finally {
      clearWatchdog();
      setStreaming(false);
      state.streaming = false;
      localStorage.removeItem(`finagent.run.${threadId}`);
      await loadThreads();
    }
    return true;
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
        state.lastEventAt = Date.now();
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
        if (data.verdict === "revise") {
          // 返工 = 这一轮的结论作废。不清空的话，新结论会接在旧结论后面，
          // 最后 done 再整体覆盖，看起来就像「生成了两遍又被删掉」。
          beginAttempt(view, (data.revision || 0) + 1);
        }
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
        view.bubble.querySelectorAll(".attempt-hint").forEach((n) => n.remove());
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
        view.bubble.querySelectorAll(".attempt-hint").forEach((n) => n.remove());
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
        if (data.stop_reason) {
          const note = document.createElement('div');
          note.className = 'stop-note';
          const reason = {
            time: '时间预算',
            steps: '步数预算',
            hops: '跳转次数',
          }[data.stop_reason] || data.stop_reason;
          note.textContent = `本轮因${reason}提前收尾，结论可能不完整。`;
          view.bubble.appendChild(note);
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

  /* 开始新一稿：清空上一稿的流式文本，并挂一条「正在重做」的提示。
     返工（critic=revise）后调用，保证一个气泡里只显示**最终那一稿**。 */
  function beginAttempt(view, attempt) {
    state.tokens = "";
    view.text.classList.remove("streaming");
    view.text.textContent = "";
    view.bubble.querySelectorAll("[data-role=tool-hint], .attempt-hint").forEach((n) => n.remove());

    const hint = document.createElement("div");
    hint.className = "thinking attempt-hint";
    hint.innerHTML =
      '<span class="dots"><i></i><i></i><i></i></span>' +
      `<span>审查未通过，正在重做（第 ${attempt} 稿）…</span>`;
    view.bubble.insertBefore(hint, view.text);
  }

  function stopStream() {
    state.abortReason = "";
    if (state.controller) state.controller.abort();
  }

  function setStreaming(value) {
    state.streaming = value;
    $("send").classList.toggle("hidden", value);
    $("stop").classList.toggle("hidden", !value);
    $("send").disabled = value;
  }

  // ------------------------------------------------------------------
  // 设置抽屉：设置 / 插件 / 接入大模型 / 关于
  // ------------------------------------------------------------------
  const PROVIDER_LABELS = {
    eastmoney: "东方财富行情",
    tushare: "Tushare 行情",
    offline: "离线演示",
  };

  const AGENT_HINTS = {
    researcher: "查行情、读财报、筛股票、算指标",
    quant: "跑回测、看持仓、算组合指标",
    alert: "设价格提醒、看资金流",
  };

  function openPanel(tab) {
    $("drawer").classList.remove("hidden");
    $("drawerBackdrop").classList.remove("hidden");
    renderPanel(tab || "settings");
  }

  function closePanel() {
    $("drawer").classList.add("hidden");
    $("drawerBackdrop").classList.add("hidden");
  }

  function renderPanel(tab) {
    document.querySelectorAll("#drawerTabs button").forEach((button) => {
      button.classList.toggle("on", button.dataset.tab === tab);
    });
    const info = state.info || {};
    const body = $("drawerBody");
    body.innerHTML = "";

    if (tab === "settings") {
      body.appendChild(section("数据与运行"));
      body.appendChild(kv("数据源", PROVIDER_LABELS[info.provider] || info.provider || "—"));
      body.appendChild(kv("传输方式", info.transport || "inprocess"));
      body.appendChild(kv("工具数量", String(info.tool_count ?? "—")));
      body.appendChild(kv("Agent 数量", String((info.experts || []).length)));
      body.appendChild(kv("最大跳转", String(info.max_hops ?? "—")));

      body.appendChild(section("构建"));
      body.appendChild(kv("版本", info.version || "—"));
      body.appendChild(kv("代码版本", (info.build && info.build.sha) || "dev"));
      body.appendChild(kv("启动时间", (info.build && info.build.started) || "—"));

      body.appendChild(section("大模型"));
      const llm = info.llm || {};
      body.appendChild(kv("状态", llm.enabled ? "已接入" : "未接入"));
      if (llm.enabled) {
        body.appendChild(kv("厂商", llm.provider || "—"));
        body.appendChild(kv("模型", llm.model || "—"));
      }

      const note = document.createElement("div");
      note.className = "drawer-note";
      note.textContent =
        "「代码版本」是本进程加载的代码。改了代码但界面没变，先重启服务再看这里。";
      body.appendChild(note);
      return;
    }

    if (tab === "plugins") {
      body.appendChild(section("Agent"));
      (info.experts || []).forEach((item) => {
        const card = document.createElement("div");
        card.className = "agent-card";
        const title = document.createElement("strong");
        title.textContent = `${item.label} · ${item.tool_count} 个工具`;
        const desc = document.createElement("small");
        desc.textContent = `${item.agent} — ${AGENT_HINTS[item.agent] || ""}`;
        card.appendChild(title);
        card.appendChild(desc);
        body.appendChild(card);
      });

      body.appendChild(section("工具来源"));
      body.appendChild(kv("行情 / 选股", "market 服务"));
      body.appendChild(kv("回测 / 指标", "quant 服务"));
      body.appendChild(kv("持仓 / 预警", "portfolio 服务"));
      body.appendChild(kv("妙想 Skills", "未配置时自动隐藏"));

      const note = document.createElement("div");
      note.className = "drawer-note";
      note.textContent =
        "工具按职责拆成独立 MCP 服务，Agent 只拿到与自己相关的白名单，不共享全量工具。";
      body.appendChild(note);
      return;
    }

    if (tab === "llm") {
      const llm = info.llm || {};
      body.appendChild(section("当前状态"));
      body.appendChild(kv("状态", llm.enabled ? "已接入" : "未接入"));
      body.appendChild(kv("厂商", llm.provider || "—"));
      body.appendChild(kv("模型", llm.model || "—"));

      const open = document.createElement("button");
      open.className = "btn-primary";
      open.textContent = llm.enabled ? "修改配置" : "接入大模型";
      open.addEventListener("click", () => {
        closePanel();
        openLlmModal();
      });
      body.appendChild(open);

      const note = document.createElement("div");
      note.className = "drawer-note";
      note.textContent =
        "不接大模型也能用：规划、取数、计算、校验都是确定性代码，只是不生成自然语言解释。";
      body.appendChild(note);
      return;
    }

    if (tab === "about") {
      const title = document.createElement("div");
      title.className = "agent-card";
      title.innerHTML =
        "<strong>FinancialAI-Agent " +
        (info.version || "") +
        "</strong><small>A 股投研 Agent · MCP + LangGraph</small>";
      body.appendChild(title);

      const intro = document.createElement("div");
      intro.className = "drawer-note";
      intro.textContent =
        "研究 / 量化 / 预警三个 Agent 协作，结论先过逻辑审查、再过证据校验（数字由代码核对），最后过风控硬规则才交付。";
      body.appendChild(intro);

      body.appendChild(section("设计原则"));
      body.appendChild(kv("数字来源", "工具返回，可追溯"));
      body.appendChild(kv("涨跌幅", "代码重算，不让模型编"));
      body.appendChild(kv("失败处理", "部分成功并说明缺什么"));

      body.appendChild(section("免责"));
      const disclaimer = document.createElement("div");
      disclaimer.className = "drawer-note";
      disclaimer.textContent =
        "数据来自公开行情接口，可能存在延迟或缺失；所有内容不构成投资建议。";
      body.appendChild(disclaimer);
    }
  }

  function section(title) {
    const node = document.createElement("h3");
    node.textContent = title;
    return node;
  }

  function kv(label, value) {
    const row = document.createElement("div");
    row.className = "kv";
    const left = document.createElement("span");
    left.textContent = label;
    const right = document.createElement("span");
    right.textContent = value;
    row.appendChild(left);
    row.appendChild(right);
    return row;
  }

  // ------------------------------------------------------------------
  // 顶部状态
  // ------------------------------------------------------------------
  async function loadInfo() {
    try {
      const info = await (await fetch("/api/info")).json();
      const llmOn = info.llm && info.llm.enabled;
      // 之前只有 tushare 一个分支，其余（含 eastmoney）全落到「离线演示数据」，
      // 于是真实行情被标成演示数据——这是显示 bug，不是数据问题。
      $("providerPill").textContent = PROVIDER_LABELS[info.provider] || info.provider;
      $("providerPill").title = "当前数据源：" + (info.provider || "未知");
      $("llmPill").textContent = llmOn
        ? `${info.llm.provider} · ${info.llm.model}`
        : "未接大模型 · 点这里接入";
      $("llmPill").classList.toggle("on", !!llmOn);
      $("openLlm").classList.toggle("attention", !llmOn);

      const pills = $("statusPills");
      pills.innerHTML = "";

      // 不再是「41 个工具 / 3 个专家」两枚干巴巴的胶囊，
      // 而是一枚能点开设置的总览 + 各 Agent 的分工。
      const overview = pill(`${info.tool_count} 工具 · ${(info.experts || []).length} Agent`);
      overview.classList.add("action");
      overview.title = "查看数据源、工具分工与构建信息";
      overview.addEventListener("click", () => openPanel("settings"));
      pills.appendChild(overview);
      pills.appendChild(pill(PROVIDER_LABELS[info.provider] || info.provider));
      if (!llmOn) {
        const tip = pill("接入大模型");
        tip.classList.add("action");
        tip.title = "填入 API Key 即可启用真实推理";
        tip.addEventListener("click", () => openPanel("llm"));
        pills.appendChild(tip);
      }
      state.info = info;
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
      // 用户主动停止：清掉看门狗并立刻把界面恢复可用，不等 finally
      state.abortReason = "";
      clearWatchdog();
      if (state.controller) state.controller.abort();
    });

    $("toggleSidebar").addEventListener("click", () => {
      $("sidebar").classList.toggle("open");
    });

    // ---- 接入大模型 ----
    $("openLlm").addEventListener("click", openLlmModal);
  $("openPanel").addEventListener("click", () => openPanel("settings"));
  $("drawerClose").addEventListener("click", closePanel);
  $("drawerBackdrop").addEventListener("click", closePanel);
  document.querySelectorAll("#drawerTabs button").forEach((button) => {
    button.addEventListener("click", () => renderPanel(button.dataset.tab));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("drawer").classList.contains("hidden")) {
      closePanel();
    }
  });
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
