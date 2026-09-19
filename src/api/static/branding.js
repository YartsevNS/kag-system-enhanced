/* Единый брендинг KAG.
   Подтягивает название/версию/подпись из /api/v1/branding и заполняет
   элементы с data-brand / data-brand-version / data-brand-footer.
   Одна настройка в админке (Брендинг) — применяется на всех страницах. */
(function () {
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
     отдаёт 302 обычным пользователям, поэтому и ссылку показываем только админу.
     Проверяем по /auth/me (поле is_admin), молча выходим при любой ошибке. */
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
