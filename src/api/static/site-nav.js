/* Общее меню сайта KAG — одна точка правды (05.10.2026).
 *
 * Зачем: раньше у каждой страницы был свой список пунктов (18 разных вариантов разметки),
 * в чате меню сайта не было вообще, а при правке приходилось менять всё вручную.
 * Теперь список описан здесь один раз и рисуется в любой элемент с атрибутом data-site-nav.
 *
 * Использование на странице:
 *   <div data-site-nav></div>
 *   <script src="/static/site-nav.js" defer></script>
 * Активный пункт подсвечивается по адресу страницы; разделы для администратора показываются
 * только администратору (иначе сотрудник видел бы ссылки, ведущие на 403).
 * На узких экранах (≤768px) страницы прячут боковую колонку — поэтому скрипт добавляет бургер
 * и выезжающую панель с тем же меню, чтобы навигация осталась доступной с телефона.
 */
(function () {
  'use strict';

  var SECTIONS = [
    { title: 'Работа с документами', items: [
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
      ['/logs', '📋', 'Логи'],
      ['/monitoring', '📊', 'Мониторинг'],
      ['/system', '📊', 'Состояние'],
      ['/qdrant', '🗄️', 'Qdrant'],
      ['/docker', '🐳', 'Docker']
    ]},
    { title: 'Администрирование', admin: true, items: [
      ['/admin', '⚙️', 'Админ'],
      ['/users', '👥', 'Пользователи']
    ]},
    { title: 'Помощь', items: [
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

  function build(isAdmin, forDrawer) {
    var frag = document.createDocumentFragment();
    SECTIONS.forEach(function (sec) {
      if (sec.admin && !isAdmin) return;          // разделы только для администратора
      if (sec.title) {
        var t = document.createElement('div');
        t.className = 'nav-group';
        t.textContent = sec.title;
        frag.appendChild(t);
      }
      sec.items.forEach(function (it) {
        var a = document.createElement('a');
        a.href = it[0];
        a.className = 'nav-item' + (isActive(it[0]) ? ' active' : '');
        a.innerHTML = '<span class="icon">' + it[1] + '</span> <span>' + it[2] + '</span>';
        if (forDrawer) a.addEventListener('click', closeDrawer);
        frag.appendChild(a);
      });
    });
    return frag;
  }

  var targets = document.querySelectorAll('[data-site-nav]');
  if (!targets.length) return;

  // Сначала рисуем общие разделы (без задержки на запрос), затем дорисовываем админские.
  targets.forEach(function (el) { el.appendChild(build(false, false)); });

  fetch('/api/v1/auth/me', { credentials: 'include' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (u) {
      if (!u || !u.is_admin) return;
      isAdminUser = true;
      var adminFrag = build(true, false);
      targets.forEach(function (el) { el.appendChild(adminFrag.cloneNode(true)); });
      if (drawer) {   // панель уже открыта/создана — дорисовываем админские разделы
        drawer.appendChild(build(true, true));
      }
    })
    .catch(function () { /* не админ или нет связи — админских пунктов просто нет */ });

  /* ── бургер и выезжающая панель для узких экранов ───────────────────────── */
  var burger = null, drawer = null, backdrop = null, isAdminUser = false;

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
    drawer.appendChild(build(false, true));
    if (isAdminUser) drawer.appendChild(build(true, true));

    burger = document.createElement('button');
    burger.type = 'button';
    burger.className = 'kag-burger';
    burger.setAttribute('aria-label', 'Открыть меню');
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
