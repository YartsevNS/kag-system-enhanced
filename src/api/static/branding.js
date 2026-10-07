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

  /* ── знак и витринное имя ─────────────────────────────────────────────
     Знак — три дуги, сходящиеся к точке: R, K, C вокруг пользователя. Рисуется здесь (SVG),
     поэтому лицензий не требует и работает без интернета. Градиент зелёный->бирюзовый->индиго
     даёт «краски», остальной интерфейс остаётся спокойным. */
  var MARK_SVG = ''
    + '<svg class="kag-mark" viewBox="0 0 48 48" role="img" aria-label="Знак KAG">'
    +   '<defs><linearGradient id="kagMarkGrad" x1="0" y1="0" x2="1" y2="1">'
    +     '<stop offset="0" stop-color="#0BA45C"/>'
    +     '<stop offset="0.55" stop-color="#0E7C6B"/>'
    +     '<stop offset="1" stop-color="#1E3A5F"/>'
    +   '</linearGradient></defs>'
    +   '<g fill="none" stroke="url(#kagMarkGrad)" stroke-linecap="round">'
    +     '<path d="M24 4 A 20 20 0 1 1 4 24" stroke-width="2.4"/>'
    +     '<path d="M24 11 A 13 13 0 1 1 11 24" stroke-width="1.9" opacity="0.85"/>'
    +     '<path d="M24 18 A 6 6 0 1 1 18 24" stroke-width="1.5" opacity="0.7"/>'
    +   '</g>'
    +   '<circle cx="24" cy="24" r="3" fill="#12161B"/>'
    +   '<circle cx="4" cy="24" r="1.7" fill="#0BA45C"/>'
    +   '<circle cx="24" cy="4" r="1.7" fill="#1E3A5F"/>'
    + '</svg>';

  function injectBrandMark() {
    if (document.querySelector('.kag-mark')) return;
    var host = document.querySelector('nav .logo, .sidebar .logo, header .logo, .brand, .logo');
    if (!host) return;
    host.classList.add('kag-brand');
    host.insertAdjacentHTML('afterbegin', MARK_SVG);
    // витринное имя: берём из брендинга, если уже подставлено, иначе оставляем как есть
    var nameEl = host.querySelector('span, strong, .brand-name');
    if (nameEl) nameEl.classList.add('brand-name');
    // расшифровка RAG · KAG · CAG: заполняем существующую подпись или добавляем свою
    var parent = host.parentElement || host;
    var sub = parent.querySelector('.subtitle, .brand-sub');
    if (!sub) {
      var d = document.createElement('div');
      d.className = 'brand-sub';
      parent.appendChild(d);
      sub = d;
    }
    sub.textContent = 'RAG \u00B7 KAG \u00B7 CAG';
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', injectBrandMark);
  else injectBrandMark();

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
      var adminItems = list.querySelectorAll('a[href="/admin"], a[href="/users"]');
      var anchor = adminItems.length ? adminItems[adminItems.length - 1] : null;
      if (anchor && anchor.parentElement === list) anchor.insertAdjacentElement('afterend', a);
      else list.appendChild(a);
    })
    .catch(function () { /* не админ или нет связи — ссылки просто нет */ });
})();
