// Agent session transport. Rust renders every transcript item, including partial Markdown.
export function startChat($, request, token, times) {
  const byId = id => $("#chat-" + id);
  const thread = byId("thread"), scroll = byId("scroll"), welcome = byId("welcome");
  const history = byId("sessions"), message = byId("message"), send = byId("send");
  const form = byId("form"), error = byId("error"), upload = byId("upload");
  const sessionPath = id => "/agent/sessions/" + encodeURIComponent(id);
  const credentials = "token=" + encodeURIComponent(token);
  const render = (kind, body) => request("/ui/chat/render/" + kind, "POST", body);
  let active, stream, revision = 0, cursor, running, busy = false, uploading = false;
  let sessions = [], files = [], assistant, tools = new Map(), retry;
  let stick = true, historyVersion = 0, historyFetch = 0;
  const setError = text => { error.textContent = text; };
  const report = cause => setError(cause.message);

  function resize() {
    message.rows = Math.min(6, Math.max(1, message.value.split("\n").length));
  }

  function draw() {
    send.textContent = running ? "Stop" : "Send";
    send.disabled = busy || uploading || (!running && !message.value.trim() && !files.length);
    byId("typing").hidden = !running || !!assistant;
    welcome.hidden = !!thread.childElementCount || running || !byId("starting").hidden;
    if (stick) scroll.scrollTop = scroll.scrollHeight;
  }

  scroll.addEventListener("scroll", () => {
    stick = scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop < 80;
  });

  function selection(id) {
    try {
      if (id) localStorage.setItem("agent-hire-session", id);
      else localStorage.removeItem("agent-hire-session");
    } catch {}
  }

  async function historyList(reload = true) {
    if (reload) {
      const version = ++historyFetch;
      const response = await request("/agent/sessions");
      if (version !== historyFetch) return;
      sessions = response.sessions;
    }
    const version = ++historyVersion;
    const query = byId("filter").value.trim().toLowerCase();
    const matches = sessions.filter(row => row.title.toLowerCase().includes(query));
    const html = await render("history", {sessions: matches, active});
    if (version !== historyVersion) return;
    history.innerHTML = html;
    if (query && !matches.length) history.firstElementChild.textContent = "No matching chats";
    let previous;
    const today = new Date(), yesterday = new Date();
    yesterday.setDate(today.getDate() - 1);
    [...history.querySelectorAll(".session")].forEach((node, index) => {
      const date = new Date(matches[index].updated_at);
      const day = date.toDateString();
      if (day === previous) return;
      previous = day;
      const label = document.createElement("h2");
      label.textContent = day === today.toDateString() ? "Today" : day === yesterday.toDateString()
        ? "Yesterday" : date.toLocaleDateString([], {day:"numeric", month:"short", year:"numeric"});
      node.before(label);
    });
  }

  async function schedules() {
    const id = active;
    if (!id) return;
    const data = await request("/agent/schedules?session=" + encodeURIComponent(id));
    const html = await render("schedules", data);
    if (id !== active) return;
    byId("schedules").innerHTML = html;
    times(byId("schedules"));
  }

  async function status() {
    const {llm} = await request("/agent/status", "GET", undefined, 45000);
    byId("banner").hidden = llm.configured;
    byId("reason").textContent = llm.reason || "Add an AI provider key to start chatting.";
    byId("model").textContent = llm.configured ? [llm.provider, llm.model].filter(Boolean).join(" · ") : "";
    byId("connection").textContent = "";
    byId("starting").hidden = true;
    draw();
  }

  function effects(name) {
    const groups = {
      scrape:["start_scrape","scrape_status"], profile:["save_profile","edit_profile","import_document","save_application_answers"],
      leads:["add_custom_job","delete_jobs","update_job_status","reset_jobs","shortlist_jobs","tailor_resume"]
    };
    const flags = Object.keys(groups).filter(key => groups[key].includes(name));
    if (flags.length) document.dispatchEvent(new CustomEvent("ui-refresh", {detail:flags}));
  }

  // Only one render request at a time per item. If more tokens arrive, render the newest version next.
  function paint(item, node) {
    let dirty = false, rendering = false;
    return () => {
      dirty = true;
      if (rendering) return;
      rendering = true;
      setTimeout(async () => {
        while (dirty && node.isConnected) {
          dirty = false;
          try {
            const html = await render("item", item);
            if (!node.isConnected) break;
            const expanded = node.querySelector("details")?.open;
            node.innerHTML = html;
            if (expanded && node.querySelector("details")) node.querySelector("details").open = true;
            times(node);
            if (!html && !item.streaming && !dirty) node.remove();
          } catch {
            if (item.text) node.textContent = item.text;
          }
          draw();
        }
        rendering = false;
      }, 75);
    };
  }

  function item(data) {
    const node = document.createElement("div");
    thread.append(node);
    const record = {data, update:paint(data, node)};
    record.update();
    return record;
  }

  function eventOf(event) {
    const {type, seq, text, id, name} = event;
    if (type === "resync") { select(active).catch(report); return; }
    if (seq <= cursor) return;
    if (Number.isFinite(seq)) cursor = seq;
    switch (type) {
      case "run_start": running = true; break;
      case "run_end":
        running = false;
        if (assistant) {
          assistant.data.streaming = false;
          assistant.update();
          assistant = undefined;
        }
        schedules().catch(report);
        historyList().catch(report);
        break;
      case "user": item({kind:"user", text, source:event.source}); break;
      case "assistant_start": assistant = item({kind:"assistant", text:"", streaming:true}); break;
      case "text_delta":
        assistant ??= item({kind:"assistant", text:"", streaming:true});
        assistant.data.text += event.delta;
        assistant.update();
        break;
      case "assistant_end":
        assistant ??= item({kind:"assistant"});
        Object.assign(assistant.data, {text, error:event.error, streaming:false});
        assistant.update();
        assistant = undefined;
        break;
      case "tool_start": tools.set(id, item({...event, kind:"tool"})); break;
      case "tool_end": {
        const record = tools.get(id);
        if (record) {
          Object.assign(record.data, event);
          record.update();
          tools.delete(id);
        } else {
          const persisted = [...thread.querySelectorAll("[data-tool-id]")].find(node => node.dataset.toolId === id);
          if (persisted) {
            const data = {...event, kind:"tool", args:JSON.parse(persisted.dataset.args)};
            paint(data, persisted)();
          } else item({...event, kind:"tool"});
        }
        effects(name);
        break;
      }
      case "notice": case "error": item({kind:type, text}); break;
    }
    draw();
  }

  function subscribe(id, version, delay = 500) {
    if (version !== revision) return;
    const source = new EventSource(sessionPath(id) + "/events?after=" + cursor + "&" + credentials);
    stream = source;
    source.onopen = () => { delay = 500; byId("connection").textContent = ""; };
    source.onmessage = event => { if (version === revision) eventOf(JSON.parse(event.data)); };
    source.onerror = () => {
      source.close();
      if (version !== revision) return;
      byId("connection").textContent = "Reconnecting…";
      retry = setTimeout(() => subscribe(id, version, Math.min(delay * 2, 10000)), delay);
    };
  }

  async function select(id) {
    const version = ++revision;
    stream?.close();
    clearTimeout(retry);
    assistant = undefined;
    tools.clear();
    running = false;
    active = id;
    selection(id);
    stick = true;
    thread.replaceChildren();
    byId("schedules").replaceChildren();
    byId("title").textContent = "Agent chat";
    if (id) {
      const transcript = await request(sessionPath(id));
      const html = await render("thread", transcript.items);
      if (version !== revision) return;
      running = transcript.running;
      cursor = transcript.seq;
      byId("title").textContent = transcript.title;
      thread.innerHTML = html;
      times(thread);
      subscribe(id, version);
      schedules().catch(report);
    }
    historyList(false).catch(report);
    draw();
  }

  async function create() {
    const {id} = await request("/agent/sessions", "POST");
    await select(id);
    await historyList();
  }

  function attachments() {
    const chips = files.map(file => {
      const chip = document.createElement("span"), button = document.createElement("button");
      chip.className = "chip";
      button.type = "button";
      button.className = "icon";
      button.textContent = "×";
      button.setAttribute("aria-label", "Remove " + file.name);
      button.onclick = () => { files = files.filter(value => value !== file); attachments(); };
      chip.append(file.name, button);
      return chip;
    });
    byId("files").replaceChildren(...chips);
    draw();
  }

  upload.addEventListener("change", async () => {
    const picked = [...upload.files];
    upload.value = "";
    if (!picked.length) return;
    setError(picked.length > 5 ? "Attachments are limited to five files per pick." : "");
    uploading = true;
    const button = byId("attach");
    button.disabled = true;
    draw();
    for (const file of picked.slice(0, Math.min(5, Math.max(0, 10 - files.length)))) {
      button.setAttribute("aria-label", "Uploading " + file.name);
      button.classList.add("uploading");
      const body = new FormData();
      body.set("file", file);
      try { files.push(await request("/agent/uploads", "POST", body)); }
      catch (cause) { report(cause); }
      attachments();
    }
    if (files.length === 10) setError("This message has ten attachments.");
    uploading = button.disabled = false;
    button.setAttribute("aria-label", "Attach files");
    button.classList.remove("uploading");
    draw();
  });

  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (send.disabled) return;
    busy = true;
    draw();
    setError("");
    try {
      if (running) await request(sessionPath(active) + "/abort", "POST");
      else {
        if (!active) await create();
        await request(sessionPath(active) + "/messages", "POST", {text:message.value, attachments:files.map(file => file.upload_id)});
        message.value = "";
        files = [];
        attachments();
        resize();
        historyList().catch(report);
        message.focus();
      }
    } catch (cause) { report(cause); status().catch(() => {}); }
    finally { busy = false; draw(); }
  });

  message.addEventListener("input", () => { resize(); draw(); });
  message.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && !running) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  byId("filter").addEventListener("input", () => historyList(false).catch(report));
  byId("attach").addEventListener("click", () => upload.click());
  byId("new").addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    draw();
    try { await create(); message.focus(); } catch (cause) { report(cause); }
    finally { busy = false; draw(); }
  });
  byId("history-toggle").addEventListener("click", event => {
    const open = byId("history").classList.toggle("open");
    event.currentTarget.setAttribute("aria-expanded", open);
    if (open) byId("filter").focus();
  });

  document.addEventListener("click", async event => {
    const button = event.target.closest("button");
    if (!button) return;
    const d = button.dataset;
    try {
      if (d.session && !busy) {
        await select(d.session);
        byId("history").classList.remove("open");
        byId("history-toggle").setAttribute("aria-expanded", "false");
        message.focus();
      }
      if (d.delete && confirm("Delete this chat and its scheduled check-ins?")) {
        await request(sessionPath(d.delete), "DELETE");
        if (d.delete === active) await select(undefined);
        await historyList();
      }
      if (d.cancel) {
        await request("/agent/schedules/" + encodeURIComponent(d.cancel), "DELETE");
        await schedules();
      }
      if (d.suggestion && !busy) {
        message.value = d.suggestion;
        resize();
        draw();
        if ("attach" in d) upload.click();
        else form.requestSubmit();
      }
    } catch (cause) { report(cause); }
  });

  function connectHost() {
    const host = new EventSource("/agent/events?" + credentials);
    host.onmessage = event => {
      if (["sessions_changed", "session_running"].includes(JSON.parse(event.data).type)) historyList().catch(report);
    };
    host.onopen = () => {
      historyList().catch(report);
      status().catch(cause => { byId("starting").hidden = true; draw(); report(cause); });
    };
    host.onerror = () => { byId("connection").textContent = "Reconnecting…"; };
  }

  async function init() {
    setTimeout(() => {
      if (!byId("model").textContent && byId("banner").hidden && !thread.childElementCount) {
        byId("starting").hidden = false;
        draw();
      }
    }, 350);
    connectHost();
    let remembered;
    try { remembered = localStorage.getItem("agent-hire-session"); } catch {}
    try { await select(remembered); } catch (cause) { selection(undefined); await select(undefined); report(cause); }
    if (new URLSearchParams(location.search).has("compose")) message.focus();
  }
  init().catch(report);
}
