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
})();
