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
    if (document.querySelector('.kag-theme-toggle')) return;
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
      var nav = document.querySelector('nav');
      if (!nav || nav.querySelector('a[href="/experiments"]')) return;
      var a = document.createElement('a');
      a.href = '/experiments';
      a.innerHTML = '<span>Опыты и модели</span>';
      nav.appendChild(a);
    })
    .catch(function () { /* не админ или нет связи — ссылки просто нет */ });
})();
