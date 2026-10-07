/* Единый брендинг KAG.
   Подтягивает название/версию/подпись из /api/v1/branding и заполняет
   элементы с data-brand / data-brand-version / data-brand-footer.
   Одна настройка в админке (Брендинг) — применяется на всех страницах.

   Плюс тема: светлая по умолчанию (интерфейс для офисных работников), тёмная — вариант.
   Значение хранится в localStorage ('kag-theme'); раннее применение делает inline-скрипт в <head>,
   здесь — переключатель и синхронизация. */
(function () {
  /* ── тема ────────────────────────────────────────────────────────────── */
  var KEY = 'kag-theme';
  function current() {
    return document.documentElement.getAttribute('data-theme') || 'light';
  }
  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem(KEY, theme); } catch (e) { /* приватный режим */ }
    document.querySelectorAll('.kag-theme-toggle').forEach(function (b) {
      b.textContent = theme === 'dark' ? '\u2600\uFE0F Светлая' : '\u{1F319} Тёмная';
    });
  }
  window.KAGTheme = {
    get: current,
    set: applyTheme,
    toggle: function () { applyTheme(current() === 'light' ? 'dark' : 'light'); }
  };
  function injectToggle() {
    // Страница может иметь свой переключатель (например, админка: #theme-toggle) — не дублируем.
    if (document.querySelector('.kag-theme-toggle') || document.querySelector('#theme-toggle')
        || document.querySelector('[onclick*="toggleTheme"]')) return;
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'kag-theme-toggle float';
    b.title = 'Переключить светлую/тёмную тему';
    b.onclick = function () { window.KAGTheme.toggle(); };
    document.body.appendChild(b);
    applyTheme(current());            // выставить подпись на кнопке
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectToggle);
  } else {
    injectToggle();
  }
  window.addEventListener('storage', function (e) {   // смена темы в другой вкладке
    if (e.key === KEY && e.newValue) applyTheme(e.newValue);
  });

  /* ── брендинг ────────────────────────────────────────────────────────── */
  function apply(d) {
    var name = d.name || 'KAG';
    var version = d.version || '';
    var footer = d.footer || (version ? name + ' ' + version : name);
    document.querySelectorAll('[data-brand]').forEach(function (el) {
      el.textContent = name;
    });
    document.querySelectorAll('[data-brand-version]').forEach(function (el) {
      el.textContent = version;
    });
    document.querySelectorAll('[data-brand-footer]').forEach(function (el) {
      el.textContent = footer;
    });
  }
  fetch('/api/v1/branding', { credentials: 'include' })
    .then(function (r) { return r.json(); })
    .then(apply)
    .catch(function () { /* оставляем как есть */ });

  /* Пункт «Опыты и модели» — только для администраторов: страница /experiments
     отдаёт 302 обычным пользователям, поэтому и ссылку показываем только админу. */
  fetch('/api/v1/auth/me', { credentials: 'include' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (u) {
      if (!u || !u.is_admin) return;
      var list = document.querySelector('nav .nav-items') || document.querySelector('nav');
      if (!list || list.querySelector('a[href="/experiments"]')) return;
      var a = document.createElement('a');
      a.href = '/experiments';
      a.className = 'nav-item';
      a.innerHTML = '<span class="icon">🧪</span> <span>Опыты и модели</span>';
      list.appendChild(a);
    })
    .catch(function () { /* не админ или нет связи — ссылки просто нет */ });
})();
