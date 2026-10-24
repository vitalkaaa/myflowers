// Тема: выбор пользователя (localStorage) или, если он не выбирал, — настройка системы.
// Подключается в <head> без defer: атрибут ставится до отрисовки, страница не мигает светлой темой.
(function () {
  const media = matchMedia("(prefers-color-scheme: dark)");
  const saved = () => { try { return localStorage.getItem("theme"); } catch { return null; } };

  function apply() {
    const choice = saved();
    const theme = choice === "dark" || choice === "light" ? choice : (media.matches ? "dark" : "light");
    document.documentElement.dataset.theme = theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = theme === "dark" ? "#121a15" : "#f3f5f0";
    const button = document.querySelector("[data-theme-toggle]");
    if (button) {
      const label = theme === "dark" ? "Светлая тема" : "Тёмная тема";
      button.setAttribute("aria-label", label);
      button.title = label;
    }
  }

  // Переключение: если выбранная тема совпала с системной — забываем выбор и снова следуем системе
  window.toggleTheme = function () {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    try {
      if (next === (media.matches ? "dark" : "light")) localStorage.removeItem("theme");
      else localStorage.setItem("theme", next);
    } catch { /* приватный режим — тема переключится до перезагрузки */ document.documentElement.dataset.theme = next; return; }
    apply();
  };

  apply();
  media.addEventListener("change", apply);
  document.addEventListener("DOMContentLoaded", apply); // подписи кнопки — когда она появится в DOM
})();
