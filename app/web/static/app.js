"use strict";

// Демо «прямой вызов»: чат слева, подсказка AI-помощника справа. Общие функции — в common.js.

const CLIENT_DEBOUNCE_MS = 1500;
const TIMING = {
  now: { label: "предложить сейчас", cls: "pill-now" },
  after_resolution: { label: "после решения вопроса", cls: "pill-later" },
  not_now: { label: "не предлагать сейчас", cls: "pill-no" },
};
const INTENT = {
  price: "цена", availability: "наличие", delivery: "доставка", payment: "оплата",
  warranty: "гарантия", complaint: "жалоба", order_status: "статус заказа", other: "другое",
};
const SENTIMENT = { positive: "позитивное", neutral: "нейтральное", negative: "негативное" };

const state = {
  messages: [],       // {role, text, ts, author_name}
  leadId: null,
  scenarios: [],
  requestSeq: 0,
  controller: null,
  debounceTimer: null,
  suggestion: null,   // последний ответ /api/v1/suggest
  suggestionForIndex: -1,
  kbLoaded: false,
  livePassword: null, // пароль живой модели на стенде в mock-режиме (только до закрытия вкладки)
};

function setStatus(text, kind = "") {
  const node = $("status");
  node.textContent = text;
  node.className = `status ${kind || "muted"}`;
  node.hidden = !text;
}

// ---------- Сделка ----------

function splitList(value) {
  return value.split(",").map((s) => s.trim()).filter(Boolean);
}

function leadPayload() {
  const form = $("lead-form");
  const get = (name) => form.elements[name].value.trim();
  const budget = get("budget");
  return {
    id: state.leadId,
    contact_name: get("contact_name") || null,
    pipeline: get("pipeline") || null,
    stage: get("stage") || null,
    budget: budget === "" ? null : Math.max(0, Math.round(Number(budget))),
    products: splitList(get("products")),
    tags: splitList(get("tags")),
  };
}

function channelValue() {
  return $("lead-form").elements.channel.value || null;
}

function fillLeadForm(lead = {}, channel = "") {
  const form = $("lead-form");
  form.elements.contact_name.value = lead.contact_name || "";
  form.elements.pipeline.value = lead.pipeline || "";
  form.elements.stage.value = lead.stage || "";
  form.elements.budget.value = lead.budget ?? "";
  form.elements.products.value = (lead.products || []).join(", ");
  form.elements.tags.value = (lead.tags || []).join(", ");
  form.elements.channel.value = channel || "";
  state.leadId = lead.id ?? null;
  renderLeadHeader();
}

function renderLeadHeader() {
  const lead = leadPayload();
  const channel = channelValue();
  const title = lead.contact_name || "Новая сделка";
  $("lead-title").textContent = state.leadId ? `${title} · сделка #${state.leadId}` : title;
  $("lead-sub").textContent = `${lead.stage || "Этап не указан"} · канал: ${channel || "не указан"}`;
}

// ---------- Чат ----------

function renderMessages() {
  const box = $("messages");
  box.replaceChildren();
  if (!state.messages.length) {
    box.append(el("p", { class: "empty muted", text: "Выберите сценарий или напишите сообщение от имени клиента." }));
    return;
  }
  for (const message of state.messages) {
    const author = message.role === "client"
      ? (leadPayload().contact_name || ROLE_LABEL.client)
      : (message.author_name ? `${ROLE_LABEL[message.role]} · ${message.author_name}` : ROLE_LABEL[message.role]);
    const time = formatTime(message.ts);
    box.append(
      el("div", { class: `msg msg-${message.role}` },
        el("span", { class: "msg-author", text: time ? `${author}, ${time}` : author }),
        el("div", { class: "msg-bubble", text: message.text }),
      ),
    );
  }
  box.scrollTop = box.scrollHeight;
}

function currentRole() {
  return document.querySelector('input[name="role"]:checked').value;
}

function setRole(role) {
  document.querySelector(`input[name="role"][value="${role}"]`).checked = true;
}

function addMessage(role, text) {
  state.messages.push({
    role,
    text,
    ts: new Date().toISOString(),
    author_name: role === "manager" ? "Вы" : null,
  });
  renderMessages();
  if (role === "client") {
    scheduleSuggestion();
  } else if (state.suggestion && state.suggestionForIndex < state.messages.length - 1) {
    $("stale").hidden = false;
  }
}

// Новое обращение — сообщения клиента подряд в конце диалога; всё до них — история.
function splitDialog() {
  let start = state.messages.length;
  while (start > 0 && state.messages[start - 1].role === "client") start -= 1;
  const trailing = state.messages.slice(start);
  if (!trailing.length) return null;
  return {
    history: state.messages.slice(0, start),
    message: trailing.map((m) => m.text).join("\n"),
    lastIndex: state.messages.length - 1,
  };
}

// ---------- Подсказка ----------

function scheduleSuggestion() {
  clearTimeout(state.debounceTimer);
  setStatus("Жду, не допишет ли клиент ещё…", "muted");
  state.debounceTimer = setTimeout(requestSuggestion, CLIENT_DEBOUNCE_MS);
}

async function requestSuggestion() {
  clearTimeout(state.debounceTimer);
  const split = splitDialog();
  if (!split) {
    setStatus("Нет нового сообщения клиента — подсказка не нужна.", "muted");
    return;
  }
  const seq = ++state.requestSeq;
  if (state.controller) state.controller.abort();
  state.controller = new AbortController();
  setStatus("Готовлю подсказку…", "loading");
  $("regenerate").disabled = true;

  const body = {
    message: split.message,
    history: split.history.map(({ role, text, ts, author_name }) => ({ role, text, ts, author_name })),
    lead: leadPayload(),
    channel: channelValue(),
  };
  // Модель по умолчанию не передаём: тогда, если она не ответит, сработают запасные (LLM_FALLBACK_PROVIDERS).
  const picked = $("llm-provider")?.selectedOptions[0];
  const url = picked && !picked.dataset.default
    ? `/api/v1/suggest?provider=${encodeURIComponent(picked.value)}`
    : "/api/v1/suggest";
  const headers = { "Content-Type": "application/json" };
  if (state.livePassword) headers["X-Live-Password"] = state.livePassword;
  try {
    const response = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: state.controller.signal,
    });
    const data = await response.json().catch(() => null);
    if (seq !== state.requestSeq) return;
    if (response.status === 401 && state.livePassword) {
      liveLogout();
      setStatus("Пароль живой модели больше не действует — войдите снова.", "error");
      return;
    }
    if (!response.ok) {
      setStatus(errorText(data, response.status), "error");
      return;
    }
    state.suggestion = data;
    state.suggestionForIndex = split.lastIndex;
    renderSuggestion(data);
    setStatus("");
  } catch (error) {
    if (error.name === "AbortError") return;
    setStatus(`Сервис недоступен: ${error.message}`, "error");
  } finally {
    if (seq === state.requestSeq) $("regenerate").disabled = false;
  }
}

function addKv(list, key, value, cls) {
  if (!value) return;
  list.append(el("dt", { text: key }), el("dd", cls ? { class: cls, text: value } : { text: value }));
}

function renderSuggestion({ suggestion: s, meta }) {
  $("suggestion").hidden = false;
  $("stale").hidden = true;

  const upsellOnly = meta.mode === "upsell_only";
  $("reply-text").textContent = upsellOnly ? "Менеджер уже ответил — черновик не нужен." : s.client_reply;
  $("needs-human").hidden = !s.needs_human;
  $("needs-human-reason").hidden = !s.needs_human;
  $("needs-human-reason").textContent = s.needs_human_reason || "Проверьте ответ перед отправкой.";
  $("insert-reply").disabled = upsellOnly || !s.client_reply;
  $("copy-reply").disabled = upsellOnly || !s.client_reply;

  const refs = $("kb-refs");
  refs.replaceChildren();
  if (s.kb_refs.length) {
    refs.append(el("span", { class: "muted small", text: "Основано на:" }));
    for (const ref of s.kb_refs) refs.append(el("span", { class: "chip", text: ref }));
  } else if (!upsellOnly) {
    refs.append(el("span", { class: "muted small", text: "Нет опоры на базу знаний" }));
  }

  const u = s.upsell;
  const timing = TIMING[u.timing] || TIMING.not_now;
  const pill = $("upsell-timing");
  pill.textContent = u.recommended ? timing.label : "не предлагать";
  pill.className = `pill ${u.recommended ? timing.cls : "pill-no"}`;

  const body = $("upsell-body");
  body.replaceChildren();
  if (u.recommended) {
    addKv(body, "Что", u.offer);
    addKv(body, "Товары", u.product_ids.join(", "));
    addKv(body, "Почему", u.reason);
    addKv(body, "Фраза", u.pitch ? `«${u.pitch}»` : "", "pitch");
  } else {
    addKv(body, "Почему нет", u.reason);
  }
  addKv(body, "Не делать", u.avoid);
  $("copy-pitch").disabled = !u.recommended || !u.pitch;

  const warnings = $("warnings");
  warnings.replaceChildren(...meta.warnings.map((w) => el("li", { text: w.message })));
  $("warnings-block").hidden = !meta.warnings.length;

  const metaList = $("meta");
  metaList.replaceChildren();
  const usage = meta.usage;
  // В mock-режиме провайдер ничего не значит: отвечает запись или FAQ, а не выбранная модель.
  const showProvider = meta.provider && meta.llm_mode !== "mock";
  addKv(metaList, "Модель", showProvider ? `${meta.model} (${meta.provider}, ${meta.llm_mode})` : `${meta.model} (${meta.llm_mode})`);
  addKv(metaList, "Тема / настроение", `${INTENT[s.intent] || s.intent} / ${SENTIMENT[s.sentiment] || s.sentiment}`);
  addKv(metaList, "Задержка", `${meta.latency_ms} мс, попыток: ${meta.attempts}`);
  addKv(metaList, "Токены", `вход ${usage.input_tokens}, кэш ${usage.cache_read_input_tokens}, выход ${usage.output_tokens}`);
  addKv(metaList, "Версия БЗ", meta.kb_version);
  addKv(metaList, "id подсказки", meta.suggestion_id);
}

function resetSuggestion(text) {
  state.suggestion = null;
  state.suggestionForIndex = -1;
  $("suggestion").hidden = true;
  $("stale").hidden = true;
  setStatus(text, "muted");
}

// ---------- Сценарии ----------

async function loadScenarios() {
  try {
    const response = await fetch("/api/v1/demo/scenarios");
    if (!response.ok) return;
    state.scenarios = await response.json();
    const select = $("scenario-select");
    for (const scenario of state.scenarios) {
      select.append(el("option", { value: scenario.id, text: scenario.title }));
    }
  } catch {
    // Демо работает и без сценариев.
  }
}

function applyScenario(id) {
  const scenario = state.scenarios.find((s) => s.id === id);
  if (!scenario) return;
  clearTimeout(state.debounceTimer);
  fillLeadForm(scenario.lead, scenario.channel);
  state.messages = scenario.dialog.map((m) => ({ ...m, ts: m.ts || null }));
  renderMessages();
  resetSuggestion(scenario.description || "");
  requestSuggestion();
}

// ---------- База знаний ----------

function adminHeaders() {
  let token = null;
  try { token = sessionStorage.getItem("adminToken"); } catch { /* хранилище недоступно */ }
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function renderKb(kb) {
  const box = $("kb-content");
  box.replaceChildren();
  $("kb-badge").textContent = `БЗ: ${kb.version}`;
  $("kb-status").textContent = `Версия ${kb.version}`;

  const section = (title, items, render) => {
    if (!items.length) return;
    box.append(el("h3", { text: `${title} (${items.length})` }));
    for (const item of items) box.append(render(item));
  };
  section("Товары и услуги", kb.products, (p) => el("div", { class: "kb-item" },
    el("div", { class: "kb-row" },
      el("span", { text: p.name }),
      el("span", { class: "price", text: rub(p.price) + (p.unit ? ` ${p.unit}` : "") })),
    el("span", { class: "chip", text: p.id })));
  section("Частые вопросы", kb.faq, (f) => el("div", { class: "kb-item" },
    el("div", { text: f.questions[0] }),
    el("div", { class: "muted small", text: f.answer }),
    el("span", { class: "chip", text: f.id })));
  section("Условия", kb.policies, (p) => el("div", { class: "kb-item" },
    el("div", { text: p.title }),
    el("div", { class: "muted small", text: p.text }),
    el("span", { class: "chip", text: p.id })));
  section("Правила допродаж", kb.upsell_rules, (r) => el("div", { class: "kb-item" },
    el("div", { text: r.when }),
    el("div", { class: "muted small", text: `Предложить: ${r.offer.join(", ")}${r.discount_percent ? ` · скидка ${r.discount_percent}%` : ""}` }),
    el("span", { class: "chip", text: r.id })));
  section("Запрещённые фразы", kb.forbidden_phrases, (phrase) => el("div", { class: "kb-item", text: phrase }));
}

async function kbRequest(method, path) {
  const response = await fetch(path, { method, headers: adminHeaders() });
  const data = await response.json().catch(() => null);
  if (response.status === 401) {
    $("kb-token-form").hidden = false;
    $("kb-status").textContent = "Нужен ADMIN_TOKEN";
    return null;
  }
  $("kb-token-form").hidden = true;
  if (!response.ok) {
    const error = data && data.error;
    $("kb-status").textContent = error ? error.message : `Ошибка ${response.status}`;
    if (error && Array.isArray(error.details)) {
      $("kb-content").prepend(el("ul", { class: "kb-errors" }, ...error.details.map((d) => el("li", { text: String(d) }))));
    }
    return null;
  }
  return data;
}

async function loadKb() {
  const kb = await kbRequest("GET", "/api/v1/kb");
  if (kb) {
    renderKb(kb);
    state.kbLoaded = true;
  }
}

async function reloadKb() {
  $("kb-status").textContent = "Перезагружаю…";
  const result = await kbRequest("POST", "/api/v1/kb/reload");
  if (!result) return;
  await loadKb();
  $("kb-status").textContent = result.changed ? `Загружена новая версия ${result.version}` : `Без изменений (${result.version})`;
}

// ---------- Живая модель за паролем (стенд в mock-режиме) ----------

function restoreProvider() {
  const select = $("llm-provider");
  try {
    const saved = localStorage.getItem("llmProvider");
    const option = [...select.options].find((o) => o.value === saved);
    if (option && !option.disabled) select.value = saved;
  } catch { /* хранилище недоступно */ }
}

function fillProviders(data) {
  const select = $("llm-provider");
  select.replaceChildren(...data.providers.map((p) => {
    const option = el("option", { value: p.id, text: `${p.name} · ${p.model}${p.available ? "" : " — недоступна"}` });
    option.disabled = !p.available;
    if (p.default) {
      option.dataset.default = "1";
      option.selected = true;
    }
    return option;
  }));
  restoreProvider();
}

function showLive(on) {
  $("llm-badge").hidden = on;
  $("live-open").hidden = on;
  $("live-picker").hidden = !on;
  $("live-logout").hidden = !on;
  $("live-form").hidden = true;
  $("live-open").setAttribute("aria-expanded", "false");
}

async function liveLogin(password, { silent = false } = {}) {
  const error = $("live-error");
  error.hidden = true;
  try {
    const data = await fetchJSON("/api/v1/live/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    state.livePassword = password;
    try { sessionStorage.setItem("livePassword", password); } catch { /* хранилище недоступно */ }
    fillProviders(data);
    showLive(true);
    if (!silent) toast("Живая модель включена");
  } catch (exc) {
    liveLogout();
    if (!silent) {
      error.textContent = exc.message;
      error.hidden = false;
      $("live-form").hidden = false;
    }
  }
}

function liveLogout() {
  state.livePassword = null;
  try { sessionStorage.removeItem("livePassword"); } catch { /* хранилище недоступно */ }
  $("llm-provider").replaceChildren();
  showLive(false);
}

function initLiveDemo() {
  const form = $("live-form");
  $("live-open").addEventListener("click", () => {
    form.hidden = !form.hidden;
    $("live-open").setAttribute("aria-expanded", String(!form.hidden));
    if (!form.hidden) $("live-password").focus();
  });
  $("live-cancel").addEventListener("click", () => {
    form.hidden = true;
    $("live-open").setAttribute("aria-expanded", "false");
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("live-password");
    const password = input.value;
    input.value = "";
    if (password) liveLogin(password);
  });
  $("live-logout").addEventListener("click", () => {
    liveLogout();
    toast("Снова записанные ответы");
  });
  let saved = null;
  try { saved = sessionStorage.getItem("livePassword"); } catch { /* хранилище недоступно */ }
  if (saved) liveLogin(saved, { silent: true });
}

// ---------- Вкладки и события ----------

function openTab(name) {
  for (const tab of document.querySelectorAll(".tab")) {
    const active = tab.dataset.tab === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  }
  for (const panel of document.querySelectorAll(".tab-panel")) {
    panel.hidden = panel.id !== `tab-${name}`;
  }
  if (name === "kb" && !state.kbLoaded) loadKb();
}

function init() {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => openTab(tab.dataset.tab));
  }

  $("composer").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("input");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    addMessage(currentRole(), text);
  });
  $("input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("composer").requestSubmit();
    }
  });

  $("insert-reply").addEventListener("click", () => {
    if (!state.suggestion) return;
    setRole("manager");
    const input = $("input");
    input.value = state.suggestion.suggestion.client_reply;
    input.focus();
  });
  $("copy-reply").addEventListener("click", () => state.suggestion && copyText(state.suggestion.suggestion.client_reply));
  $("copy-pitch").addEventListener("click", () => state.suggestion && copyText(state.suggestion.suggestion.upsell.pitch));
  $("regenerate").addEventListener("click", requestSuggestion);

  // Выбор модели (режим live или живое демо): запоминаем между визитами.
  const providerSelect = $("llm-provider");
  if (providerSelect) {
    restoreProvider();
    providerSelect.addEventListener("change", () => {
      try { localStorage.setItem("llmProvider", providerSelect.value); } catch { /* хранилище недоступно */ }
    });
  }
  if ($("live-demo")) initLiveDemo();

  $("scenario-select").addEventListener("change", (event) => applyScenario(event.target.value));
  $("new-dialog").addEventListener("click", () => {
    clearTimeout(state.debounceTimer);
    state.requestSeq += 1;
    if (state.controller) state.controller.abort();
    state.messages = [];
    $("scenario-select").value = "";
    fillLeadForm({}, "");
    renderMessages();
    resetSuggestion("Подсказка появится после сообщения клиента.");
    setRole("client");
  });

  $("lead-form").addEventListener("input", () => { renderLeadHeader(); renderMessages(); });

  $("kb-reload").addEventListener("click", reloadKb);
  $("kb-token-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const token = event.target.elements.token.value.trim();
    try { sessionStorage.setItem("adminToken", token); } catch { /* хранилище недоступно */ }
    loadKb();
  });

  loadScenarios();
}

document.addEventListener("DOMContentLoaded", init);
