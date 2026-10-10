"""Общие справочники для страниц: словарь видов документов и подобное.

Зачем отдельный роут. Страницы «Документы» и «Просмотрщик» держали СВОЙ список видов
(фильтр, подписи, редактор типа) — он расходился с анализатором и с правилами разметки.
Админский `/admin/models/doc-types` для этого не годится: он закрыт middleware целиком,
и обычный пользователь получит 403, а фильтр по видам нужен всем, кто видит документы.

Источник данных — `src/indexing/document_kinds.py` (словарь v0). Здесь он только отдаётся.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends

from src.api.middleware.auth_v2 import get_current_user_optional
from src.database.user_models import User

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/document-kinds", summary="Словарь видов документов (единый источник)")
async def document_kinds(
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Виды документов с подписями и группами — для фильтров и подписей на страницах.

    Контракт: {version, groups: {код: название}, kinds: [{code, title, short, group,
    group_title, definition}]}. Дополнительные типы, добавленные админом, страницы
    домешивают из `/admin/models/doc-types`, если он им доступен.
    """
    from src.indexing import document_kinds as kinds
    from src.indexing import document_topics as topics

    return {
        "version": kinds.VOCABULARY_VERSION,
        "topics_version": topics.TOPICS_VERSION,
        "groups": kinds.GROUPS,
        "kinds": kinds.as_list(),
        # Темы (рубрики) — вторая ось: «о чём документ». Отдаём тем же роутом, чтобы страницы
        # не держали своих списков ни по видам, ни по темам.
        "rubrics": topics.as_list(),
    }
