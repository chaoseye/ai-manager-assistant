"use strict";

// Общее для страниц демо. Разметка строится через textContent — пользовательский текст не попадает в innerHTML.

const ROLE_LABEL = { client: "Клиент", manager: "Менеджер", bot: "Бот" };

const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child == null) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function formatTime(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

function rub(value) {
  return `${Number(value).toLocaleString("ru-RU")} ₽`;
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
