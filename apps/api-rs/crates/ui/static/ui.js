// Same-origin requests and Datastar patches shared by the shell and chat.
export const $ = (selector, root = document) => root.querySelector(selector);
const all = (selector, root = document) => [...root.querySelectorAll(selector)];
export const token = $('meta[name="agent-token"]').content;

export async function request(url, method = "GET", body, timeout = 600000) {
  const headers = { Authorization: "Bearer " + token };
  if (body && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  const response = await fetch(url, { method, headers, body, signal: AbortSignal.timeout(timeout) });
  const json = response.headers.get("Content-Type")?.includes("application/json");
  const data = await (json ? response.json() : response.text());
  if (!response.ok || data?.error || data?.ok === false) {
    const message = data?.error || data?.detail;
    throw new Error(typeof message === "string" ? message : "Request failed (" + response.status + ").");
  }
  return data;
}

const live = new URL(document.body.dataset.live, location.href);
const drawer = $("#side-panel"), popover = $("#popover");
let toastTimer, validationTimer;
export function notify(text) {
  $("#toast").textContent = text;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("#toast").textContent = ""; }, 5000);
}

export function times(root = document) {
  for (const time of all("time[data-relative]", root)) {
    const date = new Date(time.dateTime), mode = time.dataset.relative;
    if (!Number.isFinite(+date)) continue;
    const minutes = Math.max(0, Math.floor((Date.now() - date) / 60000));
    time.textContent = mode === "clock" ? date.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"})
      : mode === "local" ? date.toLocaleString()
      : minutes < 1 && mode !== "jobs" ? "just now"
      : minutes < 60 ? minutes + "m ago"
      : minutes < 2880 ? Math.floor(minutes / 60) + "h ago"
      : mode === "chat" ? date.toLocaleDateString() : Math.floor(minutes / 1440) + "d ago";
  }
}

// Feed already parsed SSE records to Datastar's patch watcher. This avoids expression compilation.
const datastar = Promise.all([import(document.body.dataset.datastar), import(document.body.dataset.patch)])
  .then(([{watcher}, {register}]) => register(watcher));
async function patch(elements) {
  if (!elements) return;
  await datastar;
  document.dispatchEvent(new CustomEvent("datastar-fetch", {
    detail: {type:"datastar-patch-elements", el:document.body, argsRaw:{elements}}
  }));
  times();
}

function filter(input) {
  const root = input.closest("article, [data-picker-panel]");
  const query = input.value.trim().toLowerCase();
  let visible = 0;
  for (const row of all("[data-filter-row]", root)) {
    row.hidden = !row.dataset.filterRow.toLowerCase().includes(query);
    visible += !row.hidden;
  }
  for (const group of all(".company-group", root)) {
    group.hidden = !$("[data-filter-row]:not([hidden])", group);
    if (query && !group.hidden) group.open = true;
  }
  $("[data-no-match]", root).hidden = visible > 0;
}

function picker(panel) {
  if (!panel) return;
  const checked = all("[data-pick]:checked", panel);
  const button = popover.trigger;
  button.previousElementSibling.value = checked.map(input => input.value).join(",");
  const groups = all("[data-group-check]", panel);
  for (const group of groups) {
    const options = all("[data-pick]", group.closest("fieldset"));
    const picked = options.filter(input => input.checked).length;
    group.checked = picked > 0 && picked === options.length;
    group.indeterminate = picked > 0 && !group.checked;
  }
  const count = checked.length;
  const whole = groups.find(group => group.checked && all("[data-pick]", group.closest("fieldset")).length === count);
  button.textContent = whole?.dataset.label || (count === 1 ? checked[0].dataset.label : count
    ? count + (groups.length ? " subdepartments" : " companies") : button.dataset.empty);
  button.classList.toggle("selected", count > 0);
  $("[data-picked]", panel).textContent = count ? count + " picked" : "None picked = all";
}

async function open(dialog, trigger, url) {
  if (!dialog.open) {
    dialog.trigger = trigger;
    dialog.showModal();
  }
  dialog.dataset.url = url;
  const host = $("[data-dialog-body]", dialog);
  host.textContent = "Loading…";
  try {
    const html = await request(url);
    if (!dialog.open || dialog.dataset.url !== url) return;
    host.innerHTML = html;
    dialog.setAttribute("aria-label", $("h2", host)?.textContent || "Details");
    ($("[autofocus], [data-filter]", host) || $("button", dialog)).focus();
    picker($("[data-picker-panel]", host));
    times(host);
  } catch (error) { host.textContent = error.message; }
}
for (const dialog of [drawer, popover]) {
  dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => dialog.trigger?.focus());
}

async function updatePanel(signal) {
  const dialog = signal === "kaggle" ? popover : drawer;
  const url = "/ui/" + (signal === "scrape" ? "companies" : signal);
  if (!dialog.open || dialog.dataset.url !== url) return;
  const selector = signal === "kaggle" ? "#kaggle-state" : "#" + url.split("/").pop() + "-panel";
  const current = $(selector);
  if (!current || $("[data-busy-now]", current) || $("[data-dirty]", current)) return;
  const content = document.createElement("template");
  content.innerHTML = await request(url);
  if (dialog.open && dialog.dataset.url === url) {
    const marked = all("[data-company]:checked", current).map(input => input.value);
    const openGroups = all("details[open][id]", current).map(group => group.id);
    const search = $("[data-filter]", current)?.value;
    const next = $(selector, content.content);
    for (const group of all("details[id]", next)) group.open = openGroups.includes(group.id);
    for (const input of all("[data-company]", next)) input.checked = marked.includes(input.value);
    await patch(next.outerHTML);
    const field = $(selector + " [data-filter]");
    if (field && search) { field.value = search; filter(field); }
    companySelection();
  }
}

async function refresh(...flags) {
  const url = new URL(live);
  url.pathname = "/ui/refresh";
  url.searchParams.set("signals", flags.join(","));
  await patch(await request(url));
  await Promise.all(flags.filter(flag => ["profile","scrape","kaggle"].includes(flag)).map(updatePanel));
}

const stream = new EventSource(live);
stream.addEventListener("datastar-patch-elements", async event => {
  await patch(event.data.split("\n").filter(line => line.startsWith("elements ")).map(line => line.slice(9)).join("\n"));
  if ($("#toast").textContent) notify($("#toast").textContent);
  for (const marker of all("[data-refresh]")) {
    updatePanel(marker.dataset.refresh).catch(() => {});
    delete marker.dataset.refresh;
  }
});

const due = {};
async function poll() {
  if (document.hidden) return;
  const period = {
    scrape: $("#scrape-widget .live") || $("#companies-panel[data-active=true]") ? 5 : 30,
    classifiers: $("#classifier-widget .live") ? 10 : 60,
    kaggle: $("#notebook-widget[data-deploying=true]") ? 5 : 60,
    ...($("#jobs-status") ? {"jobs-status":20} : {})
  };
  const now = Date.now();
  const flags = Object.keys(period).filter(key => now - (due[key] || 0) >= period[key] * 1000);
  if (flags.length) {
    for (const key of flags) due[key] = now;
    await refresh(...flags).catch(() => {});
  }
}
setInterval(poll, 5000);
document.addEventListener("visibilitychange", poll);
const changes = new BroadcastChannel("agent-hire-ui");
changes.onmessage = ({data}) => {
  refresh(...data).catch(() => {});
  if (data.includes("leads")) search(new URL(location.href), false);
};
document.addEventListener("ui-refresh", event => {
  changes.postMessage(event.detail);
  refresh(...event.detail).catch(() => {});
});

function companySelection() {
  const panel = $("#companies-panel"), button = $("[data-company-selection] button");
  if (!panel || !button) return;
  const picked = all("[data-company]:checked", panel).map(input => +input.dataset.company);
  const n = picked.length;
  const seconds = Math.max(...picked, picked.reduce((a, b) => a + b, 0) / Math.min(+panel.dataset.concurrency, n || 1));
  const duration = seconds < 60 ? Math.round(seconds) + "s" : seconds < 3600
    ? Math.round(seconds / 60) + " min" : (seconds / 3600).toFixed(seconds < 36000 ? 1 : 0) + "h";
  button.disabled = !n || panel.dataset.active === "true";
  button.textContent = "Scrape " + n + " selected" + (n ? " · ~" + duration : "");
}

function formData(form, submitter) {
  const data = {};
  for (const [name, value] of new FormData(form, submitter)) {
    if (["job_refs", "categories"].includes(name)) (data[name] ??= []).push(value);
    else if (name.includes(".")) {
      const [group, key] = name.split(".");
      (data[group] ??= {})[key] = value;
    } else data[name] = value;
  }
  for (const field of all("input[data-secret]", form)) if (!field.value.trim()) delete data[field.name];
  for (const field of all("input[data-boolean]", form)) data[field.name] = field.checked;
  for (const key of ["days", "port"]) if (key in data) data[key] = Number(data[key]);
  if (data.job_refs) data.add_to_cart = true;
  if (form.matches("[data-company-selection]")) data.companies = all("[data-company]:checked").map(input => input.value);
  if (data.confirm) data.confirm = data.confirm.trim().toUpperCase();
  delete data.provider_preset;
  return data;
}

function resultMessage(url, data) {
  if (url.includes("/promote")) return data.added.length + " added to Applications and the extension queue" +
    (data.already_in_pipeline.length ? " (" + data.already_in_pipeline.length + " were already there)" : "");
  if (url.endsWith("/scrape")) {
    if (!data.started) return data.reason === "already_running" ? "A scrape is already running." : "Not started: " + data.reason;
    return "Started: " + (data.run.label || "Company scrape") + " (" + data.run.companies_planned + " companies, fastest first).";
  }
  if (url.includes("/email/poll")) return "Scanned " + data.scanned + " emails · " + data.matched + " about your applications · " + data.updated + " stages updated.";
  if (url.endsWith("/email/connect")) return "Saved. Agent-Hire will check for application updates every 20 minutes.";
  if (url.endsWith("/email/check")) return "Connection works. Agent-Hire has read-only access.";
  if (url.endsWith("/email/disconnect")) return "Email monitoring is off and the saved password was removed.";
  return "Saved";
}

async function search(url, push = true) {
  const button = $("#job-search button.primary");
  if (!button || button.disabled) return;
  button.disabled = true;
  button.textContent = "Searching…";
  try {
    await patch(await request("/ui/search" + url.search));
    if (push) history.pushState(null, "", url);
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; button.textContent = "Search"; }
}

document.addEventListener("submit", async event => {
  const form = event.target, button = event.submitter || $("button", form);
  if (form.id === "job-search") {
    event.preventDefault();
    const url = new URL("/jobs", location.href);
    url.search = new URLSearchParams(new FormData(form));
    return search(url);
  }
  let url = form.dataset.action;
  if (!url) return;
  event.preventDefault();
  if (form.hasAttribute("data-busy-now")) return;
  const data = formData(form, event.submitter);
  const output = $("output", form);
  const label = button?.textContent;
  const controls = all("button:not(:disabled), select:not(:disabled)", form);
  for (const control of controls) control.disabled = true;
  form.dataset.busyNow = "";
  if (button) button.textContent = form.dataset.busy || "Saving…";
  output.textContent = "";
  try {
    const validate = button?.hasAttribute("data-validate");
    const probe = button?.hasAttribute("data-probe");
    if (validate) url += "/validate";
    const timeout = url.endsWith("/email/check") ? 45000 : url.includes("/email/poll") ? 300000
      : url.endsWith("/kaggle/connect") ? 240000 : url.endsWith("/kaggle/stop") ? 90000 : undefined;
    const response = await request(url, form.dataset.method || "POST", data, timeout);
    if (url.endsWith("/data/reset")) {
      const errors = response.summary?.errors;
      if (!errors?.length) return location.reload();
      $("[data-reload-app]", form).hidden = false;
      throw new Error("Your data was deleted, but part of the reset reported a problem: " + errors.join(" · "));
    }
    const results = validate ? response : probe ? await request(url + "/probe", "POST", undefined, 120000) : "";
    if (results) {
      await patch(results);
      clearTimeout(validationTimer);
      if (validate) validationTimer = setTimeout(() => $("#settings-result")?.replaceChildren(), 30000);
    }
    if (button && form.hasAttribute("data-save") && !validate) {
      button.textContent = "Saved";
      setTimeout(() => { if (button.textContent === "Saved") button.textContent = label; }, 2000);
    }
    delete form.dataset.dirty;
    delete form.dataset.busyNow;
    const message = validate ? "" : resultMessage(url, response);
    const effects = (form.dataset.effects || "").split(" ").filter(Boolean);
    if (response.started) all("[data-company]:checked").forEach(input => { input.checked = false; });
    if (effects.length || message !== "Saved") notify(message);
    output.textContent = message;
    if (effects.length) { changes.postMessage(effects); await refresh(...effects); }
    if (effects.includes("search")) await search(new URL(location.href), false);
    if (url.endsWith("/email/connect") || url.endsWith("/email/disconnect")) await patch(await request("/ui/settings/email"));
    if (form.elements.link) form.elements.link.value = "";
    if (form.dataset.settings && !validate && Object.keys(data).some(key => /_key$/.test(key))) {
      const section = form.dataset.settings;
      if (section !== "email") {
        all("[data-secret]", form).forEach(input => { input.value = ""; });
        await patch(await request("/ui/settings/" + section));
      }
    }
  } catch (error) { output.textContent = error.message; output.classList.add("bad"); }
  finally {
    delete form.dataset.busyNow;
    for (const control of controls) control.disabled = false;
    if (button && button.textContent !== "Saved") button.textContent = label;
    companySelection();
  }
});

document.addEventListener("click", async event => {
  const trigger = event.target.closest("button, a");
  if (!trigger) {
    const row = event.target.closest("[data-row-open]");
    if (row && !event.target.closest("input, select, form, label")) open(drawer, $("[data-open]", row), row.dataset.rowOpen);
    return;
  }
  const d = trigger.dataset;
  if (d.open) open(drawer, trigger, d.open);
  if (d.popover) {
    const url = d.popover + (d.picker ? "?selected=" + encodeURIComponent(trigger.previousElementSibling.value) : "");
    open(popover, trigger, url);
  }
  if ("jobPage" in d && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
    event.preventDefault();
    search(new URL(trigger.href));
  }
  if ("clearPicker" in d) {
    const panel = trigger.closest("[data-picker-panel]");
    all("[data-pick]", panel).forEach(input => { input.checked = false; });
    picker(panel);
  }
  if (d.expand) {
    const expanded = trigger.getAttribute("aria-expanded") !== "true";
    trigger.setAttribute("aria-expanded", expanded);
    trigger.textContent = expanded ? "Show less" : "Show more";
    $("#" + d.expand).classList.toggle("expanded", expanded);
  }
  if (d.model) trigger.form.elements[d.modelField].value = d.model;
  if ("cancelReset" in d) {
    trigger.form.reset();
    resetForm(trigger.form);
    $("#reset-confirmation").open = false;
  }
  if (d.deleteKey) {
    try {
      await request(d.deleteKey, "DELETE");
      const input = $("[data-secret]", trigger.closest(".key-field"));
      input.placeholder = input.dataset.emptyKey;
      trigger.closest(".saved-key").remove();
      notify("Saved key removed.");
    } catch (error) { notify(error.message); }
  }
  if ("pdf" in d) {
    event.preventDefault();
    const tab = window.open("", "_blank");
    if (tab) tab.opener = null;
    try {
      const response = await fetch(trigger.href, {method:"HEAD"});
      if (!response.ok) throw new Error();
      if (tab) tab.location.href = trigger.href;
    } catch { tab?.close(); notify("No tailored résumé yet — ask the agent to tailor one."); }
  }
});

function resetForm(form) {
  const allData = form.elements.clear_settings.checked, button = $("[data-reset-submit]", form);
  button.disabled = form.elements.confirm.value.trim().toUpperCase() !== "DELETE";
  button.textContent = allData ? "Delete everything" : "Delete all data";
  $("[data-reset-copy]", form).textContent = allData
    ? "Deletes your saved data, settings, provider keys, preferences and local job source selections without a backup."
    : "A backup of the local database is written before saved leads, profile and generated documents are deleted.";
}

function endpoint(field) {
  const set = field.closest("[data-provider]");
  const advanced = set && $("[data-format-default]", set);
  if (!advanced) return;
  const protocol = $('[name$="_protocol"]', set), format = $('[name$="_endpoint_type"]', set);
  if (field.name.endsWith("_base_url")) {
    const match = field.value.match(/\/(chat\/completions|messages|responses)(?:[/?#]|$)/)?.[1];
    if (match) {
      format.value = match;
      protocol.value = match === "messages" ? "anthropic" : "openai";
    }
  }
  if (field === protocol || field === format) {
    if (field === protocol) {
      if (protocol.value === "anthropic") format.value = "messages";
      else if (format.value === "messages") format.value = advanced.dataset.formatDefault;
    } else protocol.value = format.value === "messages" ? "anthropic" : "openai";
  }
}

document.addEventListener("input", event => {
  const field = event.target;
  if (field.matches("[data-filter]")) filter(field);
  if (field.form?.matches("#reset-confirmation form")) resetForm(field.form);
  if (field.form?.closest("#profile-answers")) field.form.dataset.dirty = "";
  if (field.name?.endsWith("_base_url")) endpoint(field);
});

document.addEventListener("change", event => {
  const field = event.target, d = field.dataset;
  if ("submit" in d) field.form.requestSubmit();
  if ("level" in d) field.form.elements.levels.value = all("[data-level]:checked").map(input => input.dataset.level).join(",");
  if (field.name === "job_refs") {
    const count = all("[name=job_refs]:checked").length, button = $("[data-count-jobs]");
    button.disabled = count === 0;
    button.textContent = count ? `Add ${count} to applications` : "Add to applications";
  }
  if ("company" in d) companySelection();
  if ("groupCheck" in d) all("[data-pick]", field.closest("fieldset")).forEach(input => { input.checked = field.checked; });
  if ("pick" in d || "groupCheck" in d) picker(field.closest("[data-picker-panel]"));
  if (field.name === "theme") {
    if (field.value === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = field.value;
    try { localStorage.setItem("agent-hire-theme", field.value); } catch {}
  }
  if (field.name === "llm_provider") {
    for (const set of all("[data-provider]", field.form)) set.hidden = set.disabled = set.dataset.provider !== field.value;
  }
  if (field.name === "provider_preset" && field.value) {
    field.form.elements.host.value = field.value;
    field.form.elements.port.value = 993;
  }
  if (field.name?.match(/_(protocol|endpoint_type)$/)) endpoint(field);
});

const theme = document.documentElement.dataset.theme ?? "system";
if ($("#theme-settings")) $(`[name="theme"][value="${theme}"]`).checked = true;
window.addEventListener("popstate", () => location.reload());
times();

if (document.body.dataset.chat) {
  import(document.body.dataset.chat).then(({startChat}) => startChat($, request, token, times));
}
