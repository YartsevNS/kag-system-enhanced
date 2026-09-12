"""Единый идентификатор чанка для Qdrant и Neo4j.

Проблема, которую решаем: раньше point_id в Qdrant считался как
uuid5(f"{document_id}-{i}") — то есть от порядкового номера, а в Neo4j узел
Chunk имел id = chunk_id. Связь между базами была возможна только через поиск
по payload (не точечным запросом по id).

Теперь point_id — детерминированная функция от chunk_id. Значит:
  - из графа можно вычислить point_id и достать вектор точечно;
  - chunk_id остаётся человекочитаемым ({document_id}_chunk_00001);
  - повторная индексация того же chunk_id даёт тот же point_id (идемпотентность).
"""

from __future__ import annotations

import re
import uuid

# Отдельный префикс, чтобы пространство имён чанков не пересекалось
# с другими uuid5 в проекте.
_CHUNK_NAMESPACE_PREFIX = "kag-chunk:"


def point_id_for_chunk(chunk_id: str, document_id: str | None = None) -> str:
    """Детерминированный UUID точки Qdrant по chunk_id.

    document_id участвует в ключе: chunk_id вида «chunk_00001» (без префикса
    документа) НЕ уникален между документами, и без этого разные документы
    писали бы точки с одинаковым id (перезапись/потеря данных).
    """
    if not chunk_id:
        raise ValueError("chunk_id обязателен")
    key = f"{document_id}:{chunk_id}" if document_id else chunk_id
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{_CHUNK_NAMESPACE_PREFIX}{key}"))


def build_embedding_text(content: str, metadata: dict | None = None) -> str:
    """Текст для эмбеддинга: структурный префикс + содержимое чанка.

    Зачем: номера стандарта и пункта несут сильный сигнал о смысле фрагмента.
    Без префикса чанк «5.2.1 Требования к средствам защиты информации»
    и чанк с похожим общим текстом из другого раздела почти неразличимы.

    Правило: префикс добавляется ТОЛЬКО к документу (passage), к запросу — нет
    (асимметрия как в e5: query:/passage:). Пустые поля не добавляются.
    """
    content = content or ""
    meta = metadata or {}

    parts = []
    standard = meta.get("standard_number")
    clause = meta.get("clause")
    section = meta.get("section")

    if standard:
        parts.append(str(standard))
    if clause:
        parts.append(f"п. {clause}")
    elif section:
        parts.append(f"раздел {section}")

    if not parts:
        return content
    return ", ".join(parts) + ": " + content


# Префикс, под которым файл лежит на диске: "<document_id>_"
_UPLOAD_PREFIX_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}_"
)


def display_filename(filename: str) -> str:
    """Человекочитаемое имя файла: без префикса <document_id>_.

    Файлы сохраняются как «<document_id>_<имя>», и это же имя уходит в payload
    Qdrant и в узлы графа. В интерфейсе такой префикс выглядит как мусор
    («b93f09…_Инфляция.pdf»), поэтому на ВЫДАЧЕ его снимаем; в хранилище имя
    остаётся как есть (по нему строится путь к файлу).
    """
    if not filename:
        return ""
    return _UPLOAD_PREFIX_RE.sub("", str(filename))
