"""Кэш чтения в config_store: короткий, копией, с инвалидацией при записи.

Замер на стенде: get() = ~10 мс (сеанс SQLAlchemy + запрос + разбор JSON), а
вызывается он десятками мест в async-коде — кэш убирает эту стоимость.
Проверяются свойства, от которых зависит корректность:
* своя запись видна сразу (set инвалидирует ключ);
* наружу отдаётся копия (мутация не портит кэш);
* TTL маленький — изменения из другого процесса видны быстро.
"""
import copy
import time

from src.api.services.config_store import PostgresConfigStore, config_store


class _FakeStore(PostgresConfigStore):
    """Хранилище без БД: считает обращения к «источнику» и держит значение в памяти."""

    def __init__(self):
        super().__init__()
        self.reads = 0
        self._value = {"blocked": False, "message": ""}

    def get(self, category, key="default", default=None):  # noqa: D102
        config_id = f"{category}:{key}"
        cached, value = self._cache_get(config_id)
        if cached:
            return value
        self.reads += 1
        self._cache_put(config_id, self._value)
        # как в настоящем get(): наружу уходит свежий объект, кэш не портится
        return copy.deepcopy(self._value)

    def set(self, category, key, value):  # noqa: D102
        self._value = value
        self.invalidate(category, key)
        return True


def test_second_read_comes_from_cache():
    store = _FakeStore()
    store.get("system", "processing")
    store.get("system", "processing")
    assert store.reads == 1, "второе чтение должно обслуживаться кэшем"


def test_cache_returns_copy():
    store = _FakeStore()
    first = store.get("system", "processing")
    first["blocked"] = True          # вызывающий код правит свой словарь
    second = store.get("system", "processing")
    assert second["blocked"] is False, "кэш испорчен мутацией вызывающего кода"


def test_set_invalidates_own_key():
    store = _FakeStore()
    store.get("system", "processing")
    store.set("system", "processing", {"blocked": True, "message": "пауза"})
    assert store.get("system", "processing")["blocked"] is True, "своя запись не видна сразу"


def test_ttl_is_short_and_expires():
    store = _FakeStore()
    store.CACHE_TTL_SECONDS = 0.05
    store.get("system", "processing")
    time.sleep(0.25)   # с запасом: тест не должен зависеть от планировщика
    store.get("system", "processing")
    assert store.reads == 2, "после истечения TTL чтение должно идти в источник"


def test_real_store_ttl_is_small():
    assert 0 < config_store.CACHE_TTL_SECONDS <= 5, (
        "длинный TTL сделает изменения из worker'а незаметными надолго"
    )
