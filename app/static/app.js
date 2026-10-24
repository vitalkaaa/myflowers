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

// Сортировка на телефоне — через выпадающий список
document.addEventListener("change", (e) => {
  if (e.target.id === "sort-select") location.search = "?sort=" + e.target.value;
});

// Быстрый поиск по таблице
document.addEventListener("input", (e) => {
  if (e.target.id !== "filter") return;
  const q = e.target.value.trim().toLowerCase();
  document.querySelectorAll("table.plants tbody tr[data-name]").forEach((row) => {
    row.hidden = q && !row.dataset.name.includes(q);
  });
});

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
fitTable();
