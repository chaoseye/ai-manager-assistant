"use strict";

// Общее для страниц демо. Разметка строится через textContent — пользовательский текст не попадает в innerHTML.

const ROLE_LABEL = { client: "Клиент", manager: "Менеджер", bot: "Бот" };

const $ = (id) => document.getElementById(id);

// Суммы не рвутся при переносе строки («32 / 900 ₽»): пробел между разрядами и перед валютой — неразрывный.
// Только при выводе: копирование и вставка в ответ берут исходный текст.
const AMOUNT_GAP_RE = /(\d) (?=\d{3}(?!\d)|₽|руб|р\.)/g;

function keepAmounts(text) {
  return typeof text === "string" ? text.replace(AMOUNT_GAP_RE, "$1 ") : text;
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = keepAmounts(value);
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child == null) continue;
    node.append(child instanceof Node ? child : document.createTextNode(keepAmounts(String(child))));
  }
  return node;
}

function formatTime(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

function rub(value) {
  return `${Number(value).toLocaleString("ru-RU")} ₽`;
}

function toast(text) {
  const node = $("toast");
  node.textContent = text;
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 1800);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = el("textarea");
    area.value = text;
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  toast("Скопировано");
}

function errorText(data, status) {
  const error = data && data.error;
  if (!error) return `Ошибка сервиса (${status})`;
  if (error.code === "invalid_request" && Array.isArray(error.details) && error.details.length) {
    return `Некорректный запрос: ${error.details[0].msg}`;
  }
  return error.message || `Ошибка сервиса (${status})`;
}

async function fetchJSON(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(errorText(data, response.status));
    error.status = response.status;
    throw error;
  }
  return data;
}
