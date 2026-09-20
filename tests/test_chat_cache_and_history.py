"""Кэш префикса и бюджет истории диалога (стоимость запроса).

Разбор стоимости (консультация 20.09.2026, reports/_scratch/consult_cost_answer.md):
провайдер кэширует самый длинный общий префикс запроса, поэтому стабильное должно идти первым,
растущее (история) — вторым, а меняющийся контекст документов — последним. Плюс история должна
иметь серверный бюджет: клиент присылает всю переписку, и без предела стоимость вопроса растёт
с каждым ходом.
"""
import asyncio

from tests.test_chat_context_params import _chunk, _service


def test_history_is_trimmed_to_budget():
    from src.api.services.chat_service import _trim_history, HISTORY_KEEP_LAST

    # 20 длинных ходов: без бюджета это ~40 тыс. символов истории в каждом запросе
    history = []
    for i in range(20):
        history.append({"role": "user", "content": f"вопрос {i} " + "т" * 1500})
        history.append({"role": "assistant", "content": f"ответ {i} " + "о" * 1500})

    kept = _trim_history(history)
    assert kept, "история не должна исчезнуть целиком"
    chars = sum(len(m["content"]) for m in kept)
    assert chars <= 2500 * 2.5 + 200, f"история не влезла в бюджет: {chars} символов"
    assert len(kept) < len(history), "обрезать должно было"

    # последние ходы неприкосновенны: там «а по этому адресу?»
    tail = kept[-HISTORY_KEEP_LAST:]
    assert tail[-1]["content"] == history[-1]["content"], "последний ответ должен остаться целиком"
    assert tail[-2]["content"] == history[-2]["content"], "последний вопрос тоже"


def test_empty_history_is_ok():
    from src.api.services.chat_service import _trim_history

    assert _trim_history(None) == []
    assert _trim_history([]) == []


def test_short_history_untouched():
    from src.api.services.chat_service import _trim_history

    history = [{"role": "user", "content": "привет"},
               {"role": "assistant", "content": "здравствуйте"}]
    assert _trim_history(history) == history


def test_messages_are_ordered_for_prefix_cache(monkeypatch):
    """Порядок блоков: стабильный промпт → история → меняющийся контекст → вопрос.

    Именно этот порядок делает префикс кэшируемым: промпт и история не меняются от вопроса
    к вопросу, а контекст документов меняется и потому идёт последним.
    """
    captured = {}
    history = [{"role": "user", "content": "прошлый вопрос"},
               {"role": "assistant", "content": "прошлый ответ"}]
    service = _service(monkeypatch, {}, [_chunk(0.9, "c1", "текст фрагмента")], captured)
    asyncio.run(service.generate_response(user_message="новый вопрос", history=history,
                                          use_rag=True, is_admin=True))

    messages = captured["messages"]
    roles = [m["role"] for m in messages]
    assert roles[0] == "system" and "КОНТЕКСТ ИЗ ДОКУМЕНТОВ" not in messages[0]["content"], \
        "первым сообщением должен быть ЧИСТЫЙ системный промпт (он кэшируется)"
    assert messages[1]["content"] == "прошлый вопрос" and messages[2]["content"] == "прошлый ответ", \
        "история должна идти сразу за промптом"
    ctx_idx = next(i for i, m in enumerate(messages) if "КОНТЕКСТ ИЗ ДОКУМЕНТОВ" in m.get("content", ""))
    assert ctx_idx > 2, "контекст документов должен идти ПОСЛЕ истории"
    assert messages[-1]["content"] == "новый вопрос", "вопрос — последним"
