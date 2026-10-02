"use strict";

// Страница «amoCRM (mock)»: карточка сделки поддельного аккаунта и её лента — сообщения чата вперемешку
// со служебными примечаниями AI-помощника, как в amoCRM. Сообщения уходят в сервис как вебхуки amoCRM.

const POLL_ACTIVE_MS = 1000;
const POLL_IDLE_MS = 3000;
const SCENARIO_STEP_SECONDS = 30;

const state = {
  leads: [],
  scenarios: [],
  leadId: null,
  chatId: null,
  pollTimer: null,
  pollSeq: 0,
  lastRendered: "",
  sending: false,
};

// ---------- Карточка сделки ----------

function currentLead() {
  return state.leads.find((lead) => lead.id === state.leadId) || null;
}

function renderLead() {
  const lead = currentLead();
  const fields = $("lead-fields");
  fields.replaceChildren();
  if (!lead) return;
  const add = (key, value) => fields.append(el("dt", { text: key }), el("dd", { text: value }));
  add("Сделка", `#${lead.id} · ${lead.name}`);
  add("Этап", lead.pipeline ? `${lead.pipeline} → ${lead.stage}` : lead.stage || "не указан");
  add("Бюджет", lead.price ? rub(lead.price) : "не указан");
  add("Контакт", lead.contact_name || "без имени");
  add("Товары", lead.products.length ? lead.products.join(", ") : "нет");
  add("Теги", lead.tags.length ? lead.tags.join(", ") : "нет");
  $("feed-title").textContent = `Лента сделки #${lead.id}`;
  $("feed-sub").textContent = `чат ${state.chatId} · ${lead.contact_name || "клиент"}`;
}

function startChat(leadId) {
  stopPolling();
  state.leadId = leadId;
  // Новый чат — новый диалог в сервисе: лента показывает только его сообщения и примечания.
  state.chatId = `web-${leadId}-${Math.random().toString(36).slice(2, 8)}`;
  state.lastRendered = "";
  $("lead-select").value = String(leadId);
  renderLead();
  renderFeed({ items: [], queue: null });
  schedulePoll(0);
}

// ---------- Отправка ----------

// Чат передаётся явно: проигрывание сценария не должно дописывать в чат, который зритель открыл позже.
async function send(direction, text, {
  createdAt = null, authorName = null, leadId = state.leadId, chatId = state.chatId,
} = {}) {
  const body = { lead_id: leadId, chat_id: chatId, text, direction };
  if (createdAt) body.created_at = createdAt;
  if (authorName) body.author_name = authorName;
  await fetchJSON("/api/v1/amocrm-mock/messages", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function sendFromComposer() {
  const input = $("amo-input");
  const text = input.value.trim();
  if (!text || state.sending) return;
  const direction = document.querySelector('input[name="amo-role"]:checked').value;
  state.sending = true;
  try {
    await send(direction, text);
    input.value = "";
    schedulePoll(0);
  } catch (error) {
    toast(error.message);
  } finally {
    state.sending = false;
  }
}

async function replayScenario(id) {
  const scenario = state.scenarios.find((s) => s.id === id);
  if (!scenario) return;
  $("scenario-desc").textContent = scenario.description || "";
  $("scenario-desc").hidden = !scenario.description;
  startChat(scenario.lead.id);
  const { leadId, chatId } = state;
  const now = Math.floor(Date.now() / 1000);
  const count = scenario.dialog.length;
  state.sending = true;
  try {
    for (const [index, message] of scenario.dialog.entries()) {
      if (state.chatId !== chatId) return; // зритель выбрал другой сценарий или начал новый чат
      await send(message.role === "client" ? "in" : "out", message.text, {
        createdAt: now - (count - 1 - index) * SCENARIO_STEP_SECONDS,
        authorName: message.role === "client" ? null : message.author_name,
        leadId,
        chatId,
      });
    }
  } catch (error) {
    toast(error.message);
  } finally {
    if (state.chatId === chatId) state.sending = false;
  }
  schedulePoll(0);
}

// ---------- Лента ----------

function queueText(queue) {
  if (!queue) return { text: "", kind: "" };
  switch (queue.status) {
    case "pending":
      return queue.seconds_left > 0
        ? { text: `Пауза перед генерацией: ${Math.ceil(queue.seconds_left)} с — ждём, не допишет ли клиент`, kind: "" }
        : { text: "В очереди на генерацию…", kind: "loading" };
    case "running":
      return { text: "AI-помощник готовит подсказку…", kind: "loading" };
    case "stale":
      return { text: "Клиент написал ещё, пока шла генерация: подсказка устарела, готовим новую", kind: "" };
    case "skipped":
      return { text: `Подсказка не нужна: ${queue.error || ""}`, kind: "" };
    case "failed":
      return { text: `Подсказка не сформирована: ${queue.error || ""}`, kind: "error" };
    default:
      return queue.error ? { text: queue.error, kind: "" } : { text: "", kind: "" };
  }
}

function noteNode(item) {
  const actions = el("div", { class: "actions" });
  if (item.draft) {
    const insert = el("button", { type: "button", class: "btn btn-primary", text: "Вставить черновик в ответ" });
    insert.addEventListener("click", () => {
      document.querySelector('input[name="amo-role"][value="out"]').checked = true;
      $("amo-input").value = item.draft;
      $("amo-input").focus();
    });
    actions.append(insert);
  }
  if (item.pitch) {
    const copy = el("button", { type: "button", class: "btn", text: "Скопировать фразу допродажи" });
    copy.addEventListener("click", () => copyText(item.pitch));
    actions.append(copy);
  }
  return el("article", { class: "feed-note" },
    el("div", { class: "feed-note-head" },
      el("span", { class: "feed-note-service", text: item.service || "AI-помощник" }),
      el("span", { class: "muted small", text: `служебное примечание · ${formatTime(item.at)}` })),
    el("pre", { class: "feed-note-text", text: item.text }),
    actions.childElementCount ? actions : null);
}

function messageNode(item) {
  const role = item.direction === "in" ? "client" : item.author_type === "user" ? "manager" : "bot";
  const name = item.author_name || ROLE_LABEL[role];
  const label = role === "client" || name === ROLE_LABEL[role] ? name : `${ROLE_LABEL[role]} · ${name}`;
  const text = item.text || (item.attachment_type ? `[вложение: ${item.attachment_type}]` : "");
  return el("div", { class: `msg msg-${role}` },
    el("span", { class: "msg-author", text: `${label}, ${formatTime(item.at)}` }),
    el("div", { class: "msg-bubble", text }));
}

function renderFeed(data) {
  const snapshot = JSON.stringify(data);
  if (snapshot === state.lastRendered) return;
  state.lastRendered = snapshot;

  const feed = $("feed");
  const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
  feed.replaceChildren();
  if (!data.items.length) {
    feed.append(el("p", {
      class: "empty muted",
      text: "Напишите от имени клиента или проиграйте сценарий. Сообщения уходят в сервис как вебхуки amoCRM, а подсказка появится здесь служебным примечанием.",
    }));
  }
  for (const item of data.items) {
    feed.append(item.kind === "note" ? noteNode(item) : messageNode(item));
  }
  if (nearBottom) feed.scrollTop = feed.scrollHeight;

  const status = $("queue-status");
  const { text, kind } = queueText(data.queue);
  status.textContent = text;
  status.className = `queue-status ${kind}`;
  status.hidden = !text;
}

function stopPolling() {
  clearTimeout(state.pollTimer);
  state.pollSeq += 1;
}

function schedulePoll(delay) {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(poll, delay);
}

async function poll() {
  const seq = ++state.pollSeq;
  const chatId = state.chatId;
  let active = false;
  try {
    const params = new URLSearchParams({ lead_id: state.leadId, chat_id: chatId });
    const data = await fetchJSON(`/api/v1/amocrm-mock/feed?${params}`);
    if (seq !== state.pollSeq || chatId !== state.chatId) return;
    renderFeed(data);
    active = Boolean(data.queue && ["pending", "running"].includes(data.queue.status));
  } catch (error) {
    if (seq !== state.pollSeq) return;
    const status = $("queue-status");
    status.textContent = `Сервис недоступен: ${error.message}`;
    status.className = "queue-status error";
    status.hidden = false;
  }
  schedulePoll(active || state.sending ? POLL_ACTIVE_MS : POLL_IDLE_MS);
}

// ---------- Запуск ----------

async function init() {
  try {
    state.leads = await fetchJSON("/api/v1/amocrm-mock/leads");
    const scenarios = await fetchJSON("/api/v1/demo/scenarios");
    const leadIds = new Set(state.leads.map((lead) => lead.id));
    state.scenarios = scenarios.filter((s) => leadIds.has(s.lead.id));
  } catch (error) {
    toast(error.message);
    return;
  }

  const leadSelect = $("lead-select");
  for (const lead of state.leads) {
    const label = `#${lead.id} · ${lead.name}${lead.contact_name ? ` · ${lead.contact_name}` : ""}`;
    leadSelect.append(el("option", { value: String(lead.id), text: label }));
  }
  const scenarioSelect = $("amo-scenario");
  for (const scenario of state.scenarios) {
    scenarioSelect.append(el("option", { value: scenario.id, text: scenario.title }));
  }

  leadSelect.addEventListener("change", () => {
    scenarioSelect.value = "";
    $("scenario-desc").hidden = true;
    startChat(Number(leadSelect.value));
  });
  scenarioSelect.addEventListener("change", () => replayScenario(scenarioSelect.value));
  $("new-chat").addEventListener("click", () => {
    scenarioSelect.value = "";
    $("scenario-desc").hidden = true;
    startChat(state.leadId);
  });
  $("amo-composer").addEventListener("submit", (event) => {
    event.preventDefault();
    sendFromComposer();
  });
  $("amo-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendFromComposer();
    }
  });

  // По умолчанию — сделка первого сценария: с неё удобнее начинать показ.
  const firstLead = state.scenarios.length ? state.scenarios[0].lead.id : state.leads[0]?.id;
  if (firstLead) startChat(firstLead);
}

document.addEventListener("DOMContentLoaded", init);
