"""Настройки контекста чата: глубина, пометка лучшего фрагмента, порог отказа.

Зачем: это рычаги «скорость против точности». Меньше фрагментов — короче промпт (быстрее и
дешевле), но выше риск потерять факт; пометка лучшего фрагмента помогает модели опираться на
главное; порог отказа экономит вызов LLM на вопросах вне корпуса. Значения задаются в привязке
функции chat (админка) и меняются только по замеру.
"""
import asyncio


def _run(coro):
    return asyncio.run(coro)


def _provider():
    from src.api.services.provider_service import ProviderConfig

    return ProviderConfig(id="p1", name="тестовый", type="deepseek", url="http://127.0.0.1:9", api_key="k")


def _func_map(parameters):
    from src.api.services.provider_service import FunctionMap

    return FunctionMap(function="chat", provider_id="p1", model="deepseek-flash",
                       system_prompt="отвечай кратко", parameters=parameters)


def _chunk(score, cid="c1", content="текст фрагмента", filename="doc.pdf"):
    return {"id": cid, "score": score, "content": content, "document_id": "d1",
            "filename": filename, "chunk_id": cid}


def _service(monkeypatch, parameters, results, captured):
    from src.api.services.chat_service import ChatService

    service = ChatService()
    monkeypatch.setattr(service, "_get_chat_provider", lambda: (_provider(), _func_map(parameters)))
    monkeypatch.setattr(service, "_get_total_docs", lambda: 0)

    async def fake_widening(query, limit, **kwargs):
        captured["limit"] = limit
        return list(results)

    async def fake_analysis(_q):
        return None

    def fake_meta(_q):  # синхронный: в коде вызывается без await
        return None

    async def fake_comparison(*a, **kw):
        return "", []

    async def fake_decompose(_q):
        return []

    async def fake_call_llm(**kwargs):
        captured["messages"] = kwargs.get("messages")
        captured["called"] = captured.get("called", 0) + 1
        return {"id": "1", "content": "ответ", "model": "m", "usage": {}, "elapsed": 0.1}

    monkeypatch.setattr(service, "_search_with_widening", fake_widening)
    monkeypatch.setattr(service, "_detect_query_analysis", fake_analysis)
    monkeypatch.setattr(service, "_detect_meta_intent", fake_meta)
    monkeypatch.setattr(service, "_comparison_context", fake_comparison)
    monkeypatch.setattr(service, "_decompose_query", fake_decompose)
    monkeypatch.setattr(service, "_call_llm", fake_call_llm)
    return service


def test_context_limit_is_passed_to_search(monkeypatch):
    captured = {}
    service = _service(monkeypatch, {"context_limit": 5}, [_chunk(0.9)], captured)
    _run(service.generate_response(user_message="вопрос", use_rag=True, is_admin=True))
    assert captured["limit"] == 5, f"поиск вызван с limit={captured['limit']}"


def test_default_context_limit_is_10(monkeypatch):
    """Дефолт глубины — 10 (решение владельца, несмотря на замер).

    Замер 20.09.2026 на внешнем наборе (75 вопросов, по 4 прогона на настройку): глубина 10 —
    0.737, глубина 20 — 0.820 при +0,3 с на ответ, диапазоны не пересекаются. Но контекст в 20
    фрагментов дороже по токенам у платного провайдера, поэтому 20/30/40 проверяем позже на
    локальных моделях, а дефолт остаётся 10 (docs/handoff.md: «Что дальше», п. 7 и «Пилот
    второй волны»). Тест стережёт дефолт, чтобы он не уехал без решения.
    """
    captured = {}
    service = _service(monkeypatch, {}, [_chunk(0.9)], captured)
    _run(service.generate_response(user_message="вопрос", use_rag=True, is_admin=True))
    assert captured["limit"] == 10, f"дефолт глубины контекста — 10, а не {captured['limit']}"

    # А на конкретный запрос глубину 20 поставить можно: ручка существует, просто не по умолчанию.
    captured = {}
    service = _service(monkeypatch, {}, [_chunk(0.9)], captured)
    _run(service.generate_response(user_message="вопрос", use_rag=True, is_admin=True, context_limit=20))
    assert captured["limit"] == 20


def test_context_limit_is_clamped(monkeypatch):
    captured = {}
    service = _service(monkeypatch, {"context_limit": 99}, [_chunk(0.9)], captured)
    _run(service.generate_response(user_message="вопрос", use_rag=True, is_admin=True))
    assert captured["limit"] == 20, "верхняя граница глубины контекста — 20"

    captured = {}
    service = _service(monkeypatch, {"context_limit": 1}, [_chunk(0.9)], captured)
    _run(service.generate_response(user_message="вопрос", use_rag=True, is_admin=True))
    assert captured["limit"] == 3, "нижняя граница глубины контекста — 3"


def test_mark_best_marks_the_top_fragment(monkeypatch):
    captured = {}
    service = _service(monkeypatch, {"mark_best": True},
                       [_chunk(0.9, "c1", "первый фрагмент"), _chunk(0.5, "c2", "второй фрагмент")],
                       captured)
    _run(service.generate_response(user_message="вопрос", use_rag=True, is_admin=True))
    joined = "\n".join(m.get("content", "") for m in captured["messages"])
    assert "САМЫЙ РЕЛЕВАНТНЫЙ ФРАГМЕНТ" in joined, "пометка лучшего фрагмента не попала в промпт"
    assert joined.index("первый фрагмент") < joined.index("второй фрагмент")


def test_mark_best_off_by_default(monkeypatch):
    captured = {}
    service = _service(monkeypatch, {}, [_chunk(0.9), _chunk(0.5)], captured)
    _run(service.generate_response(user_message="вопрос", use_rag=True, is_admin=True))
    joined = "\n".join(m.get("content", "") for m in captured["messages"])
    assert "САМЫЙ РЕЛЕВАНТНЫЙ ФРАГМЕНТ" not in joined


def test_low_score_threshold_refuses_without_llm(monkeypatch):
    captured = {}
    service = _service(monkeypatch, {"min_top_score": 0.5}, [_chunk(0.3)], captured)
    response = _run(service.generate_response(user_message="вопрос", use_rag=True, is_admin=True))
    assert "не найдена" in response["response"].lower()
    assert captured.get("called") is None, "при отказе по порогу модель вызываться не должна"
    assert response["metadata"].get("refused_low_score") is True
    assert response["sources"] == []


def test_threshold_off_keeps_normal_answer(monkeypatch):
    captured = {}
    service = _service(monkeypatch, {"min_top_score": 0.0}, [_chunk(0.3)], captured)
    response = _run(service.generate_response(user_message="вопрос", use_rag=True, is_admin=True))
    assert response["response"] == "ответ"
    assert captured.get("called") == 1


def test_threshold_passes_when_top_score_is_high(monkeypatch):
    captured = {}
    service = _service(monkeypatch, {"min_top_score": 0.5}, [_chunk(0.9)], captured)
    response = _run(service.generate_response(user_message="вопрос", use_rag=True, is_admin=True))
    assert response["response"] == "ответ"
    assert captured.get("called") == 1


# ── глубина контекста из запроса клиента (перебивает привязку функции) ──────────

def test_request_context_limit_wins_over_binding(monkeypatch):
    captured = {}
    service = _service(monkeypatch, {"context_limit": 10}, [_chunk(0.9)], captured)
    _run(service.generate_response(user_message="вопрос", use_rag=True, is_admin=True, context_limit=3))
    assert captured["limit"] == 3, f"клиент просил 3, поиск вызван с {captured['limit']}"


def test_request_context_limit_is_clamped(monkeypatch):
    captured = {}
    service = _service(monkeypatch, {}, [_chunk(0.9)], captured)
    _run(service.generate_response(user_message="вопрос", use_rag=True, is_admin=True, context_limit=99))
    assert captured["limit"] == 20, "сервер обязан зажимать границы, а не доверять клиенту"

    captured = {}
    service = _service(monkeypatch, {}, [_chunk(0.9)], captured)
    _run(service.generate_response(user_message="вопрос", use_rag=True, is_admin=True, context_limit=1))
    assert captured["limit"] == 3


def test_request_context_limit_none_uses_binding(monkeypatch):
    captured = {}
    service = _service(monkeypatch, {"context_limit": 7}, [_chunk(0.9)], captured)
    _run(service.generate_response(user_message="вопрос", use_rag=True, is_admin=True))
    assert captured["limit"] == 7, "без запроса клиента действует привязка функции"


def test_broken_request_context_limit_ignored(monkeypatch):
    captured = {}
    service = _service(monkeypatch, {"context_limit": 7}, [_chunk(0.9)], captured)
    _run(service.generate_response(user_message="вопрос", use_rag=True, is_admin=True,
                                   context_limit="abc"))
    assert captured["limit"] == 7, "мусор в поле не должен ломать поиск"


# ── отсечение слабых фрагментов (относительный порог) ──────────────────────────

def test_apply_score_gap_drops_weak_tail():
    from src.api.services.chat_service import apply_score_gap

    # Фрагментов заведомо больше минимума (3), иначе сработает правило «минимум три»
    # и тест будет проверять не отсечение, а его.
    frags = [_chunk(0.80, "a"), _chunk(0.79, "b"), _chunk(0.78, "c"),
             _chunk(0.70, "d"), _chunk(0.64, "e")]
    kept = apply_score_gap(frags, 0.05)
    assert [f["id"] for f in kept] == ["a", "b", "c"], "ниже лучшего на 0.05 — выбрасываем"


def test_apply_score_gap_keeps_minimum_three():
    from src.api.services.chat_service import apply_score_gap

    frags = [_chunk(0.90, "a"), _chunk(0.50, "b"), _chunk(0.40, "c"), _chunk(0.30, "d")]
    kept = apply_score_gap(frags, 0.25)
    assert len(kept) == 3, "даже при жёстком пороге оставляем минимум три фрагмента"
    assert [f["id"] for f in kept] == ["a", "b", "c"], "оставляем именно лучшие три"


def test_apply_score_gap_off_and_empty():
    from src.api.services.chat_service import apply_score_gap

    frags = [_chunk(0.9, "a"), _chunk(0.1, "b")]
    assert len(apply_score_gap(frags, 0)) == 2, "порог 0 = выключено"
    assert apply_score_gap([], 0.1) == []


def test_resolve_score_gap_priority_and_bounds():
    from src.api.services.chat_service import resolve_score_gap

    # запрос важнее привязки
    assert resolve_score_gap({"min_score_gap": 0.25}, 0.10, "universal")[0] == 0.10
    # из интерфейса нельзя выключить отсечение (0) и нельзя взять почти всю выдачу (мягкий край)
    assert resolve_score_gap({}, 0.0, None)[0] == 0.02
    assert resolve_score_gap({}, 0.9, None)[0] == 0.12
    # привязка может выключить отсечение — это решение администратора
    assert resolve_score_gap({"min_score_gap": 0}, None, "universal")[0] == 0.0
    # доменная карта важнее доменного дефолта
    assert resolve_score_gap({"score_gap_by_domain": {"accounting": 0.03}}, None, "accounting")[0] == 0.03
    # доменный дефолт, когда ничего не задано
    assert resolve_score_gap({}, None, "universal")[0] == 0.08
    assert resolve_score_gap({}, None, "infosec")[0] == 0.03
    # неизвестный домен — встроенный дефолт
    assert resolve_score_gap({}, None, "нечто")[0] == 0.05
    assert resolve_score_gap({}, None, None)[0] == 0.05


def test_smaller_gap_is_stricter():
    """Смысл числа: это допустимое отставание от лучшего, поэтому МЕНЬШЕ = жёстче.

    Тест фиксирует семантику, потому что её легко перевернуть (первая версия подписей
    в интерфейсе была именно перевёрнута) и потом долго искать, почему «строгое» значение
    оставляет больше фрагментов.
    """
    from src.api.services.chat_service import apply_score_gap

    frags = [_chunk(0.90, "a"), _chunk(0.86, "b"), _chunk(0.82, "c"), _chunk(0.78, "d")]
    assert len(apply_score_gap(frags, 0.02, min_keep=1)) == 1, "0.02 — только самое близкое"
    assert len(apply_score_gap(frags, 0.05, min_keep=1)) == 2, "0.05 — отставание до 0.05"
    assert len(apply_score_gap(frags, 0.09, min_keep=1)) == 3
    assert len(apply_score_gap(frags, 0.13, min_keep=1)) == 4
    assert len(apply_score_gap(frags, 0.20, min_keep=1)) == 4, "0.20 — почти вся выдача"


def test_gap_filters_junk_from_context_and_sources(monkeypatch):
    """Сквозная проверка: слабые фрагменты не попадают ни в промпт, ни в список источников."""
    captured = {}
    results = [_chunk(0.80, "c1", "нужный факт про реку"),
               _chunk(0.78, "c2", "ещё по делу"),
               _chunk(0.79, "c3", "и это по делу"),
               _chunk(0.62, "c4", "инфляция, мусор")]
    service = _service(monkeypatch, {"min_score_gap": 0.08}, results, captured)
    response = _run(service.generate_response(user_message="вопрос", use_rag=True, is_admin=True))

    joined = "\n".join(m.get("content", "") for m in captured["messages"])
    assert "инфляция, мусор" not in joined, "мусорный фрагмент не должен попадать в промпт"
    assert "нужный факт про реку" in joined
    ids = [s["id"] for s in response["sources"]]
    assert "c4" not in ids, "и в источники ответа он попадать не должен"
