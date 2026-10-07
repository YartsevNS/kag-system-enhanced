/* Общее меню сайта KAG — одна точка правды (05.10.2026).
 *
 * Зачем: раньше у каждой страницы был свой список пунктов (18 разных вариантов разметки),
 * в чате меню сайта не было вообще, при правке приходилось менять всё вручную.
 * Теперь список описан здесь один раз и рисуется в любой элемент с атрибутом data-site-nav.
 *
 * Использование:
 *   <div data-site-nav></div>              — полное меню
 *   <div data-site-nav="compact"></div>    — короткое (для боковой колонки чата)
 *   <script src="/static/site-nav.js" defer></script>
 *
 * Роли: разделы администратора рисуются только администратору — иначе сотрудник получил бы
 * ссылки на страницы, закрытые проверкой доступа (403): /admin, /users, /logs, /docker, /qdrant.
 *
 * Узкие экраны: страницы прячут боковую колонку на ≤768px, поэтому скрипт добавляет бургер
 * и выезжающую панель с тем же меню — навигация остаётся доступной с телефона.
 */
(function () {
  'use strict';

  var SECTIONS = [
    { title: 'Работа с документами', compact: true, items: [
      ['/', '⬡', 'Дашборд'],
      ['/chat', '💬', 'Чат с AI'],
      ['/documents', '📄', 'Документы'],
      ['/chunks', '🧩', 'Чанки'],
      ['/search', '🔎', 'Поиск'],
      ['/kg', '🕸️', 'Граф знаний']
    ]},
    { title: 'Наблюдение и данные', items: [
      ['/monitor', '🌐', 'Веб-монитор'],
      ['/news', '📰', 'Новости'],
      ['/know', '📚', 'Know'],
      ['/monitoring', '📊', 'Мониторинг'],
      ['/system', '📊', 'Состояние']
    ]},
    { title: 'Администрирование', role: 'admin', items: [
      ['/admin', '⚙️', 'Админ'],
      ['/users', '👥', 'Пользователи'],
      ['/logs', '📋', 'Логи'],
      ['/qdrant', '🗄️', 'Qdrant'],
      ['/docker', '🐳', 'Docker']
    ]},
    { title: 'Помощь', compact: true, items: [
      ['/architecture', '🔭', 'Архитектура'],
      ['/docs', '📖', 'Документация'],
      ['/guide', '📘', 'Руководство']
    ]}
  ];

  var path = location.pathname.replace(/\/+$/, '') || '/';

  function isActive(href) {
    if (href === '/') return path === '/';
    return path === href || path.indexOf(href + '/') === 0;
  }

  /* role: 'public' — без админских разделов; 'admin' — ТОЛЬКО админские (дорисовываются отдельно).
     opts.compact — только разделы, помеченные compact (и админские, если role === 'admin'). */
  function build(role, opts) {
    opts = opts || {};
    var frag = document.createDocumentFragment();
    SECTIONS.forEach(function (sec) {
      var isAdminSection = sec.role === 'admin';
      if (role !== 'admin' && isAdminSection) return;
      if (role === 'admin' && !isAdminSection) return;
      if (role !== 'admin' && opts.compact && !sec.compact) return;
      var t = document.createElement('div');
      t.className = 'nav-group';
      t.textContent = sec.title;
      frag.appendChild(t);
      sec.items.forEach(function (it) {
        var a = document.createElement('a');
        a.href = it[0];
        a.className = 'nav-item' + (isActive(it[0]) ? ' active' : '');
        a.innerHTML = '<span class="icon">' + it[1] + '</span> <span>' + it[2] + '</span>';
        if (opts.onClick) a.addEventListener('click', opts.onClick);
        frag.appendChild(a);
      });
    });
    return frag;
  }

  var targets = [];
  document.querySelectorAll('[data-site-nav]').forEach(function (el) {
    var mode = (el.getAttribute('data-site-nav') || '').trim();
    targets.push({ el: el, compact: mode === 'compact' });
    el.appendChild(build('public', { compact: mode === 'compact' }));
  });
  if (!targets.length) return;

  var burger = null, drawer = null, backdrop = null, isAdminUser = false;

  fetch('/api/v1/auth/me', { credentials: 'include' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (u) {
      if (!u || !u.is_admin) return;
      isAdminUser = true;
      targets.forEach(function (t) { t.el.appendChild(build('admin')); });
      if (drawer) drawer.appendChild(build('admin', { onClick: closeDrawer }));
    })
    .catch(function () { /* не админ или нет связи — админских пунктов просто нет */ });

  function openDrawer() {
    if (!drawer) return;
    drawer.classList.add('open');
    backdrop.classList.add('open');
    document.body.style.overflow = 'hidden';
  }
  function closeDrawer() {
    if (!drawer) return;
    drawer.classList.remove('open');
    backdrop.classList.remove('open');
    document.body.style.overflow = '';
  }

  function makeDrawer() {
    if (drawer) return;
    backdrop = document.createElement('div');
    backdrop.className = 'kag-nav-backdrop';
    backdrop.addEventListener('click', closeDrawer);

    drawer = document.createElement('div');
    drawer.className = 'kag-nav-drawer';
    var head = document.createElement('div');
    head.className = 'kag-nav-drawer-head';
    head.innerHTML = '<strong>Разделы</strong>';
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'kag-nav-close';
    close.innerHTML = '&times;';
    close.setAttribute('aria-label', 'Закрыть меню');
    close.addEventListener('click', closeDrawer);
    head.appendChild(close);
    drawer.appendChild(head);
    drawer.appendChild(build('public', { onClick: closeDrawer }));
    if (isAdminUser) drawer.appendChild(build('admin', { onClick: closeDrawer }));

    burger = document.createElement('button');
    burger.type = 'button';
    burger.className = 'kag-burger';
    burger.setAttribute('aria-label', 'Открыть меню разделов');
    burger.innerHTML = '&#9776;';
    burger.addEventListener('click', openDrawer);

    document.body.appendChild(backdrop);
    document.body.appendChild(drawer);
    document.body.appendChild(burger);

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeDrawer();
    });
  }

  function sync() {
    if (window.innerWidth <= 768) makeDrawer();
    else closeDrawer();
  }
  sync();
  window.addEventListener('resize', sync);
})();
