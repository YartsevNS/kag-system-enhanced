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
 *
 * Стили меню едут вместе с компонентом (06.11.2026). Причина: пункты наследовали вид от
 * локальных правил страницы, а их не было у /card, /graph-prompt, /doc-relations и др. —
 * там меню выводилось без отступов и выглядело «не по схеме». Теперь вид задаётся здесь и
 * одинаков на всех страницах; правила скоуплены на контейнер меню, чтобы не задевать
 * ссылки самой страницы. Значения совпадают с theme.css, но с fallback-цветами: часть
 * страниц не подключает theme.css и переменных может не быть.
 */
(function () {
  'use strict';

  var NAV_CSS = [
    '[data-site-nav],.kag-nav-drawer{display:block}',
    '[data-site-nav] .nav-group,.kag-nav-drawer .nav-group{font-size:12px;letter-spacing:.02em;',
    'color:var(--text3,#898989);padding:14px 24px 4px;text-transform:none}',
    '[data-site-nav] .nav-group::after,.kag-nav-drawer .nav-group::after{content:"";display:block;',
    'height:1px;background:var(--border-s,#242424);margin-top:7px}',
    '[data-site-nav] .nav-item,.kag-nav-drawer .nav-item{display:flex;align-items:center;gap:9px;',
    'padding:8px 24px;color:var(--text2,#b4b4b4);text-decoration:none;font-size:14px;',
    'border-left:2px solid transparent;transition:color .15s,background .15s}',
    '[data-site-nav] .nav-item:hover,.kag-nav-drawer .nav-item:hover{color:var(--text,#fafafa);',
    'background:var(--elevated,#242424)}',
    '[data-site-nav] .nav-item.active,.kag-nav-drawer .nav-item.active{color:var(--accent,#3ecf8e);',
    'font-weight:600;background:var(--accent-tint,rgba(62,207,142,.12));border-left-color:var(--accent,#3ecf8e)}'
  ].join('');

  function injectNavStyles() {
    if (document.getElementById('kag-site-nav-styles')) return;
    var st = document.createElement('style');
    st.id = 'kag-site-nav-styles';
    st.textContent = NAV_CSS;
    document.head.appendChild(st);
  }

  var SECTIONS = [
    { title: 'Работа с документами', compact: true, items: [
      ['/', 'Дашборд'],
      ['/chat', 'Чат с AI'],
      ['/documents', 'Документы'],
      ['/search', 'Поиск'],
      ['/chunks', 'Чанки'],
      ['/labels', 'Спорные случаи']
    ]},
    { title: 'Знания', compact: true, items: [
      // Граф знаний из TypeDB (схема, объекты, связи). Ранее здесь был Neo4j —
      // выведен из кода 12.11.2026, страница показывает онтологию.
      ['/kg', 'Граф знаний'],
      ['/ontology', 'Онтология'],
      ['/terms', 'Словарь терминов'],
      // Пробелы схемы: термины из вопросов и корпуса, которых нет в онтологии.
      ['/ontology/gaps', 'Пробелы схемы'],
      // Журнал ошибок онтологии: файл /var/log/kag/ontology/errors.log + таблица.
      ['/errors', 'Ошибки онтологии']
    ]},
    { title: 'Очереди решений', role: 'admin', items: [
      // Сводка: что требует человека сегодня и что накопилось. Один экран вместо шести.
      ['/ontology/health', 'Здоровье онтологии'],
      // Решения, которые приняла система сама (правила), с возможностью отозвать.
      // Здесь же видно, сколько осталось человеку — это и есть уровень автоматизации.
      ['/ontology/automation', 'Авто-решения'],
      // Очередь возможных дублей: другой файл того же документа.
      ['/duplicates', 'Возможные дубли']
    ]},
    { title: 'Наблюдение', items: [
      // /monitor — источники (RSS, порталы) и расписание проверок.
      ['/monitor', 'Источники и RSS'],
      // /monitoring — слежение за страницами и папками (change detection).
      ['/monitoring', 'Страницы и папки'],
      ['/news', 'Лента RSS'],
      // /documents?collection=news — те же материалы как документы.
      ['/documents?collection=news', 'Новости (документы)'],
      ['/know', 'Know'],
      ['/system', 'Состояние']
    ]},
    { title: 'Администрирование', role: 'admin', items: [
      ['/admin', 'Админ'],
      ['/users', 'Пользователи'],
      ['/logs', 'Логи'],
      ['/semantics', 'Семантика'],
      // Задание извлечения сущностей: текст, по которому модель строит сущности
      // и связи для TypeDB. Правится в админке, история версий хранится.
      ['/ontology/prompt', 'Промпт сущностей'],
      // Задание извлечения связей графа (последствия правки, словарь пар).
      ['/graph-prompt', 'Связи графа'],
      // Учебная страница: TBox/ABox, роли, ключи, правила — на примерах живой схемы.
      ['/ontology/guide', 'Уроки онтологии']
    ]},
    { title: 'Разработчику', role: 'admin', items: [
      // Инструменты разработчика: не для заказчика, поэтому отдельной группой и под admin.
      ['/docker', 'Docker'],
      ['/qdrant', 'Qdrant'],
      ['/experiments', 'Опыты и модели']
    ]},
    { title: 'Помощь', compact: true, items: [
      ['/architecture', 'Архитектура'],
      ['/docs', 'Документация'],
      ['/guide', 'Руководство'],
      ['/prompts-help', 'Справка: промпты']
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
        a.textContent = it[1];
        if (opts.onClick) a.addEventListener('click', opts.onClick);
        frag.appendChild(a);
      });
    });
    return frag;
  }

  injectNavStyles();

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

    // Переключатель темы в панели: на телефоне плавающая кнопка внизу наезжала на поля ввода
    if (window.KAGTheme) {
      var themeBtn = document.createElement('button');
      themeBtn.type = 'button';
      themeBtn.className = 'kag-theme-toggle';
      themeBtn.style.margin = '14px 20px 0';
      themeBtn.textContent = window.KAGTheme.get() === 'dark' ? 'Светлая' : 'Тёмная';
      themeBtn.addEventListener('click', function () { window.KAGTheme.toggle(); });
      drawer.appendChild(themeBtn);
    }

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
