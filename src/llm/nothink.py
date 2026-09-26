"""Отключение «размышлений» у моделей: у каждого провайдера свой флаг.

Замер 26.09.2026 (probe_glm_nothink.py, polza):
  * deepseek/openai/openrouter — работает `thinking: {"type": "disabled"}`;
  * polza/GLM (`custom`) — этот флаг ИГНОРИРУЕТСЯ: модель всё равно уходит в reasoning
    (reasoning 241 символ при отключённом флаге против 269 без него), а при малом max_tokens
    весь бюджет уходит в размышления и content приходит ПУСТЫМ — именно поэтому резервный
    провайдер на GLM не защищал. Работает другой флаг: `enable_thinking: false`
    (reasoning падает до 9 символов, ответ нормальный);
  * локальные llama.cpp/Ollama с шаблоном чата — `chat_template_kwargs: {enable_thinking: false}`.

Поэтому одной строки на всех не хватает: держим один помощник, чтобы не разъезжалось по коду
(сейчас такие места были в трёх файлах, и все три слали только `thinking`).
"""

from typing import Any, Dict, Optional

# Провайдеры, которым достаточно `thinking.type=disabled`
_THINKING_TYPE_DISABLED = ("deepseek", "openai", "openrouter")
# Провайдеры, которые понимают флаг Zhipu/GLM (и не понимают thinking.type)
_ENABLE_THINKING_FALSE = ("custom", "polza", "zhipu")


def nothink_payload(provider_type: Optional[str], model: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Доп. поля запроса, отключающие размышления, или None, если отключать нечего.

    `provider_type` — тип провайдера из привязки функции (`deepseek`, `custom`, `ollama`, …).
    `model` — на случай моделей, которые ведут себя иначе, чем их провайдер (задел на будущее).
    """
    ptype = (provider_type or "").strip().lower()
    model_l = (model or "").strip().lower()
    looks_like_glm = "glm" in model_l

    if ptype in _THINKING_TYPE_DISABLED and not looks_like_glm:
        return {"thinking": {"type": "disabled"}}
    if ptype in _ENABLE_THINKING_FALSE or looks_like_glm:
        # Отдаём оба флага: thinking для совместимости, enable_thinking — то, что GLM слушает.
        return {"thinking": {"type": "disabled"}, "enable_thinking": False}
    # Локальные модели (ollama/llama.cpp): раньше ничего не отправляли, и замеров, что им нужен
    # именно этот флаг, нет — оставляем как было, чтобы не менять работающее поведение.
    return None
