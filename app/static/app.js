// Подтверждение для форм удаления (inline-обработчики запрещены CSP)
document.addEventListener("submit", (e) => {
  const message = e.target.dataset.confirm;
  if (message && !confirm(message)) e.preventDefault();
});

// Фото загружается сразу после выбора файла, фильтры истории — сразу после выбора
document.addEventListener("change", (e) => {
  if (!e.target.matches("[data-autosubmit]")) return;
  if (e.target.type === "file" && !e.target.files.length) return;
  e.target.form.submit();
});

// Сортировка через выпадающий список
document.addEventListener("change", (e) => {
  if (e.target.id === "sort-select") location.search = "?sort=" + e.target.value;
});

// ---------- поиск и фильтры списка растений ----------
// Состояние хранится в sessionStorage: после отметки и обновления списка фильтры не сбрасываются.
const FILTERS = { filter: "value", "room-filter": "value", "due-filter": "checked", "toxic-filter": "checked" };

function applyFilters() {
  const list = document.getElementById("plants-list");
  if (!list) return;
  const get = (id) => { const el = document.getElementById(id); return el ? el[FILTERS[id]] : ""; };
  const q = String(get("filter")).trim().toLowerCase();
  const room = get("room-filter");
  let shown = 0;
  const items = list.querySelectorAll("[data-item]");
  items.forEach((item) => {
    const d = item.dataset;
    const hide = (q && !d.name.includes(q)) || (room && d.location !== room)
      || (get("due-filter") && !d.due) || (get("toxic-filter") && !d.toxic);
    item.hidden = Boolean(hide);
    if (!hide) shown++;
  });
  const empty = list.querySelector(".filter-empty");
  if (empty) empty.hidden = shown > 0 || items.length === 0;
}

function restoreFilters() {
  const saved = JSON.parse(sessionStorage.getItem("plant-filters") || "{}");
  for (const [id, prop] of Object.entries(FILTERS)) {
    const el = document.getElementById(id);
    if (el && id in saved) el[prop] = saved[id];
  }
  applyFilters();
}

function onFilterChange(e) {
  if (!(e.target.id in FILTERS)) return;
  const saved = {};
  for (const [id, prop] of Object.entries(FILTERS)) {
    const el = document.getElementById(id);
    if (el) saved[id] = el[prop];
  }
  sessionStorage.setItem("plant-filters", JSON.stringify(saved));
  applyFilters();
}
document.addEventListener("input", onFilterChange);
document.addEventListener("change", onFilterChange);

// Сортировка таблицы: клик — сортировать по колонке (повторный клик меняет направление),
// Shift+клик — добавить колонку к текущей сортировке.
document.addEventListener("click", (e) => {
  const link = e.target.closest("a.sort");
  if (!link) return;
  e.preventDefault();
  const key = link.dataset.key;
  const current = (new URLSearchParams(location.search).get("sort") || "")
    .split(",").filter(Boolean);
  const index = current.findIndex((p) => p.replace(/^-/, "") === key);
  let next;
  if (e.shiftKey) {
    next = [...current];
    if (index === -1) next.push(key);
    else next[index] = next[index].startsWith("-") ? key : "-" + key;
  } else {
    next = [index !== -1 && !current[index].startsWith("-") ? "-" + key : key];
  }
  location.search = "?sort=" + next.join(",");
});

// Высота таблицы подгоняется под окно: низ таблицы всегда виден, заголовки закреплены сверху.
// На узких экранах таблица не скроллится отдельно — работает обычная прокрутка страницы.
function fitTable() {
  const wrap = document.querySelector(".table-wrap");
  if (!wrap) return;
  if (matchMedia("(max-width: 960px)").matches) {
    wrap.style.maxHeight = "";
    return;
  }
  const top = wrap.getBoundingClientRect().top + window.scrollY;
  const bottomGap = 30; // отступ под карточкой таблицы
  wrap.style.maxHeight = Math.max(320, window.innerHeight - top - bottomGap) + "px";
}
window.addEventListener("load", fitTable);
window.addEventListener("resize", fitTable);

// ---------- отметки без перезагрузки страницы ----------
// Формы с data-ajax отправляются через fetch; сервер отвечает JSON, после чего обновляются
// только блоки страницы с data-refresh — прокрутка и поиск остаются на месте.

const toast = {
  el: () => document.getElementById("toast"),
  timer: null,
  show(text, undo, isError) {
    const el = this.el();
    if (!el) return;
    el.querySelector(".toast-text").textContent = text;
    el.classList.toggle("is-error", Boolean(isError));
    const button = el.querySelector(".toast-undo");
    button.hidden = !undo;
    button.onclick = undo ? () => { this.hide(); post(undo.url, undo.fields); } : null;
    el.hidden = false;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.hide(), undo ? 7000 : 4000);
  },
  hide() { const el = this.el(); if (el) el.hidden = true; },
};

function csrfToken() {
  const meta = document.querySelector('meta[name="csrf"]');
  return meta ? meta.content : "";
}

async function refreshBlocks() {
  const res = await fetch(location.href, { headers: { Accept: "text/html" } });
  if (!res.ok || res.redirected) { location.reload(); return; }
  const doc = new DOMParser().parseFromString(await res.text(), "text/html");
  // фокус с клавиатуры: если он был внутри обновляемого блока — ставим на кнопку с тем же номером
  // (отмеченное растение уходит вниз, и фокус оказывается на следующем)
  const FOCUSABLE = "a[href], button:not([disabled]), input, select, textarea, summary";
  const active = document.activeElement;
  const block = active && active.closest("[data-refresh][id]");
  const focusIndex = block ? [...block.querySelectorAll(FOCUSABLE)].indexOf(active) : -1;
  document.querySelectorAll("[data-refresh][id]").forEach((old) => {
    const fresh = doc.getElementById(old.id);
    if (fresh) old.replaceWith(document.importNode(fresh, true));
  });
  const meta = doc.querySelector('meta[name="csrf"]');
  if (meta) document.querySelector('meta[name="csrf"]').content = meta.content;
  applyFilters();
  if (block) {
    const items = [...(document.getElementById(block.id) || document).querySelectorAll(FOCUSABLE)];
    const target = items[Math.min(focusIndex, items.length - 1)];
    if (target) target.focus();
  } else if (active && active.isConnected) {
    active.focus();
  }
}

async function send(url, body) {
  let res;
  try {
    res = await fetch(url, { method: "POST", body, headers: { "X-Requested-With": "fetch", Accept: "application/json" } });
  } catch {
    toast.show("Нет связи с сервером — попробуйте ещё раз", null, true);
    return;
  }
  const type = res.headers.get("content-type") || "";
  if (!type.includes("application/json")) { location.reload(); return; } // например, сессия закончилась
  const data = await res.json();
  if (!data.ok) { toast.show(data.error || "Не получилось", null, true); return; }
  await refreshBlocks();
  toast.show(data.message, data.undo);
}

function post(url, fields) {
  const body = new FormData();
  body.append("csrf", csrfToken());
  for (const [k, v] of Object.entries(fields || {})) body.append(k, v);
  return send(url, body);
}

document.addEventListener("submit", async (e) => {
  const form = e.target;
  if (!form.matches("[data-ajax]") || e.defaultPrevented) return;
  e.preventDefault();
  if (form.getAttribute("aria-busy")) return; // защита от двойного нажатия (кнопки не отключаем — с них ушёл бы фокус)
  const body = new FormData(form);
  if (e.submitter && e.submitter.name) body.append(e.submitter.name, e.submitter.value);
  form.setAttribute("aria-busy", "true");
  try {
    await send(form.action, body);
  } finally {
    form.removeAttribute("aria-busy");
  }
});

// ---------- меню «…» (details): закрывать по клику мимо и по Esc ----------
document.addEventListener("click", (e) => {
  document.querySelectorAll("details.more[open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  document.querySelectorAll("details.more[open]").forEach((d) => { d.open = false; d.querySelector("summary").focus(); });
});

// Кнопки-подписи для выбора файла работают и с клавиатуры
document.addEventListener("keydown", (e) => {
  const label = e.target.closest && e.target.closest("label[tabindex]");
  if (!label || (e.key !== "Enter" && e.key !== " ")) return;
  e.preventDefault();
  const input = label.querySelector("input[type=file]");
  if (input) input.click();
});

// Имя выбранного файла рядом с кнопкой «Фото к записи»
document.addEventListener("change", (e) => {
  if (!e.target.matches("[data-filename]")) return;
  const out = e.target.closest("form").querySelector(".file-name");
  if (out) out.textContent = e.target.files.length ? "📎 " + e.target.files[0].name : "";
});

// Правка записи дневника: показать форму вместо текста
document.addEventListener("click", (e) => {
  const button = e.target.closest("[data-edit]");
  if (!button) return;
  const item = document.getElementById(button.dataset.edit);
  const form = item.querySelector(".comment-edit");
  const view = item.querySelector("[data-view]");
  form.hidden = !form.hidden;
  if (view) view.hidden = !form.hidden;
  if (!form.hidden) form.querySelector("textarea").focus();
});

// Кнопка «← Вернуться» на странице ошибки — назад, если есть куда (введённое в форме браузер обычно помнит)
document.addEventListener("click", (e) => {
  if (e.target.closest("[data-back]") && history.length > 1) {
    e.preventDefault();
    history.back();
  }
});

// ---------- просмотр фото поверх страницы ----------
const lightbox = {
  items: [],
  index: 0,
  el: () => document.getElementById("lightbox"),
  open(link) {
    const group = link.dataset.lightbox;
    const seen = new Set();
    this.items = [...document.querySelectorAll(`a[data-lightbox="${group}"]`)].filter((a) => {
      if (seen.has(a.href)) return false;
      seen.add(a.href);
      return true;
    });
    this.index = Math.max(0, this.items.findIndex((a) => a.href === link.href));
    this.opener = link;
    this.show();
    this.el().hidden = false;
    document.body.classList.add("no-scroll");
    this.el().querySelector(".lightbox-close").focus();
  },
  show() {
    const el = this.el();
    const link = this.items[this.index];
    const img = el.querySelector("img");
    img.src = link.href;
    img.alt = link.querySelector("img") ? link.querySelector("img").alt : "";
    const date = link.dataset.caption || "";
    const pretty = date ? date.split("-").reverse().join(".") : "";
    el.querySelector("figcaption").textContent = `${pretty}${this.items.length > 1 ? ` · ${this.index + 1} из ${this.items.length}` : ""}`;
    el.querySelector(".lightbox-prev").hidden = this.items.length < 2;
    el.querySelector(".lightbox-next").hidden = this.items.length < 2;
  },
  step(delta) {
    if (this.items.length < 2) return;
    this.index = (this.index + delta + this.items.length) % this.items.length;
    this.show();
  },
  close() {
    this.el().hidden = true;
    document.body.classList.remove("no-scroll");
    if (this.opener) this.opener.focus();
  },
};

document.addEventListener("click", (e) => {
  const link = e.target.closest("a[data-lightbox]");
  if (link && document.getElementById("lightbox") && !e.metaKey && !e.ctrlKey) {
    e.preventDefault();
    lightbox.open(link);
    return;
  }
  const box = e.target.closest("#lightbox");
  if (!box) return;
  if (e.target.closest(".lightbox-prev")) lightbox.step(-1);
  else if (e.target.closest(".lightbox-next")) lightbox.step(1);
  else if (e.target.closest(".lightbox-close") || !e.target.closest("figure")) lightbox.close();
});
document.addEventListener("keydown", (e) => {
  const box = document.getElementById("lightbox");
  if (!box || box.hidden) return;
  if (e.key === "Escape") lightbox.close();
  else if (e.key === "ArrowLeft") lightbox.step(-1);
  else if (e.key === "ArrowRight") lightbox.step(1);
  else if (e.key === "Tab") { // фокус не уходит из окна просмотра
    const buttons = [...box.querySelectorAll("button:not([hidden])")];
    const i = buttons.indexOf(document.activeElement);
    e.preventDefault();
    buttons[(i + (e.shiftKey ? -1 : 1) + buttons.length) % buttons.length].focus();
  }
});
let touchX = null;
document.addEventListener("touchstart", (e) => { if (e.target.closest("#lightbox")) touchX = e.touches[0].clientX; }, { passive: true });
document.addEventListener("touchend", (e) => {
  if (touchX === null) return;
  const dx = e.changedTouches[0].clientX - touchX;
  touchX = null;
  if (Math.abs(dx) > 50) lightbox.step(dx < 0 ? 1 : -1);
});

// ---------- новое растение: заполнить по справочнику ----------
document.addEventListener("change", (e) => {
  if (!e.target.matches("[data-catalog]")) return;
  const data = JSON.parse(document.getElementById("catalog-data").textContent);
  const entry = data[e.target.value];
  if (!entry) return;
  const form = document.getElementById("plant-form");
  for (const [field, value] of Object.entries(entry.values)) {
    const input = form.elements[field];
    if (!input) continue;
    if (field === "name" && input.value.trim()) continue; // своё название не трогаем
    input.value = value ?? "";
  }
  toast.show("Заполнено по справочнику: " + entry.label + " — проверьте и сохраните");
});

restoreFilters();
fitTable();

// PWA: service worker нужен, чтобы сайт можно было установить на главный экран телефона
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
