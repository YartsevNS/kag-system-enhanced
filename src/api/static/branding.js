/* Единый брендинг и оформление KAG (одна точка правды на все страницы).
 *
 * Что делает:
 *   1) название / версия / подпись  — из /api/v1/branding в элементы data-brand*;
 *   2) знак и витринное имя         — три дуги к точке (R, K, C вокруг пользователя), SVG рисуем сами;
 *   3) тема                         — светлая по умолчанию, тёмная как вариант (localStorage 'kag-theme');
 *   4) оформление из админки        — шрифт, размер, толщина, контраст, цвета (применяются переменными CSS).
 *
 * Оформление настраивается в админке (раздел «Оформление») и хранится в config_store 'system'/'branding'.
 * Цвета применяются только к светлой теме: тёмная остаётся стандартной, иначе получилось бы
 * светло-серое поле с тёмным текстом темы — нечитаемо.
 */
(function () {
  /* ── шрифты: ключ из админки -> стек с подстановками ─────────────────────
     Tahoma/Times New Roman/Verdana — системные шрифты Windows; на Linux подставится
     ближайший свободный (Liberation/DejaVu), поэтому стек задан явно. */
  var FONTS = {
    'system': "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    'pt-serif': "'PT Serif', Georgia, 'DejaVu Serif', serif",
    'tahoma': "Tahoma, 'Segoe UI', Geneva, 'DejaVu Sans', sans-serif",
    'times': "'Times New Roman', 'Liberation Serif', 'DejaVu Serif', serif",
    'georgia': "Georgia, 'Liberation Serif', 'DejaVu Serif', serif",
    'verdana': "Verdana, Geneva, 'DejaVu Sans', sans-serif",
    'arial': "Arial, Helvetica, 'Liberation Sans', 'DejaVu Sans', sans-serif"
  };

  var appearance = {};          // настройки оформления, полученные из админки

  function applyAppearance() {
    var root = document.documentElement;
    var css = root.style;

    // Тёмная тема — своя палитра: пользовательские цвета к ней не подмешиваем
    if (root.getAttribute('data-theme') === 'dark') {
      ['--font', '--font-display', '--fs-base', '--fw-base', '--accent', '--accent-solid',
       '--bg', '--surface', '--panel', '--text'].forEach(function (v) { css.removeProperty(v); });
      root.setAttribute('data-contrast', 'normal');
      return;
    }

    var a = appearance || {};
    if (a.font_body && FONTS[a.font_body]) css.setProperty('--font', FONTS[a.font_body]);
    if (a.font_display && FONTS[a.font_display]) css.setProperty('--font-display', FONTS[a.font_display]);
    if (a.font_size) css.setProperty('--fs-base', a.font_size + 'px');
    if (a.font_weight) css.setProperty('--fw-base', String(a.font_weight));
    if (a.color_accent) {
      css.setProperty('--accent', a.color_accent);
      css.setProperty('--accent-solid', a.color_accent);
    }
    if (a.color_bg) css.setProperty('--bg', a.color_bg);
    if (a.color_surface) {
      css.setProperty('--surface', a.color_surface);
      css.setProperty('--panel', a.color_surface);
    }
    if (a.color_text) css.setProperty('--text', a.color_text);
    root.setAttribute('data-contrast', a.contrast === 'high' ? 'high' : 'normal');
  }
  window.KAGAppearance = { set: function (a) { appearance = a || {}; applyAppearance(); } };

  /* ── тема ────────────────────────────────────────────────────────────── */
  var KEY = 'kag-theme';
  function current() {
    return document.documentElement.getAttribute('data-theme') || 'light';
  }
  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem(KEY, theme); } catch (e) { /* приватный режим */ }
    document.querySelectorAll('.kag-theme-toggle').forEach(function (b) {
      b.textContent = theme === 'dark' ? 'Светлая' : 'Тёмная';   // без значков: текст читается сразу
    });
    applyAppearance();
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
    applyTheme(current());
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
     поэтому лицензий не требует и работает без интернета. */
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
    var nameEl = host.querySelector('span, strong, .brand-name');
    var sub = (host.parentElement || host).querySelector('.subtitle, .brand-sub');
    if (nameEl && !sub) {
      nameEl.classList.add('brand-name');
      var col = document.createElement('div');
      col.className = 'kag-brand-text';
      host.insertBefore(col, nameEl);
      col.appendChild(nameEl);
      sub = document.createElement('div');
      sub.className = 'brand-sub';
      col.appendChild(sub);
    }
    if (sub) sub.textContent = 'RAG \u00B7 KAG \u00B7 CAG';
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', injectBrandMark);
  else injectBrandMark();

  /* ── брендинг + оформление из админки ─────────────────────────────────── */
  function apply(d) {
    var name = d.name || 'KAG';
    var version = d.version || '';
    var footer = d.footer || (version ? name + ' ' + version : name);
    document.querySelectorAll('[data-brand]').forEach(function (el) { el.textContent = name; });
    document.querySelectorAll('[data-brand-version]').forEach(function (el) { el.textContent = version; });
    document.querySelectorAll('[data-brand-footer]').forEach(function (el) { el.textContent = footer; });
    window.KAGAppearance.set(d);
  }
  fetch('/api/v1/branding', { credentials: 'include' })
    .then(function (r) { return r.json(); })
    .then(apply)
    .catch(function () { /* оставляем оформление темы по умолчанию */ });
})();
