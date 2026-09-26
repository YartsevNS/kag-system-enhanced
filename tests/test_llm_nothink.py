"""Флаги отключения «размышлений»: у каждого провайдера свой (замер 26.09.2026).

Проверено на polza: GLM-5.3-flash ИГНОРИРУЕТ `thinking: {"type": "disabled"}` (reasoning остаётся,
а при малом max_tokens ответ приходит пустым — из-за этого резервный провайдер не защищал).
Работает `enable_thinking: false`. Deepseek/OpenAI, наоборот, понимают thinking.type.
"""
from src.llm.nothink import nothink_payload


def test_deepseek_gets_thinking_type_disabled():
    payload = nothink_payload("deepseek", "deepseek-flash")
    assert payload == {"thinking": {"type": "disabled"}}


def test_openai_and_openrouter_are_same_family():
    assert nothink_payload("openai", "gpt-4o")["thinking"] == {"type": "disabled"}
    assert nothink_payload("openrouter", "x/y")["thinking"] == {"type": "disabled"}


def test_custom_provider_gets_enable_thinking_false():
    """polza/GLM: главное — enable_thinking=false, иначе модель уходит в reasoning."""
    payload = nothink_payload("custom", "z-ai/glm-5.3-flash")
    assert payload is not None
    assert payload.get("enable_thinking") is False, "без этого флага GLM отдаёт пустой ответ"


def test_glm_detected_by_model_name_even_for_other_types():
    payload = nothink_payload("openrouter", "z-ai/glm-5.3-flash")
    assert payload and payload.get("enable_thinking") is False


def test_local_models_get_nothing():
    """Локальным (ollama/llama.cpp) параметр не отправляем: раньше не отправляли, замеров нет."""
    assert nothink_payload("ollama", "qwen2.5:1.5b") is None
    assert nothink_payload("llamacpp", "some-gguf") is None


def test_unknown_provider_gets_nothing():
    assert nothink_payload("", None) is None
    assert nothink_payload("some-unknown", "model-x") is None
